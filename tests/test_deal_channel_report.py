"""
Tests for src/didar/deal_channel_report.py - the ONE Deal-level,
per-Channel aggregation + message layout every Telegram aggregate report
(daily/weekly/monthly/yearly/custom range) shares.

Everything here runs for each of the four periods, because the client's
requirement is that the guarantees (Total == sum of Channels, each Deal
once, Deal.Price only, no status/product text) hold identically for a
day, a week, a month and a year. No HTTP: aggregate_deals() and
format_channel_report() are pure functions over already-fetched rows.

The "golden" test uses a SYNTHETIC fixture built to sum to Shahrivar's
Excel figures (162 deals / 8,854,529,400). It proves the arithmetic and
that nothing else (items, duplicates, out-of-window rows) can change it;
it cannot prove Didar's live data produces those figures - that needs a
real run against the account.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.didar.deal_channel_report import (
    CHANNELS,
    OTHER_NAME,
    aggregate_deals,
    channel_for_titles,
    format_channel_report,
)

IRAN = timezone(timedelta(hours=3, minutes=30))
_BASE = datetime(2026, 9, 6, tzinfo=IRAN)  # an arbitrary Iran-local midnight

# (period name, [since, until)) - same shapes the four report senders build.
PERIODS = {
    "day": (_BASE, _BASE + timedelta(days=1)),
    "week": (_BASE, _BASE + timedelta(days=7)),
    "month": (_BASE, _BASE + timedelta(days=30)),
    "year": (_BASE, _BASE + timedelta(days=365)),
}
PERIOD_TITLES = {
    "day": "📊 گزارش روزانه", "week": "📊 گزارش هفتگی",
    "month": "📊 گزارش ماهانه", "year": "📊 گزارش سالانه",
}

TITLE_BY_ID = {
    "L-snapp": "اسنپ",
    "L-tapsi": "تپسی",
    "L-faraz": "سایت فرازهنر",
    "L-digi": "دیجی کالا",
    "L-basalam": "با سلام",
    "L-person": "شخصیت C",
    "L-phone": "تلفنی",
}
_CHANNEL_LABELS = ["L-snapp", "L-tapsi", "L-faraz", "L-digi", "L-basalam"]

FORBIDDEN = ("معاملات موفق", "معاملات جاری", "معاملات ناموفق")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _parse(value):
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _fmt(amount) -> str:
    return f"{int(round(float(amount))):,}"


def _row(i, when, price, labels, **extra):
    row = {"Id": f"deal-{i}", "RegisterTime": _iso(when), "Price": price, "LabelIds": labels}
    row.update(extra)
    return row


def _agg(rows, period):
    since, until = PERIODS[period]
    return aggregate_deals(rows, TITLE_BY_ID, since, until, _parse)


def _mixed_rows(period, n=25):
    """n deals spread over every channel + 'other' shapes, all inside the window."""
    since, until = PERIODS[period]
    step = (until - since) / (n + 1)
    rows = []
    for i in range(n):
        when = since + step * (i + 1)
        kind = i % 7
        if kind < 5:
            labels = [_CHANNEL_LABELS[kind]]
        elif kind == 5:
            labels = ["L-person"]      # non-marketplace label only -> other
        else:
            labels = []                # no label at all -> other
        rows.append(_row(i, when, 1_000_000 + i * 12_345, labels))
    return rows


@pytest.mark.parametrize("period", list(PERIODS))
def test_total_equals_sum_of_channels_and_other(period):
    report = _agg(_mixed_rows(period), period)
    assert report.total.count == 25
    assert report.total.count == sum(s.count for _, s in report.channels) + report.other.count
    assert report.total.total == sum((s.total for _, s in report.channels), Decimal("0")) + report.other.total


@pytest.mark.parametrize("period", list(PERIODS))
def test_channels_always_listed_in_fixed_order_even_when_empty(period):
    report = _agg([], period)
    assert [name for name, _ in report.channels] == [name for name, _ in CHANNELS]
    assert report.total.count == 0 and report.total.total == Decimal("0")


@pytest.mark.parametrize("period", list(PERIODS))
def test_multiple_items_or_duplicate_rows_never_inflate_a_deal(period):
    since, until = PERIODS[period]
    when = since + (until - since) / 2
    items = [
        {"ProductTitle": "ZZ-PRODUCT-TITLE", "ProductCode": "ZZ-CODE-9", "Quantity": 7, "UnitPrice": 999_999}
        for _ in range(5)
    ]
    row = _row(1, when, 500_000, ["L-snapp"], DealItems=items)
    # Same Deal Id returned twice (e.g. pagination overlap) + 5 items each.
    report = _agg([row, dict(row)], period)
    snapp = dict(report.channels)["اسنپ"]
    assert (snapp.count, snapp.total) == (1, Decimal("500000"))
    assert (report.total.count, report.total.total) == (1, Decimal("500000"))


@pytest.mark.parametrize("period", list(PERIODS))
def test_only_deals_registered_inside_window_count(period):
    since, until = PERIODS[period]
    rows = [
        _row(1, since, 100, ["L-snapp"]),                            # since is inclusive
        _row(2, until - timedelta(seconds=1), 200, ["L-snapp"]),     # last instant inside
        _row(3, until, 400, ["L-snapp"]),                            # until is exclusive
        _row(4, since - timedelta(seconds=1), 800, ["L-snapp"]),     # just before
    ]
    report = _agg(rows, period)
    assert (report.total.count, report.total.total) == (2, Decimal("300"))


@pytest.mark.parametrize("period", list(PERIODS))
def test_deal_with_two_marketplace_labels_is_not_double_counted(period):
    since, until = PERIODS[period]
    when = since + (until - since) / 2
    rows = [_row(1, when, 700, ["L-snapp", "L-digi"]), _row(2, when, 300, ["L-tapsi"])]
    report = _agg(rows, period)
    assert (report.total.count, report.total.total) == (2, Decimal("1000"))
    assert (report.other.count, report.other.total) == (1, Decimal("700"))
    assert report.ambiguous_deal_ids == ("deal-1",)
    by_name = dict(report.channels)
    assert by_name["اسنپ"].count == 0 and by_name["دیجی‌کالا"].count == 0
    assert by_name["تپسی"].count == 1
    assert report.total.count == sum(s.count for s in by_name.values()) + report.other.count


@pytest.mark.parametrize("period", list(PERIODS))
def test_marketplace_label_plus_non_marketplace_label_uses_the_marketplace(period):
    since, until = PERIODS[period]
    when = since + (until - since) / 2
    report = _agg([_row(1, when, 50, ["L-person", "L-faraz"])], period)
    assert dict(report.channels)["سایت فرازهنر"].count == 1
    assert report.other.count == 0


def test_unlabeled_and_unrecognized_labels_go_to_other_and_are_reported_not_added():
    since, until = PERIODS["month"]
    when = since + timedelta(days=3)
    title_by_id = dict(TITLE_BY_ID, **{"L-new": "مارکت جدید"})
    rows = [_row(1, when, 10, []), _row(2, when, 20, ["L-new"]), _row(3, when, 30, ["L-unknown-id"])]
    report = aggregate_deals(rows, title_by_id, since, until, _parse)
    assert (report.other.count, report.other.total) == (3, Decimal("60"))
    assert report.unrecognized_label_titles == ("مارکت جدید",)
    assert [n for n, _ in report.channels] == [n for n, _ in CHANNELS]  # nothing appended


def test_channel_matching_tolerates_real_title_variants():
    assert channel_for_titles(["دیجی‌کالا"]) == ("دیجی‌کالا", False)   # ZWNJ
    assert channel_for_titles(["دیجی کالا"]) == ("دیجی‌کالا", False)   # space
    assert channel_for_titles(["باسلام"]) == ("با سلام", False)
    assert channel_for_titles(["سایت فرازهنر"]) == ("سایت فرازهنر", False)
    assert channel_for_titles(["اسنپ", "تپسی"]) == (None, True)
    assert channel_for_titles(["تلفنی"]) == (None, False)


def test_missing_price_counts_the_deal_with_zero_and_naive_time_is_utc():
    since, until = PERIODS["day"]
    when = since + timedelta(hours=2)
    naive = when.astimezone(timezone.utc).replace(tzinfo=None).isoformat()
    rows = [
        {"Id": "a", "RegisterTime": _iso(when), "Price": None, "LabelIds": ["L-snapp"]},
        {"Id": "b", "RegisterTime": naive, "Price": "150", "LabelIds": ["L-snapp"]},
    ]
    report = _agg(rows, "day")
    assert (report.total.count, report.total.total) == (2, Decimal("150"))


# ---------------------------------------------------------------------
# Message layout
# ---------------------------------------------------------------------

_EXPECTED_LAYOUT = """\
📊 گزارش روزانه
📅 شنبه ۱۴۰۵/۰۶/۱۵

