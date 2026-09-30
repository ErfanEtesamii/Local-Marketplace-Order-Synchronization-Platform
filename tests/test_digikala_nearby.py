"""Tests for src/marketplaces/digikala_nearby.py (3-hour / near-by-stores orders).

Uses a small fake host adapter instead of respx so the mixin's own logic
(cold start, dedup, forced EXPRESS, never-raise) is tested in isolation.
"""
from datetime import datetime
from decimal import Decimal

import pytest

from src.db.repository import Repository
from src.marketplaces.base import NormalizedOrder
from src.marketplaces.digikala_nearby import NearbyStoresMixin


def _row(shipment_id, status="pending"):
    return {"shipmentId": shipment_id, "orderId": shipment_id, "status": {"text": status}}


class _FakeAdapter(NearbyStoresMixin):
    name = "digikala"

    def __init__(self, repo):
        self._repo = repo
        self.rows = [_row(1), _row(2, "processing")]
        self.fail = False

    def _get(self, path, params):
        if self.fail:
            raise RuntimeError("boom")
        status = params.get("search[status]")
        items = [r for r in self.rows if status is None or r["status"]["text"] == status]
        term = params.get("search[search_term]")
        if term:
            items = [r for r in items if str(r["shipmentId"]) == term]
        return {"data": {"items": items, "pager": {"total_pages": 1}}}

    def _normalize_sbs_row(self, row, promotion_map=None):
        return NormalizedOrder(
            source="digikala",
            source_order_id=str(row["shipmentId"]),
            order_number=str(row["orderId"]),
            created_at=datetime.now(),
            total_price=Decimal(0),
            status=row["status"]["text"],
            shipping_method="NORMAL",
        )

    def _build_promotion_map(self, row):
        return {}


@pytest.fixture
def adapter(tmp_path):
    return _FakeAdapter(Repository(str(tmp_path / "t.db")))


def test_cold_start_ignores_existing_open_rows(adapter):
    assert adapter.fetch_new_nearby_orders() == []
    assert adapter._repo.get_ignored_ids("digikala") == {"1", "2"}


def test_new_row_after_cold_start_is_returned_once_as_express(adapter):
    adapter.fetch_new_nearby_orders()
    adapter.rows.append(_row(3))
    orders = adapter.fetch_new_nearby_orders()
    assert [o.source_order_id for o in orders] == ["3"]
    assert orders[0].shipping_method == "EXPRESS"
    assert adapter.fetch_new_nearby_orders() == []


def test_fetch_never_raises(adapter):
    adapter.fail = True
    assert adapter.fetch_new_nearby_orders() == []


def test_detail_lookup_finds_row_and_swallows_errors(adapter):
    assert adapter._fetch_nearby_row(2)["shipmentId"] == 2
    assert adapter._lookup_nearby_for_detail(99) == {}
    adapter.fail = True
    assert adapter._lookup_nearby_for_detail(2) == {}


def test_auto_confirm_is_off_by_default(adapter):
    row = _row(5)
    assert adapter._confirm_nearby_if_pending(row) is row
