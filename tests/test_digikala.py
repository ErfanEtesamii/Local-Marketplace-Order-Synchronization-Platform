from datetime import datetime, timezone

import respx
import httpx

from src.config import DigikalaConfig
from src.marketplaces.digikala import DigikalaAdapter

_CFG = DigikalaConfig(base_url="https://seller.digikala.com", access_token="test-token")


@respx.mock
def test_fetch_new_orders_groups_multi_item_rows_into_one_order():
    respx.get("https://seller.digikala.com/open-api/v1/orders/history").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "ok",
                "data": {
                    "pager": {"page": 1, "item_per_page": 50, "total_pages": 1, "total_rows": 2},
                    "items": [
                        {
                            "order_id": 999,
                            "order_created_at": "2026-08-10T09:00:00+03:30",
                            "product_variant_title": "Product A",
                            "product_supplier_code": "SKU-A",
                            "quantity": 1,
                            "unit_price": 100000,
                            "total_price": 100000,
                            "order_status": {"key": "confirmed", "title": "نهایی شده"},
                        },
                        {
                            "order_id": 999,
                            "order_created_at": "2026-08-10T09:00:00+03:30",
                            "product_variant_title": "Product B",
                            "product_supplier_code": "SKU-B",
                            "quantity": 2,
                            "unit_price": 50000,
                            "total_price": 100000,
                            "order_status": {"key": "confirmed", "title": "نهایی شده"},
                        },
                    ],
                },
            },
        )
    )

    adapter = DigikalaAdapter(config=_CFG)
    orders = adapter.fetch_new_orders(since=datetime(2026, 8, 1, tzinfo=timezone.utc))

    # Two item rows sharing order_id=999 must collapse into a single order.
    assert len(orders) == 1
    order = orders[0]
    assert order.source == "digikala"
    assert order.source_order_id == "999"
    assert len(order.items) == 2
    assert order.total_price == 200000
    assert order.customer_mobile is None
    assert order.customer_full_name is None
