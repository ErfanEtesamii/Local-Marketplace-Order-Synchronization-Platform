from datetime import datetime, timezone

import respx
import httpx

from src.config import TapsiShopConfig
from src.marketplaces.tapsishop import TapsiShopAdapter

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
