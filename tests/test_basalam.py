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
    assert o.total_price == 4800000  # 480000 تومان × ۱۰ (BasalamConfig defaults to price_unit="toman")
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
    assert order.items[0].final_price == 4800000  # (price * quantity) تومان × ۱۰
    assert order.customer_full_name == "علی رضایی"
    assert order.customer_mobile == "09121234567"


@respx.mock
def test_fetch_new_orders_uses_confirmed_max_per_page():
    """
    Regression test: per_page=50 (an assumption carried over from other
    adapters) is rejected with a live 422 ("Input should be less than
    or equal to 30"). 30 is the confirmed maximum.
    """
    route = respx.get("https://order-processing.basalam.com/v3/vendor-parcels").mock(
        return_value=httpx.Response(200, json={"data": [], "next_cursor": None})
    )

    adapter = BasalamAdapter(config=_CFG)
    adapter.fetch_new_orders(since=datetime(2026, 8, 1, tzinfo=timezone.utc))

    assert route.calls[0].request.url.params["per_page"] == "30"


@respx.mock
def test_fetch_new_orders_uses_confirmed_valid_sort_value():
    """
    Regression test: created_at:desc and id:desc are both rejected with
    a live 422 ("مرتب سازی معتبر نمی باشد"). Only estimate_send_at:desc
    (the documented default) is confirmed valid.
    """
    route = respx.get("https://order-processing.basalam.com/v3/vendor-parcels").mock(
        return_value=httpx.Response(200, json={"data": [], "next_cursor": None})
    )

    adapter = BasalamAdapter(config=_CFG)
    adapter.fetch_new_orders(since=datetime(2026, 8, 1, tzinfo=timezone.utc))

    assert route.calls[0].request.url.params["sort"] == "estimate_send_at:desc"


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


@respx.mock
def test_price_unit_rial_config_does_not_multiply():
    """
    Regression test for the Toman->Rial conversion (src/currency.py):
    a vendor explicitly configured with price_unit="rial" must NOT have
    its prices multiplied by 10 - only "toman" (BasalamConfig's
    default) does.
    """
    rial_cfg = BasalamConfig(
        base_url="https://order-processing.basalam.com",
        access_token="test-pat",
        price_unit="rial",
    )
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

    adapter = BasalamAdapter(config=rial_cfg)
    orders = adapter.fetch_new_orders(since=datetime(2026, 8, 1, tzinfo=timezone.utc))

    assert orders[0].total_price == 480000