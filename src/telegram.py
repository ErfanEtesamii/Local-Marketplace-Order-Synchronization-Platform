"""
Telegram notifications for the order sync platform.

Single entry point for all Telegram-side work: a per-order alert right
after a deal is created in Didar, end-of-day / end-of-week /
end-of-month / end-of-year aggregate reports, and an interactive
/report command that lets the operator pick any custom Jalali date
range via inline-keyboard buttons and get the same kind of report for
it - all in Persian (Jalali calendar, RTL).

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

- All reports - the daily/weekly/monthly/yearly ones AND the
  custom-range /report picker - are built LIVE from Didar itself via
  DidarDealClient.get_won_stats() (POST /deal/search_v2, Won deals,
  isolated to each source's own Deal Label), not from this project's
  own local sync cache (Repository.get_amount_stats_since()/
  synced_orders). Changed 2026-09 (client request: "باید ... با
  اندپوینت مستقیم از خود دیدار بگیره نه اینکه سفارش هایی که روی
  سیستم ثبت شدن رو بررسی کنه") after the local cache was found to
  silently undercount whenever a poll cycle was missed for longer
  than the sync engine's own fetch window - Didar is the account's
  actual source of truth and never has that gap. One consequence:
  every report now shows count + total sale amount only, no
  products/shipping breakdown - see get_won_stats()'s docstring for
  why that split isn't retrievable from Didar at all once a deal is
  saved. (Repository.get_amount_stats_since() and _format_report_message
  still exist, unused by any report now, purely so historical local
  figures remain queryable if ever needed.)

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
from src.didar.deal_client import DealStatusBreakdown, DidarDealClient
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

# ------------------------------------------------------------------
# Custom-range report picker (interactive /report command)
# ------------------------------------------------------------------
# Client request, 2026-09: a report "like the yearly one" but for any
# Jalali date range picked on demand - confirmed with the client that
# they choose the exact range themselves each time (e.g. "3 Tir to 6
# Azar"), not a fixed recurring schedule, so this is driven entirely by
# inline-keyboard button presses in Telegram rather than a cron job.
#
# Deliberately STATELESS: every button's callback_data encodes the
# partial date being built (e.g. "rpt:sm:1405:04" = start date, year
# 1405 already chosen, now picking the month) instead of keeping picker
# progress in a server-side session keyed by chat id. A process restart
# mid-pick just makes the next press on that stale keyboard fail
# harmlessly (nothing keys off in-memory state that could vanish) - the
# user re-sends /report. No per-chat session table or expiry logic
# needed. Telegram caps callback_data at 64 bytes; the longest value
# used here ("rpt:ed:1405-04-03:1405-09-06") is well under that.
_REPORT_CALLBACK_PREFIX = "rpt:"

# Key under which the getUpdates offset is persisted, via Repository's
# report_progress table (see get/set_report_marker) - reused as the
# generic key-value store it already is rather than adding a dedicated
# table just for one integer.
_UPDATE_OFFSET_MARKER = "telegram_update_offset"

# Offer the current Jalali year and the one before it as start/end year
# choices - this business only started keeping data in 1405, but one
# extra year of headroom costs nothing and avoids hardcoding a single
# year.
_REPORT_YEAR_SPAN = 2


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


def _jalali_days_in_month(year: int, month: int) -> int:
    """Days in a Jalali month: 31 for months 1-6, 30 for months 7-11,
    and for month 12 (Esfand) 29 normally or 30 in a leap year. Rather
    than reimplementing the Jalali leap-year rule (33-year cycle) by
    hand, this probes jdatetime.date itself - it already raises
    ValueError for an invalid day, exactly like the stdlib datetime.date
    it mirrors (same "don't trust an unverified library detail, check
    what's actually installed" caution as _iranian_weekday() above)."""
    if month <= 6:
        return 31
    if month <= 11:
        return 30
    try:
        jdatetime.date(year, 12, 30)
        return 30
    except ValueError:
        return 29


def _current_jalali_year() -> int:
    now_local = datetime.now(timezone.utc).astimezone(IRAN_TZ)
    return jdatetime.date.fromgregorian(date=now_local.date()).year


def _inline_keyboard(rows: list) -> dict:
    """Build a Telegram `reply_markup` dict from rows of (label,
    callback_data) tuples."""
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": data} for label, data in row]
            for row in rows
        ]
    }


