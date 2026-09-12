"""
Regression test for src/didar/deal_client.py's
get_created_date_stats_for_label() (client request, 2026-09 follow-up
5: "بازه‌ای که میگیرم بر اساس تاریخ ایجاد سفارشات باشه، کاری ندارم
وضعیتش چیه" - the custom-range /report picker was still massively
over-counting against a live Didar export filtered by "تاریخ ایجاد
معامله", even after the per-Status fix in
test_didar_status_breakdown.py).

Root cause: get_status_breakdown_for_label() trusts Didar's own
server-computed TotalCount/TotalPrice for a Status-filtered
SearchFromTime/SearchToTime window. That window is not reliably keyed
to RegisterTime (creation time) for every Status - a Pending deal that
was simply touched (stage change, note, etc.) during the window, or a
Won/Lost deal that closed during the window, can be matched even
though it was created long before [since, until). The fix never trusts
the server-side aggregate: it paginates the actual List rows (Status
left unset in Criteria - every status in one pass) and independently
verifies each row's own RegisterTime falls inside [since, until)
before counting it, the same client-side-verification pattern
DidarDealPoller.search_deals() already uses for detecting newly
registered deals. These tests lock in that shape.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import respx

from src.config import DidarConfig
from src.didar.deal_client import DealStatusBreakdown, DidarDealClient

_CFG = DidarConfig(
    base_url="https://app.didar.me/api",
    api_key="test-key",
    pipeline_id="pipeline-orders",
)

_SINCE = datetime(2026, 9, 4, 20, 30, tzinfo=timezone.utc)
_UNTIL = datetime(2026, 9, 5, 20, 30, tzinfo=timezone.utc)


def _row(deal_id: str, register_time: str, price: str) -> dict:
    return {"Id": deal_id, "RegisterTime": register_time, "Price": price}


@respx.mock
def test_counts_only_rows_whose_own_registertime_is_in_window():
    """Didar returns a row outside [since, until) alongside two rows
    genuinely inside it (the exact "leak" that made the Status-based
    method over-count) - the leaked row must be dropped client-side and
    never counted, regardless of what Didar's own filtering did."""
    route = respx.post("https://app.didar.me/api/deal/search_v2")
    route.side_effect = [
        httpx.Response(
            200,
            json={
                "Response": {
                    "List": [
                        _row("d1", "2026-09-05T02:00:00.000Z", "1000000"),   # in window
                        _row("d2", "2026-09-05T10:00:00.000Z", "2000000"),  # in window
                        _row("d3", "2026-08-20T09:00:00.000Z", "9999999"),  # LEAK - created weeks earlier
                    ]
                }
            },
        )
    ]

    client = DidarDealClient(config=_CFG)
    result = client.get_created_date_stats_for_label("label-1", _SINCE, _UNTIL)

    assert route.call_count == 1
    body = json.loads(route.calls[0].request.content)
    # No Status in Criteria - a single pass must see every status.
    assert "Status" not in body["Criteria"]
    assert body["Criteria"]["PipelineId"] == "pipeline-orders"
    assert body["Criteria"]["LabelIds"] == ["label-1"]

    assert result == DealStatusBreakdown(all_count=2, all_total=Decimal("3000000"))


@respx.mock
def test_paginates_until_a_short_page():
    """A full first page (== page size) must trigger a second request at
    the next offset; a short second page ends pagination."""
    full_page = [_row(f"d{i}", "2026-09-05T02:00:00.000Z", "1") for i in range(50)]
    route = respx.post("https://app.didar.me/api/deal/search_v2")
    route.side_effect = [
        httpx.Response(200, json={"Response": {"List": full_page}}),
        httpx.Response(
            200,
            json={"Response": {"List": [_row("d50", "2026-09-05T02:00:00.000Z", "1")]}},
        ),
    ]

    client = DidarDealClient(config=_CFG)
    result = client.get_created_date_stats_for_label("label-1", _SINCE, _UNTIL)

    assert route.call_count == 2
    offsets = [json.loads(c.request.content)["From"] for c in route.calls]
    assert offsets == [0, 50]
    assert result.all_count == 51


@respx.mock
def test_missing_registertime_is_dropped_not_counted():
    route = respx.post("https://app.didar.me/api/deal/search_v2")
    route.side_effect = [
        httpx.Response(
            200,
            json={
                "Response": {
                    "List": [
                        {"Id": "d1", "RegisterTime": None, "Price": "500000"},
                        _row("d2", "2026-09-05T02:00:00.000Z", "1000000"),
                    ]
                }
            },
        )
    ]

    client = DidarDealClient(config=_CFG)
    result = client.get_created_date_stats_for_label("label-1", _SINCE, _UNTIL)

    assert result == DealStatusBreakdown(all_count=1, all_total=Decimal("1000000"))


def test_no_pipeline_id_returns_zero_without_request():
    cfg = DidarConfig(base_url="https://app.didar.me/api", api_key="test-key", pipeline_id="")
    client = DidarDealClient(config=cfg)
    with respx.mock:
        result = client.get_created_date_stats_for_label("label-1", _SINCE, _UNTIL)
    assert result == DealStatusBreakdown()


@respx.mock
def test_request_failure_returns_whatever_was_accumulated_so_far():
    route = respx.post("https://app.didar.me/api/deal/search_v2")
    full_page = [_row(f"d{i}", "2026-09-05T02:00:00.000Z", "1") for i in range(50)]
    route.side_effect = [
        httpx.Response(200, json={"Response": {"List": full_page}}),
        httpx.Response(400, json={"error": "boom"}),
    ]

    client = DidarDealClient(config=_CFG)
    result = client.get_created_date_stats_for_label("label-1", _SINCE, _UNTIL)

    assert result.all_count == 50