━━━━━━━━━━━━━━━━━━━━

📦 کل معاملات
└─ 3 معامله
└─ 600 ریال

━━━━━━━━━━━━━━━━━━━━

🟣 اسنپ
└─ 1 سفارش - 100 ریال

🟠 تپسی
└─ 0 سفارش - 0 ریال

🟢 سایت فرازهنر
└─ 0 سفارش - 0 ریال

🔴 دیجی‌کالا
└─ 1 سفارش - 200 ریال

🔵 با سلام
└─ 0 سفارش - 0 ریال

⚫ سایر
└─ 1 سفارش - 300 ریال

━━━━━━━━━━━━━━━━━━━━
🟢 برگرفته از معامله‌های ثبت‌شده در دیدار
(بدون احتساب محصولات و هزینه ارسال).
#گزارش"""


def test_message_matches_the_requested_layout_exactly():
    since, until = PERIODS["day"]
    when = since + timedelta(hours=1)
    rows = [_row(1, when, 100, ["L-snapp"]), _row(2, when, 200, ["L-digi"]), _row(3, when, 300, ["L-phone"])]
    text = format_channel_report(PERIOD_TITLES["day"], "📅 شنبه ۱۴۰۵/۰۶/۱۵", _agg(rows, "day"), _fmt)
    assert text == _EXPECTED_LAYOUT


