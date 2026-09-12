"""
Tests for src/didar/deal_poller.py - the order-pipeline deal poller
behind the 2026-09 requirement that every Deal registered in Didar's
کاریز سفارشات (order pipeline; manual or automatic) triggers a
Telegram notification, and ONLY deals newly registered in that
pipeline - never other pipelines, never old history.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
import respx

from src.config import DidarConfig
from src.db.repository import Repository
from src.didar.deal_poller import DidarDealPoller, NewDealInfo

_CFG = DidarConfig(
    base_url="https://app.didar.me/api",
    api_key="test-key",
    pipeline_id="pipe-orders",
)


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


def _iso_z(dt: datetime) -> str:
    """Formats like Didar's own RegisterTime examples, e.g.
    "2026-07-16T09:17:49Z" (second precision, no microseconds)."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _recent(seconds_ago: int = 30) -> datetime:
    """A RegisterTime that falls inside any [now - a few minutes, now]
    poll window used by these tests - see search_deals()'s RegisterTime
    guard, which drops rows outside the queried window."""
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).replace(microsecond=0)


def _deal_row(deal_id: str, register_time: datetime | None = None) -> dict:
    return {
        "Id": deal_id,
        "Title": f"معامله {deal_id}",
        "RegisterTime": _iso_z(register_time or _recent()),
        "Price": "100000",
        "PipelineStageId": "stage-1",
        "OwnerId": "owner-1",
    }


