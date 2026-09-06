"""
Telegram notifications for the order sync platform.

Single entry point for all Telegram-side work: a per-order alert right
after a deal is created in Didar, and end-of-day / end-of-week /
end-of-month / end-of-year aggregate reports in Persian (Jalali
calendar, RTL).

DESIGN CHOICES:

- Pure no-op when TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is unset. The
  SyncEngine calls into this module after every successful Didar sync,
  and main.py's poll cycle calls into it every cycle for the report
  rollover check - a Telegram outage or missing config must never
  affect order syncing. Every public method wraps its real work in
  try/except and only logs failures, mirroring the SyncEngine's own
  "each source isolated" pattern.

- The bot is instantiated lazily inside is_configured() rather than in
  __init__, because importing the SyncEngine shouldn't need a working
  Telegram connection (and should never make a network call at import
  time - tests import SyncEngine without env vars set).

- Fan-out to multiple recipients: TELEGRAM_CHAT_ID (legacy, single) and
  TELEGRAM_CHAT_ID_1..TELEGRAM_CHAT_ID_10 are all merged into one list -
  every per-order notification and every report is sent to every
  configured chat id independently. Each accepts either a numeric chat
  id or a "@channel_username" string, per the feature request - only a
  value that's neither numeric nor "@..." is skipped (logged) rather
  than disabling the whole feature, and a send failure to one recipient
  never blocks sends to the others (see _send()).

- Reports are built directly from Repository.get_amount_stats_since()
  (see src/db/repository.py) rather than reusing src/reporting.py's
  generate_daily_report(): that file is a separate, deliberately
  English/ops-facing "is everything still polling?" health file
  (Stage 6 of the original proposal), not the Persian, money-breakdown
  report this feature asks for. Reusing it would tie two genuinely
  different audiences/formats to the same code path.

- Report scheduling is a per-poll-cycle rollover check
  (check_and_send_reports(), called from main.py's _poll_cycle), not a
  cron trigger. Two reasons: (1) APScheduler's cron trigger only
  understands the Gregorian calendar, so a Gregorian "day=1" cron
  job drifts against Jalali month boundaries (a Jalali month is 29-31
  days and doesn't line up with Gregorian day-of-month); (2) rollover
  detection also works out-of-the-box in the local server's clock
  without a separate timezone= argument to get wrong. Each check is a
  handful of dict lookups plus a Jalali date computation, so running it
  every poll cycle (default: every 2 minutes) is cheap. Markers are
  persisted (Repository.get/set_report_marker), so a restart never
  re-sends or silently skips a report even though the trigger isn't a
  dedicated once-a-day job. The same rollover-check approach is used
  for the yearly report: a Jalali year doesn't start on Gregorian
  Jan 1 either, so a Gregorian cron would drift there too.

- PLAIN SYNCHRONOUS HTTP, NO asyncio (2026-09 rewrite - root cause of
  the "orders sync to Didar but Telegram never fires" incident): this
  used to go through python-telegram-bot's async `Bot`, run via a
  single "persistent" event loop this notifier kept open for its whole
  lifetime (see git history for `_get_loop()`/`_run()`). That was
  itself a fix for an earlier bug (a fresh `asyncio.run()` per call
  closing its loop while PTB's pooled httpx connection stayed bound to
  it), but it didn't actually solve things: main.py's scheduler
  (`BlockingScheduler`) runs every poll-cycle job through APScheduler's
  default `ThreadPoolExecutor`, so different poll cycles can - and in
  production did - execute on different OS threads while the *first*
  manual `_poll_cycle()` call in `run_forever()` happens on the main
  thread. Reusing one asyncio loop/httpx connection pool across
  threads like that is exactly what kept producing intermittent
  `RuntimeError('Event loop is closed')` (confirmed in production logs
  throughout 2026-09-01, well after the "persistent loop" fix had
  already been deployed - e.g. "failed to send per-order notification
  for digikala order 361691017" right after that same order's Didar
  deal was created successfully). Since `notify_new_order()`/
  `notify_new_deal()` catch and only log every error (by design - a
  Telegram outage must never break order syncing) and
  `Repository.mark_synced()`/`mark_deal_notified()` already ran before
  the send, a failure at that point used to mean the message was gone
  forever - nothing ever retried it.

  The fix has two parts:
    1. Talk to the Telegram Bot API directly over plain synchronous
       HTTP (`httpx.Client`, not `httpx.AsyncClient`) - see
       `_post_message()`. No event loop, no thread affinity, nothing
       to go stale across a thread-pool hand-off.
    2. A failed send is no longer just logged and dropped: `_deliver()`
       persists it to `Repository`'s `notification_failures` table,
       and `retry_pending_notifications()` (called every poll cycle
       from main.py, same pattern as `SyncEngine.
       retry_pending_failures()`) retries it on a later cycle instead
       of it being silently lost - this is the piece that was missing
       even when the send itself worked reliably: a real transient
       Telegram/network hiccup had no retry path of its own.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Optional, Union

import httpx
import jdatetime

from src.db.repository import Repository
from src.http_utils import default_retry, raise_for_status_with_body
from src.logger import get_logger
from src.shipping_fees import shipping_fee_rial

if TYPE_CHECKING:
    # Only for type hints - see notify_new_deal() below. No runtime
    # import: src/didar/deal_poller.py has no dependency on this module
    # and shouldn't gain one just because this file references its type.
    from src.didar.deal_poller import NewDealInfo

log = get_logger(__name__)

_TELEGRAM_API_BASE = "https://api.telegram.org"


class TelegramError(Exception):
    """Raised for a Telegram Bot API error response (`ok: false`) or a
    network/transport failure while talking to it. Our own lightweight
    stand-in for python-telegram-bot's exception of the same name, kept
    so the switch to a plain synchronous HTTP client (see module
    docstring) didn't have to touch every `except TelegramError` clause
    below."""

# Iran has not observed daylight saving time since 2022, so a fixed
# UTC+03:30 offset is correct year-round and avoids depending on the
# `tzdata` package (needed for zoneinfo lookups on Windows, where this
# service is deployed - see README/deploy/).
IRAN_TZ = timezone(timedelta(hours=3, minutes=30))

# Persian digit set - jdatetime's strftime uses Latin digits by default
# and dates should look native in the chat (per the feature request's
# own example: "۱۴۰۵/۰۶/۰۹ — ۱۸:۴۲").
_PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")

# Money is intentionally left in ASCII digits with a plain comma
# (e.g. "12,500,000") - that's the exact example given for monetary
# values, distinct from the Persian-digit convention used for dates.
_KEYCAP_DIGITS = {
    "0": "0\ufe0f\u20e3", "1": "1\ufe0f\u20e3", "2": "2\ufe0f\u20e3",
    "3": "3\ufe0f\u20e3", "4": "4\ufe0f\u20e3", "5": "5\ufe0f\u20e3",
    "6": "6\ufe0f\u20e3", "7": "7\ufe0f\u20e3", "8": "8\ufe0f\u20e3",
    "9": "9\ufe0f\u20e3",
}

_WEEKDAY_FA = ["شنبه", "یکشنبه", "دوشنبه", "سه‌شنبه", "چهارشنبه", "پنج‌شنبه", "جمعه"]

_MONTH_NAMES_FA = [
    "", "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
    "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند",
]

# Platform emoji mapping (distinct color circle per platform), exactly as
# specified in the feature request. SnappShop isn't one of the four
# platforms named there (and is disabled by default - see config.py), so
# it gets a neutral fallback circle rather than an invented color.
_PLATFORM_DISPLAY = {
    "digikala": ("🟣", "دیجی‌کالا"),
    "basalam": ("🟢", "باسلام"),
    "tapsishop": ("🟠", "تپسی‌شاپ"),
    "farazhonar": ("🔵", "فرازهنر"),
    "snappshop": ("⚪", "اسنپ‌شاپ"),
}

# Width (in "═" characters) of the report box, matched to the box shown
# in the feature request's daily-report example. Centering text inside it
# is best-effort only - Telegram renders with a proportional font and
# emoji glyphs don't have a fixed character width, so perfect visual
# centering isn't achievable regardless of how the padding is computed.
_BOX_WIDTH = 26


def _to_persian_digits(text: str) -> str:
    """Convert ASCII digits in `text` to Persian (۰-۹)."""
    return text.translate(_PERSIAN_DIGITS)


def _format_rial(amount) -> str:
    """Format a Decimal/int/float Rial amount with thousands separators,
    e.g. 12500000 -> "12,500,000". None is treated as 0."""
    if amount is None:
        amount = 0
    rial = int(round(float(amount)))
    return f"{rial:,}"


def _emoji_number(n: int) -> str:
    """Render `n` as keycap-digit emoji (1️⃣, 2️⃣, ... 🔟 for exactly 10,
    then keycap digits concatenated for 11+, e.g. 11 -> "1️⃣1️⃣")."""
    if n == 10:
        return "🔟"
    return "".join(_KEYCAP_DIGITS[d] for d in str(n))


def _iranian_weekday(d: "jdatetime.date") -> int:
    """0=Saturday .. 6=Friday, for the Iranian week. Deliberately computed
    from Python's own `date.weekday()` (Monday=0..Sunday=6) via
    `d.togregorian()`, rather than trusting jdatetime.date.weekday()'s own
    convention - saves depending on a library detail we can't verify
    against a real interpreter with jdatetime installed in this
    environment. Mapping: Saturday(5)->0, Sunday(6)->1, Monday(0)->2,
    Tuesday(1)->3, Wednesday(2)->4, Thursday(3)->5, Friday(4)->6."""
    return (d.togregorian().weekday() - 5) % 7


def _jalali_key(d: "jdatetime.date") -> str:
    """Stable string key for a Jalali date, used as a rollover marker."""
    return f"{d.year:04d}-{d.month:02d}-{d.day:02d}"


def _jalali_from_key(key: str) -> "jdatetime.date":
    year, month, day = (int(part) for part in key.split("-"))
    return jdatetime.date(year, month, day)


def _jalali_date_str(d: "jdatetime.date") -> str:
    return _to_persian_digits(f"{d.year:04d}/{d.month:02d}/{d.day:02d}")


def _iran_midnight_utc(d: "jdatetime.date") -> datetime:
    """UTC instant corresponding to 00:00 Iran-local time on Jalali date
    `d` - used as the `since`/`until` bounds for report aggregation,
    since synced_at is stored in UTC."""
    gregorian = d.togregorian()
    local_midnight = datetime.combine(gregorian, datetime.min.time(), tzinfo=IRAN_TZ)
    return local_midnight.astimezone(timezone.utc)


def _boxed_title(label: str) -> str:
    """The ╔══╗ / label / ╚══╝ box used by every report, per the feature
    request's daily-report example."""
    pad = max(_BOX_WIDTH - len(label), 0)
    left = pad // 2 + pad % 2
    right = pad // 2
    return (
        "╔" + "═" * _BOX_WIDTH + "╗\n"
        + (" " * left) + label + (" " * right) + "\n"
        + "╚" + "═" * _BOX_WIDTH + "╝"
    )


