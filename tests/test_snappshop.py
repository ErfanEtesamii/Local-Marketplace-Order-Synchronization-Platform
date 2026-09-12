"""
Tests for the SnappShop adapter.

Rewritten (2026-09) against the confirmed v2.1.2 vendor-API PDF -
every JSON payload below is either lifted directly from the doc's
worked examples (sections 2-3-2 "دریافت آخرین جزئیات یک سفارش" and
2-3-3 "اندپوینت دریافت تاریخچه سفارشات") or a minimal variant of them,
not the old placeholder shape (`status`/`total_price`/`unit_price` at
the top level) the adapter never actually received from SnappShop.
See src/marketplaces/snappshop.py's module docstring for the schema
confirmation notes these tests are meant to lock in.
"""
from datetime import datetime

import respx
import httpx

from src.config import SnappShopConfig
from src.marketplaces.snappshop import SnappShopAdapter

_CFG = SnappShopConfig(
    base_url="https://apix.snappshop.ir",
    auth_token="test-token",
    agent_user="agent-123",
    vendor_id="v1",
    # Pinned explicitly rather than relying on SnappShopConfig's own
    # "toman" default, so this suite passes/fails on the code (and the
    # confirmed x10 conversion) and not on whatever a local .env has -
    # same reasoning as test_basalam.py's _CFG.price_unit.
    price_unit="toman",
)

# Doc section 2-3-2 ("دریافت آخرین جزئیات یک سفارش با استفاده از شماره
# سفارش"), order-detail endpoint sample - verbatim field names/shape,
# order_number swapped for a plain string for readability.
_ORDER_DETAIL_SAMPLE = {
    "data": {
        "order_number": "1216515253",
        "created_at": "2025-11-01 11:39:54",
        "delivery_type": "NORMAL",
        "order_status": "CONFIRMED",
        "item_origin": "VENDOR",
        "point_of_sales_at": None,
        "pickup_time": {"start": "2025-11-01 11:00:00", "end": "2025-11-01 18:00:00"},
        "customer": {
            "first_name": "مهسا",
            "last_name": "رضایی",
            "phone": None,
            "national_id": None,
        },
        "items": [
            {
                "sku": None,
                "product_number": 1564841615164,
                "parent_product_number": 1564841615160,
                "item_status": "CONFIRMED",
                "quantity": 1,
                "canceled_quantity": 0,
                "discount_amount": 13800000,
                "final_price": 13799990,
            }
        ],
    }
}

# Doc section 2-3-3 ("اندپوینت دریافت تاریخچه سفارشات"), order-history
# endpoint sample - adds vendor_product_info_id/original_price per item
# and a populated meta.pagination block, per the doc.
_ORDER_HISTORY_SAMPLE = {
    "status": True,
    "data": [
        {
            "order_number": 1885177654,
            "created_at": "2025-10-04 13:32:27",
            "delivery_type": "EXPRESS",
            "order_status": "CONFIRMED",
            "item_origin": "VENDOR",
            "point_of_sales_at": None,
            "pickup_time": {"start": "2025-10-04 13:00:00", "end": "2025-10-04 14:51:00"},
            "customer": {
                "first_name": "مهسا",
                "last_name": "رضایی",
                "phone": None,
                "national_id": None,
                "address": [],
            },
            "items": [
                {
                    "sku": None,
                    "vendor_product_info_id": "gwpGMM",
                    "product_number": 98456101161,
                    "parent_product_number": "68456101195",
                    "item_status": "CONFIRMED",
                    "quantity": 1,
                    "canceled_quantity": 0,
                    "original_price": 4500000,
                    "discount_amount": 13800000,
                    "final_price": 9300000,
                }
            ],
        }
    ],
    "meta": {
        "pagination": {
            "path": "{baseUrl}/vendors/{vendor_id}/orders",
            "per_page": 20,
            "count": 20,
            "has_more": False,
            "next_cursor": None,
        }
    },
}


