"""
SMS delivery for the express-order warehouse alert, via Modir Payamak's
IPPanel Edge API ("Send Webservice SMS": POST /v1/api/send).

Architecture deliberately copied from src/telegram.py's TelegramNotifier
(see that module's docstring for the full incident history this avoids
repeating):

  1. Plain synchronous HTTP (`httpx.Client`, never `httpx.AsyncClient`).
     TelegramNotifier used to run on a persistent asyncio event loop and
     hit `RuntimeError('Event loop is closed')` because main.py's
     scheduler (APScheduler's `BlockingScheduler` + its default
     `ThreadPoolExecutor`) can run different poll cycles on different OS
     threads, while a loop/pooled-connection object stays bound to
     whichever thread created it. A plain `httpx.Client` has no event
     loop and no thread affinity, so it cannot hit that failure mode
     regardless of which thread a given poll cycle lands on - see
     `_post`/`_request` below.

  2. A failed send is never just logged and dropped. `notify_if_express`
     runs strictly after the order's Didar deal already exists (called
     from src/sync_engine.py, same call sites as
     `TelegramNotifier.notify_new_order`), so by the time this module
     sees the order, nothing else in the system would ever retry a lost
     send on its own. A failure is instead persisted via
     `Repository.record_sms_failure()` and picked up later by
     `retry_pending_notifications()`, called every poll cycle from
     main.py - the SMS-side counterpart to
     `TelegramNotifier.retry_pending_notifications()`.

ONE DELIBERATE DIFFERENCE FROM TelegramNotifier: `is_configured()` here
makes NO network call (unlike Telegram's `getMe()` reachability check).
It only checks that `MODIR_PAYAMAK_TOKEN` and
`MODIR_PAYAMAK_FROM_NUMBER` are non-empty. Two reasons: (a) IPPanel's
API has no cheap no-op "ping" endpoint the way Telegram's `getMe` is,
so a config-only check avoids spending a real send-quota call just to
validate credentials; (b) unlike Telegram, where a `getMe()` failure
specifically needs to be told apart from "not configured at all" (see
TelegramNotifier._has_credentials()'s docstring for the 2026-09
incident that distinction fixed), there is only one way for this
notifier to be unusable before a send is even attempted - the two env
vars are missing - so that single check is both necessary and
sufficient, and every real send failure (bad token, network error, a
`meta.status: false` validation error) is uniformly a `_deliver()`-time
failure that goes through the same retry queue either way.

ANOTHER DELIBERATE DIFFERENCE: unlike a Telegram message (informational
- a duplicate is mildly annoying at worst), this SMS pages five real
people. `notify_if_express()` therefore treats "have we already
committed to notifying about this order" as a durable, checked fact
(`Repository.has_express_alert_been_sent()` /
`mark_express_alert_sent()`) rather than relying on it "naturally"
following from the Didar-sync dedup the way Telegram's per-order message
does - because unlike Telegram, this method is explicitly documented
(src/sync_engine.py) to be called from BOTH `_sync_one_order()` and
`retry_pending_failures()`, and a false positive there (the same order
reaching this method twice) must never mean two SMS batches to the
warehouse. The guard is written BEFORE the send is attempted, and the
one-off *delivery* failure (API reachable, this one send failed) is a
completely separate concern handled by the `record_sms_failure()` /
`retry_pending_notifications()` queue below - an order that is "marked
sent" but whose delivery is still queued for retry is exactly the
intended state, mirroring `mark_synced()` running before
`notify_new_order()`'s own send attempt in the Telegram flow.

EACH RECIPIENT GETS THEIR OWN TEXT, so (unlike the earlier 3-number
"one batch call" design) a send is per-recipient, not a single
`params.recipients` list. The retry queue's `ref_id` therefore carries
the phone number too (`express_sms:{source}:{order_id}:{phone}`) so
`retry_pending_notifications()` knows which one number a queued
failure belongs to without Repository needing a new column.

Env vars (added to config.py / .env in stage 8 of this feature):
    MODIR_PAYAMAK_TOKEN, MODIR_PAYAMAK_FROM_NUMBER,
    EXPRESS_ALERT_SMS_RECIPIENT_1.._5,
    EXPRESS_ALERT_SMS_RECIPIENT_1_NAME.._5_NAME
Read directly via os.getenv here, same as TelegramNotifier does for
TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID* - config.py's "nothing outside this
file calls os.getenv" convention is, in this codebase, already the
notifier-module exception documented by that precedent rather than
something introduced here.

Repository methods this module depends on (stage 5 of this feature -
not yet implemented as of this stage):
    has_express_alert_been_sent(source, source_order_id) -> bool
    mark_express_alert_sent(source, source_order_id) -> None
    record_sms_failure(ref_id, message_text, error_message) -> None
    get_pending_sms_failures(max_attempts=5) -> list[NotificationFailure]
    clear_sms_failure(ref_id) -> None
(Same shapes as the existing notification_failures methods used by
TelegramNotifier, reusing that same `NotificationFailure` dataclass -
see src/db/repository.py - but against a SEPARATE table, so a Telegram
outage and a Modir Payamak outage never queue into or drain from each
other's retry list.)
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING, Optional

import httpx

from src.express_alert import is_express_order
from src.http_utils import default_retry, raise_for_status_with_body
from src.logger import get_logger
from src.telegram import _PLATFORM_DISPLAY

if TYPE_CHECKING:
    from src.db.repository import Repository
    from src.marketplaces.base import NormalizedOrder

log = get_logger(__name__)

_IPPANEL_API_BASE = "https://edge.ippanel.com/v1/api"

# Recipient number N and the name used in N's own message text
# ("{name} عزیز ...") are two separate env vars so a phone number is
# never parsed out of a free-text name field (Persian names can contain
# almost anything, including digits/punctuation that would make a
# packed "name|phone" var ambiguous to split).
_RECIPIENT_ENV_VARS = (
    "EXPRESS_ALERT_SMS_RECIPIENT_1",
    "EXPRESS_ALERT_SMS_RECIPIENT_2",
    "EXPRESS_ALERT_SMS_RECIPIENT_3",
    "EXPRESS_ALERT_SMS_RECIPIENT_4",
    "EXPRESS_ALERT_SMS_RECIPIENT_5",
)


class ModirPayamakError(Exception):
    """Raised for a Modir Payamak / IPPanel API error response
    (`meta.status: false`) or a network/transport failure while talking
    to it. Mirrors TelegramError's role: the one exception type every
    caller in this module needs to catch, regardless of whether the
    underlying cause was httpx or the API's own validation."""


