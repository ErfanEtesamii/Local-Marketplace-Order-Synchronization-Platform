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


# ---------------------------------------------------------------------
# Regression: Shahrivar 1405 report (1405/06/01..1405/06/31) showed 136
# Deals / 6,686,011,400 instead of the Didar export's 162 / 8,854,529,400.
# Root cause: Didar's SearchFromTime/SearchToTime window also keys on
# ChangeToWonTime, so Deals CREATED in the window but WON after its end
# (1405/07/01) were omitted from the response. Fix: SearchToTime is
# widened to max(until, now); RegisterTime is the only inclusion rule.
# ---------------------------------------------------------------------
from src.didar.deal_client import _iso, _search_to_time  # noqa: E402

_SHAHRIVAR_SINCE = datetime(2026, 8, 22, 20, 30, tzinfo=timezone.utc)   # 1405/06/01 00:00 Iran
_SHAHRIVAR_UNTIL = datetime(2026, 9, 22, 20, 30, tzinfo=timezone.utc)   # 1405/07/01 00:00 Iran
_NOW = datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)


def _fake_didar_search(request: httpx.Request) -> httpx.Response:
    """Emulates the OBSERVED Didar behaviour: a Won deal whose
    ChangeToWonTime is after SearchToTime is not returned."""
    criteria = json.loads(request.content)["Criteria"]
    to_time = criteria["SearchToTime"]
    rows = [
        _row(1, "2026-09-10T06:00:00.000Z", "1000", ["L1"], ChangeToWonTime="2026-09-11T06:00:00.000Z"),
        # created in Shahrivar, won on 1405/07/01 -> must be in the report
        _row(2, "2026-09-15T06:00:00.000Z", "2000", ["L2"], ChangeToWonTime="2026-09-23T06:00:00.000Z"),
        # created AFTER the window, won inside it is impossible; instead:
        # created before the window, won inside it -> must NOT be counted
        _row(3, "2026-08-10T06:00:00.000Z", "9999", ["L2"], ChangeToWonTime="2026-09-12T06:00:00.000Z"),
        # created after the window (Mehr), won later -> must NOT be counted
        _row(4, "2026-09-23T06:00:00.000Z", "8888", ["L1"], ChangeToWonTime="2026-09-24T06:00:00.000Z"),
    ]
    served = [r for r in rows if r["ChangeToWonTime"] <= to_time]
    return httpx.Response(200, json={"Response": {"List": served}})


@respx.mock
def test_deal_created_in_period_but_won_after_period_end_is_counted():
    respx.get(_LABELS_URL).mock(return_value=httpx.Response(200, json=_LABELS))
    search = respx.post("https://app.didar.me/api/deal/search_v2")
    search.side_effect = _fake_didar_search

    report = DidarDealClient(config=_CFG).get_channel_report(
        _SHAHRIVAR_SINCE, _SHAHRIVAR_UNTIL, now=_NOW
    )

    criteria = json.loads(search.calls[0].request.content)["Criteria"]
    assert criteria["SearchToTime"] == _iso(_NOW)            # widened past `until`
    assert (report.total.count, report.total.total) == (2, Decimal("3000"))
    assert dict(report.channels)["دیجی‌کالا"].total == Decimal("2000")   # the 1405/07/01 winner


@respx.mock
def test_deal_created_outside_period_is_excluded_even_if_won_inside_it():
    respx.get(_LABELS_URL).mock(return_value=httpx.Response(200, json=_LABELS))
    search = respx.post("https://app.didar.me/api/deal/search_v2")
    search.side_effect = _fake_didar_search

    report = DidarDealClient(config=_CFG).get_channel_report(
        _SHAHRIVAR_SINCE, _SHAHRIVAR_UNTIL, now=_NOW
    )
    # d3 (created 1405/05, won inside Shahrivar) and d4 (created Mehr) are
    # both returned by the widened search but must be filtered client-side.
    assert report.total.total == Decimal("3000")


def test_search_to_time_never_shrinks_the_window():
    until = datetime(2026, 9, 22, 20, 30, tzinfo=timezone.utc)
    assert _search_to_time(until, datetime(2026, 9, 24, tzinfo=timezone.utc)) == datetime(2026, 9, 24, tzinfo=timezone.utc)
    # a report window ending in the future (e.g. "today so far") keeps its own end
    assert _search_to_time(until, datetime(2026, 9, 1, tzinfo=timezone.utc)) == until


@respx.mock
def test_label_scoped_created_date_stats_also_widen_search_to_time():
    search = respx.post("https://app.didar.me/api/deal/search_v2")
    search.side_effect = [httpx.Response(200, json={"Response": {"List": [
        _row(1, "2020-01-05T06:00:00.000Z", "500", ["L1"]),
    ]}})]
    since = datetime(2020, 1, 1, tzinfo=timezone.utc)
    until = datetime(2020, 2, 1, tzinfo=timezone.utc)

    stats = DidarDealClient(config=_CFG).get_created_date_stats_for_label("L1", since, until)

    to_time = json.loads(search.calls[0].request.content)["Criteria"]["SearchToTime"]
    assert to_time > _iso(until)
    assert (stats.all_count, stats.all_total) == (1, Decimal("500"))


# Regression: Didar's own export for 1405/06 = Pending + Won only
# (162 / 8,854,529,400); the 3 Lost deals (210,905,000) must not be counted.
@respx.mock
def test_lost_deals_are_excluded_but_pending_and_won_are_counted():
    respx.get(_LABELS_URL).mock(return_value=httpx.Response(200, json=_LABELS))
    search = respx.post("https://app.didar.me/api/deal/search_v2")
    search.side_effect = [httpx.Response(200, json={"Response": {"List": [
        _row(1, "2026-09-05T02:00:00.000Z", "1000", ["L1"], Status="Pending"),
        _row(2, "2026-09-05T03:00:00.000Z", "2000", ["L1"], Status="Won"),
        _row(3, "2026-09-05T04:00:00.000Z", "500", ["L1"], Status="Lost"),
        _row(4, "2026-09-05T05:00:00.000Z", "700", ["L2"]),   # no Status -> counted
    ]}})]

    report = DidarDealClient(config=_CFG).get_channel_report(_SINCE, _UNTIL)

    assert (report.total.count, report.total.total) == (3, Decimal("3700"))