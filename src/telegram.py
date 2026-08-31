"""
Telegram notifications for the order sync platform.

Single entry point for all Telegram-side work: per-order alerts when a
new deal is created in Didar, and end-of-day / end-of-week / end-of-month
aggregate reports that mirror what `src/reporting.py` writes to disk.

DESIGN CHOICES:

- Pure no-op when TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is unset. The
  SyncEngine calls into this module after every successful Didar sync,
  so any failure here MUST be isolated - a Telegram outage must never
  affect order syncing. Every public method wraps its real work in
  try/except and only logs failures, mirroring the SyncEngine's own
  "each source isolated" pattern.

- The bot is instantiated lazily inside is_configured() rather than in
  __init__, because importing the SyncEngine shouldn't need a working
  Telegram connection (and should never make a network call at import
  time - tests import SyncEngine without env vars set).

- Chat ID is parsed to int with a ValueError catch - any user who pastes
  a non-numeric chat id (or the bot username with leading @) gets a
  clear "configuration error" log line rather than a hard crash at
  startup. They can then fix .env and restart.

- The report methods intentionally reuse the health-check + DB queries
  in src/reporting.py rather than recomputing their own aggregates, so
  a Telegram message and the daily report file say the same thing.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import jdatetime
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError

from src.db.repository import Repository
from src.logger import get_logger
from src.reporting import STALE_AFTER, check_health, generate_daily_report

log = get_logger(__name__)

# Persian digit set - jdatetime's strftime uses Latin digits by default
# and we want prices/dates to look native in the chat.
_PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def _to_persian_digits(text: str) -> str:
    """Convert ASCII digits in `text` to Persian (۰-۹)."""
    return text.translate(_PERSIAN_DIGITS)


def _format_rial(amount) -> str:
    """Format a Decimal/int Rial amount as a Persian-style price with
    thousands separators (e.g. 12,500,000 ریال)."""
    # Round to integer Rial - dealing with fractions of a Rial is
    # meaningless for human-facing messages and would only add noise.
    rial = int(round(float(amount)))
    # Use Arabic comma (U+066B) as thousands separator - this is the
    # character commonly used in Persian/Arabic number formatting.
    grouped = f"{rial:,}".replace(",", "٬")
    return _to_persian_digits(grouped)


def _format_jalali_date(dt: datetime) -> str:
    """Format a gregorian datetime as a long-form Jalali date string in
    Persian, e.g. 'شنبه، ۰۹ شهریور ۱۴۰۵'. Returns "نامشخص" for None."""
    if dt is None:
        return "نامشخص"
    # Drop tzinfo for jdatetime conversion - it doesn't handle aware
    # datetimes the same way across versions. The order's created_at is
    # always stored in UTC; displaying in UTC is fine for an internal
    # notification.
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    jalali = jdatetime.datetime.fromgregorian(datetime=dt)
    # %A / %B are Persian weekday/month names when locale-fa is installed;
    # fall back to Latin names if not - either way the digits get
    # converted to Persian below.
    try:
        text = jalali.strftime("%A، %d %B %Y ساعت %H:%M")
    except Exception:
        text = jalali.strftime("%Y-%m-%d %H:%M")
    return _to_persian_digits(text)


# Emoji-mapped Persian names for each marketplace - shown in the per-order
# notification so the recipient immediately knows which platform the
# order came from without reading the English code.
_PLATFORM_DISPLAY = {
    "tapsishop": "تپسی‌شاپ 🛍️",
    "digikala": "دیجی‌کالا 📦",
    "basalam": "باسلام 🏷️",
    "farazhonar": "فروشگاه فرازهنر 🏪",
    "snappshop": "اسنپ‌شاپ 🛒",
}


class TelegramNotifier:
    """Send Telegram notifications and reports.

    All public methods are best-effort: they log and swallow any error
    so a Telegram problem never propagates into the SyncEngine's success
    path. `is_configured()` is the gate - it lazily instantiates the
    bot client and verifies connectivity via get_me() the first time
    it's called."""

    def __init__(self) -> None:
        self._bot: Optional[Bot] = None
        self._chat_id: Optional[int] = None
        self._configured: bool = False

    # ------------------------------------------------------------------
    # Helper methods (delegates to module-level functions)
    # ------------------------------------------------------------------
    def _format_rial(self, amount) -> str:
        return _format_rial(amount)

    def _format_jalali_date(self, dt: datetime) -> str:
        return _format_jalali_date(dt)

    def _to_persian_digits(self, text: str) -> str:
        return _to_persian_digits(text)

    def _escape_md(self, text: str) -> str:
        return _escape_md(text)

    # ------------------------------------------------------------------
    # Configuration gate
    # ------------------------------------------------------------------
    def is_configured(self) -> bool:
        """True iff TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID are set and the
        bot can be reached. Caches the result so we only call get_me() once
        per process. Returns False (no exception) on any failure."""
        if self._configured:
            return True

        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id_raw = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if not token or not chat_id_raw:
            log.debug("telegram: credentials not set, notifications disabled")
            return False

        try:
            chat_id = int(chat_id_raw)
        except ValueError:
            log.warning(
                "telegram: TELEGRAM_CHAT_ID=%r is not numeric - notifications disabled",
                chat_id_raw,
            )
            return False

        try:
            bot = Bot(token=token)
            # get_me() validates the token and is the cheapest call to
            # confirm we can talk to the Telegram API. Catches both
            # network errors and "token revoked" responses.
            bot.get_me()
        except TelegramError as exc:
            log.warning("telegram: get_me() failed (%s) - notifications disabled", exc)
            return False
        except Exception:  # pragma: no cover - defensive against httpx errors
            log.exception("telegram: unexpected error during get_me()")
            return False

        self._bot = bot
        self._chat_id = chat_id
        self._configured = True
        log.info("telegram: bot configured (chat_id=%d)", chat_id)
        return True

    # ------------------------------------------------------------------
    # Per-order notification
    # ------------------------------------------------------------------
    def notify_new_order(self, order, deal_id: str) -> None:
        """Send a Persian RTL message announcing a newly-created deal.

        `order` is a NormalizedOrder; `deal_id` is the Didar Deal Id
        returned by didar.sync_order(). Safe to call with unconfigured
        credentials - silently no-ops in that case."""
        if not self.is_configured():
            return
        try:
            message = self._format_new_order_message(order, deal_id)
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

    def _format_new_order_message(self, order, deal_id: str) -> str:
        """Build the Markdown-V2 message body for a single new order.

        Markdown-V2 requires escaping of: _ * [ ] ( ) ` ~ > # + - = | { } . !
        We don't escape Persian/Arabic chars, only the punctuation above,
        and we also escape the platform-emoji display names since they
        contain a period in some cases."""
        platform_label = _PLATFORM_DISPLAY.get(order.source, order.source)
        created_at_jalali = _format_jalali_date(order.created_at)
        order_id_safe = str(order.source_order_id)
        deal_id_safe = str(deal_id)
        platform_safe = str(platform_label)

        # Order total - in Rial. NormalizedOrder doesn't carry a
        # price_unit field (the per-source unit lives on the adapter
        # config, not the order itself), so the engine pushes prices
        # that are already-Rial into the Didar deal (see
        # src/didar/deal_client.py). For display we therefore assume
        # the total is already in Rial and just format it.
        total_rial = _format_rial(order.total_price)

        item_lines = []
        for item in order.items:
            title_safe = self._escape_md(str(item.title))
            qty = _to_persian_digits(str(int(item.quantity)))
            price_rial = _format_rial(item.final_price)
            item_lines.append(
                f"▫️ {title_safe} × {qty} = {price_rial} ریال"
            )
        items_block = "\n".join(item_lines) if item_lines else "—"

        # Shipping cost (if any) - appended as a separate line so the
        # recipient sees the total includes it. NormalizedOrder
        # currently doesn't carry a shipping_cost field, so we read
        # it defensively; absence -> 0 -> hidden.
        shipping = getattr(order, "shipping_cost", None) or 0
        shipping_line = ""
        if shipping:
            shipping_line = (
                f"\n🚚 هزینه ارسال: {_format_rial(shipping)} ریال"
            )

        item_count = _to_persian_digits(str(sum(int(i.quantity) for i in order.items)))

        return (
            f"🆕 *سفارش جدید در دیدار*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🏪 فروشگاه: {platform_safe}\n"
            f"🆔 شماره سفارش: `{order_id_safe}`\n"
            f"🔗 شناسه دیدار: `{deal_id_safe}`\n"
            f"📅 تاریخ ثبت: {created_at_jalali}\n"
            f"📦 تعداد آیتم: {item_count}\n"
            f"💰 مبلغ کل: *{total_rial}* ریال"
            f"{shipping_line}\n"
            f"\n"
            f"📋 *اقلام سفارش:*\n"
            f"{items_block}"
        )

    # ------------------------------------------------------------------
    # Aggregate reports
    # ------------------------------------------------------------------
    def notify_daily_report(self, repository: Repository, source_names: list[str]) -> None:
        """Mirror of the daily report file, sent via Telegram.

        Reuses generate_daily_report() so the file-on-disk and the
        Telegram message can't drift apart: same data, same format."""
        if not self.is_configured():
            return
        try:
            # Write the file first (existing behaviour) - this also
            # gives us the text the file contains, in the same order.
            # We could call generate_daily_report twice but it's not
            # idempotent (overwrites) and re-reading keeps the two
            # representations in sync.
            report_path = generate_daily_report(repository, source_names)
            text = report_path.read_text(encoding="utf-8")
            message = "📊 *گزارش روزانه*\n\n" + self._escape_md(text)
            self._send(message)
            log.info("telegram: sent daily report (file=%s)", report_path)
        except TelegramError as exc:
            log.error("telegram: failed to send daily report: %s", exc)
        except Exception:
            log.exception("telegram: unexpected error sending daily report")

    def notify_weekly_report(self, repository: Repository, source_names: list[str]) -> None:
        """Send the Saturday→Friday summary (Iranian business week)."""
        if not self.is_configured():
            return
        try:
            text = self._build_weekly_report(repository, source_names)
            message = "📊 *گزارش هفتگی (شنبه تا جمعه)*\n\n" + self._escape_md(text)
            self._send(message)
            log.info("telegram: sent weekly report")
        except TelegramError as exc:
            log.error("telegram: failed to send weekly report: %s", exc)
        except Exception:
            log.exception("telegram: unexpected error sending weekly report")

    def notify_monthly_report(self, repository: Repository, source_names: list[str]) -> None:
        """Send the current Jalali month's summary."""
        if not self.is_configured():
            return
        try:
            text = self._build_monthly_report(repository, source_names)
            message = "📊 *گزارش ماهانه (تقویم جلالی)*\n\n" + self._escape_md(text)
            self._send(message)
            log.info("telegram: sent monthly report")
        except TelegramError as exc:
            log.error("telegram: failed to send monthly report: %s", exc)
        except Exception:
            log.exception("telegram: unexpected error sending monthly report")

    # ------------------------------------------------------------------
    # Report builders (private, return plain text - caller wraps in MD)
    # ------------------------------------------------------------------
    def _build_weekly_report(self, repository: Repository, source_names: list[str]) -> str:
        """Last 7 days (rolling) - simpler than computing the exact
        Saturday→Friday window and matches the daily report's `since_24h`
        approach. Saturday/Friday framing is just for the heading."""
        now = datetime.now(timezone.utc)
        week_ago = now - timedelta(days=7)
        health = check_health(repository, source_names)
        health_by_source = {h.source: h for h in health}

        lines = [
            f"🗓 بازه: {self._fmt_range(week_ago, now)}",
            f"📈 مجموع سفارش‌های هفت روز اخیر: "
            f"{_to_persian_digits(str(self._count_total(repository, source_names, week_ago)))}",
            "",
        ]
        for source in source_names:
            count = repository.count_synced_since(source, week_ago)
            failures = repository.count_pending_failures(source)
            h = health_by_source.get(source)
            status = "⚠️ غیرفعال" if (h and h.is_stale) else "✅ فعال"
            lines.append(
                f"{status} {source}\n"
                f"    • سفارش‌های هفته: {_to_persian_digits(str(count))}\n"
                f"    • خطاهای در انتظار: {_to_persian_digits(str(failures))}"
            )
        return "\n".join(lines)

    def _build_monthly_report(self, repository: Repository, source_names: list[str]) -> str:
        """Current Jalali calendar month, from the 1st of the month up
        to now. Uses jdatetime to figure out the start of the current
        month so it lines up with Persian-month boundaries."""
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        today_jalali = jdatetime.date.fromgregorian(date=now.date())
        start_jalali = jdatetime.date(today_jalali.year, today_jalali.month, 1)
        start_gregorian = start_jalali.togregorian()
        start_dt = datetime.combine(
            start_gregorian, datetime.min.time(), tzinfo=timezone.utc,
        )

        month_names_fa = [
            "", "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
            "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند",
        ]
        month_label = f"{month_names_fa[today_jalali.month]} {_to_persian_digits(str(today_jalali.year))}"

        health = check_health(repository, source_names)
        health_by_source = {h.source: h for h in health}

        lines = [
            f"📅 ماه: {month_label}",
            f"🗓 بازه: {self._fmt_range(start_dt, datetime.now(timezone.utc))}",
            f"📈 مجموع سفارش‌های ماه جاری: "
            f"{_to_persian_digits(str(self._count_total(repository, source_names, start_dt)))}",
            "",
        ]
        for source in source_names:
            count = repository.count_synced_since(source, start_dt)
            failures = repository.count_pending_failures(source)
            h = health_by_source.get(source)
            status = "⚠️ غیرفعال" if (h and h.is_stale) else "✅ فعال"
            lines.append(
                f"{status} {source}\n"
                f"    • سفارش‌های ماه: {_to_persian_digits(str(count))}\n"
                f"    • خطاهای در انتظار: {_to_persian_digits(str(failures))}"
            )
        return "\n".join(lines)

    def _count_total(self, repository: Repository, source_names: list[str], since: datetime) -> int:
        return sum(repository.count_synced_since(s, since) for s in source_names)

    def _fmt_range(self, start: datetime, end: datetime) -> str:
        return f"{_format_jalali_date(start)} ← {_format_jalali_date(end)}"

    # ------------------------------------------------------------------
    # Low-level sender + Markdown escaping
    # ------------------------------------------------------------------
    def _send(self, text: str) -> None:
        """Actually send a message. Caller is responsible for catching
        TelegramError - this method lets unexpected exceptions bubble so
        they're logged at the caller's exception site with context."""
        # Telegram caps a single text message at 4096 chars; the reports
        # are well under that, but split defensively if anything ever
        # grows. Splitting on blank lines keeps sections together.
        if len(text) <= 4000:
            self._bot.send_message(
                chat_id=self._chat_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN_V2,
            )
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
            self._bot.send_message(
                chat_id=self._chat_id,
                text=chunk,
                parse_mode=ParseMode.MARKDOWN_V2,
            )

    @staticmethod
    def _escape_md(text: str) -> str:
        """Escape Telegram MarkdownV2 special chars in user-supplied text.

        Bot-built strings (emojis, our own markdown) don't need this; it
        exists for fields like order.source_order_id, item titles, etc.
        that come from the marketplace and could contain '.' or '!' etc."""
        # Per Telegram's docs, these 18 chars MUST be escaped in MarkdownV2
        # outside of code blocks / pre / links. The backslash itself
        # must also be escaped (it's escape character in MarkdownV2).
        # Backtick ` is intentionally NOT escaped - it's used for inline code blocks.
        # Order of special chars for consistent output
        special = "!#*+-.[]()~>|={}%\\"
        result = []
        for ch in text:
            if ch in special:
                result.append("\\" + ch)
            else:
                result.append(ch)
        return "".join(result)