class TelegramNotifier:
    """Send Telegram notifications and reports.

    All public methods are best-effort: they log and swallow any error
    so a Telegram problem never propagates into the SyncEngine's success
    path or main.py's poll loop. `is_configured()` is the gate - it
    lazily instantiates the bot client and verifies connectivity via
    get_me() the first time it's called."""

    def __init__(self) -> None:
        self._client: Optional[httpx.Client] = None
        self._chat_ids: list[Union[int, str]] = []
        self._configured: bool = False

    def close(self) -> None:
        """Release the underlying HTTP connection pool. Optional - the
        process exiting does this anyway - but useful for clean
        shutdown/tests that create many TelegramNotifier instances.
        Plain `httpx.Client.close()` - no event loop to manage anymore,
        see module docstring."""
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
        self._client = None

    # ------------------------------------------------------------------
    # Helper methods (delegates to module-level functions - exposed on
    # the instance because tests exercise them this way)
    # ------------------------------------------------------------------
    def _format_rial(self, amount) -> str:
        return _format_rial(amount)

    def _to_persian_digits(self, text: str) -> str:
        return _to_persian_digits(text)

    # ------------------------------------------------------------------
    # Configuration gate
    # ------------------------------------------------------------------
    def is_configured(self) -> bool:
        """True iff TELEGRAM_BOT_TOKEN is set, at least one recipient chat
        id is set and valid, and the bot can be reached. Caches the result
        so we only call get_me() once per process. Returns False (no
        exception) on any failure.

        Recipients come from TELEGRAM_CHAT_ID (legacy, single) plus
        TELEGRAM_CHAT_ID_1..TELEGRAM_CHAT_ID_10, merged into one
        deduplicated list - every one of them gets every notification and
        report (see _send()). Each may be a numeric chat id or a
        "@channel_username" string; a value that's neither is skipped
        (logged) rather than disabling the whole feature - only having
        zero valid recipients (or no token) disables it."""
        if self._configured:
            return True

        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        raw_ids = self._collect_raw_chat_ids()
        if not token or not raw_ids:
            log.debug("telegram: credentials not set, notifications disabled")
            return False

        chat_ids: list[Union[int, str]] = []
        for raw in raw_ids:
            if raw.startswith("@"):
                chat_ids.append(raw)
            else:
                try:
                    chat_ids.append(int(raw))
                except ValueError:
                    log.warning(
                        "telegram: chat id %r is neither numeric nor a "
                        "@channel_username - skipping this recipient",
                        raw,
                    )
        if not chat_ids:
            log.warning(
                "telegram: no valid chat ids among %r - notifications disabled",
                raw_ids,
            )
            return False

        client = httpx.Client(base_url=f"{_TELEGRAM_API_BASE}/bot{token}", timeout=15.0)
        try:
            # getMe validates the token and is the cheapest call to
            # confirm we can talk to the Telegram API. Catches both
            # network errors and "token revoked" responses. Plain sync
            # HTTP - see module docstring for why this is no longer an
            # async python-telegram-bot call run through an event loop.
            self._request(client, "getMe")
        except TelegramError as exc:
            log.warning("telegram: get_me() failed (%s) - notifications disabled", exc)
            client.close()
            return False
        except Exception:  # pragma: no cover - defensive against httpx errors
            log.exception("telegram: unexpected error during get_me()")
            client.close()
            return False

        self._client = client
        self._chat_ids = chat_ids
        self._configured = True
        log.info("telegram: bot configured (%d recipient(s): %r)", len(chat_ids), chat_ids)
        return True

    @staticmethod
    def _collect_raw_chat_ids() -> list[str]:
        """Merge TELEGRAM_CHAT_ID (legacy, single) with
        TELEGRAM_CHAT_ID_1..TELEGRAM_CHAT_ID_10 into one deduplicated,
        order-preserved list of raw (unparsed) chat id strings. Blank
        values are skipped."""
        raw_ids: list[str] = []
        legacy = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if legacy:
            raw_ids.append(legacy)
        for i in range(1, 11):
            value = os.getenv(f"TELEGRAM_CHAT_ID_{i}", "").strip()
            if value:
                raw_ids.append(value)

        seen: set[str] = set()
        unique: list[str] = []
        for raw in raw_ids:
            if raw not in seen:
                seen.add(raw)
                unique.append(raw)
        return unique

    # ------------------------------------------------------------------
    # Per-order notification (Requirement 1)
    # ------------------------------------------------------------------
    def notify_new_order(self, order, deal_id: str, repository: Repository) -> None:
        """Send the Persian RTL "new order" message. `order` is a
        NormalizedOrder; `deal_id` is the Didar Deal Id returned by
        didar.sync_order() - used only for logging here, since the
        message format itself doesn't reference the Didar deal id. Safe
        to call with unconfigured credentials - silently no-ops.

        `repository` is required (unlike before this rewrite) so a send
        failure can be queued for retry via `_deliver()` instead of
        being silently lost forever - see this module's docstring for
        why that used to happen: by the time this is ever called,
        `Repository.mark_synced()`/`mark_deal_notified()` have already
        run, so nothing else in the system would ever retry it."""
        if not self.is_configured():
            return
        message = self._format_new_order_message(order)
        ref_id = f"order:{order.source}:{order.source_order_id}"
        description = (
            f"per-order notification for {order.source} order "
            f"{order.source_order_id} (deal {deal_id})"
        )
        self._deliver(ref_id, message, repository, description)

    def _format_new_order_message(self, order) -> str:
        """Build the exact message body specified for Requirement 1."""
        emoji, platform_name = _PLATFORM_DISPLAY.get(order.source, ("⚪", order.source))
        customer = order.customer_full_name or "نامشخص"

        item_lines = []
        for index, item in enumerate(order.items, start=1):
            item_lines.append(f"{_emoji_number(index)} {item.title}")
            item_lines.append(
                f"   └─ {_format_rial(item.unit_price)} ریال × {item.quantity}"
            )
        items_block = "\n".join(item_lines) if item_lines else "—"

        products_total = sum((i.final_price for i in order.items), Decimal("0"))

        # FIXED SHIPPING FEE (client request, 2026-09; corrected 2026-09 -
        # see src/shipping_fees.py's module docstring for the Toman
        # figures and why they're 1,000x the original client-stated
        # values). Digikala and Faraz Honar show a flat, client-specified
        # fee here instead of the real order.shipping_cost - unlike the
        # Didar DealItem Description (src/didar/deal_client.py), which
        # shows this same fee in Toman, Telegram shows it in RIAL
        # (shipping_fee_rial() = shipping_fee_toman() * 10), and the
        # "مبلغ کل" grand total is built from products_total + this fee
        # rather than order.total_price - so the displayed total always
        # equals what's actually shown above it. Every other source (and
        # a Faraz Honar order shipped by neither Pishtaz nor Tipax) keeps
        # the original behaviour: real shipping_cost in Rial, and
        # order.total_price as the grand total.
        fixed_fee_rial = shipping_fee_rial(order)
        if fixed_fee_rial is not None:
            shipping_display = f"{_format_rial(fixed_fee_rial)} ریال"
            grand_total = products_total + fixed_fee_rial
        else:
            shipping = order.shipping_cost if order.shipping_cost is not None else Decimal("0")
            shipping_display = f"{_format_rial(shipping)} ریال"
            grand_total = order.total_price

        when = self._format_jalali_datetime(order.created_at)

        return (
            "🟢 سفارش جدید ثبت شد\n"
            f"🛍 پلتفرم:\n"
            f"{emoji} {platform_name}\n"
            "👤 مشتری:\n"
            f"{customer}\n"
            "📦 محصولات:\n"
            f"{items_block}\n"
            "🚚 هزینه ارسال:\n"
            f"{shipping_display}\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "💰 مبلغ محصولات:\n"
            f"{_format_rial(products_total)} ریال\n"
            "🚚 ارسال:\n"
            f"{shipping_display}\n"
            "💳 مبلغ کل:\n"
            f"{_format_rial(grand_total)} ریال\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 {when}\n"
            "🟢 ثبت موفق در دیدار\n"
            f"#{platform_name}"
        )

    # ------------------------------------------------------------------
    # "Any deal" notification - every Deal registered in Didar, manual
    # or automatic (client requirement, 2026-09; see
    # src/didar/deal_poller.py for how these are discovered/deduped)
    # ------------------------------------------------------------------
    def notify_new_deal(self, deal: "NewDealInfo", repository: Repository) -> None:
        """Send the Persian RTL "new deal" message for a Deal detected
        by DidarDealPoller - i.e. ANY deal that showed up in Didar,
        regardless of whether a human typed it in by hand or this
        program's own SyncEngine created it from a marketplace order.

        Deals SyncEngine itself creates are marked notified up front
        (see Repository.mark_deal_notified(), called from
        sync_engine.py right before notify_new_order()) so they reach
        Telegram exactly once - via notify_new_order() above, with the
        full order/money breakdown - never a second time through this
        generic path. Safe to call with unconfigured credentials -
        silently no-ops, same as every other public method here.

        `repository` - see notify_new_order()'s docstring: a failed
        send here is queued for retry rather than lost, and this path
        specifically has NO other safety net at all if the send fails,
        since DidarDealPoller already marked the deal notified before
        handing it to this method (see main.py's _poll_new_deals).
        """
        if not self.is_configured():
            return
        message = self._format_new_deal_message(deal)
        ref_id = f"deal:{deal.deal_id}"
        description = f"new-deal notification for deal {deal.deal_id}"
        self._deliver(ref_id, message, repository, description)

    def _format_new_deal_message(self, deal: "NewDealInfo") -> str:
        """Field set matches what's actually confirmed available from
        POST /deal/getdealdetail (Title, Person/Company.DisplayName,
        Price, Owner.DisplayName, PipelineStageId -> stage Title via
        DidarDealPoller.pipeline_stage_title()) - no field is guessed
        beyond that response shape."""
        when = self._format_jalali_datetime(deal.register_time)
        reference = f"#{deal.code}" if deal.code else deal.deal_id
        return (
            "🔔 معامله جدید در دیدار\n"
            "📌 عنوان:\n"
            f"{deal.title}\n"
            "👤 مشتری:\n"
            f"{deal.customer_name or 'نامشخص'}\n"
            "💰 مبلغ:\n"
            f"{_format_rial(deal.price)} ریال\n"
            "🧑\u200d💼 مسئول:\n"
            f"{deal.owner_name or 'نامشخص'}\n"
            "🚦 مرحله:\n"
            f"{deal.stage_name or 'نامشخص'}\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 {when}\n"
            f"شناسه معامله: {reference}\n"
            f"#{deal.title}"
        )

    def _format_jalali_datetime(self, dt: Optional[datetime]) -> str:
        """Iran-local "YYYY/MM/DD — HH:MM" in Persian digits, matching the
        feature request's example (۱۴۰۵/۰۶/۰۹ — ۱۸:۴۲). `dt` is assumed
        UTC if naive (matches how NormalizedOrder.created_at is produced);
        None falls back to the current time."""
        if dt is None:
            local = datetime.now(timezone.utc).astimezone(IRAN_TZ)
        else:
            local = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
            local = local.astimezone(IRAN_TZ)
        jalali = jdatetime.date.fromgregorian(date=local.date())
        text = f"{_jalali_date_str(jalali)} — {local.hour:02d}:{local.minute:02d}"
        return _to_persian_digits(text)

    # ------------------------------------------------------------------
    # Aggregate reports (Requirements 2-4)
    # ------------------------------------------------------------------
    def check_and_send_reports(self, repository: Repository, source_names: list[str]) -> None:
        """Called once per poll cycle from main.py's _poll_cycle(). Detects
        whether the Iran-local day / Iranian week (Saturday-Friday) / Jalali
        month has just rolled over and, if so, sends the report for the
        period that just ended - exactly once, via persisted markers in
        the repository. Runs its rollover bookkeeping even when Telegram
        isn't configured (so markers don't silently drift and cause a
        backlog to fire the day credentials are finally set), but only
        the actual send is skipped while unconfigured - see
        _send_daily_report/_send_weekly_report/_send_monthly_report."""
        try:
            now_local = datetime.now(timezone.utc).astimezone(IRAN_TZ)
            today = jdatetime.date.fromgregorian(date=now_local.date())
        except Exception:
            log.exception("telegram: failed to compute Iran-local Jalali date")
            return

        self._check_daily_rollover(repository, source_names, today)
        self._check_weekly_rollover(repository, source_names, today)
        self._check_monthly_rollover(repository, source_names, today)
        self._check_yearly_rollover(repository, source_names, today)

    def _check_daily_rollover(self, repository, source_names, today) -> None:
        key = _jalali_key(today)
        marker = repository.get_report_marker("day")
        if marker is None:
            # First run ever - nothing to report retroactively for.
            repository.set_report_marker("day", key)
            return
        if marker == key:
            return
        self._send_daily_report(repository, source_names, _jalali_from_key(marker))
        repository.set_report_marker("day", key)

    def _check_weekly_rollover(self, repository, source_names, today) -> None:
        anchor = today - timedelta(days=_iranian_weekday(today))  # this week's Saturday
        key = _jalali_key(anchor)
        marker = repository.get_report_marker("week")
        if marker is None:
            repository.set_report_marker("week", key)
            return
        if marker == key:
            return
        self._send_weekly_report(repository, source_names, _jalali_from_key(marker))
        repository.set_report_marker("week", key)

    def _check_monthly_rollover(self, repository, source_names, today) -> None:
        key = f"{today.year:04d}-{today.month:02d}"
        marker = repository.get_report_marker("month")
        if marker is None:
            repository.set_report_marker("month", key)
            return
        if marker == key:
            return
        year_str, month_str = marker.split("-")
        ended_month_first_day = jdatetime.date(int(year_str), int(month_str), 1)
        self._send_monthly_report(repository, source_names, ended_month_first_day)
        repository.set_report_marker("month", key)

    def _check_yearly_rollover(self, repository, source_names, today) -> None:
        key = f"{today.year:04d}"
        marker = repository.get_report_marker("year")
        if marker is None:
            repository.set_report_marker("year", key)
            return
        if marker == key:
            return
        ended_year_first_day = jdatetime.date(int(marker), 1, 1)
        self._send_yearly_report(repository, source_names, ended_year_first_day)
        repository.set_report_marker("year", key)

    def _aggregate(self, repository, source_names, since, until=None):
        products = shipping = total = count = 0
        for source in source_names:
            p, s, t, c = repository.get_amount_stats_since(source, since, until)
            products += p
            shipping += s
            total += t
            count += c
        return products, shipping, total, count

    def _format_report_message(
        self, title_line: str, box_label: str, period_line: str,
        products: int, shipping: int, total: int, count: int,
    ) -> str:
        average = round(total / count) if count else 0
        return (
            f"{title_line}\n"
            f"{_boxed_title(box_label)}\n"
            f"{period_line}\n"
            "🛒 تعداد سفارش‌های موفق\n"
            f"└─ {count} سفارش\n"
            "💰 مبلغ فروش محصولات\n"
            f"└─ {_format_rial(products)} ریال\n"
            "🚚 مجموع هزینه ارسال\n"
            f"└─ {_format_rial(shipping)} ریال\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "💳 مجموع فروش\n"
            f"└─ {_format_rial(total)} ریال\n"
            "📈 میانگین هر سفارش\n"
            f"└─ {_format_rial(average)} ریال\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "🟢 همه سفارش‌ها با موفقیت\n"
            "در دیدار ثبت شده‌اند.\n"
            "#گزارش"
        )

    def _send_daily_report(self, repository, source_names, day) -> None:
        if not self.is_configured():
            return
        try:
            since = _iran_midnight_utc(day)
            until = _iran_midnight_utc(day + timedelta(days=1))
            products, shipping, total, count = self._aggregate(repository, source_names, since, until)
            period_line = f"📅 {_WEEKDAY_FA[_iranian_weekday(day)]} {_jalali_date_str(day)}"
            message = self._format_report_message(
                "📊 گزارش پایان روز", "📊 گزارش روزانه", period_line,
                products, shipping, total, count,
            )
            self._send(message)
            log.info("telegram: sent daily report for %s", _jalali_key(day))
        except TelegramError as exc:
            log.error("telegram: failed to send daily report: %s", exc)
        except Exception:
            log.exception("telegram: unexpected error sending daily report")

    def _send_weekly_report(self, repository, source_names, week_start) -> None:
        if not self.is_configured():
            return
        try:
            week_end = week_start + timedelta(days=6)  # Friday
            since = _iran_midnight_utc(week_start)
            until = _iran_midnight_utc(week_end + timedelta(days=1))
            products, shipping, total, count = self._aggregate(repository, source_names, since, until)
            period_line = f"📅 {_jalali_date_str(week_start)} تا {_jalali_date_str(week_end)}"
            message = self._format_report_message(
                "📊 گزارش پایان هفته", "📊 گزارش هفتگی", period_line,
                products, shipping, total, count,
            )
            self._send(message)
            log.info(
                "telegram: sent weekly report %s..%s",
                _jalali_key(week_start), _jalali_key(week_end),
            )
        except TelegramError as exc:
            log.error("telegram: failed to send weekly report: %s", exc)
        except Exception:
            log.exception("telegram: unexpected error sending weekly report")

    def _send_monthly_report(self, repository, source_names, month_first_day) -> None:
        if not self.is_configured():
            return
        try:
            if month_first_day.month == 12:
                next_month_first = jdatetime.date(month_first_day.year + 1, 1, 1)
            else:
                next_month_first = jdatetime.date(month_first_day.year, month_first_day.month + 1, 1)
            since = _iran_midnight_utc(month_first_day)
            until = _iran_midnight_utc(next_month_first)
            products, shipping, total, count = self._aggregate(repository, source_names, since, until)
            month_label = (
                f"{_MONTH_NAMES_FA[month_first_day.month]} "
                f"{_to_persian_digits(str(month_first_day.year))}"
            )
            period_line = f"📅 {month_label}"
            message = self._format_report_message(
                "📊 گزارش پایان ماه", "📊 گزارش ماهانه", period_line,
                products, shipping, total, count,
            )
            self._send(message)
            log.info("telegram: sent monthly report for %04d-%02d",
                      month_first_day.year, month_first_day.month)
        except TelegramError as exc:
            log.error("telegram: failed to send monthly report: %s", exc)
        except Exception:
            log.exception("telegram: unexpected error sending monthly report")

    def _send_yearly_report(self, repository, source_names, year_first_day) -> None:
        if not self.is_configured():
            return
        try:
            next_year_first = jdatetime.date(year_first_day.year + 1, 1, 1)
            since = _iran_midnight_utc(year_first_day)
            until = _iran_midnight_utc(next_year_first)
            products, shipping, total, count = self._aggregate(repository, source_names, since, until)
            period_line = f"📅 سال {_to_persian_digits(str(year_first_day.year))}"
            message = self._format_report_message(
                "📊 گزارش پایان سال", "📊 گزارش سالانه", period_line,
                products, shipping, total, count,
            )
            self._send(message)
            log.info("telegram: sent yearly report for %04d", year_first_day.year)
        except TelegramError as exc:
            log.error("telegram: failed to send yearly report: %s", exc)
        except Exception:
            log.exception("telegram: unexpected error sending yearly report")

    # ------------------------------------------------------------------
    # Retry queue for failed sends (2026-09 - see module docstring)
    # ------------------------------------------------------------------
    def _deliver(self, ref_id: str, text: str, repository: Repository, description: str) -> None:
        """Attempt to send `text` right now; on ANY failure, persist it
        to Repository's notification_failures table under `ref_id` so
        retry_pending_notifications() picks it up on a later poll cycle
        instead of the message being silently gone forever. `ref_id`
        must be stable and unique per logical notification (e.g.
        "order:<platform>:<source_order_id>" or "deal:<deal_id>") so a
        retry replaces the same queued row rather than piling up
        duplicates."""
        try:
            self._send(text)
            log.info("telegram: sent %s", description)
        except TelegramError as exc:
            log.error("telegram: failed to send %s: %s - queued for retry", description, exc)
            repository.record_notification_failure(ref_id, text, str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("telegram: unexpected error sending %s - queued for retry", description)
            repository.record_notification_failure(ref_id, text, str(exc))

    def retry_pending_notifications(self, repository: Repository, max_attempts: int = 5) -> None:
        """Re-attempt every notification that failed to send on a
        previous poll cycle (see _deliver()). Called once per poll
        cycle from main.py's _poll_cycle - the matching retry path for
        the Telegram-send step specifically, mirroring SyncEngine.
        retry_pending_failures() for the Didar-sync step. Unlike a
        Didar-sync failure, a Telegram-send failure was never retried
        by anything else: mark_synced()/mark_deal_notified() already
        ran before the send was even attempted (see notify_new_order()/
        notify_new_deal()), so without this queue a failed send had no
        path back to the user at all. Gives up (leaves the row in
        place, logged, but stops retrying) after `max_attempts`, same
        cutoff convention as get_pending_failures()."""
        if not self.is_configured():
            return
        for failure in repository.get_pending_notification_failures(max_attempts=max_attempts):
            try:
                self._send(failure.message_text)
                repository.clear_notification_failure(failure.ref_id)
                log.info(
                    "telegram: retry succeeded for queued notification %s", failure.ref_id
                )
            except TelegramError as exc:
                log.error(
                    "telegram: retry failed for queued notification %s: %s",
                    failure.ref_id, exc,
                )
                repository.record_notification_failure(
                    failure.ref_id, failure.message_text, str(exc)
                )
            except Exception as exc:  # pragma: no cover - defensive
                log.exception(
                    "telegram: unexpected error retrying queued notification %s",
                    failure.ref_id,
                )
                repository.record_notification_failure(
                    failure.ref_id, failure.message_text, str(exc)
                )

    # ------------------------------------------------------------------
    # Low-level sender
    # ------------------------------------------------------------------
    def _send(self, text: str) -> None:
        """Send a message, plain text (no Markdown parse mode), to every
        configured recipient in self._chat_ids - every report/message
        format here is an exact literal template, so parsing/escaping
        Markdown would only risk corrupting it.

        Fan-out semantics: each recipient is sent to independently, so one
        bad chat id (bot blocked/kicked, chat deleted, etc.) never stops
        the message from reaching the others - that failure is logged
        here and skipped. Only if EVERY recipient fails does this method
        re-raise (the last error), so a total outage still surfaces via
        the caller's existing TelegramError handling (logged with the
        order/report context - see notify_new_order() etc.) exactly like
        before there was more than one recipient.

        Every chunk goes out via _post_message() - a plain synchronous
        HTTPS POST (see module docstring). No event loop involved, so
        there is nothing here that can go stale across a thread-pool
        hand-off the way the old async-client-on-a-persistent-loop
        approach did.
        """
        # Telegram caps a single text message at 4096 chars; the reports
        # are well under that, but split defensively if anything ever
        # grows. Splitting on blank lines keeps sections together.
        if len(text) <= 4000:
            chunks = [text]
        else:
            chunks = []
            current = ""
            for paragraph in text.split("\n\n"):
                if len(current) + len(paragraph) + 2 > 4000 and current:
                    chunks.append(current)
                    current = paragraph
                else:
                    current = (current + "\n\n" + paragraph).strip() if current else paragraph
            if current:
                chunks.append(current)

        any_success = False
        last_exc: Optional[BaseException] = None
        for chat_id in self._chat_ids:
            try:
                for chunk in chunks:
                    self._post_message(chat_id, chunk)
                any_success = True
            except TelegramError as exc:
                last_exc = exc
                log.error("telegram: failed to send to chat_id=%r: %s", chat_id, exc)
            except Exception as exc:  # pragma: no cover - defensive
                last_exc = exc
                log.exception("telegram: unexpected error sending to chat_id=%r", chat_id)

        if not any_success and last_exc is not None:
            raise last_exc

    @default_retry()
    def _post(self, client: httpx.Client, method: str, **json_body) -> dict:
        """The actual HTTPS POST, decorated with the same
        exponential-backoff retry every other external API client in
        this project uses (src/http_utils.default_retry) for transient
        5xx/network errors - a 4xx (bad token, blocked chat, etc.) is
        never retried, matching is_retryable_http_error()'s policy.
        Deliberately left raising httpx's own exceptions (not
        TelegramError) so @default_retry()'s predicate - which checks
        for httpx.HTTPStatusError/TransportError specifically - can see
        them; _request() below converts whatever survives all retries
        into TelegramError for the rest of this module."""
        resp = client.post(f"/{method}", json=json_body or None)
        raise_for_status_with_body(resp)
        return resp.json()

    def _request(self, client: httpx.Client, method: str, **json_body) -> dict:
        """One call to `https://api.telegram.org/bot<token>/<method>` -
        no asyncio, no event loop, so nothing here can ever raise
        `RuntimeError('Event loop is closed')` regardless of which OS
        thread APScheduler's ThreadPoolExecutor happens to run this
        poll cycle on (see module docstring for why that was the real
        root cause)."""
        try:
            payload = self._post(client, method, **json_body)
        except httpx.HTTPStatusError as exc:
            raise TelegramError(str(exc)) from exc
        except httpx.TransportError as exc:
            raise TelegramError(f"network error calling {method}: {exc}") from exc
        except ValueError as exc:
            raise TelegramError(f"{method}: non-JSON response") from exc
        if not payload.get("ok", False):
            raise TelegramError(f"{method} failed: {payload!r}")
        return payload

    def _post_message(self, chat_id: Union[int, str], text: str) -> None:
        """sendMessage for one chat_id/chunk - see _request()."""
        assert self._client is not None  # only called after is_configured()
        self._request(self._client, "sendMessage", chat_id=chat_id, text=text)