from datetime import datetime, timedelta, timezone

import pytest

from src.db.repository import Repository
from src.reporting import check_health, generate_daily_report


@pytest.fixture
def repo(tmp_path):
    return Repository(db_path=str(tmp_path / "test.db"))


def test_source_that_never_synced_is_flagged_stale(repo):
    results = check_health(repo, ["tapsishop"])
    assert results[0].is_stale is True
    assert results[0].last_synced_at is None


def test_recently_synced_source_is_not_stale(repo):
    repo.set_last_sync_time("digikala", datetime.now(timezone.utc))
    results = check_health(repo, ["digikala"])
    assert results[0].is_stale is False


def test_source_synced_long_ago_is_flagged_stale(repo):
    old = datetime.now(timezone.utc) - timedelta(hours=5)  # older than STALE_AFTER (2h)
    repo.set_last_sync_time("basalam", old)
    results = check_health(repo, ["basalam"])
    assert results[0].is_stale is True


def test_health_includes_pending_failure_count(repo):
    repo.set_last_sync_time("snappshop", datetime.now(timezone.utc))
    repo.record_failure("snappshop", "order-1", "boom")

    results = check_health(repo, ["snappshop"])
    assert results[0].pending_failures == 1


def test_daily_report_is_written_with_expected_content(repo, monkeypatch, tmp_path):
    monkeypatch.setattr("src.reporting.REPORTS_DIR", tmp_path)

    repo.set_last_sync_time("farazhonar", datetime.now(timezone.utc))
    repo.mark_synced("farazhonar", "1", "deal-1")
    repo.mark_synced("farazhonar", "2", "deal-2")

    report_path = generate_daily_report(repo, ["farazhonar", "tapsishop"])

    assert report_path.exists()
    content = report_path.read_text(encoding="utf-8")
    assert "[OK] farazhonar" in content
    assert "orders synced in last 24h : 2" in content
    assert "[STALE] tapsishop" in content  # never synced
