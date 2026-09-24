"""
DidarDealClient.get_channel_report() - the fetch half of the shared
Deal-level channel report (see src/didar/deal_channel_report.py for the
aggregation rules, tested in test_deal_channel_report.py).

Locks in: ONE label-less, pipeline-scoped pass (not one query per
label); pagination; Deal.Price only; and that any incomplete fetch RAISES
instead of returning partial/zero numbers.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest
import respx

from src.config import DidarConfig
from src.didar.deal_channel_report import DealReportError
from src.didar.deal_client import DidarDealClient

_CFG = DidarConfig(base_url="https://app.didar.me/api", api_key="k", pipeline_id="pipe-1")
_SINCE = datetime(2026, 9, 4, 20, 30, tzinfo=timezone.utc)
_UNTIL = datetime(2026, 9, 5, 20, 30, tzinfo=timezone.utc)
_LABELS_URL = f"https://app.didar.me/api/{_CFG.get_deal_labels_path.lstrip('/')}"
_LABELS = {"Response": [
    {"Id": "L1", "Title": "اسنپ", "Type": "Deal"},
    {"Id": "L2", "Title": "دیجی کالا", "Type": "Deal"},
    {"Id": "LC", "Title": "اسنپ", "Type": "Contact"},   # non-Deal label ignored
]}


def _row(i, when, price, labels, **extra):
    return {"Id": f"d{i}", "RegisterTime": when, "Price": price, "LabelIds": labels, **extra}


@respx.mock
def test_single_labelless_pipeline_scoped_pass_counts_each_deal_once():
    respx.get(_LABELS_URL).mock(return_value=httpx.Response(200, json=_LABELS))
    search = respx.post("https://app.didar.me/api/deal/search_v2")
    search.side_effect = [httpx.Response(200, json={"Response": {"List": [
        _row(1, "2026-09-05T02:00:00.000Z", "1000", ["L1"], DealItems=[{"UnitPrice": 5}] * 4),
        _row(2, "2026-09-05T03:00:00.000Z", "2000", ["L1", "L2"]),        # two channels -> other
        _row(3, "2026-08-01T03:00:00.000Z", "9999", ["L2"]),              # leaked, out of window
    ]}})]

    report = DidarDealClient(config=_CFG).get_channel_report(_SINCE, _UNTIL)

    assert search.call_count == 1
    criteria = json.loads(search.calls[0].request.content)["Criteria"]
    assert "LabelIds" not in criteria and "Status" not in criteria
    assert criteria["PipelineId"] == "pipe-1"
    assert (report.total.count, report.total.total) == (2, Decimal("3000"))
    assert dict(report.channels)["اسنپ"].total == Decimal("1000")
    assert (report.other.count, report.other.total) == (1, Decimal("2000"))


@respx.mock
def test_paginates_until_a_short_page():
    respx.get(_LABELS_URL).mock(return_value=httpx.Response(200, json=_LABELS))
    full = [_row(i, "2026-09-05T02:00:00.000Z", "10", ["L1"]) for i in range(50)]
    last = [_row(100 + i, "2026-09-05T02:00:00.000Z", "10", ["L2"]) for i in range(7)]
    search = respx.post("https://app.didar.me/api/deal/search_v2")
    search.side_effect = [httpx.Response(200, json={"Response": {"List": full}}),
                          httpx.Response(200, json={"Response": {"List": last}})]

    report = DidarDealClient(config=_CFG).get_channel_report(_SINCE, _UNTIL)

    assert search.call_count == 2
    assert [json.loads(c.request.content)["From"] for c in search.calls] == [0, 50]
    assert (report.total.count, report.total.total) == (57, Decimal("570"))


def test_missing_pipeline_raises_instead_of_counting_every_pipeline():
    cfg = DidarConfig(base_url="https://app.didar.me/api", api_key="k", pipeline_id="")
    with pytest.raises(DealReportError):
        DidarDealClient(config=cfg).get_channel_report(_SINCE, _UNTIL)


@respx.mock
def test_label_fetch_failure_raises():
    respx.get(_LABELS_URL).mock(return_value=httpx.Response(400, json={}))
    with pytest.raises(DealReportError):
        DidarDealClient(config=_CFG).get_channel_report(_SINCE, _UNTIL)


@respx.mock
def test_search_failure_midway_raises_rather_than_reporting_a_partial_total():
    respx.get(_LABELS_URL).mock(return_value=httpx.Response(200, json=_LABELS))
    full = [_row(i, "2026-09-05T02:00:00.000Z", "10", ["L1"]) for i in range(50)]
    search = respx.post("https://app.didar.me/api/deal/search_v2")
    search.side_effect = [httpx.Response(200, json={"Response": {"List": full}}),
                          httpx.Response(400, json={})]
    with pytest.raises(DealReportError):
        DidarDealClient(config=_CFG).get_channel_report(_SINCE, _UNTIL)
