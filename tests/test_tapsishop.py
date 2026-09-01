from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import respx
import httpx
import pytest

from src.config import TapsiShopConfig
from src.marketplaces.tapsishop import TapsiShopAdapter

_CFG = TapsiShopConfig(base_url="https://vendorgw.tapsi.shop", auth_token="test-token")


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """
    _throttle() calls time.sleep() to respect Tapsi Shop's confirmed
    5-second rate limit. Without this, the suite would take minutes -
    the throttle logic itself is verified separately below, with
    mocked timing.
    """
    monkeypatch.setattr("src.marketplaces.tapsishop.time.sleep", lambda seconds: None)


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
    orders = adapter.fetch_new_orders(since=None)

    assert len(orders) == 1
    o = orders[0]
    assert o.source == "tapsishop"
    assert o.source_order_id == "111"
    assert o.total_price == 250000
    assert o.items == []  # list endpoint has no line items by design


@respx.mock
def test_request_body_includes_confirmed_date_filter_type():
    """
    Regression test: dateFilterTypeCode is required whenever fromDate/
    toDate are sent (confirmed via a live 400 without it) - the PDF's
    example value of 0 was just a placeholder, not valid. 1 is confirmed
    to work.
    """
    route = respx.post("https://vendorgw.tapsi.shop/Web/Hub/vendors/v1/orders").mock(
        return_value=httpx.Response(200, json={"data": {"totalItems": 0, "items": []}})
    )

    adapter = TapsiShopAdapter(config=_CFG)
    adapter.fetch_new_orders(since=None)

    assert b'"dateFilterTypeCode":1' in route.calls[0].request.content


@respx.mock
def test_request_body_filters_to_active_orders_only():
    """
    Regression test for a real production incident: previously-completed
    orders (already handled manually in Didar) were being re-synced.
    Only orderStatusId=4 (تایید سفارش - still active) should be
    requested, excluding 6 (cancelled) and 9 (delivered).
    """
    route = respx.post("https://vendorgw.tapsi.shop/Web/Hub/vendors/v1/orders").mock(
        return_value=httpx.Response(200, json={"data": {"totalItems": 0, "items": []}})
    )

    adapter = TapsiShopAdapter(config=_CFG)
    adapter.fetch_new_orders(since=None)

    assert b'"orderStatusId":[4]' in route.calls[0].request.content


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
def test_fetch_order_detail_extracts_product_image():
    """
    Each item's "picture" field (confirmed in docs/TapsiShop.v.0.2.pdf,
    order-detail response schema) must end up on that item's
    product_image_url, and the first item's photo must also be promoted to
    NormalizedOrder.product_image_url - that's the order-level field
    DidarSyncService reads to attach a photo to the "ارسال محصول" Activity.
    """
    respx.get("https://vendorgw.tapsi.shop/Web/Hub/vendors/v1/orders/222").mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "order": {
                        "orderNumber": "ORD-222",
                        "orderDate": "2026-08-10T09:00:00Z",
                        "originalAmount": "300000",
                        "amountAfterDiscount": "250000",
                        "status": "confirmed",
                    },
                    "items": [
                        {"sku": "SKU-1", "name": "Product A", "price": "150000",
                         "finalPrice": "125000", "picture": "https://cdn.tapsi.shop/a.jpg"},
                        {"sku": "SKU-2", "name": "Product B", "price": "150000",
                         "finalPrice": "125000", "picture": "https://cdn.tapsi.shop/b.jpg"},
                    ],
                },
            },
        )
    )

    adapter = TapsiShopAdapter(config=_CFG)
    order = adapter.fetch_order_detail("222")

    assert order.items[0].product_image_url == "https://cdn.tapsi.shop/a.jpg"
    assert order.items[1].product_image_url == "https://cdn.tapsi.shop/b.jpg"
    assert order.product_image_url == "https://cdn.tapsi.shop/a.jpg"


@respx.mock
def test_fetch_order_detail_handles_missing_picture():
    """No "picture" field on an item must yield None, not a crash or the
    string "None"."""
    respx.get("https://vendorgw.tapsi.shop/Web/Hub/vendors/v1/orders/223").mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "order": {
                        "orderNumber": "ORD-223",
                        "orderDate": "2026-08-10T09:00:00Z",
                        "originalAmount": "150000",
                        "amountAfterDiscount": "150000",
                        "status": "confirmed",
                    },
                    "items": [
                        {"sku": "SKU-1", "name": "Product A", "price": "150000", "finalPrice": "150000"},
                    ],
                },
            },
        )
    )

    adapter = TapsiShopAdapter(config=_CFG)
    order = adapter.fetch_order_detail("223")

    assert order.items[0].product_image_url is None
    assert order.product_image_url is None


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
        adapter.fetch_new_orders(since=None)

    assert route.call_count == 1  # no retries for a 4xx client error


@respx.mock
def test_server_errors_are_retried():
    route = respx.post("https://vendorgw.tapsi.shop/Web/Hub/vendors/v1/orders").mock(
        return_value=httpx.Response(503, json={"message": "unavailable"})
    )

    adapter = TapsiShopAdapter(config=_CFG)
    with pytest.raises(httpx.HTTPStatusError):
        adapter.fetch_new_orders(since=None)

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
    orders = adapter.fetch_new_orders(since=None)

    assert route.call_count == 2
    assert len(orders) == 51


@respx.mock
def test_long_date_range_is_split_into_7_day_windows():
    """
    Regression test: the vendor API rejects any [fromDate, toDate] span
    longer than 7 days with a 400. A 20-day lookback must therefore
    fan out into multiple <=7-day requests rather than one big one.
    """
    route = respx.post("https://vendorgw.tapsi.shop/Web/Hub/vendors/v1/orders").mock(
        return_value=httpx.Response(200, json={"data": {"totalItems": 0, "items": []}})
    )

    now = datetime.now(timezone.utc)
    adapter = TapsiShopAdapter(config=_CFG)
    adapter.fetch_new_orders(since=now - timedelta(days=20))

    # ceil(20 / 7) = 3 separate windows
    assert route.call_count == 3


@patch("src.marketplaces.tapsishop.time.sleep")
@patch("src.marketplaces.tapsishop.time.monotonic")
def test_throttle_enforces_minimum_interval_between_requests(mock_monotonic, mock_sleep):
    mock_monotonic.side_effect = [100.0, 100.0, 101.0, 106.0]

    adapter = TapsiShopAdapter(config=_CFG)
    adapter._throttle()  # first call: nothing to wait for yet
    adapter._throttle()  # second call: only 1s elapsed, must wait out the remaining 4.5s

    mock_sleep.assert_called_once_with(4.5)