def _report_cancel_row() -> list:
    return [("❌ لغو", f"{_REPORT_CALLBACK_PREFIX}cancel")]


def _report_year_keyboard(next_prefix: str) -> dict:
    """`next_prefix` is e.g. "rpt:sy" or "rpt:ey:1405-04-03" - the year
    picked gets appended as ":<year>"."""
    current = _current_jalali_year()
    years = [current - offset for offset in range(_REPORT_YEAR_SPAN - 1, -1, -1)]
    row = [(_to_persian_digits(str(y)), f"{next_prefix}:{y}") for y in years]
    return _inline_keyboard([row, _report_cancel_row()])


def _report_month_keyboard(next_prefix: str) -> dict:
    """`next_prefix` is e.g. "rpt:sm:1405" - the month picked gets
    appended as ":<month>". 3 months per row, in calendar order."""
    rows = []
    for start in range(1, 13, 3):
        rows.append([
            (_MONTH_NAMES_FA[m], f"{next_prefix}:{m}")
            for m in range(start, start + 3)
        ])
    rows.append(_report_cancel_row())
    return _inline_keyboard(rows)


def _report_day_keyboard(next_prefix: str, year: int, month: int) -> dict:
    """`next_prefix` is e.g. "rpt:sd:1405:04" - the day picked gets
    appended as ":<day>". 7 buttons per row, matching a calendar week."""
    days = _jalali_days_in_month(year, month)
    rows = []
    for start in range(1, days + 1, 7):
        rows.append([
            (_to_persian_digits(str(d)), f"{next_prefix}:{d}")
            for d in range(start, min(start + 7, days + 1))
        ])
    rows.append(_report_cancel_row())
    return _inline_keyboard(rows)


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


# The only platforms the custom-range /report picker's per-label
# breakdown shows (client request, 2026-09 follow-up 3: drop the
# شخصیت*/سازمانی/تلفنی labels Didar also returns via
# list_deal_labels() and show just these 5, in this fixed order) - see
# _select_range_report_platforms() and
# TelegramNotifier._format_live_range_report_message().
_RANGE_REPORT_PLATFORM_KEYWORDS = ["اسنپ", "تپسی", "فرازهنر", "دیجی", "سلام"]


