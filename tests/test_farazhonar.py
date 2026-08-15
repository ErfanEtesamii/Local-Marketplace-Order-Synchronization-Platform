from datetime import datetime, timezone

import respx
import httpx

from src.config import FarazHonarConfig
from src.marketplaces.farazhonar import FarazHonarAdapter

_CFG = FarazHonarConfig(
    base_url="https://farazhonar.com",
    consumer_key="ck_test",
    consumer_secret="cs_test",
)

_RAW_ORDER = {
    "id": 501,
    "number": "501",
    "status": "processing",
    "date_created_gmt": "2026-08-10T09:00:00",
    "total": "480000",
    "billing": {"first_name": "علی", "last_name": "رضایی", "phone": "09121234567"},
    "line_items": [
        {"sku": "SKU-A", "name": "جعبه خاتم", "quantity": 2, "price": "150000", "total": "300000"},
        {"sku": "SKU-B", "name": "تخته نرد", "quantity": 1, "price": "180000", "total": "180000"},
    ],
}


@respx.mock
def test_fetch_new_orders_uses_basic_auth_and_includes_line_items_directly():
    """
    Unlike every marketplace adapter, WooCommerce's list endpoint already
    returns full line_items - this test locks in that no second request
    is needed to get items (a real behavioral difference worth guarding).
    """
    route = respx.get("https://farazhonar.com/wp-json/wc/v3/orders").mock(
        return_value=httpx.Response(200, json=[_RAW_ORDER], headers={"X-WP-TotalPages": "1"})
    )

    adapter = FarazHonarAdapter(config=_CFG)
    orders = adapter.fetch_new_orders(since=datetime(2026, 8, 1, tzinfo=timezone.utc))

    assert len(orders) == 1
    order = orders[0]
    assert order.source == "farazhonar"
    assert order.source_order_id == "501"
    assert order.total_price == 480000
    assert len(order.items) == 2  # got items from the LIST call, no detail call needed
    assert order.customer_full_name == "علی رضایی"
    assert order.customer_mobile == "09121234567"

    request = route.calls[0].request
    assert request.headers["Authorization"].startswith("Basic ")


@respx.mock
def test_fetch_new_orders_paginates_using_wp_total_pages_header():
    route = respx.get("https://farazhonar.com/wp-json/wc/v3/orders")
    route.mock(
        side_effect=[
            httpx.Response(200, json=[_RAW_ORDER],
                            headers={"X-WP-TotalPages": "2"}),
            httpx.Response(200, json=[{**_RAW_ORDER, "id": 502, "number": "502"}],
                            headers={"X-WP-TotalPages": "2"}),
        ]
    )

    adapter = FarazHonarAdapter(config=_CFG)
    orders = adapter.fetch_new_orders(since=datetime(2026, 8, 1, tzinfo=timezone.utc))

    assert route.call_count == 2
    assert {o.source_order_id for o in orders} == {"501", "502"}


@respx.mock
def test_fetch_order_detail():
    respx.get("https://farazhonar.com/wp-json/wc/v3/orders/501").mock(
        return_value=httpx.Response(200, json=_RAW_ORDER)
    )
    adapter = FarazHonarAdapter(config=_CFG)
    order = adapter.fetch_order_detail("501")
    assert order.source_order_id == "501"
    assert len(order.items) == 2
