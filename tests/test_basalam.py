from datetime import datetime, timezone

import respx
import httpx

from src.config import BasalamConfig
from src.marketplaces.basalam import BasalamAdapter, BasalamAuthError

_CFG = BasalamConfig(
    base_url="https://order-processing.basalam.com",
    access_token="test-pat",
    # Explicit, not left to the BasalamConfig default: src/config.py calls
    # load_dotenv() at import time, so leaving this unset means every test
    # here silently inherits whatever BASALAM_PRICE_UNIT happens to be set
    # to in the real, local .env (currently "rial" for this account) -
    # not the code's own "toman" default these tests are meant to exercise.
    # Pinning it here makes the suite pass/fail on the code, not on
    # whatever value someone's local .env happens to have.
    price_unit="toman",
)


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
                        "estimate_send_at": "2026-08-12T18:00:00Z",
                    }
                ],
                "next_cursor": None,
                "previous_cursor": None,
            },
        )
    )

    adapter = BasalamAdapter(config=_CFG)
    orders = adapter.fetch_new_orders(since=None)

    assert len(orders) == 1
    o = orders[0]
    assert o.source == "basalam"
    assert o.source_order_id == "555"      # parcel id
    assert o.order_number == "9001"        # underlying platform order id
    assert o.total_price == 4800000  # 480000 تومان × ۱۰ (this test's _CFG explicitly pins price_unit="toman" now - see its comment; BasalamConfig's own default is "rial", confirmed 2026-09)
    assert o.status == "جدید"
    assert o.items == []  # list endpoint - detail call needed for line items
    assert o.ship_time == datetime(2026, 8, 12, 18, 0, tzinfo=timezone.utc)


@respx.mock
def test_fetch_new_orders_ship_time_is_none_when_estimate_send_at_missing():
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
                        "order": {"id": 9001},
                        # no estimate_send_at
                    }
                ],
                "next_cursor": None,
                "previous_cursor": None,
            },
        )
    )

    adapter = BasalamAdapter(config=_CFG)
    orders = adapter.fetch_new_orders(since=None)

    # Must stay None, not fall back to "now" like created_at does - see
    # _parse_date_or_none's docstring for why.
    assert orders[0].ship_time is None


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
                    "customer": {
                        "recipient": {
                            "name": "علی رضایی",
                            "mobile": "09121234567",
                            "postal_code": "1234567890",
                            "postal_address": "تهران، خیابان ولیعصر، پلاک ۱۰",
                        },
                        "city": {"title": "تهران"},
                    },
                },
                "estimate_send_at": "2026-08-12T18:00:00Z",
                "items": [
                    {"id": 1, "title": "گلدان سفالی", "quantity": 2, "price": 240000,
                     "product": {"id": 4242, "photos": [
                         {"id": 1, "original": "https://cdn.basalam.com/photos/4242-original.jpg",
                          "resized": {"lg": "https://cdn.basalam.com/photos/4242-lg.jpg"}},
                     ]}}
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
    assert order.customer_postal_code == "1234567890"
    assert order.customer_address == "تهران، خیابان ولیعصر، پلاک ۱۰"
    assert order.customer_city == "تهران"
    assert order.ship_time == datetime(2026, 8, 12, 18, 0, tzinfo=timezone.utc)
    assert order.product_image_url == "https://cdn.basalam.com/photos/4242-original.jpg"


@respx.mock
def test_fetch_order_detail_finglish_customer_name_converted_to_persian():
    respx.get("https://order-processing.basalam.com/v3/vendor-parcels/556").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 556,
                "total_items_price": 100000,
                "created_at": "2026-08-10T10:00:00Z",
                "status": {"id": 3739, "title": "جدید"},
                "order": {
                    "id": 9002,
                    "paid_at": "2026-08-10T09:59:00Z",
                    "customer": {
                        "recipient": {"name": "mohammad ahmadi", "mobile": "09121234567"},
                    },
                },
                "items": [],
            },
        )
    )

    adapter = BasalamAdapter(config=_CFG)
    order = adapter.fetch_order_detail("556")

    assert order.customer_full_name == "محمد احمدی"


@respx.mock
def test_fetch_order_detail_missing_recipient_falls_back_to_none():
    respx.get("https://order-processing.basalam.com/v3/vendor-parcels/557").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 557,
                "total_items_price": 100000,
                "created_at": "2026-08-10T10:00:00Z",
                "status": {"id": 3739, "title": "جدید"},
                "order": {"id": 9003, "paid_at": "2026-08-10T09:59:00Z", "customer": {}},
                "items": [],
            },
        )
    )

    adapter = BasalamAdapter(config=_CFG)
    order = adapter.fetch_order_detail("557")

    assert order.customer_full_name is None
    assert order.customer_mobile is None


@respx.mock
def test_fetch_order_detail_product_image_falls_back_to_resized_without_original():
    respx.get("https://order-processing.basalam.com/v3/vendor-parcels/555").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 555,
                "total_items_price": 480000,
                "created_at": "2026-08-10T10:00:00Z",
                "status": {"id": 3739, "title": "جدید"},
                "order": {"id": 9001},
                "items": [
                    {"id": 1, "title": "گلدان سفالی", "quantity": 1, "price": 240000,
                     "product": {"id": 4242, "photos": [
                         {"id": 1, "original": "", "resized": {"lg": "https://cdn.basalam.com/photos/4242-lg.jpg"}},
                     ]}}
                ],
            },
        )
    )

    adapter = BasalamAdapter(config=_CFG)
    order = adapter.fetch_order_detail("555")

    assert order.product_image_url == "https://cdn.basalam.com/photos/4242-lg.jpg"


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
    adapter.fetch_new_orders(since=None)

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
    adapter.fetch_new_orders(since=None)

    assert route.calls[0].request.url.params["sort"] == "estimate_send_at:desc"


@respx.mock
def test_expired_token_raises_clear_auth_error():
    respx.get("https://order-processing.basalam.com/v3/vendor-parcels").mock(
        return_value=httpx.Response(401, json={"detail": "unauthorized"})
    )

    adapter = BasalamAdapter(config=_CFG)
    try:
        adapter.fetch_new_orders(since=None)
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
    orders = adapter.fetch_new_orders(since=None)

    assert orders[0].total_price == 480000