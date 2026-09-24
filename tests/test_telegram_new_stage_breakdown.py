"""
Tests for TelegramNotifier._aggregate_new_stage_breakdown() (src/telegram.py)
- step 3 of the "تفکیک سفارش‌های مرحله «مشتری جدید»" feature.

Unlike _build_channel_report (which calls out to
Didar live), this function reads purely from
Repository.get_new_stage_deals(), so these tests use a real (temp-file)
Repository seeded via record_new_stage_deal() - no Didar client, no
mocking of network calls - and never touch the existing Won/Pending/
Lost report paths.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.db.repository import Repository
from src.telegram import NewStageBreakdown, TelegramNotifier


@pytest.fixture
def repo(tmp_path):
    return Repository(db_path=str(tmp_path / "test.db"))


@pytest.fixture
def notifier():
    return TelegramNotifier()


def test_empty_window_returns_zero_total_and_empty_breakdown(repo, notifier):
    since = datetime.now(timezone.utc) - timedelta(days=1)
    total, per_label = notifier._aggregate_new_stage_breakdown(repo, since)
    assert total == NewStageBreakdown()
    assert per_label == []


def test_groups_and_sums_by_label_title(repo, notifier):
    base = datetime.now(timezone.utc)
    repo.record_new_stage_deal("deal-1", "lbl-a", "دیجی‌کالا", 100_000, base)
    repo.record_new_stage_deal("deal-2", "lbl-a", "دیجی‌کالا", 250_000, base + timedelta(minutes=1))
    repo.record_new_stage_deal("deal-3", "lbl-b", "باسلام", 50_000, base + timedelta(minutes=2))

    total, per_label = notifier._aggregate_new_stage_breakdown(
        repo, since=base - timedelta(minutes=1)
    )

    assert total == NewStageBreakdown(count=3, total=Decimal("400000"))
    assert per_label == [
        ("دیجی‌کالا", NewStageBreakdown(count=2, total=Decimal("350000"))),
        ("باسلام", NewStageBreakdown(count=1, total=Decimal("50000"))),
    ]


def test_unresolved_label_is_grouped_under_no_label_bucket(repo, notifier):
    base = datetime.now(timezone.utc)
    repo.record_new_stage_deal("deal-1", None, None, 10_000, base)
    repo.record_new_stage_deal("deal-2", None, None, 20_000, base + timedelta(minutes=1))

    total, per_label = notifier._aggregate_new_stage_breakdown(
        repo, since=base - timedelta(minutes=1)
    )

    assert total == NewStageBreakdown(count=2, total=Decimal("30000"))
    assert per_label == [("بدون لیبل", NewStageBreakdown(count=2, total=Decimal("30000")))]


def test_null_amount_contributes_zero_total_but_still_counts(repo, notifier):
    base = datetime.now(timezone.utc)
    repo.record_new_stage_deal("deal-1", "lbl-a", "دیجی‌کالا", None, base)

    total, per_label = notifier._aggregate_new_stage_breakdown(
        repo, since=base - timedelta(minutes=1)
    )

    assert total == NewStageBreakdown(count=1, total=Decimal("0"))
    assert per_label == [("دیجی‌کالا", NewStageBreakdown(count=1, total=Decimal("0")))]


def test_since_until_window_excludes_rows_outside_it(repo, notifier):
    base = datetime.now(timezone.utc)
    before = base - timedelta(days=2)
    inside = base - timedelta(hours=1)
    after = base + timedelta(days=2)
    repo.record_new_stage_deal("deal-before", "lbl-a", "دیجی‌کالا", 1_000, before)
    repo.record_new_stage_deal("deal-inside", "lbl-a", "دیجی‌کالا", 2_000, inside)
    repo.record_new_stage_deal("deal-after", "lbl-a", "دیجی‌کالا", 3_000, after)

    total, per_label = notifier._aggregate_new_stage_breakdown(
        repo, since=base - timedelta(days=1), until=base + timedelta(days=1)
    )

    assert total == NewStageBreakdown(count=1, total=Decimal("2000"))
    assert per_label == [("دیجی‌کالا", NewStageBreakdown(count=1, total=Decimal("2000")))]


def test_until_none_means_no_upper_bound(repo, notifier):
    base = datetime.now(timezone.utc)
    far_future = base + timedelta(days=365)
    repo.record_new_stage_deal("deal-now", "lbl-a", "دیجی‌کالا", 1_000, base)
    repo.record_new_stage_deal("deal-far-future", "lbl-a", "دیجی‌کالا", 2_000, far_future)

    total, _ = notifier._aggregate_new_stage_breakdown(repo, since=base - timedelta(minutes=1))

    assert total == NewStageBreakdown(count=2, total=Decimal("3000"))


def test_does_not_touch_won_pending_lost_report_paths(notifier):
    """Purely a sanity check that this feature is additive: the shared
    live-Didar Deal/Channel report path is still present and distinct
    from the new local one."""
    assert hasattr(notifier, "_build_channel_report")
    assert notifier._aggregate_new_stage_breakdown is not notifier._build_channel_report
