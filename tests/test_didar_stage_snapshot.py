"""
Tests for src/didar/deal_client.py's list_current_deals_in_stage() and
record_current_stage_snapshot() - the live, unfiltered snapshot of
whoever is CURRENTLY sitting in the "new customer" ("مشتری جدید")
pipeline stage, and the poll-cycle step that persists it locally.

Part A (list_current_deals_in_stage - pure Didar-API read, no
Repository writes, no LabelIds->title resolution) and Part B
(record_current_stage_snapshot - label resolution + Repository
persistence, called every poll cycle from main.py) of the two-part
"Stage 2" split (see the "تفکیک سفارش‌های مرحله «مشتری جدید»" prompt)
both live in this one file.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from src.config import DidarConfig
from src.db.repository import Repository
from src.didar.deal_client import DidarDealClient

_CFG = DidarConfig(
    base_url="https://app.didar.me/api",
    api_key="test-key",
    pipeline_id="pipe-orders",
    pipeline_stage_id="stage-new-customer",
)


def _search_response(rows: list[dict]) -> dict:
    return {"Response": {"TotalCount": len(rows), "List": rows}}


def _deal_row(deal_id: str, **overrides) -> dict:
    row = {
        "Id": deal_id,
        "Title": f"معامله {deal_id}",
        "Price": "100000",
        "PipelineStageId": "stage-new-customer",
    }
    row.update(overrides)
    return row


@respx.mock
def test_sends_configured_pipeline_and_stage_id_with_no_time_filter():
    """Criteria must carry BOTH PipelineId and PipelineStageId - the
    bug this whole feature exists to fix - and never a SearchFromTime/
    SearchToTime window, since this is a live snapshot re-taken every
    poll cycle, not a "what's new since X" search."""
    route = respx.post("https://app.didar.me/api/deal/search_v2").mock(
        return_value=httpx.Response(200, json=_search_response([]))
    )

    client = DidarDealClient(config=_CFG)
    client.list_current_deals_in_stage()

    payload = json.loads(route.calls[0].request.content)
    assert payload["Criteria"]["PipelineId"] == "pipe-orders"
    assert payload["Criteria"]["PipelineStageId"] == "stage-new-customer"
    assert "SearchFromTime" not in payload["Criteria"]
    assert "SearchToTime" not in payload["Criteria"]


@respx.mock
def test_returns_rows_from_a_single_page():
    respx.post("https://app.didar.me/api/deal/search_v2").mock(
        return_value=httpx.Response(
            200, json=_search_response([_deal_row("deal-1"), _deal_row("deal-2")])
        )
    )

    client = DidarDealClient(config=_CFG)
    rows = client.list_current_deals_in_stage()

    assert [r["Id"] for r in rows] == ["deal-1", "deal-2"]


@respx.mock
def test_paginates_across_full_pages():
    page_1 = [_deal_row(f"deal-{i}") for i in range(50)]
    page_2 = [_deal_row("deal-50")]

    route = respx.post("https://app.didar.me/api/deal/search_v2")
    route.side_effect = [
        httpx.Response(200, json=_search_response(page_1)),
        httpx.Response(200, json=_search_response(page_2)),
    ]

    client = DidarDealClient(config=_CFG)
    rows = client.list_current_deals_in_stage(limit=50)

    assert len(rows) == 51
    assert route.call_count == 2
    # Second page must continue from From=50, not restart at 0.
    second_call_body = json.loads(route.calls[1].request.content)
    assert second_call_body["From"] == 50


@respx.mock
def test_stops_paginating_once_a_short_page_is_returned():
    route = respx.post("https://app.didar.me/api/deal/search_v2").mock(
        return_value=httpx.Response(200, json=_search_response([_deal_row("deal-1")]))
    )

    client = DidarDealClient(config=_CFG)
    client.list_current_deals_in_stage(limit=50)

    assert route.call_count == 1


@respx.mock
def test_drops_rows_with_no_id():
    respx.post("https://app.didar.me/api/deal/search_v2").mock(
        return_value=httpx.Response(
            200, json=_search_response([{"Title": "no id here"}, _deal_row("deal-ok")])
        )
    )

    client = DidarDealClient(config=_CFG)
    rows = client.list_current_deals_in_stage()

    assert [r["Id"] for r in rows] == ["deal-ok"]


def test_returns_empty_and_makes_no_request_without_pipeline_stage_id():
    cfg = DidarConfig(
        base_url="https://app.didar.me/api",
        api_key="test-key",
        pipeline_id="pipe-orders",
        pipeline_stage_id="",
    )
    client = DidarDealClient(config=cfg)

    with respx.mock:
        # No route registered at all - any request would raise.
        rows = client.list_current_deals_in_stage()

    assert rows == []


def test_returns_empty_and_makes_no_request_without_pipeline_id():
    cfg = DidarConfig(
        base_url="https://app.didar.me/api",
        api_key="test-key",
        pipeline_id="",
        pipeline_stage_id="stage-new-customer",
    )
    client = DidarDealClient(config=cfg)

    with respx.mock:
        rows = client.list_current_deals_in_stage()

    assert rows == []


# ----------------------------------------------------------------------
# Part B - record_current_stage_snapshot(): label resolution + writing
# every row into Repository.new_customer_stage_deals.
# ----------------------------------------------------------------------

@pytest.fixture
def repo(tmp_path):
    return Repository(db_path=str(tmp_path / "test.db"))


_DEAL_LABELS_RESPONSE = {
    "Response": [
        {"Id": "label-tapsi-guid", "Title": "تپسی", "Code": 1, "Type": "Deal"},
        {"Id": "label-digikala-guid", "Title": "دیجی‌کالا", "Code": 2, "Type": "Deal"},
        # A Contact-type label sharing an Id-shaped string with nothing in
        # particular - must never leak into the id->title map used for
        # resolving a Deal's LabelIds (Type != "Deal").
        {"Id": "label-contact-guid", "Title": "مشتری VIP", "Code": 3, "Type": "Contact"},
    ]
}


def _mock_deal_labels():
    return respx.get("https://app.didar.me/api/Label/GetDealLabels").mock(
        return_value=httpx.Response(200, json=_DEAL_LABELS_RESPONSE)
    )


def _mock_stage_search(rows: list[dict]):
    return respx.post("https://app.didar.me/api/deal/search_v2").mock(
        return_value=httpx.Response(200, json=_search_response(rows))
    )


@respx.mock
def test_records_every_row_with_its_resolved_label(repo):
    _mock_deal_labels()
    _mock_stage_search(
        [
            _deal_row("deal-1", Price="150000", LabelIds=["label-tapsi-guid"]),
            _deal_row("deal-2", Price="200000", LabelIds=["label-digikala-guid"]),
        ]
    )

    client = DidarDealClient(config=_CFG)
    handled = client.record_current_stage_snapshot(repo)

    assert handled == 2
    since = datetime.now(timezone.utc) - timedelta(minutes=1)
    rows = {r.deal_id: r for r in repo.get_new_stage_deals(since)}
    assert rows["deal-1"].label_title == "تپسی"
    assert rows["deal-1"].amount == 150000
    assert rows["deal-2"].label_title == "دیجی‌کالا"


@respx.mock
def test_deal_with_no_resolvable_label_is_still_recorded(repo):
    """A manually-entered deal with no Label, or a Label Id
    list_deal_labels() doesn't recognise, must still be recorded (with
    label_id=label_title=None) - it's not filtered out, just unlabeled.
    """
    _mock_deal_labels()
    _mock_stage_search(
        [
            _deal_row("deal-no-label", Price="90000", LabelIds=[]),
            _deal_row("deal-unknown-label", Price="10000", LabelIds=["label-nonexistent"]),
        ]
    )

    client = DidarDealClient(config=_CFG)
    handled = client.record_current_stage_snapshot(repo)

    assert handled == 2
    since = datetime.now(timezone.utc) - timedelta(minutes=1)
    rows = {r.deal_id: r for r in repo.get_new_stage_deals(since)}
    assert rows["deal-no-label"].label_title is None
    assert rows["deal-unknown-label"].label_title is None


@respx.mock
def test_a_contact_type_label_is_never_used_to_resolve_a_deal_label(repo):
    _mock_deal_labels()
    _mock_stage_search(
        [_deal_row("deal-x", Price="1", LabelIds=["label-contact-guid"])]
    )

    client = DidarDealClient(config=_CFG)
    client.record_current_stage_snapshot(repo)

    since = datetime.now(timezone.utc) - timedelta(minutes=1)
    rows = {r.deal_id: r for r in repo.get_new_stage_deals(since)}
    assert rows["deal-x"].label_title is None


@respx.mock
def test_first_label_id_that_resolves_wins_when_a_deal_has_several(repo):
    _mock_deal_labels()
    _mock_stage_search(
        [
            _deal_row(
                "deal-multi",
                Price="1",
                LabelIds=["label-unknown", "label-digikala-guid", "label-tapsi-guid"],
            )
        ]
    )

    client = DidarDealClient(config=_CFG)
    client.record_current_stage_snapshot(repo)

    since = datetime.now(timezone.utc) - timedelta(minutes=1)
    rows = {r.deal_id: r for r in repo.get_new_stage_deals(since)}
    assert rows["deal-multi"].label_title == "دیجی‌کالا"


@respx.mock
def test_seeing_the_same_deal_id_again_never_overwrites_its_entered_at(repo):
    """The whole point of INSERT OR IGNORE in
    Repository.record_new_stage_deal(): a deal_id still present in the
    stage on a LATER poll cycle must keep its original entered_at, not
    get bumped forward to the later snapshot's timestamp."""
    _mock_deal_labels()
    _mock_stage_search([_deal_row("deal-1", Price="1", LabelIds=["label-tapsi-guid"])])

    client = DidarDealClient(config=_CFG)
    client.record_current_stage_snapshot(repo)

    since = datetime.now(timezone.utc) - timedelta(minutes=1)
    first_entered_at = {r.deal_id: r.entered_at for r in repo.get_new_stage_deals(since)}

    # Second poll cycle, same deal still sitting in the stage.
    client.record_current_stage_snapshot(repo)
    second_entered_at = {r.deal_id: r.entered_at for r in repo.get_new_stage_deals(since)}

    assert first_entered_at["deal-1"] == second_entered_at["deal-1"]


@respx.mock
def test_no_deals_currently_in_stage_records_nothing_and_makes_no_label_request(repo):
    labels_route = _mock_deal_labels()
    _mock_stage_search([])

    client = DidarDealClient(config=_CFG)
    handled = client.record_current_stage_snapshot(repo)

    assert handled == 0
    assert labels_route.call_count == 0
    since = datetime.now(timezone.utc) - timedelta(minutes=1)
    assert repo.get_new_stage_deals(since) == []