def _deal_detail(deal_id: str, register_time: datetime | None = None, **overrides) -> dict:
    detail = {
        "Id": deal_id,
        "Code": 4242,
        "Title": f"معامله {deal_id}",
        "RegisterTime": _iso_z(register_time or _recent()),
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

_DEAL_LABELS_RESPONSE = {
    "Response": [
        {"Id": "label-farazhonar", "Title": "فرازهنر", "Type": "Deal"},
        {"Id": "label-digikala", "Title": "دیجی‌کالا", "Type": "Deal"},
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
    register_time = _recent()

    respx.post("https://app.didar.me/api/deal/search_v2").mock(
        return_value=httpx.Response(
            200, json=_search_response([_deal_row("deal-1", register_time)])
        )
    )
    respx.post("https://app.didar.me/api/deal/getdealdetail").mock(
        return_value=httpx.Response(200, json=_deal_detail("deal-1", register_time))
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
        register_time=register_time,
    )
    # Discovering it must have marked it notified so it isn't sent twice.
    assert repo.is_deal_notified("deal-1") is True


@respx.mock
def test_poll_new_deals_resolves_platform_label_from_label_ids(poller, repo):
    """Confirmed 2026-09 (Didar's own support agent, re: Get Deal By
    Id): the field on a Deal's detail response is `LabelIds` (a list
    of Label Id strings), NOT a `Labels` list of {Id, Title} objects.
    Resolving an Id to its Title requires a SEPARATE GET
    /Label/GetDealLabels call - getdealdetail itself never returns the
    Title text for a Label."""
    repo.set_deal_poll_watermark(datetime.now(timezone.utc) - timedelta(minutes=5))
    register_time = _recent()

    respx.post("https://app.didar.me/api/deal/search_v2").mock(
        return_value=httpx.Response(
            200, json=_search_response([_deal_row("deal-1", register_time)])
        )
    )
    respx.post("https://app.didar.me/api/deal/getdealdetail").mock(
        return_value=httpx.Response(
            200,
            json=_deal_detail("deal-1", register_time, LabelIds=["label-farazhonar"]),
        )
    )
    respx.get("https://app.didar.me/api/Label/GetDealLabels").mock(
        return_value=httpx.Response(200, json=_DEAL_LABELS_RESPONSE)
    )

    [deal] = poller.poll_new_deals(repo)

    assert deal.platform_label == "فرازهنر"


@respx.mock
def test_poll_new_deals_platform_label_none_when_label_ids_empty(poller, repo):
    """A manually-typed deal with no Label picked - the documented
    example shape ("LabelIds": []) - must fall back to None, never
    raise or pick an arbitrary label."""
    repo.set_deal_poll_watermark(datetime.now(timezone.utc) - timedelta(minutes=5))
    register_time = _recent()

    respx.post("https://app.didar.me/api/deal/search_v2").mock(
        return_value=httpx.Response(
            200, json=_search_response([_deal_row("deal-1", register_time)])
        )
    )
    respx.post("https://app.didar.me/api/deal/getdealdetail").mock(
        return_value=httpx.Response(
            200, json=_deal_detail("deal-1", register_time, LabelIds=[])
        )
    )

    [deal] = poller.poll_new_deals(repo)

    assert deal.platform_label is None


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


# --- pipeline scoping -------------------------------------------------


@respx.mock
def test_search_deals_sends_configured_pipeline_id(poller):
    """Only کاریز سفارشات should ever be queried - see
    DidarDealPoller.search_deals()'s Criteria.PipelineId."""
    route = respx.post("https://app.didar.me/api/deal/search_v2").mock(
        return_value=httpx.Response(200, json=_search_response([]))
    )

    since = datetime.now(timezone.utc) - timedelta(minutes=5)
    until = datetime.now(timezone.utc)
    poller.search_deals(since, until)

    payload = json.loads(route.calls[0].request.content)
    assert payload["Criteria"]["PipelineId"] == "pipe-orders"


@respx.mock
def test_search_deals_warns_but_still_searches_without_pipeline_id():
    """If DIDAR_PIPELINE_ID isn't configured, the poller still works
    (searches every pipeline) rather than crashing - but this is a
    degraded/legacy mode, not the intended behaviour."""
    cfg = DidarConfig(base_url="https://app.didar.me/api", api_key="test-key", pipeline_id="")
    poller = DidarDealPoller(config=cfg)

    route = respx.post("https://app.didar.me/api/deal/search_v2").mock(
        return_value=httpx.Response(200, json=_search_response([]))
    )

    since = datetime.now(timezone.utc) - timedelta(minutes=5)
    until = datetime.now(timezone.utc)
    poller.search_deals(since, until)

    payload = json.loads(route.calls[0].request.content)
    assert "PipelineId" not in payload["Criteria"]


# --- RegisterTime double-check (old-deal-history guard) -------------------


@respx.mock
def test_search_deals_drops_rows_whose_register_time_is_outside_window(poller):
    """The core fix for the "4-month-old order" bug: even if Didar's
    own SearchFromTime/SearchToTime filtering lets an old row through,
    this poller must never forward it - each row's RegisterTime is
    re-checked against the queried window."""
    since = datetime.now(timezone.utc) - timedelta(minutes=5)
    until = datetime.now(timezone.utc)

    old_row = _deal_row("deal-old", register_time=datetime.now(timezone.utc) - timedelta(days=120))
    fresh_row = _deal_row("deal-fresh", register_time=_recent())

    respx.post("https://app.didar.me/api/deal/search_v2").mock(
        return_value=httpx.Response(200, json=_search_response([old_row, fresh_row]))
    )

    rows = poller.search_deals(since, until)

    assert [r["Id"] for r in rows] == ["deal-fresh"]


@respx.mock
def test_search_deals_drops_rows_with_missing_register_time(poller):
    since = datetime.now(timezone.utc) - timedelta(minutes=5)
    until = datetime.now(timezone.utc)

    row = _deal_row("deal-no-time")
    row["RegisterTime"] = None

    respx.post("https://app.didar.me/api/deal/search_v2").mock(
        return_value=httpx.Response(200, json=_search_response([row]))
    )

    rows = poller.search_deals(since, until)

    assert rows == []


@respx.mock
def test_poll_new_deals_never_notifies_an_old_deal_even_if_returned_by_search(poller, repo):
    """End-to-end version of the RegisterTime guard: an old Deal
    returned by /deal/search_v2 (e.g. Didar ignoring SearchFromTime)
    must never reach the caller, must never hit getdealdetail, and
    must never be marked notified."""
    repo.set_deal_poll_watermark(datetime.now(timezone.utc) - timedelta(minutes=5))

    old_deal_id = "deal-from-four-months-ago"
    old_row = _deal_row(old_deal_id, register_time=datetime.now(timezone.utc) - timedelta(days=120))

    respx.post("https://app.didar.me/api/deal/search_v2").mock(
        return_value=httpx.Response(200, json=_search_response([old_row]))
    )
    detail_route = respx.post("https://app.didar.me/api/deal/getdealdetail").mock(
        return_value=httpx.Response(200, json=_deal_detail(old_deal_id))
    )

    result = poller.poll_new_deals(repo)

    assert result == []
    assert detail_route.call_count == 0
    assert repo.is_deal_notified(old_deal_id) is False


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
