"""
Shared Deal-level, per-Channel aggregation + message format for EVERY
Telegram aggregate report (daily / weekly / monthly / yearly / custom
range).

WHY THIS MODULE EXISTS (client request, 2026-09): the periodic reports
must (1) count real Didar Deals - each exactly once, priced from
Deal.Price, never from DealItems/products; (2) have a Total that always
equals the sum of the per-marketplace lines under it; (3) break down by
marketplace/Channel instead of Pending/Won/Lost; and (4) do all of that
through ONE piece of logic, so a day, a week, a month and a year can
never drift apart again. Before this, `_aggregate_live` (periodic) and
`_aggregate_live_breakdown` (custom range) were two separate
implementations, and both had the same structural flaws:
  - they queried Didar once PER LABEL, so a Deal carrying two labels was
    counted (with its full Price) under both;
  - `_aggregate_live` looped over the sync engine's adapter names, and
    "snappshop"/"snappshop2" deliberately share one Deal Label
    (config.py's deal_label_title_by_source), so that label was summed
    twice whenever both stores were enabled;
  - a Deal whose labels are none of the marketplaces (manual/phone
    deals, "شخصیت ..." labels) was silently missing from the Total, so
    it could never match a Didar export of "all deals created in the
    period".

THE CHANNEL FIELD: `LabelIds` on the Deal row returned by
POST /deal/search_v2 (a list of Deal Label Ids, resolved to Titles via
GET /Label/GetDealLabels). It is the only Deal field this project has
ever confirmed to carry the marketplace (see the "platform_label"
handling in deal_poller.py and DidarConfig.deal_label_title_by_source).
It is NOT unique per Deal - a Deal may carry several labels - which is
why the rules below exist.

RULES (each one is a decision to confirm with the client, not a fact
Didar gave us):
  - A Deal is counted once, keyed by its Deal Id, only if its own
    RegisterTime is inside [since, until).
  - Its amount is `Price` only. A missing/invalid Price counts the Deal
    with amount 0 (and logs a warning) rather than dropping it.
  - Exactly one marketplace label -> that Channel (other, non-marketplace
    labels on the same Deal are ignored).
  - No marketplace label, OR labels of more than one marketplace ->
    the "سایر" bucket, never guessed into a Channel. Ambiguous Deal Ids
    and unrecognised label titles are logged, and are returned on the
    report so callers can surface them.
  - The Channel list and its order are the constants below. A label that
    matches none of them is NEVER added automatically - it is only
    reported (log + `unrecognized_label_titles`).

No network and no Telegram here - pure functions over already-fetched
rows, so the numeric guarantees are unit-testable without mocking HTTP.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable, Iterable, Optional

from src.didar.category_mapping import _normalize_fa
from src.logger import get_logger

log = get_logger(__name__)

# (name shown in the report, keyword matched as a substring against the
# normalized Didar label Title). Fixed order = report order. Keywords
# are the same ones the custom-range report has always used; the
# confirmed real titles vary a little from the display name (e.g.
# "سایت فرازهنر", "با سلام" with a space), hence substring matching.
CHANNELS: tuple[tuple[str, str], ...] = (
    ("اسنپ", "اسنپ"),
    ("تپسی", "تپسی"),
    ("سایت فرازهنر", "فرازهنر"),
    ("دیجی‌کالا", "دیجی"),
    ("با سلام", "سلام"),
)
OTHER_NAME = "سایر"

_SEPARATOR = "━" * 20


class DealReportError(RuntimeError):
    """The Deal data needed for a report could not be fetched completely.
    Raised (rather than reporting partial/zero numbers) so a wrong report
    is never sent - callers log it and send nothing."""


@dataclass(frozen=True)
class ChannelStat:
    count: int = 0
    total: Decimal = Decimal("0")


@dataclass(frozen=True)
class DealChannelReport:
    total: ChannelStat
    # Always all of CHANNELS, in CHANNELS order (zero rows included).
    channels: tuple[tuple[str, ChannelStat], ...]
    other: ChannelStat = ChannelStat()
    # Deals carrying labels of more than one marketplace (sent to "سایر").
    ambiguous_deal_ids: tuple[str, ...] = ()
    # Titles of labels on "سایر" deals that match no configured Channel.
    unrecognized_label_titles: tuple[str, ...] = ()


def channel_for_titles(titles: Iterable[str]) -> tuple[Optional[str], bool]:
    """(channel_name, ambiguous). channel_name is None when no
    marketplace label matched OR when several did (ambiguous=True)."""
    normalized = [_normalize_fa(t) for t in titles if t]
    matched = [
        name
        for name, keyword in CHANNELS
        if any(_normalize_fa(keyword) in t for t in normalized)
    ]
    if len(matched) == 1:
        return matched[0], False
    return None, len(matched) > 1


def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def aggregate_deals(
    rows: Iterable[dict],
    title_by_id: dict[str, str],
    since: datetime,
    until: datetime,
    parse_datetime: Callable[[object], Optional[datetime]],
) -> DealChannelReport:
    """The one aggregation every report goes through. `rows` are raw
    /deal/search_v2 List rows (Id, RegisterTime, Price, LabelIds)."""
    since, until = _as_utc(since), _as_utc(until)
    seen: set[str] = set()
    total_count, total_amount = 0, Decimal("0")
    per_channel: dict[str, tuple[int, Decimal]] = {n: (0, Decimal("0")) for n, _ in CHANNELS}
    other_count, other_amount = 0, Decimal("0")
    ambiguous: list[str] = []
    unrecognized: dict[str, None] = {}

    for row in rows:
        deal_id = row.get("Id") if isinstance(row, dict) else None
        if not deal_id:
            continue
        deal_id = str(deal_id)
        if deal_id in seen:
            continue  # a Deal is counted exactly once
        registered = parse_datetime(row.get("RegisterTime"))
        if registered is None:
            log.warning("deal report: dropping Deal %s - missing/unparseable RegisterTime %r",
                        deal_id, row.get("RegisterTime"))
            continue
        if not (since <= _as_utc(registered) < until):
            continue  # Didar's date filter is only a hint; verify ourselves
        seen.add(deal_id)

        try:
            price = Decimal(str(row.get("Price")))
            if not price.is_finite():
                raise InvalidOperation
        except (InvalidOperation, TypeError, ValueError):
            log.warning("deal report: Deal %s has missing/invalid Price %r - counted with amount 0",
                        deal_id, row.get("Price"))
            price = Decimal("0")

        titles = [title_by_id[str(i)] for i in (row.get("LabelIds") or []) if str(i) in title_by_id]
        channel, is_ambiguous = channel_for_titles(titles)

        total_count += 1
        total_amount += price
        if channel is not None:
            c, a = per_channel[channel]
            per_channel[channel] = (c + 1, a + price)
        else:
            other_count += 1
            other_amount += price
            if is_ambiguous:
                ambiguous.append(deal_id)
            else:
                for t in titles:
                    unrecognized.setdefault(t)

    if ambiguous:
        log.warning("deal report: %d Deal(s) carry labels of more than one marketplace and were "
                    "put under %r: %s", len(ambiguous), OTHER_NAME, ", ".join(ambiguous))
    if unrecognized:
        log.info("deal report: label(s) matching no configured Channel on %r Deals: %s",
                 OTHER_NAME, ", ".join(unrecognized))

    return DealChannelReport(
        total=ChannelStat(total_count, total_amount),
        channels=tuple((n, ChannelStat(*per_channel[n])) for n, _ in CHANNELS),
        other=ChannelStat(other_count, other_amount),
        ambiguous_deal_ids=tuple(ambiguous),
        unrecognized_label_titles=tuple(unrecognized),
    )


def format_channel_report(
    title: str, period_line: str, report: DealChannelReport,
    format_rial: Callable[[object], str],
) -> str:
    """The single Telegram text layout for every aggregate report; only
    `title` ("📊 گزارش ...") and `period_line` ("📅 ...") vary. "سایر"
    is shown only when it is non-empty, so the fixed 5-Channel layout is
    unchanged in the normal case."""
    lines = [
        title,
        period_line,
        "",
        _SEPARATOR,
        "",
        "📦 کل معاملات",
        f"└─ {report.total.count} معامله",
        f"└─ {format_rial(report.total.total)} ریال",
        "",
        _SEPARATOR,
    ]
    rows = list(report.channels)
    if report.other.count:
        rows.append((OTHER_NAME, report.other))
    for name, stat in rows:
        lines.extend(["", name, f"└─ {stat.count} سفارش - {format_rial(stat.total)} ریال"])
    lines.extend([
        "",
        _SEPARATOR,
        "🟢 برگرفته از معامله‌های ثبت‌شده در دیدار",
        "(بدون احتساب محصولات و هزینه ارسال).",
        "#گزارش",
    ])
    return "\n".join(lines)
