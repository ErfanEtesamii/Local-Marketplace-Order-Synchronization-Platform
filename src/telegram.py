"""
Telegram notifications for the order sync platform.

Single entry point for all Telegram-side work: a per-order alert right
after a deal is created in Didar, and end-of-day / end-of-week /
end-of-month aggregate reports in Persian (Jalali calendar, RTL).

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

- TELEGRAM_CHAT_ID accepts either a numeric chat id or a
  "@channel_username" string, per the feature request - only bare
  int() parsing is rejected as a config error.

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
  dedicated once-a-day job.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional, Union

import jdatetime
from telegram import Bot
from telegram.error import TelegramError

from src.db.repository import Repository
from src.logger import get_logger

log = get_logger(__name__)

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
        self._bot: Optional[Bot] = None
        self._chat_id: Optional[Union[int, str]] = None
        self._configured: bool = False

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
        """True iff TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID are set and the
        bot can be reached. Caches the result so we only call get_me() once
        per process. Returns False (no exception) on any failure.

        TELEGRAM_CHAT_ID may be a numeric chat id or a "@channel_username"
        string - only a non-numeric value that also doesn't start with
        "@" is treated as a configuration error."""
        if self._configured:
            return True

        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id_raw = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if not token or not chat_id_raw:
            log.debug("telegram: credentials not set, notifications disabled")
            return False

        chat_id: Union[int, str]
        if chat_id_raw.startswith("@"):
            chat_id = chat_id_raw
        else:
            try:
                chat_id = int(chat_id_raw)
            except ValueError:
                log.warning(
                    "telegram: TELEGRAM_CHAT_ID=%r is neither numeric nor a "
                    "@channel_username - notifications disabled",
                    chat_id_raw,
                )
                return False

        try:
            bot = Bot(token=token)
            # get_me() validates the token and is the cheapest call to
            # confirm we can talk to the Telegram API. Catches both
            # network errors and "token revoked" responses.
            #
            # python-telegram-bot v20+ made every Bot method a coroutine,
            # so this must be run through asyncio.run() (or awaited) -
            # calling bot.get_me() directly would just build a coroutine
            # object and immediately discard it without ever hitting
            # Telegram's API.
            asyncio.run(bot.get_me())
        except TelegramError as exc:
            log.warning("telegram: get_me() failed (%s) - notifications disabled", exc)
            return False
        except Exception:  # pragma: no cover - defensive against httpx errors
            log.exception("telegram: unexpected error during get_me()")
            return False

        self._bot = bot
        self._chat_id = chat_id
        self._configured = True
        log.info("telegram: bot configured (chat_id=%r)", chat_id)
        return True

    # ------------------------------------------------------------------
    # Per-order notification (Requirement 1)
    # ------------------------------------------------------------------
    def notify_new_order(self, order, deal_id: str) -> None:
        """Send the Persian RTL "new order" message. `order` is a
        NormalizedOrder; `deal_id` is the Didar Deal Id returned by
        didar.sync_order() - used only for logging here, since the
        message format itself doesn't reference the Didar deal id. Safe
        to call with unconfigured credentials - silently no-ops."""
        if not self.is_configured():
            return
        try:
            message = self._format_new_order_message(order)
            self._send(message)
            log.info(
                "telegram: sent per-order notification for %s order %s (deal %s)",
                order.source, order.source_order_id, deal_id,
            )
        except TelegramError as exc:
            log.error(
                "telegram: failed to send per-order notification for %s order %s: %s",
                order.source, order.source_order_id, exc,
            )
        except Exception:
            log.exception(
                "telegram: unexpected error sending per-order notification for %s order %s",
                order.source, order.source_order_id,
            )

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

        shipping = order.shipping_cost if order.shipping_cost is not None else Decimal("0")
        products_total = sum((i.final_price for i in order.items), Decimal("0"))
        grand_total = order.total_price

        when = self._format_jalali_datetime(order.created_at)

        return (
            "🟢 سفارش جدید ثبت شد\n"
            f"🛍 پلتفرم: {emoji} {platform_name}\n"
            "👤 مشتری:\n"
            f"{customer}\n"
            "📦 محصولات:\n"
            f"{items_block}\n"
            "🚚 هزینه ارسال:\n"
            f"{_format_rial(shipping)} ریال\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "💰 مبلغ محصولات:\n"
            f"{_format_rial(products_total)} ریال\n"
            "🚚 ارسال:\n"
            f"{_format_rial(shipping)} ریال\n"
            "💳 مبلغ کل:\n"
            f"{_format_rial(grand_total)} ریال\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 {when}\n"
            "🟢 ثبت موفق در دیدار"
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
            "در دیدار ثبت شده‌اند."
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

    # ------------------------------------------------------------------
    # Low-level sender
    # ------------------------------------------------------------------
    def _send(self, text: str) -> None:
        """Actually send a message, plain text (no Markdown parse mode) -
        every report/message format here is an exact literal template, so
        parsing/escaping Markdown would only risk corrupting it. Caller is
        responsible for catching TelegramError - this method lets
        unexpected exceptions bubble so they're logged at the caller's
        exception site with context.

        Every self._bot.send_message(...) call is run through
        asyncio.run() because Bot.send_message is a coroutine (PTB v20+) -
        calling it directly with no await would build the coroutine and
        immediately drop it without ever hitting Telegram's API.
        """
        # Telegram caps a single text message at 4096 chars; the reports
        # are well under that, but split defensively if anything ever
        # grows. Splitting on blank lines keeps sections together.
        if len(text) <= 4000:
            asyncio.run(self._bot.send_message(chat_id=self._chat_id, text=text))
            return

        chunks: list[str] = []
        current = ""
        for paragraph in text.split("\n\n"):
            if len(current) + len(paragraph) + 2 > 4000 and current:
                chunks.append(current)
                current = paragraph
            else:
                current = (current + "\n\n" + paragraph).strip() if current else paragraph
        if current:
            chunks.append(current)

        for chunk in chunks:
            asyncio.run(self._bot.send_message(chat_id=self._chat_id, text=chunk))
