from datetime import datetime, timezone

import respx
import httpx

from src.config import BasalamConfig
from src.marketplaces.basalam import BasalamAdapter, BasalamAuthError

_CFG = BasalamConfig(base_url="https://order-processing.basalam.com", access_token="test-pat")


@respx.mock
def test_fetch_new_orders_paginates_via_cursor():
    respx.get("https://order-processing.basalam.com/v3/vendor-parcels").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": 555,
                        "total_items_price": 480000,
                        "created_at": "2026-08-10T10:00:00Z",
                        "status": {"id": 3739, "title": "جدید"},
                        "order": {"id": 9001, "paid_at": "2026-08-10T09:59:00Z"},
                    }
                ],
                "next_cursor": None,
                "previous_cursor": None,
            },
        )
    )

    adapter = BasalamAdapter(config=_CFG)
    orders = adapter.fetch_new_orders(since=datetime(2026, 8, 1, tzinfo=timezone.utc))

    assert len(orders) == 1
    o = orders[0]
    assert o.source == "basalam"
    assert o.source_order_id == "555"      # parcel id
    assert o.order_number == "9001"        # underlying platform order id
    assert o.total_price == 480000
    assert o.status == "جدید"
    assert o.items == []  # list endpoint - detail call needed for line items


@respx.mock
def test_fetch_order_detail_includes_items_and_customer():
    respx.get("https://order-processing.basalam.com/v3/vendor-parcels/555").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 555,
                "total_items_price": 480000,
                "created_at": "2026-08-10T10:00:00Z",
                "status": {"id": 3739, "title": "جدید"},
                "order": {
                    "id": 9001,
                    "paid_at": "2026-08-10T09:59:00Z",
                    "customer": {"name": "علی رضایی", "mobile": "09121234567"},
                },
                "items": [
                    {"id": 1, "title": "گلدان سفالی", "quantity": 2, "price": 240000,
                     "product": {"id": 4242}}
                ],
            },
        )
    )

    adapter = BasalamAdapter(config=_CFG)
    order = adapter.fetch_order_detail("555")

    assert len(order.items) == 1
    assert order.items[0].quantity == 2
    assert order.items[0].final_price == 480000  # price * quantity
    assert order.customer_full_name == "علی رضایی"
    assert order.customer_mobile == "09121234567"


@respx.mock
def test_expired_token_raises_clear_auth_error():
    respx.get("https://order-processing.basalam.com/v3/vendor-parcels").mock(
        return_value=httpx.Response(401, json={"detail": "unauthorized"})
    )

    adapter = BasalamAdapter(config=_CFG)
    try:
        adapter.fetch_new_orders(since=datetime(2026, 8, 1, tzinfo=timezone.utc))
        assert False, "expected BasalamAuthError"
    except BasalamAuthError as e:
        assert "developers.basalam.com/panel" in str(e)