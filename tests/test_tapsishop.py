from datetime import datetime, timezone

import respx
import httpx

from src.config import TapsiShopConfig
from src.marketplaces.tapsishop import TapsiShopAdapter
import pytest

_CFG = TapsiShopConfig(base_url="https://vendorgw.tapsi.shop", auth_token="test-token")


@respx.mock
def test_fetch_new_orders_paginates_and_normalizes():
    respx.post("https://vendorgw.tapsi.shop/Web/Hub/vendors/v1/orders").mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "pageNumber": 0,
                    "pageSize": 50,
                    "totalItems": 1,
                    "items": [
                        {
                            "id": "111",
                            "orderNumber": "ORD-111",
                            "stateTitle": "تایید سفارش",
                            "finalPrice": 250000,
                            "createdOn": "2026-08-10T09:00:00Z",
                        }
                    ],
                },
            },
        )
    )

    adapter = TapsiShopAdapter(config=_CFG)
    orders = adapter.fetch_new_orders(since=datetime(2026, 8, 1, tzinfo=timezone.utc))

    assert len(orders) == 1
    o = orders[0]
    assert o.source == "tapsishop"
    assert o.source_order_id == "111"
    assert o.total_price == 250000
    assert o.items == []  # list endpoint has no line items by design


@respx.mock
def test_fetch_order_detail_includes_items():
    respx.get("https://vendorgw.tapsi.shop/Web/Hub/vendors/v1/orders/111").mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "order": {
                        "orderNumber": "ORD-111",
                        "orderDate": "2026-08-10T09:00:00Z",
                        "originalAmount": "300000",
                        "amountAfterDiscount": "250000",
                        "status": "confirmed",
                    },
                    "items": [
                        {"sku": "SKU-1", "name": "Product A", "price": "150000", "finalPrice": "125000"},
                        {"sku": "SKU-2", "name": "Product B", "price": "150000", "finalPrice": "125000"},
                    ],
                },
            },
        )
    )

    adapter = TapsiShopAdapter(config=_CFG)
    order = adapter.fetch_order_detail("111")

    assert order.total_price == 250000
    assert len(order.items) == 2
    assert order.items[0].sku == "SKU-1"
    assert order.customer_mobile is None  # confirmed unavailable via REST polling


@respx.mock
def test_client_errors_are_not_retried():
    """
    Regression test: 400/401/403 must raise immediately (single call), not
    burn through 3 retry attempts with exponential backoff. Only server
    errors (5xx) and 429 should be retried - see _retryable_status().
    """
    route = respx.post("https://vendorgw.tapsi.shop/Web/Hub/vendors/v1/orders").mock(
        return_value=httpx.Response(400, json={"message": "invalid request"})
    )

    adapter = TapsiShopAdapter(config=_CFG)
    with pytest.raises(httpx.HTTPStatusError):
        adapter.fetch_new_orders(since=datetime(2026, 8, 1, tzinfo=timezone.utc))

    assert route.call_count == 1  # no retries for a 4xx client error


@respx.mock
def test_server_errors_are_retried():
    route = respx.post("https://vendorgw.tapsi.shop/Web/Hub/vendors/v1/orders").mock(
        return_value=httpx.Response(503, json={"message": "unavailable"})
    )

    adapter = TapsiShopAdapter(config=_CFG)
    with pytest.raises(httpx.HTTPStatusError):
        adapter.fetch_new_orders(since=datetime(2026, 8, 1, tzinfo=timezone.utc))

    assert route.call_count == 3  # stop_after_attempt(3)


@respx.mock
def test_pagination_continues_even_when_total_items_is_wrong():
    """
    Regression test, mirroring the same class of bug fixed in the Digikala
    adapter: pagination must not stop just because totalItems looks like
    it's already covered - a full page is itself a signal there may be more.
    """
    def _item(i):
        return {"id": str(i), "orderNumber": f"ORD-{i}", "stateTitle": "تایید سفارش", "finalPrice": 1000}

    route = respx.post("https://vendorgw.tapsi.shop/Web/Hub/vendors/v1/orders")
    route.mock(
        side_effect=[
            httpx.Response(200, json={"data": {"totalItems": 0, "items": [_item(i) for i in range(50)]}}),
            httpx.Response(200, json={"data": {"totalItems": 0, "items": [_item(50)]}}),
        ]
    )

    adapter = TapsiShopAdapter(config=_CFG)
    orders = adapter.fetch_new_orders(since=datetime(2026, 8, 1, tzinfo=timezone.utc))

    assert route.call_count == 2
    assert len(orders) == 51
