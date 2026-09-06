"""
Regression test for src/didar/deal_client.py's
get_status_breakdown()/get_status_breakdown_for_label() (client
request, 2026-09 follow-up 4: the custom-range /report picker was
over-counting فرازهنر/دیجی‌کالا/تلفنی against the client's own Didar
export for the same day, while اسنپ/تپسی matched exactly).

Root cause: the original implementation left Criteria.Status unset on
a single /deal/search_v2 call and trusted the response's
AllDealsCount/AllDealsTotalPrice to already be scoped to
SearchFromTime/SearchToTime (per Didar's own documented response
example). In production that held for some labels but not others -
some labels' aggregate counts included deals outside the requested
window. The fix issues three separate Status-filtered calls
(Pending/Won/Lost) and sums their own TotalCount/TotalPrice - the same
call shape get_won_stats() already uses successfully. These tests
lock in that shape and the summed result, so a future change can't
silently reintroduce the single unset-Status call.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import respx

from src.config import DidarConfig
from src.didar.deal_client import DealStatusBreakdown, DidarDealClient

_CFG = DidarConfig(base_url="https://app.didar.me/api", api_key="test-key")

_SINCE = datetime(2026, 9, 4, 20, 30, tzinfo=timezone.utc)
_UNTIL = datetime(2026, 9, 5, 20, 30, tzinfo=timezone.utc)


def _status_response(count: int, total: str) -> dict:
    return {"Response": {"TotalCount": count, "TotalPrice": total}}


@respx.mock
def test_status_breakdown_issues_one_call_per_status_and_sums_them():
    """Three separate Status-filtered requests (Pending/Won/Lost), each
    scoped to the same LabelIds+date window - never the old single
    unset-Status call - and the combined counts/totals equal the sum
    of the three per-status responses."""
    route = respx.post("https://app.didar.me/api/deal/search_v2")
    route.side_effect = [
        httpx.Response(200, json=_status_response(2, "20000000")),   # Pending
        httpx.Response(200, json=_status_response(3, "216570000")),  # Won
        httpx.Response(200, json=_status_response(0, "0")),          # Lost
    ]

    client = DidarDealClient(config=_CFG)
    result = client.get_status_breakdown_for_label("label-farazhonar", _SINCE, _UNTIL)

    assert route.call_count == 3
    bodies = [json.loads(call.request.content) for call in route.calls]
    assert [b["Criteria"]["Status"] for b in bodies] == ["Pending", "Won", "Lost"]
    assert all(b["Criteria"]["LabelIds"] == ["label-farazhonar"] for b in bodies)

    assert result == DealStatusBreakdown(
        all_count=5, all_total=Decimal("236570000"),
        pending_count=2, pending_total=Decimal("20000000"),
        won_count=3, won_total=Decimal("216570000"),
        lost_count=0, lost_total=Decimal("0"),
    )


@respx.mock
def test_one_failed_status_call_zeroes_only_that_status():
    """A request/parsing failure on one Status call must not blank out
    the other two - matches get_won_stats()'s own fire-and-forget
    philosophy, just per-status instead of per-source."""
    route = respx.post("https://app.didar.me/api/deal/search_v2")
    route.side_effect = [
        httpx.Response(200, json=_status_response(1, "1000000")),  # Pending
        httpx.Response(400, json={"error": "boom"}),                # Won - fails (4xx, not retried)
        httpx.Response(200, json=_status_response(1, "500000")),   # Lost
    ]

    client = DidarDealClient(config=_CFG)
    result = client.get_status_breakdown_for_label("label-x", _SINCE, _UNTIL)

    assert result.pending_count == 1
    assert result.pending_total == Decimal("1000000")
    assert result.won_count == 0
    assert result.won_total == Decimal("0")
    assert result.lost_count == 1
    assert result.lost_total == Decimal("500000")
    assert result.all_count == 2
    assert result.all_total == Decimal("1500000")
