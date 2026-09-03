"""
Tests for src/didar/deal_poller.py - the "any deal" poller behind the
2026-09 requirement that every Deal registered in Didar (manual or
automatic) triggers a Telegram notification.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
import respx

from src.config import DidarConfig
from src.db.repository import Repository
from src.didar.deal_poller import DidarDealPoller, NewDealInfo

_CFG = DidarConfig(base_url="https://app.didar.me/api", api_key="test-key")


@pytest.fixture
def repo(tmp_path):
    return Repository(db_path=str(tmp_path / "test.db"))


@pytest.fixture
def poller():
    return DidarDealPoller(config=_CFG)


def _search_response(rows: list[dict]) -> dict:
    return {
        "Response": {
            "TotalCount": len(rows),
            "List": rows,
        }
    }


def _deal_row(deal_id: str) -> dict:
    return {
        "Id": deal_id,
        "Title": f"معامله {deal_id}",
        "RegisterTime": "2026-09-03T08:00:00Z",
        "Price": "100000",
        "PipelineStageId": "stage-1",
        "OwnerId": "owner-1",
    }


def _deal_detail(deal_id: str, **overrides) -> dict:
    detail = {
        "Id": deal_id,
        "Code": 4242,
        "Title": f"معامله {deal_id}",
        "RegisterTime": "2026-09-03T08:00:00Z",
        "Price": 250000,
        "PipelineStageId": "stage-1",
        "Owner": {"UserId": "owner-1", "DisplayName": "نگین عابدیان"},
        "Person": {"Id": "person-1", "DisplayName": "علی رضایی"},
        "Company": None,
    }
    detail.update(overrides)
    return {"Response": detail}


_PIPELINES_RESPONSE = {
    "Response": [
        {
            "Id": "pipe-1",
            "Title": "کاریز تست",
            "Stages": [
                {"Id": "stage-1", "Title": "مذاکرات اولیه"},
                {"Id": "stage-2", "Title": "پیگیری"},
            ],
        }
    ]
}


# --- watermark / first-run behaviour ------------------------------------


@respx.mock
def test_first_run_seeds_watermark_and_does_not_backfill(poller, repo):
    assert repo.get_deal_poll_watermark() is None

    result = poller.poll_new_deals(repo)

    assert result == []
    assert repo.get_deal_poll_watermark() is not None
    # No API calls at all on a bootstrap run - nothing to search yet.


@respx.mock
def test_search_failure_leaves_watermark_untouched(poller, repo):
    now = datetime.now(timezone.utc)
    repo.set_deal_poll_watermark(now - timedelta(minutes=5))

    respx.post("https://app.didar.me/api/deal/search_v2").mock(
        return_value=httpx.Response(500, json={"error": "boom"})
    )

    result = poller.poll_new_deals(repo)

    assert result == []
    # Watermark unchanged (still the value we seeded) so the same
    # window is retried next cycle rather than silently skipped.
    assert repo.get_deal_poll_watermark() == now - timedelta(minutes=5)


# --- new deal discovery + dedup ------------------------------------------


@respx.mock
def test_poll_new_deals_returns_new_deal_info(poller, repo):
    repo.set_deal_poll_watermark(datetime.now(timezone.utc) - timedelta(minutes=5))

    respx.post("https://app.didar.me/api/deal/search_v2").mock(
        return_value=httpx.Response(200, json=_search_response([_deal_row("deal-1")]))
    )
    respx.post("https://app.didar.me/api/deal/getdealdetail").mock(
        return_value=httpx.Response(200, json=_deal_detail("deal-1"))
    )
    respx.post("https://app.didar.me/api/pipeline/list/0").mock(
        return_value=httpx.Response(200, json=_PIPELINES_RESPONSE)
    )

    [deal] = poller.poll_new_deals(repo)

    assert deal == NewDealInfo(
        deal_id="deal-1",
        code=4242,
        title="معامله deal-1",
        customer_name="علی رضایی",
        price=Decimal("250000"),
        owner_name="نگین عابدیان",
        stage_name="مذاکرات اولیه",
        register_time=datetime(2026, 9, 3, 8, 0, 0, tzinfo=timezone.utc),
    )
    # Discovering it must have marked it notified so it isn't sent twice.
    assert repo.is_deal_notified("deal-1") is True


@respx.mock
def test_already_notified_deal_is_skipped_without_a_detail_call(poller, repo):
    repo.set_deal_poll_watermark(datetime.now(timezone.utc) - timedelta(minutes=5))
    repo.mark_deal_notified("deal-1")

    respx.post("https://app.didar.me/api/deal/search_v2").mock(
        return_value=httpx.Response(200, json=_search_response([_deal_row("deal-1")]))
    )
    detail_route = respx.post("https://app.didar.me/api/deal/getdealdetail").mock(
        return_value=httpx.Response(200, json=_deal_detail("deal-1"))
    )

    result = poller.poll_new_deals(repo)

    assert result == []
    assert detail_route.call_count == 0


@respx.mock
def test_detail_fetch_failure_does_not_mark_notified(poller, repo):
    """A deal whose getdealdetail call fails must be retried on the
    next cycle, not silently dropped forever."""
    repo.set_deal_poll_watermark(datetime.now(timezone.utc) - timedelta(minutes=5))

    respx.post("https://app.didar.me/api/deal/search_v2").mock(
        return_value=httpx.Response(200, json=_search_response([_deal_row("deal-1")]))
    )
    respx.post("https://app.didar.me/api/deal/getdealdetail").mock(
        return_value=httpx.Response(500, json={"error": "boom"})
    )

    result = poller.poll_new_deals(repo)

    assert result == []
    assert repo.is_deal_notified("deal-1") is False


@respx.mock
def test_a_deal_created_by_sync_engine_is_never_double_notified(poller, repo):
    """Mirrors what sync_engine.py now does: mark_deal_notified() is
    called right after mark_synced(), BEFORE the poller ever runs -
    this poller must then skip that Id entirely."""
    repo.set_deal_poll_watermark(datetime.now(timezone.utc) - timedelta(minutes=5))
    repo.mark_synced("digikala", "12345", "deal-from-sync-engine")
    repo.mark_deal_notified("deal-from-sync-engine")

    respx.post("https://app.didar.me/api/deal/search_v2").mock(
        return_value=httpx.Response(
            200, json=_search_response([_deal_row("deal-from-sync-engine")])
        )
    )

    result = poller.poll_new_deals(repo)

    assert result == []


# --- pagination -----------------------------------------------------------


@respx.mock
def test_search_deals_paginates_full_pages(poller, repo):
    repo.set_deal_poll_watermark(datetime.now(timezone.utc) - timedelta(minutes=5))

    page_1 = [_deal_row(f"deal-{i}") for i in range(50)]
    page_2 = [_deal_row("deal-50")]

    route = respx.post("https://app.didar.me/api/deal/search_v2")
    route.side_effect = [
        httpx.Response(200, json=_search_response(page_1)),
        httpx.Response(200, json=_search_response(page_2)),
    ]

    since = datetime.now(timezone.utc) - timedelta(minutes=5)
    until = datetime.now(timezone.utc)
    rows = poller.search_deals(since, until, limit=50)

    assert len(rows) == 51
    assert route.call_count == 2


# --- pipeline stage title lookup ------------------------------------------


@respx.mock
def test_pipeline_stage_title_resolves_and_caches(poller):
    route = respx.post("https://app.didar.me/api/pipeline/list/0").mock(
        return_value=httpx.Response(200, json=_PIPELINES_RESPONSE)
    )

    assert poller.pipeline_stage_title("stage-2") == "پیگیری"
    assert poller.pipeline_stage_title("stage-1") == "مذاکرات اولیه"
    # Cached after the first call - second lookup shouldn't re-fetch.
    assert route.call_count == 1


def test_pipeline_stage_title_returns_none_for_zero_guid(poller):
    assert poller.pipeline_stage_title("00000000-0000-0000-0000-000000000000") is None
    assert poller.pipeline_stage_title(None) is None


@respx.mock
def test_pipeline_stage_title_returns_none_on_failure(poller):
    respx.post("https://app.didar.me/api/pipeline/list/0").mock(
        return_value=httpx.Response(500, json={"error": "boom"})
    )
    assert poller.pipeline_stage_title("stage-1") is None
