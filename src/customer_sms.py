"""
First customer SMS ("پیامک ۱") - sent to the customer ~5 hours after their
order was registered, from Farazhonar's OWN SIM card.

WHY NOT MODIR PAYAMAK: the client wants this message to come from the
company's own number rather than a panel line, so it is NOT sent through
src/modir_payamak.py (which stays exclusively for the warehouse alert).
Instead it goes to an Android phone holding the Farazhonar SIM, running
the open-source "SMS Gateway for Android" app (sms-gate.app) in *Local
Server* mode: the app exposes `POST http://<phone-ip>:8080/message` on the
LAN (HTTP Basic auth) and sends the text through the phone's own SIM.

TWO ROUTES (2026-11, the phone leaves the office with its owner):
  1. LOCAL  - POST to the phone's LAN address (SMS_GATEWAY_URL...). Used
     whenever the phone is reachable, i.e. while it is on the office Wi-Fi.
  2. CLOUD  - the app's Cloud Server mode (SMS_GATEWAY_CLOUD_*): POST to
     https://api.sms-gate.app/3rdparty/v1/messages, which relays to the
     phone over its mobile data. Used when the local route can't connect.
Local is tried first (short connect timeout); after a local connect failure
it is skipped for 5 minutes so away-from-office sends aren't slowed down.
Either route alone is enough. Only a *connection* failure or a clear HTTP
rejection falls through to the other route - an ambiguous read timeout does
not, to avoid texting the same customer twice.

HOW IT WORKS (one table, three jobs - see customer_sms_queue in
src/db/repository.py):
  * enqueue()      - called from SyncEngine right after an order is synced
                     to Didar (both the normal and the retry path). Writes
                     one row per order with send_after = order time + 5h
                     (moved out of quiet hours). INSERT OR IGNORE on
                     (platform, source_order_id) => a second call for the
                     same order can never produce a second SMS.
  * process_due()  - called at the end of every poll cycle. Sends the rows
                     whose time has come, a few per cycle, never above a
                     daily cap, and backs off + retries on failure.

SAFETY RAILS (all env-configurable, defaults in the constants below):
  * Master switch CUSTOMER_SMS_ENABLED (default off) - nothing is queued
    or sent until it is "true" AND the gateway is configured.
  * Orders older than CUSTOMER_SMS_MAX_ORDER_AGE_HOURS at sync time are
    never queued, so enabling the feature (or re-syncing history) can't
    text customers about old orders.
  * Quiet hours (Tehran time) - an SMS that would go out at night waits
    for the morning.
  * Per-cycle and per-day caps - a single SIM card sending in bursts is
    what gets throttled by the operator.
  * DRY_RUN=true logs what would be sent and sends nothing.
  * Telegram alert (via the callable passed to process_due) when messages
    have been overdue for a while, i.e. the phone/gateway is unreachable.

"Accepted by the gateway" is the success criterion: the app answers 2xx once
it has queued the message on the phone; actual delivery to the handset is
the operator's business and is not tracked here.

Env vars are read with os.getenv at call time (same notifier-module
exception to config.py's rule as src/modir_payamak.py and src/telegram.py).
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Callable

import httpx

from src.config import settings
from src.logger import get_logger

if TYPE_CHECKING:
    from src.db.repository import Repository
    from src.marketplaces.base import NormalizedOrder

log = get_logger(__name__)

# Iran abolished DST in 2022, so a fixed +03:30 offset is exact and avoids
# depending on tzdata being installed on the Windows host.
_TEHRAN = timezone(timedelta(hours=3, minutes=30))

# The text of پیامک ۱, exactly as supplied by the client (2026-11-21).
# Emoji make this a Unicode SMS (70 chars/segment), so it is billed/sent as
# several segments - the gateway handles multipart automatically.
MESSAGE_TEXT = (
    "هنر دوست عزیز ✨\n"
    "صمیمانه از اینکه فروشگاه \"فراز هنر\" را برای خرید خود انتخاب فرمودید، سپاسگزاریم.\n"
    "سفارش شما با نهایت دقت در حال آماده‌سازی است و به‌زودی ارسال خواهد شد.📦\n"
    "🌐farazhonar.com"
)

_PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

_RETRY_BACKOFF = timedelta(minutes=10)
_LOCAL_DOWN_SKIP = timedelta(minutes=5)


_STATE_SETTLE = timedelta(seconds=45)       # let the phone send before asking
_PROCESSED_STUCK = timedelta(hours=2)       # "Processed" this long = indeterminate


class _Rejected(Exception):
    """The gateway answered with a non-2xx status."""


class _StateUnavailable(Exception):
    """A message's current state couldn't be read from the gateway."""


def normalize_iran_mobile(raw: str | None) -> str | None:
    """Return an Iranian mobile number as "+989XXXXXXXXX", or None if it
    isn't one (landline, masked number, garbage). Accepts Persian/Arabic
    digits and the usual shapes: 09…, 9…, 989…, +989…, 00989…"""
    if not raw:
        return None
    digits = re.sub(r"\D", "", str(raw).translate(_PERSIAN_DIGITS))
    if digits.startswith("0098"):
        digits = digits[4:]
    elif digits.startswith("98"):
        digits = digits[2:]
    elif digits.startswith("0"):
        digits = digits[1:]
    if re.fullmatch(r"9\d{9}", digits):
        return "+98" + digits
    return None


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


class CustomerSmsError(Exception):
    """Gateway unreachable or it rejected the message."""


class CustomerSmsNotifier:
    """Schedules and sends the first customer SMS. Every public method is
    best-effort: it logs and swallows errors so an SMS problem can never
    break the order sync or the poll loop."""

    def __init__(self) -> None:
        self._last_overdue_alert: datetime | None = None
        self._last_state_alert: datetime | None = None
        self._local_down_until: datetime | None = None

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------
    @staticmethod
    def _enabled_flag() -> bool:
        return os.getenv("CUSTOMER_SMS_ENABLED", "false").strip().lower() == "true"

    @staticmethod
    def _local_route() -> tuple[str, str, str, str] | None:
        """(base_url, user, password, path) for the phone's LAN server."""
        url = os.getenv("SMS_GATEWAY_URL", "").strip()
        user = os.getenv("SMS_GATEWAY_USERNAME", "").strip()
        pwd = os.getenv("SMS_GATEWAY_PASSWORD", "").strip()
        if not (url and user and pwd):
            return None
        path = os.getenv("SMS_GATEWAY_SEND_PATH", "").strip() or "/message"
        return url, user, pwd, path

    @staticmethod
    def _cloud_route() -> tuple[str, str, str, str] | None:
        """(base_url, user, password, path) for the app's Cloud Server."""
        user = os.getenv("SMS_GATEWAY_CLOUD_USERNAME", "").strip()
        pwd = os.getenv("SMS_GATEWAY_CLOUD_PASSWORD", "").strip()
        if not (user and pwd):
            return None
        url = os.getenv("SMS_GATEWAY_CLOUD_URL", "").strip() or "https://api.sms-gate.app"
        path = os.getenv("SMS_GATEWAY_CLOUD_SEND_PATH", "").strip() or "/3rdparty/v1/messages"
        return url, user, pwd, path

    def _gateway_configured(self) -> bool:
        return self._local_route() is not None or self._cloud_route() is not None

    def is_enabled(self) -> bool:
        """Master switch on AND gateway credentials present. Cheap, no I/O."""
        return self._enabled_flag() and self._gateway_configured()

    @staticmethod
    def _allowed_platforms() -> set[str] | None:
        """None = every platform."""
        raw = os.getenv("CUSTOMER_SMS_PLATFORMS", "").strip()
        if not raw:
            return None
        return {p.strip() for p in raw.split(",") if p.strip()}

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------
    @staticmethod
    def _apply_quiet_hours(when: datetime) -> datetime:
        """If `when` falls inside the Tehran-time quiet window
        [CUSTOMER_SMS_QUIET_START, CUSTOMER_SMS_QUIET_END) (default
        21:00-09:00) push it to the end of that window."""
        start = _env_int("CUSTOMER_SMS_QUIET_START", 21)
        end = _env_int("CUSTOMER_SMS_QUIET_END", 9)
        if start == end:
            return when
        local = when.astimezone(_TEHRAN)
        h = local.hour
        in_quiet = (h >= start or h < end) if start > end else (start <= h < end)
        if not in_quiet:
            return when
        target = local.replace(hour=end, minute=0, second=0, microsecond=0)
        if target <= local:
            target += timedelta(days=1)
        return target.astimezone(timezone.utc)

    def enqueue(
        self, order: "NormalizedOrder", repository: "Repository", now: datetime | None = None
    ) -> None:
        """Schedule پیامک ۱ for this order if it qualifies. Safe to call
        any number of times for the same order."""
        try:
            if not self.is_enabled():
                return
            allowed = self._allowed_platforms()
            if allowed is not None and order.source not in allowed:
                return
            phone = normalize_iran_mobile(order.customer_mobile)
            if phone is None:
                log.info(
                    "customer_sms: %s order %s has no usable mobile number - skipping",
                    order.source, order.source_order_id,
                )
                return
            now = now or datetime.now(timezone.utc)
            created = order.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            max_age = timedelta(hours=_env_int("CUSTOMER_SMS_MAX_ORDER_AGE_HOURS", 24))
            if now - created > max_age:
                log.info(
                    "customer_sms: %s order %s is older than %s - not queuing",
                    order.source, order.source_order_id, max_age,
                )
                return
            delay = timedelta(hours=_env_int("CUSTOMER_SMS_DELAY_HOURS", 5))
            send_after = self._apply_quiet_hours(created + delay)
            if repository.enqueue_customer_sms(
                order.source, order.source_order_id, phone, send_after
            ):
                log.info(
                    "customer_sms: queued %s order %s for %s",
                    order.source, order.source_order_id,
                    send_after.astimezone(_TEHRAN).strftime("%Y-%m-%d %H:%M Tehran"),
                )
        except Exception:
            log.exception(
                "customer_sms: failed to queue %s order %s",
                getattr(order, "source", "?"), getattr(order, "source_order_id", "?"),
            )

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------
    def process_due(
        self,
        repository: "Repository",
        alert: Callable[[str], object] | None = None,
        now: datetime | None = None,
        status_check: Callable[[str, str], str | None] | None = None,
    ) -> None:
        """Send queued messages that are due. Called once per poll cycle.

        `status_check(platform, order_id)` re-reads the order from its
        marketplace right before sending and returns a reason string if
        the order is no longer eligible (e.g. cancelled) or None if it is
        fine. If it raises, the order's state can't be verified, so
        NOTHING is sent: the row is retried later like a gateway failure."""
        try:
            if not self.is_enabled():
                return
            now = now or datetime.now(timezone.utc)
            self._alert_if_overdue(repository, alert, now)
            # First settle what the phone did with earlier sends: failures
            # reported by the app go back into the queue (before the due-
            # message selection below, so they can be retried right away).
            self._reconcile(repository, alert, now)

            # 0 (default) = never give up on attempt count: a failed message
            # stays queued and is retried every 10 minutes until the phone /
            # gateway is reachable again. The only expiry is the age guard
            # below (CUSTOMER_SMS_GIVE_UP_AFTER_HOURS).
            max_attempts = _env_int("CUSTOMER_SMS_MAX_ATTEMPTS", 0)
            unlimited = max_attempts <= 0
            max_label = "∞" if unlimited else str(max_attempts)
            give_up_after = timedelta(hours=_env_int("CUSTOMER_SMS_GIVE_UP_AFTER_HOURS", 48))
            per_cycle = _env_int("CUSTOMER_SMS_MAX_PER_CYCLE", 3)
            daily_cap = _env_int("CUSTOMER_SMS_DAILY_CAP", 150)

            local_now = now.astimezone(_TEHRAN)
            day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
            sent_today = repository.count_customer_sms_sent_since(day_start)
            budget = min(per_cycle, daily_cap - sent_today)
            if budget <= 0:
                if per_cycle > 0:
                    log.warning(
                        "customer_sms: daily cap %d reached - the rest wait until tomorrow",
                        daily_cap,
                    )
                return

            # Quiet hours also apply at send time: a retry backoff or a long
            # outage can leave due rows sitting inside the night window.
            if self._apply_quiet_hours(now) != now:
                return

            for platform, order_id, phone, send_after, attempts, due_at in (
                repository.get_due_customer_sms(
                    now, 10**9 if unlimited else max_attempts, budget
                )
            ):
                due = datetime.fromisoformat(send_after)
                # Age guard: "your order is being prepared" days late is
                # pointless (it may already be shipped), so a message that
                # has been stuck longer than GIVE_UP_AFTER_HOURS since its
                # ORIGINAL due time is dropped (0 = never drop).
                if due_at and give_up_after > timedelta(0):
                    if now - datetime.fromisoformat(due_at) > give_up_after:
                        repository.record_customer_sms_failure(
                            platform, order_id,
                            f"expired: undeliverable for more than {give_up_after}",
                            now, True,
                        )
                        log.warning(
                            "customer_sms: dropping %s order %s - still unsent %s after "
                            "its due time", platform, order_id, give_up_after,
                        )
                        if alert is not None:
                            self._safe_alert(
                                alert,
                                f"⚠️ پیامک ۱ سفارش {order_id} ({platform}) بیش از "
                                f"{give_up_after} معطل مانده و منقضی شد.",
                            )
                        continue
                if status_check is not None:
                    try:
                        reason = status_check(platform, order_id)
                    except Exception as exc:
                        give_up = not unlimited and attempts + 1 >= max_attempts
                        repository.record_customer_sms_failure(
                            platform, order_id, f"status check failed: {exc}",
                            now + _RETRY_BACKOFF, give_up,
                        )
                        log.error(
                            "customer_sms: could not verify status of %s order %s "
                            "(attempt %d/%s), NOT sending: %s",
                            platform, order_id, attempts + 1, max_label, exc,
                        )
                        if give_up and alert is not None:
                            self._safe_alert(
                                alert,
                                f"⚠️ وضعیت سفارش {order_id} ({platform}) قابل بررسی نبود و "
                                "پیامک ۱ آن ارسال نشد.",
                            )
                        continue
                    if reason:
                        repository.mark_customer_sms_skipped(platform, order_id, reason)
                        log.info(
                            "customer_sms: skipping %s order %s - %s", platform, order_id, reason
                        )
                        continue
                # CUSTOMER_SMS_DRY_RUN: dry run for THIS feature only. The
                # global DRY_RUN also stops Didar sync and Telegram, so it
                # is the wrong switch for rehearsing just the SMS on a live
                # service. Rows stay pending, so flipping it off sends them.
                if settings.dry_run or (
                    os.getenv("CUSTOMER_SMS_DRY_RUN", "false").strip().lower() == "true"
                ):
                    log.info(
                        "customer_sms: [DRY_RUN] would send to %s for %s order %s",
                        phone, platform, order_id,
                    )
                    continue
                try:
                    route_used, gateway_id = self._send(phone, MESSAGE_TEXT, now)
                except CustomerSmsError as exc:
                    give_up = not unlimited and attempts + 1 >= max_attempts
                    repository.record_customer_sms_failure(
                        platform, order_id, str(exc), now + _RETRY_BACKOFF, give_up
                    )
                    log.error(
                        "customer_sms: send failed for %s order %s (attempt %d/%s) - "
                        "stays queued: %s",
                        platform, order_id, attempts + 1, max_label, exc,
                    )
                    if give_up and alert is not None:
                        self._safe_alert(
                            alert,
                            f"⚠️ پیامک ۱ برای سفارش {order_id} ({platform}) بعد از "
                            f"{max_attempts} تلاش ارسال نشد و رها شد.",
                        )
                    # Gateway likely down: stop hammering it this cycle.
                    break
                else:
                    repository.mark_customer_sms_accepted(
                        platform, order_id, route_used, gateway_id
                    )
                    log.info(
                        "customer_sms: handed to the phone (%s) for %s order %s -> %s "
                        "(was due %s), waiting for its final state",
                        route_used, platform, order_id, phone,
                        due.astimezone(_TEHRAN).strftime("%H:%M"),
                    )
        except Exception:
            log.exception("customer_sms: unexpected error in process_due")

    def _alert_if_overdue(
        self, repository: "Repository", alert: Callable[[str], object] | None, now: datetime
    ) -> None:
        if alert is None:
            return
        minutes = _env_int("CUSTOMER_SMS_ALERT_AFTER_MINUTES", 60)
        # Not while it's legitimately night (rows are held on purpose).
        if self._apply_quiet_hours(now) != now:
            return
        overdue = repository.count_overdue_customer_sms(now - timedelta(minutes=minutes))
        if overdue == 0:
            return
        if self._last_overdue_alert and now - self._last_overdue_alert < timedelta(hours=6):
            return
        self._last_overdue_alert = now
        self._safe_alert(
            alert,
            f"⚠️ {overdue} پیامک ۱ مشتری بیش از {minutes} دقیقه است که ارسال نشده. "
            "گوشی SMS Gateway روشن و به شبکه وصل است؟",
        )

    @staticmethod
    def _safe_alert(alert: Callable[[str], object], text: str) -> None:
        try:
            alert(text)
        except Exception:
            log.exception("customer_sms: alert callback failed")

    @staticmethod
    def _post(
        route: tuple[str, str, str, str], phone: str, text: str, timeout: httpx.Timeout
    ) -> str | None:
        """POST one message; returns the gateway's message id (None if the
        response carries none) so its final state can be looked up later."""
        base, user, pwd, path = route
        payload: dict = {"textMessage": {"text": text}, "phoneNumbers": [phone]}
        # Dual-SIM phone: pick the SIM explicitly (1-based, per
        # docs.sms-gate.app/features/multi-sim). Without it the app/OS
        # default SIM is used - in the 2026-11-21 field test that was the
        # wrong SIM (MCI instead of the company's Irancell) and every
        # message failed with RESULT_ERROR_GENERIC_FAILURE.
        sim = os.getenv("SMS_GATEWAY_SIM_NUMBER", "").strip()
        if sim.isdigit() and int(sim) > 0:
            payload["simNumber"] = int(sim)
        resp = httpx.post(
            base.rstrip("/") + "/" + path.lstrip("/"),
            json=payload,
            auth=(user, pwd),
            # api.sms-gate.app is behind Cloudflare, which refused a plain
            # Python client's default User-Agent with 403 "error code: 1010"
            # in the 2026-11-21 field test. An explicit UA avoids that;
            # override with SMS_GATEWAY_USER_AGENT if 403 ever persists.
            headers={
                "User-Agent": os.getenv("SMS_GATEWAY_USER_AGENT", "").strip()
                or "FarazHonar-OrderSync/1.0"
            },
            timeout=timeout,
        )
        if not 200 <= resp.status_code < 300:
            raise _Rejected(f"returned {resp.status_code}: {resp.text[:200]}")
        try:
            data = resp.json()
            mid = data.get("id") if isinstance(data, dict) else None
        except ValueError:
            mid = None
        return str(mid) if mid else None

    def _send(
        self, phone: str, text: str, now: datetime | None = None
    ) -> tuple[str, str | None]:
        """Send one message: local route first, cloud route as fallback
        (see module docstring). Returns (route_name, gateway_message_id);
        raises CustomerSmsError if no route worked. No tenacity retry: the
        queue's own backoff is the retry."""
        now = now or datetime.now(timezone.utc)
        local, cloud = self._local_route(), self._cloud_route()
        errors: list[str] = []

        if local is not None and not (
            cloud is not None and self._local_down_until and now < self._local_down_until
        ):
            try:
                mid = self._post(local, phone, text, httpx.Timeout(15.0, connect=3.0))
                self._local_down_until = None
                return "local", mid
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                self._local_down_until = now + _LOCAL_DOWN_SKIP
                errors.append(f"local unreachable: {exc}")
            except _Rejected as exc:
                errors.append(f"local {exc}")
            except httpx.HTTPError as exc:
                # e.g. read timeout: the phone may already have the message.
                # Falling back could text the customer twice.
                raise CustomerSmsError(f"local gateway ambiguous failure: {exc}") from exc
            if cloud is None:
                raise CustomerSmsError("; ".join(errors))

        if cloud is not None:
            try:
                mid = self._post(cloud, phone, text, httpx.Timeout(20.0, connect=8.0))
                if errors:
                    log.info("customer_sms: sent via cloud route (%s)", errors[0])
                return "cloud", mid
            except _Rejected as exc:
                errors.append(f"cloud {exc}")
            except httpx.HTTPError as exc:
                errors.append(f"cloud unreachable: {exc}")
        raise CustomerSmsError("; ".join(errors) or "no gateway configured")

    # ------------------------------------------------------------------
    # Final-state tracking: did the PHONE really send it?
    # ------------------------------------------------------------------
    def _fetch_state(self, route_name: str, gateway_id: str) -> tuple[str, str]:
        """Ask the gateway for a message's current state. Returns
        (state, reason); states per docs.sms-gate.app/features/status-tracking:
        Pending, Processed, Sent, Delivered, Failed (+ Cancelling/Cancelled).
        Raises _StateUnavailable if it can't be read right now."""
        if route_name == "local":
            route = self._local_route()
            tpl = os.getenv("SMS_GATEWAY_STATUS_PATH", "").strip() or "/message/{id}"
        else:
            route = self._cloud_route()
            tpl = (
                os.getenv("SMS_GATEWAY_CLOUD_STATUS_PATH", "").strip()
                or "/3rdparty/v1/messages/{id}"
            )
        if route is None:
            raise _StateUnavailable(f"{route_name} route not configured")
        base, user, pwd, _ = route
        try:
            resp = httpx.get(
                base.rstrip("/") + "/" + tpl.format(id=gateway_id).lstrip("/"),
                auth=(user, pwd),
                headers={
                    "User-Agent": os.getenv("SMS_GATEWAY_USER_AGENT", "").strip()
                    or "FarazHonar-OrderSync/1.0"
                },
                timeout=httpx.Timeout(10.0, connect=3.0),
            )
        except httpx.HTTPError as exc:
            raise _StateUnavailable(f"{route_name} unreachable: {exc}") from exc
        if not 200 <= resp.status_code < 300:
            raise _StateUnavailable(f"{route_name} status lookup returned {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise _StateUnavailable("status lookup: non-JSON response") from exc
        state = str(data.get("state") or "")
        reason = ""
        for rcpt in data.get("recipients") or []:
            if isinstance(rcpt, dict) and rcpt.get("error"):
                reason = str(rcpt["error"])
                break
        return state, reason

    def _reconcile(
        self, repository: "Repository", alert: Callable[[str], object] | None, now: datetime
    ) -> None:
        """Settle rows the gateway accepted by asking the app what the phone
        actually did with them:
          Sent / Delivered  -> final 'sent'
          Failed / Cancel*  -> back into the queue, retried (the app's
                               own failures, e.g. GENERIC_FAILURE, no credit)
          Pending/Processed -> keep waiting (phone offline / still sending).
                               Never retried while in flight: that could
                               text the customer twice.
        A state that can't be read for CUSTOMER_SMS_VERIFY_HOURS is closed
        as 'sent (unverified)' so the table doesn't poll forever."""
        verify_window = timedelta(hours=_env_int("CUSTOMER_SMS_VERIFY_HOURS", 24))
        max_failures = _env_int("CUSTOMER_SMS_MAX_APP_FAILURES", 12)
        failure_reasons: list[str] = []
        stuck = 0

        for platform, order_id, phone, route, gid, accepted_at, attempts in (
            repository.get_accepted_customer_sms()
        ):
            age = now - datetime.fromisoformat(accepted_at)
            if not gid:
                # Gateway gave no message id: nothing to look up.
                repository.finalize_customer_sms(platform, order_id, "unverified: no gateway id")
                continue
            if age < _STATE_SETTLE:
                continue  # give the phone a moment to actually send
            try:
                state, reason = self._fetch_state(route, gid)
            except _StateUnavailable as exc:
                if age > verify_window:
                    repository.finalize_customer_sms(platform, order_id, f"unverified: {exc}")
                continue

            if state in ("Sent", "Delivered"):
                repository.finalize_customer_sms(platform, order_id)
                log.info(
                    "customer_sms: confirmed %s for %s order %s", state, platform, order_id
                )
            elif state in ("Failed", "Cancelling", "Cancelled"):
                give_up = max_failures > 0 and attempts >= max_failures
                why = f"phone reported {state}" + (f" ({reason})" if reason else "")
                repository.requeue_customer_sms(
                    platform, order_id, why, now + _RETRY_BACKOFF, give_up
                )
                failure_reasons.append(why)
                log.error(
                    "customer_sms: %s for %s order %s (attempt %d) - %s",
                    why, platform, order_id, attempts,
                    "giving up" if give_up else "back in the queue",
                )
            else:  # Pending / Processed / unknown: still in flight
                if state == "Processed" and age > _PROCESSED_STUCK:
                    stuck += 1
                if age > verify_window:
                    repository.finalize_customer_sms(
                        platform, order_id, f"unverified: still {state or 'unknown'} after {age}"
                    )

        if alert is not None and (failure_reasons or stuck):
            if not self._last_state_alert or now - self._last_state_alert >= timedelta(hours=6):
                self._last_state_alert = now
                if failure_reasons:
                    self._safe_alert(
                        alert,
                        "⚠️ گوشی SMS Gateway پیامک ۱ را از سیم‌کارت نفرستاد "
                        f"({failure_reasons[0]}). پیام‌ها برای ارسال مجدد در صف ماندند. "
                        "اعتبار/سرویس پیامک سیم‌کارت و انتخاب SIM را چک کنید.",
                    )
                else:
                    self._safe_alert(
                        alert,
                        f"⚠️ {stuck} پیامک ۱ بیش از ۲ ساعت در وضعیت Processed گیر کرده "
                        "(خودکار دوباره ارسال نمی‌شود تا پیامک تکراری نرود).",
                    )

    def close(self) -> None:
        """Kept for API symmetry - requests are made per call, nothing to release."""
