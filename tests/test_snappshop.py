from datetime import datetime, timezone

import respx
import httpx

from src.config import SnappShopConfig
from src.marketplaces.snappshop import SnappShopAdapter

_CFG = SnappShopConfig(
    base_url="https://apix.snappshop.ir",
    auth_token="test-token",
    agent_user="agent-123",
    vendor_id="v1",
)


@respx.mock
def test_fetch_new_orders_sends_confirmed_auth_headers():
    route = respx.get("https://apix.snappshop.ir/vendors/v1/orders").mock(
        return_value=httpx.Response(
            200,
            json={"data": [], "meta": {"pagination": {"has_more": False, "next_cursor": None}}},
        )
    )

    adapter = SnappShopAdapter(config=_CFG)
    adapter.fetch_new_orders(since=datetime(2026, 8, 1, tzinfo=timezone.utc))

    request = route.calls[0].request
    assert request.headers["Authorization"] == "Bearer test-token"
    assert request.headers["Agent-User"] == "agent-123"


@respx.mock
def test_fetch_new_orders_paginates_via_cursor_and_has_more():
    """
    Pagination mechanics ARE confirmed from the docs (has_more / next_cursor
    under meta.pagination) even though individual order fields aren't -
    this test locks in that confirmed part.
    """
    route = respx.get("https://apix.snappshop.ir/vendors/v1/orders")
    route.mock(
        side_effect=[
            httpx.Response(200, json={
                "data": [{"order_number": "A1", "status": "confirmed",
                          "created_at": "2026-08-10T09:00:00Z", "total_price": 100000}],
                "meta": {"pagination": {"has_more": True, "next_cursor": "cur-2"}},
            }),
            httpx.Response(200, json={
                "data": [{"order_number": "A2", "status": "confirmed",
                          "created_at": "2026-08-10T09:05:00Z", "total_price": 50000}],
                "meta": {"pagination": {"has_more": False, "next_cursor": None}},
            }),
        ]
    )

    adapter = SnappShopAdapter(config=_CFG)
    orders = adapter.fetch_new_orders(since=datetime(2026, 8, 1, tzinfo=timezone.utc))

    assert route.call_count == 2
    assert len(orders) == 2
    assert {o.source_order_id for o in orders} == {"A1", "A2"}
    assert route.calls[1].request.url.params["cursor"] == "cur-2"


@respx.mock
def test_fetch_order_detail_maps_items_defensively():
    respx.get("https://apix.snappshop.ir/vendors/v1/orders/A1").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "order_number": "A1",
                    "status": "confirmed",
                    "created_at": "2026-08-10T09:00:00Z",
                    "total_price": 150000,
                    "items": [
                        {"sku": "SKU-1", "title": "Product A", "quantity": 3,
                         "unit_price": 50000, "final_price": 150000}
                    ],
                }
            },
        )
    )

    adapter = SnappShopAdapter(config=_CFG)
    order = adapter.fetch_order_detail("A1")

    assert order.source == "snappshop"
    assert len(order.items) == 1
    assert order.items[0].quantity == 3
    assert order.total_price == 150000


@respx.mock
def test_discover_vendor_id_helper():
    respx.get("https://apix.snappshop.ir/vendors").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "v-42"}]})
    )
    adapter = SnappShopAdapter(config=_CFG)
    assert adapter.discover_vendor_id() == "v-42"