def _select_range_report_platforms(
    per_label: list[tuple[str, "DealStatusBreakdown"]]
) -> list[tuple[str, "DealStatusBreakdown"]]:
    """Filters+reorders the live per-label breakdown from
    DidarDealClient.list_deal_labels() down to just the 5 marketplaces
    in _RANGE_REPORT_PLATFORM_KEYWORDS, in that fixed order - everything
    else Didar returns (شخصیت i/C/D/S, سازمانی, تلفنی, ...) is dropped.

    Matches by substring against the live Didar label Title rather
    than an exact string, since the confirmed real titles vary
    slightly from the plain platform name (e.g. "سایت فرازهنر" for
    فرازهنر, "با سلام" with a space for باسلام) - the first per_label
    entry whose Title contains the keyword wins. A keyword with no
    matching label in this Didar account is simply skipped rather than
    shown as a fabricated zero row, so this never invents a platform
    Didar didn't actually return."""
    selected: list[tuple[str, "DealStatusBreakdown"]] = []
    for keyword in _RANGE_REPORT_PLATFORM_KEYWORDS:
        for title, breakdown in per_label:
            if keyword in title:
                selected.append((title, breakdown))
                break
    return selected


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
        self._chat_names: dict[str, str] = {}
        self._configured: bool = False
        # Lazily constructed - only the custom-range /report picker
        # needs to talk to Didar directly (see _send_custom_range_report
        # / _get_didar_client below); every other report stays on
        # Repository's local sync cache, so most TelegramNotifier
        # instances (e.g. ones only ever used for per-order
        # notifications) never need this at all.
        self._didar_client: Optional["DidarDealClient"] = None

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
        self._chat_names = self._collect_raw_chat_names()
        self._configured = True
        log.info("telegram: bot configured (%d recipient(s): %r)", len(chat_ids), chat_ids)

        # Register /report in Telegram's own command menu (the "/" menu
        # next to the message box) - purely cosmetic, so a failure here
        # must never affect `_configured`; poll_updates() would still
        # answer a hand-typed "/report" either way.
        try:
            self._request(
                client, "setMyCommands",
                commands=[
                    {"command": "report", "description": "گزارش عملکرد یک بازه دلخواه"},
                ],
            )
        except TelegramError as exc:
            log.debug("telegram: setMyCommands failed (non-fatal): %s", exc)

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

    @staticmethod
    def _collect_raw_chat_names() -> dict[str, str]:
        """Optional display names for chat ids, read from
        TELEGRAM_CHAT_NAME_1..10 (paired with TELEGRAM_CHAT_ID_1..10 by
        index) plus TELEGRAM_CHAT_NAME for the legacy single
        TELEGRAM_CHAT_ID. Purely cosmetic - used only to say *who*
        pulled a /report in the broadcast notice added below (client
        request 2026-09); a chat id with no configured name just falls
        back to showing the raw id (see _display_name()). Keyed by the
        raw (unparsed) chat id string so lookups work whether the id
        ended up numeric or "@channel_username"."""
        names: dict[str, str] = {}
        legacy_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        legacy_name = os.getenv("TELEGRAM_CHAT_NAME", "").strip()
        if legacy_id and legacy_name:
            names[legacy_id] = legacy_name
        for i in range(1, 11):
            chat_id_raw = os.getenv(f"TELEGRAM_CHAT_ID_{i}", "").strip()
            name = os.getenv(f"TELEGRAM_CHAT_NAME_{i}", "").strip()
            if chat_id_raw and name:
                names[chat_id_raw] = name
        return names

    def _display_name(self, chat_id: Union[int, str]) -> str:
        """Human-readable label for a chat id, for the /report broadcast
        notice - the configured TELEGRAM_CHAT_NAME_N if there is one,
        otherwise the raw chat id itself."""
        return self._chat_names.get(str(chat_id), str(chat_id))

    def _broadcast_report_notice(
        self, requester_chat_id: Union[int, str], requester_name: str, period_line: str
    ) -> None:
        """Tells every OTHER configured recipient that `requester_name`
        just pulled a custom /report for `period_line` (client request
        2026-09 - "بنویسه فلانی ریپورت گرفته"). Deliberately its own
        small fan-out (not _send(), which targets every chat id
        including the requester) since the requester already has the
        report itself via _edit_message() and doesn't need this notice
        too. Same per-recipient isolation as _send(): one failed
        broadcast must never block the others, and must never affect
        the report the requester already received."""
        text = f"🔔 {requester_name} گزارش زیر را دریافت کرد:\n{period_line}"
        for chat_id in self._chat_ids:
            if chat_id == requester_chat_id:
                continue
            try:
                self._post_message(chat_id, text)
            except TelegramError as exc:
                log.error(
                    "telegram: failed to broadcast report notice to chat_id=%r: %s",
                    chat_id, exc,
                )
            except Exception:  # pragma: no cover - defensive
                log.exception(
                    "telegram: unexpected error broadcasting report notice to chat_id=%r",
                    chat_id,
                )

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

    def _aggregate_live(self, source_names: list[str], since: datetime, until: datetime) -> tuple[int, Decimal]:
        """Live Won-deal count/total straight from Didar via
        DidarDealClient.get_won_stats() - client request 2026-09: every
        report (daily/weekly/monthly/yearly, same as the custom-range
        /report picker) must reflect Didar itself, the account's real
        source of truth, rather than only the orders this program's own
        sync engine happened to see locally (which can undercount if a
        poll cycle was ever missed - see _sync_source()'s 5-hour window).
        Deliberately count+total only, no products/shipping split - see
        get_won_stats()'s docstring for why that breakdown isn't
        retrievable from Didar at all once a deal is saved. Returns
        (0, Decimal('0')) if no Didar client could be constructed,
        mirroring _send_custom_range_report's own degrade-to-zero
        behaviour rather than raising into the caller."""
        didar_client = self._get_didar_client()
        count = 0
        total = Decimal("0")
        if didar_client is None:
            log.error(
                "telegram: no Didar client available for periodic report - "
                "reporting zero results"
            )
            return count, total
        for source in source_names:
            source_count, source_total = didar_client.get_won_stats(source, since, until)
            count += source_count
            total += source_total
        return count, total

    def _aggregate_live_breakdown(
        self, since: datetime, until: datetime
    ) -> tuple[DealStatusBreakdown, list[tuple[str, DealStatusBreakdown]]]:
        """Overall total AND a breakdown per Didar Deal Label - EVERY
        label configured in the Didar account itself
        (DidarDealClient.list_deal_labels()), not just the marketplaces
        this local deployment happens to have an adapter/credentials
        for (client request, 2026-09 follow-up: "کل لیبل هارو از
        گزارش خود دیدار بگیره" - a label like اسنپ must still show up
        even when SNAPPSHOP_ENABLED is false locally, since Didar's own
        label list is the actual source of truth here, not this
        project's .env/source_names). Order matches whatever
        list_deal_labels() itself returns from Didar. Returns (zero
        total, []) if no Didar client could be constructed or the label
        list itself couldn't be fetched - degrade-to-empty rather than
        raising into the caller, same as _aggregate_live above."""
        didar_client = self._get_didar_client()
        total = DealStatusBreakdown()
        per_label: list[tuple[str, DealStatusBreakdown]] = []
        if didar_client is None:
            log.error(
                "telegram: no Didar client available for custom-range "
                "report - reporting all-zero breakdown"
            )
            return total, per_label
        for title, label_id in didar_client.list_deal_labels():
            label_breakdown = didar_client.get_status_breakdown_for_label(
                label_id, since, until
            )
            per_label.append((title, label_breakdown))
            total = total + label_breakdown
        return total, per_label

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
            "━━━━━━━━━━━━━━━━━━━━\n"
            "🟢 همه سفارش‌ها با موفقیت\n"
            "در دیدار ثبت شده‌اند.\n"
            "#گزارش"
        )

    def _format_live_report_message(
        self, title_line: str, box_label: str, period_line: str, count: int, total,
    ) -> str:
        """Used by the periodic live-from-Didar reports - daily/weekly/
        monthly/yearly (see _aggregate_live above). NOT used by the
        custom-range /report picker any more - that one shows a fuller
        All/Pending/Won/Lost breakdown instead, see
        _format_live_range_report_message below. No products/shipping
        split here - see _aggregate_live()'s docstring."""
        return (
            f"{title_line}\n"
            f"{_boxed_title(box_label)}\n"
            f"{period_line}\n"
            "🛒 تعداد سفارش‌های موفق\n"
            f"└─ {count} سفارش\n"
            "💰 مبلغ فروش\n"
            f"└─ {_format_rial(total)} ریال\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "🟢 برگرفته از معامله‌های موفق ثبت‌شده در دیدار\n"
            "(بدون احتساب هزینه ارسال).\n"
            "#گزارش"
        )

    def _send_daily_report(self, repository, source_names, day) -> None:
        if not self.is_configured():
            return
        try:
            since = _iran_midnight_utc(day)
            until = _iran_midnight_utc(day + timedelta(days=1))
            count, total = self._aggregate_live(source_names, since, until)
            period_line = f"📅 {_WEEKDAY_FA[_iranian_weekday(day)]} {_jalali_date_str(day)}"
            message = self._format_live_report_message(
                "📊 گزارش پایان روز", "📊 گزارش روزانه", period_line, count, total,
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
            count, total = self._aggregate_live(source_names, since, until)
            period_line = f"📅 {_jalali_date_str(week_start)} تا {_jalali_date_str(week_end)}"
            message = self._format_live_report_message(
                "📊 گزارش پایان هفته", "📊 گزارش هفتگی", period_line, count, total,
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
            count, total = self._aggregate_live(source_names, since, until)
            month_label = (
                f"{_MONTH_NAMES_FA[month_first_day.month]} "
                f"{_to_persian_digits(str(month_first_day.year))}"
            )
            period_line = f"📅 {month_label}"
            message = self._format_live_report_message(
                "📊 گزارش پایان ماه", "📊 گزارش ماهانه", period_line, count, total,
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
            count, total = self._aggregate_live(source_names, since, until)
            period_line = f"📅 سال {_to_persian_digits(str(year_first_day.year))}"
            message = self._format_live_report_message(
                "📊 گزارش پایان سال", "📊 گزارش سالانه", period_line, count, total,
            )
            self._send(message)
            log.info("telegram: sent yearly report for %04d", year_first_day.year)
        except TelegramError as exc:
            log.error("telegram: failed to send yearly report: %s", exc)
        except Exception:
            log.exception("telegram: unexpected error sending yearly report")

    # ------------------------------------------------------------------
    # Interactive /report command - custom Jalali date-range picker
    # (see the "Custom-range report picker" comment near the top of
    # this file for the design rationale)
    # ------------------------------------------------------------------
    def poll_updates(self, repository: Repository, source_names: list[str]) -> None:
        """Poll Telegram for new messages/button presses and drive the
        /report date-range picker. Called every few seconds from its
        own dedicated scheduler job in main.py - deliberately NOT
        folded into the main _poll_cycle() (which runs every
        settings.poll_interval_seconds, e.g. every 2 minutes by
        default): that cadence would make every button press feel
        broken. Short polling (timeout=0) rather than Telegram's own
        long-poll support - simpler, and at a few-second job interval
        the extra API calls cost nothing for a single-bot, low-traffic
        setup like this one.

        Best-effort like every other public method on this class: any
        failure is logged and swallowed so a Telegram/network hiccup
        here can never affect order syncing or the other scheduled
        jobs. The update offset is persisted via Repository's
        report_progress table (get/set_report_marker) under a
        dedicated marker key - reusing that table rather than adding a
        new one just for one integer.
        """
        if not self.is_configured():
            return
        try:
            marker = repository.get_report_marker(_UPDATE_OFFSET_MARKER)
            if marker is None:
                # First run ever - there was no update handling at all
                # before this feature existed, so the getUpdates
                # backlog could hold months of unrelated updates.
                # Fast-forward past all of it (offset=-1 asks Telegram
                # for only the single most recent update) instead of
                # processing a huge stale backlog on the first poll.
                payload = self._request(self._client, "getUpdates", offset=-1, timeout=0)
                results = payload.get("result", [])
                seed = results[-1]["update_id"] if results else 0
                repository.set_report_marker(_UPDATE_OFFSET_MARKER, str(seed))
                return

            payload = self._request(
                self._client, "getUpdates",
                offset=int(marker) + 1, timeout=0,
                allowed_updates=["message", "callback_query"],
            )
        except TelegramError as exc:
            log.warning("telegram: getUpdates failed: %s", exc)
            return
        except Exception:
            log.exception("telegram: unexpected error polling for updates")
            return

        max_update_id = None
        for update in payload.get("result", []):
            max_update_id = update["update_id"]
            try:
                self._handle_report_update(update, repository, source_names)
            except Exception:
                log.exception(
                    "telegram: failed to handle update %s", update.get("update_id")
                )
        if max_update_id is not None:
            repository.set_report_marker(_UPDATE_OFFSET_MARKER, str(max_update_id))

    def _handle_report_update(
        self, update: dict, repository: Repository, source_names: list[str]
    ) -> None:
        if "callback_query" in update:
            self._handle_report_callback(update["callback_query"], repository, source_names)
        elif "message" in update:
            self._handle_report_message(update["message"])

    def _handle_report_message(self, message: dict) -> None:
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        # Only the operator's own configured chat(s) can pull sales
        # figures this way - Telegram usernames are public, so anyone
        # who finds the bot and messages it is silently ignored, same
        # as if this handler didn't exist. (A recipient configured as
        # "@channel_username" can still RECEIVE reports but can't drive
        # the picker: Telegram sends the numeric chat id in updates,
        # never the @username - fine, since this bot is used from the
        # operator's own private chat, not a broadcast channel.)
        if chat_id not in self._chat_ids:
            return
        text = (message.get("text") or "").strip()
        command = text.split("@", 1)[0].split()[0] if text else ""
        if command == "/report":
            reply_markup = _report_year_keyboard(f"{_REPORT_CALLBACK_PREFIX}sy")
            self._send_message_with_keyboard(
                chat_id, "📅 سال شروع بازه را انتخاب کنید:", reply_markup
            )
        elif command == "/start":
            self._post_message(
                chat_id,
                "سلام! برای گرفتن گزارش یک بازه‌ی دلخواه دستور /report را بفرستید.",
            )

    def _handle_report_callback(
        self, query: dict, repository: Repository, source_names: list[str]
    ) -> None:
        query_id = query.get("id")
        message = query.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        message_id = message.get("message_id")
        data = query.get("data") or ""

        if chat_id not in self._chat_ids or not data.startswith(_REPORT_CALLBACK_PREFIX):
            self._answer_callback_query(query_id)
            return

        parts = data[len(_REPORT_CALLBACK_PREFIX):].split(":")
        action = parts[0] if parts else ""
        try:
            if action == "cancel":
                self._edit_message(
                    chat_id, message_id,
                    "❌ انتخاب بازه لغو شد.\nبرای شروع دوباره دستور /report را بفرستید.",
                )
            elif action == "sy":
                year = int(parts[1])
                self._edit_message(
                    chat_id, message_id, "📅 ماه شروع بازه را انتخاب کنید:",
                    _report_month_keyboard(f"{_REPORT_CALLBACK_PREFIX}sm:{year}"),
                )
            elif action == "sm":
                year, month = int(parts[1]), int(parts[2])
                self._edit_message(
                    chat_id, message_id, "📅 روز شروع بازه را انتخاب کنید:",
                    _report_day_keyboard(
                        f"{_REPORT_CALLBACK_PREFIX}sd:{year}:{month}", year, month
                    ),
                )
            elif action == "sd":
                year, month, day = int(parts[1]), int(parts[2]), int(parts[3])
                start_date = jdatetime.date(year, month, day)
                start_key = _jalali_key(start_date)
                self._edit_message(
                    chat_id, message_id,
                    f"✅ شروع بازه: {_jalali_date_str(start_date)}\n"
                    "📅 حالا سال پایان بازه را انتخاب کنید:",
                    _report_year_keyboard(f"{_REPORT_CALLBACK_PREFIX}ey:{start_key}"),
                )
            elif action == "ey":
                start_key, year = parts[1], int(parts[2])
                self._edit_message(
                    chat_id, message_id, "📅 ماه پایان بازه را انتخاب کنید:",
                    _report_month_keyboard(f"{_REPORT_CALLBACK_PREFIX}em:{start_key}:{year}"),
                )
            elif action == "em":
                start_key, year, month = parts[1], int(parts[2]), int(parts[3])
                self._edit_message(
                    chat_id, message_id, "📅 روز پایان بازه را انتخاب کنید:",
                    _report_day_keyboard(
                        f"{_REPORT_CALLBACK_PREFIX}ed:{start_key}:{year}:{month}", year, month
                    ),
                )
            elif action == "ed":
                start_key = parts[1]
                year, month, day = int(parts[2]), int(parts[3]), int(parts[4])
                self._send_custom_range_report(
                    chat_id, message_id, start_key,
                    jdatetime.date(year, month, day),
                    repository,
                )
            else:
                log.warning("telegram: unknown report-picker callback data %r", data)
        except (ValueError, IndexError):
            log.warning("telegram: malformed report-picker callback data %r", data)
            self._edit_message(
                chat_id, message_id,
                "⚠️ خطایی رخ داد. لطفاً دوباره دستور /report را بفرستید.",
            )
        finally:
            self._answer_callback_query(query_id)

    def _get_didar_client(self) -> Optional["DidarDealClient"]:
        """Lazily constructs (and caches) the DidarDealClient used only
        by the custom-range /report picker (see
        _send_custom_range_report below) to query Didar live -
        deliberately separate from whatever DidarDealClient the
        SyncEngine itself uses, since TelegramNotifier has no other
        reason to depend on the sync pipeline's own instance. Returns
        None - never raises - if construction fails (e.g. Didar config
        missing), so a /report press degrades to "zero results" instead
        of crashing poll_updates()."""
        if self._didar_client is None:
            try:
                self._didar_client = DidarDealClient()
            except Exception:
                log.exception("telegram: failed to construct DidarDealClient for /report")
                return None
        return self._didar_client

    def _format_live_range_report_message(
        self,
        period_line: str,
        total: DealStatusBreakdown,
        per_label: list[tuple[str, DealStatusBreakdown]],
    ) -> str:
        """Custom-range /report format - LIVE from Didar (see
        _send_custom_range_report). Shows the overall total (کل
        سفارشات, still summed across every Didar label - unaffected by
        the platform filter below) followed by one line per SELECTED
        platform - only the 5 marketplaces in
        _RANGE_REPORT_PLATFORM_KEYWORDS (client request, 2026-09
        follow-up 3: "نیازی به شخصیت‌ها نیست" - drop the شخصیت*/
        سازمانی/تلفنی labels Didar also returns and show just
        اسنپ/تپسی/فرازهنر/دیجی‌کالا/باسلام, in that fixed order), with
        that label's own count + total sale amount (all_count/
        all_total - every status, not just Won, matching what "کل
        سفارشات" always meant here). See
        _select_range_report_platforms() for the matching/ordering
        logic.

        Replaced the earlier Pending/Won/Lost status split (client
        request, 2026-09 follow-up: drop سفارشات جاری/موفق/ناموفق, show
        each platform instead), then changed again (2026-09 follow-up
        2: "کل لیبل هارو از گزارش خود دیدار بگیره") from a per-source
        breakdown keyed by this project's own configured marketplaces
        to a per-LABEL breakdown read straight from Didar, so a label
        with no locally-enabled adapter (e.g. اسنپ while
        SNAPPSHOP_ENABLED=false) still appears - and then narrowed
        again (this follow-up 3) to just the 5 platforms above. Same as
        before, deliberately without the products/shipping split: Didar
        has no way to return a saved shipping figure (see
        DidarDealClient.get_won_stats()'s docstring), so each line here
        only ever shows count + total sale amount.

        A blank line separates every platform block (client request,
        2026-09 follow-up 3: "بین هر مودوم هم یه اینتر بزن که قابل
        تشخیص باشن") so they're visually distinguishable in the
        Telegram message. A selected platform with zero matching deals
        in this window still shows a "0 سفارش" line rather than being
        hidden, same as before - it's only labels OUTSIDE the 5-
        platform list that are dropped now, not zero-count ones within
        it."""
        lines = [
            "📊 گزارش بازه دلخواه",
            _boxed_title("📊 گزارش بازه‌ای (زنده از دیدار)"),
            period_line,
            "📦 کل سفارشات",
            f"└─ {total.all_count} سفارش - {_format_rial(total.all_total)} ریال",
        ]
        for title, label_breakdown in _select_range_report_platforms(per_label):
            lines.append("")
            lines.append(f"🛍 {title}")
            lines.append(
                f"└─ {label_breakdown.all_count} سفارش - "
                f"{_format_rial(label_breakdown.all_total)} ریال"
            )
        lines.extend([
            "",
            "━━━━━━━━━━━━━━━━━━━━",
            "🟢 برگرفته از معامله‌های ثبت‌شده در دیدار",
            "(بدون احتساب هزینه ارسال).",
            "#گزارش",
        ])
        return "\n".join(lines)

    def _send_custom_range_report(
        self,
        chat_id,
        message_id,
        start_key: str,
        end_date: "jdatetime.date",
        repository: Repository,
    ) -> None:
        """Same live-from-Didar aggregation as the daily/weekly/monthly/
        yearly reports (see _aggregate_live), just for whatever custom
        range the operator picked via the /report picker (client
        request, 2026-09) instead of a fixed calendar period.

        No longer takes source_names (client request, 2026-09 follow-up
        2: "کل لیبل هارو از گزارش خود دیدار بگیره") - the per-label
        breakdown now comes straight from DidarDealClient.
        list_deal_labels() via _aggregate_live_breakdown(), independent
        of which marketplaces this local deployment has adapters/
        credentials for."""
        start_date = _jalali_from_key(start_key)
        if end_date < start_date:
            self._edit_message(
                chat_id, message_id,
                "⚠️ تاریخ پایان نمی‌تواند قبل از تاریخ شروع باشد.\n"
                "لطفاً دوباره دستور /report را بفرستید.",
            )
            return
        since = _iran_midnight_utc(start_date)
        until = _iran_midnight_utc(end_date + timedelta(days=1))

        total, per_label = self._aggregate_live_breakdown(since, until)

        period_line = f"📅 از {_jalali_date_str(start_date)} تا {_jalali_date_str(end_date)}"
        message = self._format_live_range_report_message(period_line, total, per_label)
        self._edit_message(chat_id, message_id, message)
        log.info(
            "telegram: sent live custom-range report %s..%s (source: Didar CRM, "
            "%d total across %d label(s))",
            start_key, _jalali_key(end_date), total.all_count, len(per_label),
        )
        # Let every other admin know who just pulled this report -
        # client request 2026-09 (see _broadcast_report_notice()).
        self._broadcast_report_notice(chat_id, self._display_name(chat_id), period_line)

    def _send_message_with_keyboard(
        self, chat_id: Union[int, str], text: str, reply_markup: dict
    ) -> Optional[int]:
        """sendMessage with an inline keyboard attached. Returns the new
        message's message_id (needed to edit it as the picker
        advances), or None on failure - callers must tolerate that,
        since a failed picker step must never raise into poll_updates()
        and break every later update in the same batch."""
        assert self._client is not None  # only called after is_configured()
        try:
            payload = self._request(
                self._client, "sendMessage",
                chat_id=chat_id, text=text, reply_markup=reply_markup,
            )
            return payload.get("result", {}).get("message_id")
        except TelegramError as exc:
            log.error(
                "telegram: failed to send report picker to chat_id=%r: %s", chat_id, exc
            )
            return None

    def _edit_message(
        self, chat_id, message_id, text: str, reply_markup: Optional[dict] = None
    ) -> None:
        """editMessageText for one picker message - advances the picker
        in place instead of sending a new message at every step."""
        if message_id is None:
            return
        assert self._client is not None
        try:
            kwargs = {"chat_id": chat_id, "message_id": message_id, "text": text}
            if reply_markup is not None:
                kwargs["reply_markup"] = reply_markup
            self._request(self._client, "editMessageText", **kwargs)
        except TelegramError as exc:
            log.error(
                "telegram: failed to edit report-picker message %s in chat_id=%r: %s",
                message_id, chat_id, exc,
            )

    def _answer_callback_query(self, callback_query_id: Optional[str]) -> None:
        """Clears the button's loading spinner. Best-effort - a failure
        here is cosmetic only (the button just keeps spinning briefly)
        and must never stop the rest of update handling."""
        if callback_query_id is None or self._client is None:
            return
        try:
            self._request(
                self._client, "answerCallbackQuery", callback_query_id=callback_query_id
            )
        except TelegramError as exc:
            log.debug("telegram: answerCallbackQuery failed (non-fatal): %s", exc)

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