@pytest.mark.parametrize("period", list(PERIODS))
def test_layout_is_identical_across_periods_except_title_and_period(period):
    report = _agg(_mixed_rows(period), period)
    text = format_channel_report(PERIOD_TITLES[period], "📅 PERIOD-LINE", report, _fmt)
    lines = text.split("\n")
    assert lines[0] == PERIOD_TITLES[period] and lines[1] == "📅 PERIOD-LINE"
    assert lines[2:9] == [
        "", "━━━━━━━━━━━━━━━━━━━━", "", "📦 کل معاملات",
        f"└─ {report.total.count} معامله", f"└─ {_fmt(report.total.total)} ریال", "",
    ]
    # Channel names appear in the fixed order.
    positions = [text.index("\n" + name + "\n") for name, _ in CHANNELS]
    assert positions == sorted(positions)
    assert lines[-4:] == [
        "━━━━━━━━━━━━━━━━━━━━", "🟢 برگرفته از معامله‌های ثبت‌شده در دیدار",
        "(بدون احتساب محصولات و هزینه ارسال).", "#گزارش",
    ]


@pytest.mark.parametrize("period", list(PERIODS))
def test_no_status_sections_and_no_product_data_in_any_report(period):
    since, until = PERIODS[period]
    when = since + (until - since) / 3
    items = [{"ProductTitle": "ZZ-PRODUCT-TITLE", "ProductCode": "ZZ-CODE-9",
              "ProductCategory": "ZZ-CAT", "Quantity": 7, "UnitPrice": 4_242_424}]
    rows = _mixed_rows(period) + [_row(999, when, 1, ["L-snapp"], DealItems=items)]
    text = format_channel_report(PERIOD_TITLES[period], "📅 x", _agg(rows, period), _fmt)
    for forbidden in FORBIDDEN:
        assert forbidden not in text
    for leaked in ("ZZ-PRODUCT-TITLE", "ZZ-CODE-9", "ZZ-CAT", "4,242,424", "DealItems", "Quantity"):
        assert leaked not in text


def test_other_row_is_hidden_when_empty():
    since, _ = PERIODS["day"]
    rows = [_row(1, since + timedelta(hours=1), 100, ["L-snapp"])]
    text = format_channel_report("📊 گزارش روزانه", "📅 x", _agg(rows, "day"), _fmt)
    assert OTHER_NAME not in text


# ---------------------------------------------------------------------
# Golden reference (Shahrivar, monthly) - SYNTHETIC, see module docstring
# ---------------------------------------------------------------------

GOLDEN_COUNT = 162
GOLDEN_TOTAL = 8_854_529_400


def golden_rows(since, until):
    """162 deals summing to exactly GOLDEN_TOTAL, plus noise that must not
    change it: duplicate Ids, out-of-window deals and multi-item deals."""
    step = (until - since) / (GOLDEN_COUNT + 2)
    rows = []
    running = 0
    for i in range(GOLDEN_COUNT):
        price = 54_000_000 if i < GOLDEN_COUNT - 1 else GOLDEN_TOTAL - running
        running += price
        labels = [_CHANNEL_LABELS[i % 5]] if i % 9 else ["L-phone"]
        items = [{"Quantity": 3, "UnitPrice": 1_000_000}] * (1 + i % 4)
        rows.append(_row(i, since + step * (i + 1), price, labels, DealItems=items))
    rows.extend(dict(r) for r in rows[:10])                         # duplicates
    rows.append(_row("out-1", since - timedelta(days=1), 777_000_000, ["L-snapp"]))
    rows.append(_row("out-2", until, 555_000_000, ["L-tapsi"]))
    return rows


def test_golden_shahrivar_total_is_162_deals_and_8854529400():
    since, until = PERIODS["month"]
    report = _agg(golden_rows(since, until), "month")
    assert report.total.count == GOLDEN_COUNT
    assert report.total.total == Decimal(GOLDEN_TOTAL)
    assert report.total.count == sum(s.count for _, s in report.channels) + report.other.count
    assert report.total.total == sum((s.total for _, s in report.channels), Decimal("0")) + report.other.total
    text = format_channel_report(PERIOD_TITLES["month"], "📅 شهریور ۱۴۰۵", report, _fmt)
    assert "└─ 162 معامله" in text
    assert "└─ 8,854,529,400 ریال" in text