@respx.mock
def test_fetch_new_orders_sends_confirmed_auth_headers():
    route = respx.get("https://apix.snappshop.ir/vendors/v1/orders").mock(
        return_value=httpx.Response(
            200,
            json={"data": [], "meta": {"pagination": {"has_more": False, "next_cursor": None}}},
        )
    )

    adapter = SnappShopAdapter(config=_CFG)
    adapter.fetch_new_orders(since=None)

    request = route.calls[0].request
    assert request.headers["Authorization"] == "Bearer test-token"
    assert request.headers["Agent-User"] == "agent-123"


@respx.mock
def test_fetch_new_orders_first_call_sends_no_params_when_since_is_none():
    """
    Confirmed (doc section 2-3-3): with no filters at all, the API
    applies its own documented default (last 14 days) - the adapter
    must not re-derive an equivalent start_date itself.
    """
    route = respx.get("https://apix.snappshop.ir/vendors/v1/orders").mock(
        return_value=httpx.Response(
            200,
            json={"data": [], "meta": {"pagination": {"has_more": False, "next_cursor": None}}},
        )
    )

    adapter = SnappShopAdapter(config=_CFG)
    adapter.fetch_new_orders(since=None)

    assert dict(route.calls[0].request.url.params) == {}


@respx.mock
def test_fetch_new_orders_first_call_sends_start_date_only_when_since_given():
    route = respx.get("https://apix.snappshop.ir/vendors/v1/orders").mock(
        return_value=httpx.Response(
            200,
            json={"data": [], "meta": {"pagination": {"has_more": False, "next_cursor": None}}},
        )
    )

    adapter = SnappShopAdapter(config=_CFG)
    adapter.fetch_new_orders(since=datetime(2025, 9, 23))

    params = dict(route.calls[0].request.url.params)
    assert params == {"start_date": "2025-09-23"}


@respx.mock
def test_fetch_new_orders_pagination_sends_cursor_alone_not_start_date_again():
    """
    Confirmed (doc section 2-3-3): once paging, continuation requests
    send `cursor` alone - the date filters are a first-request-only
    concept and must not be repeated.
    """
    route = respx.get("https://apix.snappshop.ir/vendors/v1/orders")
    route.mock(
        side_effect=[
            httpx.Response(200, json={
                "data": [],
                "meta": {"pagination": {"has_more": True, "next_cursor": "cur-2"}},
            }),
            httpx.Response(200, json={
                "data": [],
                "meta": {"pagination": {"has_more": False, "next_cursor": None}},
            }),
        ]
    )

    adapter = SnappShopAdapter(config=_CFG)
    adapter.fetch_new_orders(since=datetime(2025, 9, 23))

    assert route.call_count == 2
    first_params = dict(route.calls[0].request.url.params)
    second_params = dict(route.calls[1].request.url.params)
    assert first_params == {"start_date": "2025-09-23"}
    assert second_params == {"cursor": "cur-2"}


@respx.mock
def test_fetch_new_orders_normalizes_real_history_sample():
    respx.get("https://apix.snappshop.ir/vendors/v1/orders").mock(
        return_value=httpx.Response(200, json=_ORDER_HISTORY_SAMPLE)
    )

    adapter = SnappShopAdapter(config=_CFG)
    orders = adapter.fetch_new_orders(since=None)

    assert len(orders) == 1
    order = orders[0]
    assert order.source == "snappshop"
    assert order.source_order_id == "1885177654"
    assert order.order_number == "1885177654"
    assert order.status == "CONFIRMED"
    assert order.created_at == datetime(2025, 10, 4, 13, 32, 27)
    # Persian names pass through persianize_name() unchanged.
    assert order.customer_full_name == "مهسا رضایی"
    assert order.customer_mobile is None  # "phone": null -> buyer-info visibility not enabled
    assert order.customer_address is None  # "address": [] -> SnappShop itself handles delivery
    assert order.ship_time == datetime(2025, 10, 4, 13, 0, 0)  # pickup_time.start

    assert len(order.items) == 1
    item = order.items[0]
    assert item.sku == "gwpGMM"  # sku is null -> falls back to vendor_product_info_id
    assert item.title == ""  # confirmed: neither orders endpoint returns an item title
    assert item.quantity == 1
    # Confirmed unit is Toman -> to_rial multiplies by 10.
    assert item.final_price == 93000000  # 9300000 * 10
    # original_price is present on this endpoint -> used directly (not derived).
    assert item.unit_price == 45000000  # 4500000 * 10 / quantity(1)
    assert order.total_price == 93000000  # summed from item.final_price, only one item