class ModirPayamakNotifier:
    """Send the express-order warehouse SMS alert via Modir Payamak.

    Every public method is best-effort: it logs and swallows any error
    so an SMS-provider problem can never propagate into the SyncEngine's
    success path or main.py's poll loop, exactly like TelegramNotifier.
    """

    def __init__(self) -> None:
        self._client: Optional[httpx.Client] = None
        self._recipients: list[tuple[str, str]] = []  # (phone, name)
        self._configured: bool = False

    def close(self) -> None:
        """Release the underlying HTTP connection pool. Optional - the
        process exiting does this anyway - but useful for clean
        shutdown/tests that create many ModirPayamakNotifier instances."""
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
        self._client = None

    # ------------------------------------------------------------------
    # Configuration gate
    # ------------------------------------------------------------------
    def is_configured(self) -> bool:
        """True iff MODIR_PAYAMAK_TOKEN, MODIR_PAYAMAK_FROM_NUMBER and at
        least one EXPRESS_ALERT_SMS_RECIPIENT_{1..5} are set. Makes NO
        network call (see module docstring for why this differs from
        TelegramNotifier.is_configured()'s getMe() check) and caches the
        result so this is a cheap call on every poll cycle. Returns
        False (never raises) on any missing configuration."""
        if self._configured:
            return True

        token = os.getenv("MODIR_PAYAMAK_TOKEN", "").strip()
        from_number = os.getenv("MODIR_PAYAMAK_FROM_NUMBER", "").strip()
        recipients: list[tuple[str, str]] = []
        for var in _RECIPIENT_ENV_VARS:
            phone = os.getenv(var, "").strip()
            if not phone:
                continue
            name = os.getenv(f"{var}_NAME", "").strip() or "همکار"
            recipients.append((phone, name))

        if not token or not from_number:
            log.debug("modir_payamak: credentials not set, SMS alerts disabled")
            return False
        if not recipients:
            log.warning(
                "modir_payamak: no EXPRESS_ALERT_SMS_RECIPIENT_{1..5} configured "
                "- SMS alerts disabled"
            )
            return False

        self._client = httpx.Client(
            base_url=_IPPANEL_API_BASE,
            headers={"Authorization": token, "Content-Type": "application/json"},
            timeout=15.0,
        )
        self._from_number = from_number
        self._recipients = recipients
        self._configured = True
        log.info(
            "modir_payamak: configured (%d recipient(s))", len(recipients)
        )
        return True

    def _has_credentials(self) -> bool:
        """True iff the two required env vars are set, WITHOUT touching
        _configured/is_configured()'s cache. Not currently needed to
        tell apart failure reasons the way TelegramNotifier's version
        does (is_configured() here makes no network call that could
        fail independently of missing config - see module docstring),
        kept only for symmetry/future use if that ever changes."""
        token = os.getenv("MODIR_PAYAMAK_TOKEN", "").strip()
        from_number = os.getenv("MODIR_PAYAMAK_FROM_NUMBER", "").strip()
        return bool(token and from_number)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    def notify_if_express(self, order: "NormalizedOrder", repository: "Repository") -> None:
        """If `order` is an express shipment (src/express_alert.py) and
        no express alert has been committed for this
        (order.source, order.source_order_id) before, send the
        warehouse SMS now (or queue it for retry on failure - never
        raises). Safe to call for every synced order, express or not,
        and safe to call more than once for the same order (see module
        docstring's "durable, checked" dedup guard) - both properties
        this method needs since src/sync_engine.py calls it from two
        different call sites for the same order lifecycle."""
        if not is_express_order(order):
            return

        if repository.has_express_alert_been_sent(order.source, order.source_order_id):
            return

        # Committed BEFORE the send is attempted (see module docstring):
        # a delivery failure from here on is a `_deliver()`-level retry
        # concern, never a reason to reconsider whether this order
        # should be notified about at all.
        repository.mark_express_alert_sent(order.source, order.source_order_id)

        if not self.is_configured():
            # No recipient list at all yet (or missing credentials) - can't
            # even build per-recipient ref_ids, so queue one generic
            # failure row rather than silently dropping the alert.
            description = (
                f"express alert SMS for {order.source} order {order.source_order_id}"
            )
            log.error(
                "modir_payamak: not configured - queuing %s for retry", description
            )
            repository.record_sms_failure(
                f"express_sms:{order.source}:{order.source_order_id}",
                self._format_message("همکار", order),
                "modir payamak not configured (is_configured() failed)",
            )
            return

        # Each of the (up to 5) recipients gets their own message, with
        # their own name substituted in - so this is 5 independent
        # deliveries/retry rows, not one batch send (see module
        # docstring's "EACH RECIPIENT GETS THEIR OWN TEXT").
        for phone, name in self._recipients:
            message = self._format_message(name, order)
            ref_id = f"express_sms:{order.source}:{order.source_order_id}:{phone}"
            description = (
                f"express alert SMS for {order.source} order "
                f"{order.source_order_id} to {phone}"
            )
            self._deliver(ref_id, phone, message, repository, description)

    def retry_pending_notifications(self, repository: "Repository", max_attempts: int = 5) -> None:
        """Re-attempt every express-alert SMS that failed to send on a
        previous poll cycle. Called once per poll cycle from
        main.py's _poll_cycle, mirroring
        TelegramNotifier.retry_pending_notifications() exactly - see
        that method's docstring for why a send-step failure needs its
        own retry path distinct from the Didar-sync retry queue. Gives
        up (leaves the row in place, logged) after `max_attempts`."""
        if not self.is_configured():
            return
        for failure in repository.get_pending_sms_failures(max_attempts=max_attempts):
            # ref_id is "express_sms:{source}:{order_id}:{phone}" for a
            # per-recipient failure (see notify_if_express), or the
            # 3-part "express_sms:{source}:{order_id}" form recorded
            # when is_configured() itself failed (no phone to target -
            # skipped here and left for manual follow-up, since there is
            # no recipient list to resend it to).
            parts = failure.ref_id.split(":")
            if len(parts) != 4:
                log.warning(
                    "modir_payamak: queued SMS %s has no target phone - "
                    "skipping retry (needs manual resend)", failure.ref_id,
                )
                continue
            phone = parts[3]
            try:
                self._send(phone, failure.message_text)
                repository.clear_sms_failure(failure.ref_id)
                log.info(
                    "modir_payamak: retry succeeded for queued SMS %s", failure.ref_id
                )
            except ModirPayamakError as exc:
                log.error(
                    "modir_payamak: retry failed for queued SMS %s: %s",
                    failure.ref_id, exc,
                )
                repository.record_sms_failure(failure.ref_id, failure.message_text, str(exc))
            except Exception as exc:  # pragma: no cover - defensive
                log.exception(
                    "modir_payamak: unexpected error retrying queued SMS %s",
                    failure.ref_id,
                )
                repository.record_sms_failure(failure.ref_id, failure.message_text, str(exc))

    # ------------------------------------------------------------------
    # Message formatting
    # ------------------------------------------------------------------
    def _format_message(self, name: str, order: "NormalizedOrder") -> str:
        """Personalized alert text, addressed to this one recipient by
        name. Platform name is looked up the same way Telegram's
        per-order message does (`src.telegram._PLATFORM_DISPLAY`) so an
        internal source key like "digikala" never leaks into a message
        a non-technical recipient reads - falls back to the raw source
        string for a platform not in that mapping, same as Telegram."""
        _, platform = _PLATFORM_DISPLAY.get(order.source, ("⚪", order.source))
        return (
            f"{name} عزیز سفارش اکسپرس از پلتفرم {platform} ثبت شده "
            f"لطفا پیگیر باشید با تشکر"
        )

    # ------------------------------------------------------------------
    # Retry queue for failed sends (mirrors TelegramNotifier._deliver)
    # ------------------------------------------------------------------
    def _deliver(
        self, ref_id: str, phone: str, text: str, repository: "Repository", description: str
    ) -> None:
        """Attempt to send `text` to `phone` right now; on ANY failure,
        persist it to Repository's SMS retry queue under `ref_id` so
        retry_pending_notifications() picks it up on a later poll cycle
        instead of the message being silently gone forever."""
        try:
            self._send(phone, text)
            log.info("modir_payamak: sent %s", description)
        except ModirPayamakError as exc:
            log.error(
                "modir_payamak: failed to send %s: %s - queued for retry", description, exc
            )
            repository.record_sms_failure(ref_id, text, str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            log.exception(
                "modir_payamak: unexpected error sending %s - queued for retry", description
            )
            repository.record_sms_failure(ref_id, text, str(exc))

    def _send(self, phone: str, text: str) -> None:
        """POST /v1/api/send for one recipient. Each recipient's message
        text differs (their own name is in it - see _format_message), so
        unlike the earlier "one call, N recipients" batch design this is
        one call per phone number; IPPanel's `params.recipients` is still
        a list, just a single-element one here. Only called after
        is_configured() has populated self._client."""
        assert self._client is not None  # only called after is_configured()
        self._request(
            self._client,
            "send",
            sending_type="webservice",
            from_number=self._from_number,
            message=text,
            params={"recipients": [phone]},
        )

    @default_retry()
    def _post(self, client: httpx.Client, method: str, **json_body) -> dict:
        """The actual HTTPS POST, decorated with the same
        exponential-backoff retry every other external API client in
        this project uses (src/http_utils.default_retry) for transient
        5xx/network errors - a 4xx is never retried here, matching
        is_retryable_http_error()'s policy. Deliberately left raising
        httpx's own exceptions (not ModirPayamakError) so
        @default_retry()'s predicate can see them; _request() below
        converts whatever survives all retries into ModirPayamakError
        for the rest of this module."""
        resp = client.post(f"/{method}", json=json_body or None)
        raise_for_status_with_body(resp)
        return resp.json()

    def _request(self, client: httpx.Client, method: str, **json_body) -> dict:
        """One call to https://edge.ippanel.com/v1/api/<method> - plain
        synchronous HTTP, no asyncio, no event loop (see module
        docstring's point 1). Converts both transport failures and a
        well-formed-but-unsuccessful API response
        (`meta.status: false`, e.g. an invalid token or a validation
        error - see IPPanel's documented error shape in this module's
        docstring) into ModirPayamakError so every caller here has one
        exception type to catch."""
        try:
            payload = self._post(client, method, **json_body)
        except httpx.HTTPStatusError as exc:
            raise ModirPayamakError(str(exc)) from exc
        except httpx.TransportError as exc:
            raise ModirPayamakError(f"network error calling {method}: {exc}") from exc
        except ValueError as exc:
            raise ModirPayamakError(f"{method}: non-JSON response") from exc

        meta = payload.get("meta") or {}
        if not meta.get("status", False):
            raise ModirPayamakError(
                f"{method} failed: {meta.get('message')!r} "
                f"(code={meta.get('message_code')!r})"
            )
        return payload