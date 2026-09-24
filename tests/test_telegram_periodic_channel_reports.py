"""
Telegram aggregate reports (daily / weekly / monthly / yearly / custom
range) all go through the ONE shared Deal-level channel path
(TelegramNotifier._build_channel_report ->
DidarDealClient.get_channel_report -> src/didar/deal_channel_report.py).

These tests drive each real _send_*_report / _send_custom_range_report
method with a fake Didar client whose get_channel_report() runs the real
aggregate_deals() over fixture rows, and check what would be sent to
Telegram. The numeric rules themselves are covered in
test_deal_channel_report.py; here the point is that every period is wired
to the shared path, uses the right window, and produces the same layout.

The golden test's fixture is SYNTHETIC (built to sum to Shahrivar's Excel
figures); see test_deal_channel_report.py's docstring.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import jdatetime
import pytest

from src.db.repository import Repository
from src.didar.deal_channel_report import DealReportError, aggregate_deals
from src.telegram import TelegramNotifier, _iran_midnight_utc, _jalali_key
from tests.test_deal_channel_report import (
    FORBIDDEN,
    GOLDEN_COUNT,
    GOLDEN_TOTAL,
    TITLE_BY_ID,
    _CHANNEL_LABELS,
    _iso,
    _parse,
    _row,
    golden_rows,
)


@pytest.fixture
def repo(tmp_path):
    return Repository(db_path=str(tmp_path / "test.db"))


class FakeDidar:
    """get_channel_report() = the real aggregation over `rows_for(since, until)`."""

    def __init__(self, rows_for):
        self.rows_for = rows_for
        self.windows: list[tuple[datetime, datetime]] = []

    def get_channel_report(self, since, until):
        self.windows.append((since, until))
        return aggregate_deals(self.rows_for(since, until), TITLE_BY_ID, since, until, _parse)


def _mixed(since, until, n=20):
    step = (until - since) / (n + 1)
    rows = []
    for i in range(n):
        labels = [_CHANNEL_LABELS[i % 5]] if i % 6 else []
        items = [{"ProductTitle": "ZZ-PRODUCT", "Quantity": 9, "UnitPrice": 123}] * 3
        rows.append(_row(i, since + step * (i + 1), 1_000_000 + i, labels, DealItems=items))
    return rows


def _run(notifier, sender, *args, rows_for=_mixed):
    fake = FakeDidar(rows_for)
    with patch.object(notifier, "is_configured", return_value=True), \
         patch.object(notifier, "_get_didar_client", return_value=fake), \
         patch.object(notifier, "_send") as mock_send:
        getattr(notifier, sender)(*args)
    return fake, mock_send


_DAY = jdatetime.date(1405, 6, 15)
_WEEK_START = jdatetime.date(1405, 6, 12)  # a Saturday
_MONTH = jdatetime.date(1405, 6, 1)
_YEAR = jdatetime.date(1405, 1, 1)

CASES = [
    ("_send_daily_report", _DAY, "📊 گزارش روزانه", _DAY, _DAY + timedelta(days=1)),
    ("_send_weekly_report", _WEEK_START, "📊 گزارش هفتگی", _WEEK_START, _WEEK_START + timedelta(days=7)),
    ("_send_monthly_report", _MONTH, "📊 گزارش ماهانه", _MONTH, jdatetime.date(1405, 7, 1)),
    ("_send_yearly_report", _YEAR, "📊 گزارش سالانه", _YEAR, jdatetime.date(1406, 1, 1)),
]


@pytest.mark.parametrize("sender,arg,title,first,after", CASES)
def test_every_periodic_report_uses_shared_path_and_layout(repo, sender, arg, title, first, after):
    fake, mock_send = _run(TelegramNotifier(), sender, repo, ["digikala", "snappshop", "snappshop2"], arg)

    assert fake.windows == [(_iran_midnight_utc(first), _iran_midnight_utc(after))]
    mock_send.assert_called_once()
    text = mock_send.call_args[0][0]

    assert text.startswith(title + "\n📅 ")
    assert "📦 کل معاملات\n└─ 20 معامله\n" in text
    assert "معامله\n└─" in text and text.rstrip().endswith("#گزارش")
    assert "(بدون احتساب محصولات و هزینه ارسال)." in text
    for forbidden in FORBIDDEN:
        assert forbidden not in text
    for leaked in ("ZZ-PRODUCT", "DealItems", "Quantity", "╔"):
        assert leaked not in text

    # Total == sum of channel lines (+ "سایر").
    total_line = text.split("📦 کل معاملات\n└─ ")[1].split(" معامله")[0]
    per_channel = [int(x) for x in _findall_counts(text.split("━" * 20)[2])]
    assert int(total_line) == sum(per_channel)


def _findall_counts(block: str):
    import re
    return re.findall(r"└─ (\d+) سفارش", block)


def test_source_names_no_longer_change_the_numbers(repo):
    """snappshop + snappshop2 share ONE Didar label; the old per-source loop
    counted it twice. The shared path ignores source_names entirely."""
    n1 = TelegramNotifier()
    n2 = TelegramNotifier()
    _, send1 = _run(n1, "_send_monthly_report", repo, ["snappshop"], _MONTH)
    _, send2 = _run(n2, "_send_monthly_report", repo, ["snappshop", "snappshop2", "digikala"], _MONTH)
    assert send1.call_args[0][0] == send2.call_args[0][0]


@pytest.mark.parametrize("sender,arg,title,first,after", CASES)
def test_incomplete_didar_data_sends_nothing(repo, sender, arg, title, first, after):
    class Broken:
        def get_channel_report(self, since, until):
            raise DealReportError("boom")

    notifier = TelegramNotifier()
    with patch.object(notifier, "is_configured", return_value=True), \
         patch.object(notifier, "_get_didar_client", return_value=Broken()), \
         patch.object(notifier, "_send") as mock_send:
        getattr(notifier, sender)(repo, ["digikala"], arg)
    mock_send.assert_not_called()


def test_monthly_shahrivar_golden_reference_162_deals_8854529400_rials(repo):
    _, mock_send = _run(TelegramNotifier(), "_send_monthly_report", repo, ["digikala"], _MONTH,
                        rows_for=golden_rows)
    text = mock_send.call_args[0][0]
    assert f"└─ {GOLDEN_COUNT} معامله" in text
    assert "└─ 8,854,529,400 ریال" in text
    assert GOLDEN_TOTAL == 8_854_529_400


def test_custom_range_report_uses_same_shared_path_and_layout(repo):
    notifier = TelegramNotifier()
    fake = FakeDidar(_mixed)
    with patch.object(notifier, "_edit_message") as mock_edit, \
         patch.object(notifier, "_get_didar_client", return_value=fake), \
         patch.object(notifier, "_broadcast_report_notice"):
        notifier._send_custom_range_report(
            775753176, 42, _jalali_key(jdatetime.date(1405, 6, 1)), jdatetime.date(1405, 6, 10), repo,
        )
    assert fake.windows == [(_iran_midnight_utc(jdatetime.date(1405, 6, 1)),
                             _iran_midnight_utc(jdatetime.date(1405, 6, 11)))]
    text = mock_edit.call_args[0][2]
    assert text.startswith("📊 گزارش بازه دلخواه\n📅 از ")
    assert "📦 کل معاملات\n└─ 20 معامله\n" in text
    for forbidden in FORBIDDEN:
        assert forbidden not in text
    assert "🛍" not in text and "╔" not in text


def test_custom_range_incomplete_data_shows_error_not_zeros(repo):
    class Broken:
        def get_channel_report(self, since, until):
            raise DealReportError("boom")

    notifier = TelegramNotifier()
    with patch.object(notifier, "_edit_message") as mock_edit, \
         patch.object(notifier, "_get_didar_client", return_value=Broken()), \
         patch.object(notifier, "_broadcast_report_notice") as mock_broadcast:
        notifier._send_custom_range_report(
            775753176, 42, _jalali_key(jdatetime.date(1405, 6, 1)), jdatetime.date(1405, 6, 10), repo,
        )
    assert "ارسال نشد" in mock_edit.call_args[0][2]
    mock_broadcast.assert_not_called()
