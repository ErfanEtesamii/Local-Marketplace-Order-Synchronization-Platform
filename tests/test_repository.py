from datetime import datetime, timedelta, timezone

import pytest

from src.db.repository import Repository


@pytest.fixture
def repo(tmp_path):
    return Repository(db_path=str(tmp_path / "test.db"))


def test_dedupe_marks_and_checks_synced_orders(repo):
    assert repo.is_already_synced("tapsishop", "123") is False
    repo.mark_synced("tapsishop", "123", "deal-abc")
    assert repo.is_already_synced("tapsishop", "123") is True


def test_failure_tracking_increments_attempt_count(repo):
    repo.record_failure("digikala", "456", "timeout")
    repo.record_failure("digikala", "456", "timeout again")

    failures = repo.get_pending_failures()
    assert len(failures) == 1
    assert failures[0].attempt_count == 2
    assert failures[0].error_message == "timeout again"


def test_marking_synced_clears_a_prior_failure(repo):
    repo.record_failure("digikala", "456", "timeout")
    repo.mark_synced("digikala", "456", "deal-xyz")

    assert repo.get_pending_failures() == []


def test_sync_watermark_roundtrip(repo):
    assert repo.get_last_sync_time("tapsishop") is None

    now = datetime.now(timezone.utc)
    repo.set_last_sync_time("tapsishop", now)

    stored = repo.get_last_sync_time("tapsishop")
    assert stored == now


def test_shipment_watermark_roundtrip(repo):
    assert repo.get_last_shipment_id("digikala") is None

    repo.set_last_shipment_id("digikala", 42)
    assert repo.get_last_shipment_id("digikala") == 42


def test_notified_deals_dedup_roundtrip(repo):
    """See src/didar/deal_poller.py - this is the guard that stops a
    Deal from ever being sent to Telegram twice."""
    assert repo.is_deal_notified("deal-1") is False
    repo.mark_deal_notified("deal-1")
    assert repo.is_deal_notified("deal-1") is True


def test_marking_a_deal_notified_twice_does_not_raise(repo):
    repo.mark_deal_notified("deal-1")
    repo.mark_deal_notified("deal-1")  # INSERT OR IGNORE - must not raise
    assert repo.is_deal_notified("deal-1") is True


def test_deal_poll_watermark_roundtrip(repo):
    assert repo.get_deal_poll_watermark() is None

    now = datetime.now(timezone.utc)
    repo.set_deal_poll_watermark(now)
    assert repo.get_deal_poll_watermark() == now

    later = now + timedelta(minutes=2)
    repo.set_deal_poll_watermark(later)
    assert repo.get_deal_poll_watermark() == later

    repo.set_last_shipment_id("digikala", 99)
    assert repo.get_last_shipment_id("digikala") == 99


def test_shipment_watermark_is_scoped_per_platform(repo):
    repo.set_last_shipment_id("digikala", 10)
    assert repo.get_last_shipment_id("tapsishop") is None
    assert repo.get_last_shipment_id("digikala") == 10


def test_count_synced_since_only_counts_within_window(repo):
    repo.mark_synced("farazhonar", "1", "deal-1")

    since_far_future = datetime.now(timezone.utc) + timedelta(hours=1)
    since_recent_past = datetime.now(timezone.utc) - timedelta(hours=1)

    assert repo.count_synced_since("farazhonar", since_recent_past) == 1
    assert repo.count_synced_since("farazhonar", since_far_future) == 0


def test_count_pending_failures_is_scoped_per_source(repo):
    repo.record_failure("basalam", "1", "err")
    repo.record_failure("snappshop", "2", "err")

    assert repo.count_pending_failures("basalam") == 1
    assert repo.count_pending_failures("snappshop") == 1
    assert repo.count_pending_failures("digikala") == 0


# --- new_customer_stage_deals -----------------------------------------

def test_record_new_stage_deal_is_idempotent_on_deal_id(repo):
    now = datetime.now(timezone.utc)
    later = now + timedelta(minutes=5)

    repo.record_new_stage_deal("deal-1", "label-1", "کالای دیجیتال", 100000, now)
    # Same deal seen again on a later poll cycle: must not overwrite
    # entered_at or create a second row.
    repo.record_new_stage_deal("deal-1", "label-1", "کالای دیجیتال", 100000, later)

    rows = repo.get_new_stage_deals(since=now - timedelta(minutes=1))
    assert len(rows) == 1
    assert rows[0].deal_id == "deal-1"
    assert rows[0].entered_at == now.isoformat()


def test_get_new_stage_deals_filters_by_since_until_window(repo):
    base = datetime.now(timezone.utc)
    before = base - timedelta(days=2)
    inside = base - timedelta(hours=1)
    after = base + timedelta(days=2)

    repo.record_new_stage_deal("deal-before", None, None, None, before)
    repo.record_new_stage_deal("deal-inside", None, None, None, inside)
    repo.record_new_stage_deal("deal-after", None, None, None, after)

    rows = repo.get_new_stage_deals(since=base - timedelta(days=1), until=base + timedelta(days=1))
    ids = {row.deal_id for row in rows}
    assert ids == {"deal-inside"}


def test_get_new_stage_deals_since_is_inclusive_and_until_is_exclusive(repo):
    t0 = datetime.now(timezone.utc)
    t1 = t0 + timedelta(hours=1)

    repo.record_new_stage_deal("deal-at-since", None, None, None, t0)
    repo.record_new_stage_deal("deal-at-until", None, None, None, t1)

    rows = repo.get_new_stage_deals(since=t0, until=t1)
    ids = {row.deal_id for row in rows}
    # since boundary included, until boundary excluded
    assert ids == {"deal-at-since"}


def test_get_new_stage_deals_without_until_has_no_upper_bound(repo):
    now = datetime.now(timezone.utc)
    far_future = now + timedelta(days=365)

    repo.record_new_stage_deal("deal-now", None, None, None, now)
    repo.record_new_stage_deal("deal-far-future", None, None, None, far_future)

    rows = repo.get_new_stage_deals(since=now)
    ids = {row.deal_id for row in rows}
    assert ids == {"deal-now", "deal-far-future"}


def test_new_stage_deals_never_exposes_a_delete_or_reset_method(repo):
    """Architectural invariant from the design prompt: no row in this
    table is ever removed or reset - every report period is just a
    since/until filter over the same ever-growing table."""
    assert not hasattr(repo, "delete_new_stage_deal")
    assert not hasattr(repo, "reset_new_stage_deals")
    assert not hasattr(repo, "clear_new_stage_deals")