@respx.mock
def test_fetch_order_detail_normalizes_real_detail_sample():
    respx.get("https://apix.snappshop.ir/vendors/v1/orders/1216515253").mock(
        return_value=httpx.Response(200, json=_ORDER_DETAIL_SAMPLE)
    )

    adapter = SnappShopAdapter(config=_CFG)
    order = adapter.fetch_order_detail("1216515253")

    assert order.source == "snappshop"
    assert order.source_order_id == "1216515253"
    assert order.status == "CONFIRMED"
    assert order.customer_full_name == "مهسا رضایی"
    assert order.customer_mobile is None
    assert order.customer_address is None  # no "address" key on this sample at all
    assert order.ship_time == datetime(2025, 11, 1, 11, 0, 0)

    assert len(order.items) == 1
    item = order.items[0]
    # sku is null and vendor_product_info_id is absent on this endpoint's
    # sample -> falls back to product_number.
    assert item.sku == "1564841615164"
    assert item.final_price == 137999900  # 13799990 * 10
    # No original_price on this endpoint -> derived as final_price + discount_amount.
    assert item.unit_price == 275999900  # (13799990 + 13800000) * 10 / quantity(1)
    assert order.total_price == 137999900


@respx.mock
def test_customer_address_populated_when_vendor_handles_delivery():
    """
    Confirmed: `address` is only a real (non-empty) value when the
    vendor, not SnappShop, handles delivery - a populated string is
    trusted and surfaced as customer_address.
    """
    payload = {
        "data": {
            **_ORDER_DETAIL_SAMPLE["data"],
            "customer": {
                **_ORDER_DETAIL_SAMPLE["data"]["customer"],
                "address": "تهران، خیابان آزادی، پلاک ۱۲",
            },
        }
    }
    respx.get("https://apix.snappshop.ir/vendors/v1/orders/1216515253").mock(
        return_value=httpx.Response(200, json=payload)
    )

    adapter = SnappShopAdapter(config=_CFG)
    order = adapter.fetch_order_detail("1216515253")

    assert order.customer_address == "تهران، خیابان آزادی، پلاک ۱۲"


@respx.mock
def test_fully_canceled_item_is_dropped_not_synced_as_zero_value():
    payload = {
        "data": {
            **_ORDER_DETAIL_SAMPLE["data"],
            "items": [
                *_ORDER_DETAIL_SAMPLE["data"]["items"],
                {
                    "sku": None,
                    "product_number": 999,
                    "parent_product_number": 998,
                    "item_status": "CANCELED",
                    "quantity": 2,
                    "canceled_quantity": 2,
                    "discount_amount": 0,
                    "final_price": 0,
                },
            ],
        }
    }
    respx.get("https://apix.snappshop.ir/vendors/v1/orders/1216515253").mock(
        return_value=httpx.Response(200, json=payload)
    )

    adapter = SnappShopAdapter(config=_CFG)
    order = adapter.fetch_order_detail("1216515253")

    # Only the CONFIRMED item survives - the CANCELED one is dropped
    # entirely rather than synced as a zero-value line (see
    # _normalize_items's docstring).
    assert len(order.items) == 1
    assert order.items[0].sku == "1564841615164"
    assert order.total_price == 137999900


@respx.mock
def test_discover_vendor_id_helper():
    respx.get("https://apix.snappshop.ir/vendors").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "v-42"}]})
    )
    adapter = SnappShopAdapter(config=_CFG)
    assert adapter.discover_vendor_id() == "v-42"
