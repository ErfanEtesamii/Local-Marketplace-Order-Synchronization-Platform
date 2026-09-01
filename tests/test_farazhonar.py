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
    orders = adapter.fetch_new_orders(since=None)

    assert len(orders) == 1
    order = orders[0]
    assert order.source == "farazhonar"
    assert order.source_order_id == "501"
    assert order.total_price == 4800000  # 480000 تومان × ۱۰ (FarazHonarConfig.price_unit="toman")
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
    orders = adapter.fetch_new_orders(since=None)

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


@respx.mock
def test_fetch_order_detail_converts_english_billing_name_to_persian():
    # Customer filled the WooCommerce billing form with an English
    # keyboard layout - _normalize() should convert it to Persian
    # script (see src/finglish.py) rather than passing it through as-is.
    raw_order = {
        **_RAW_ORDER,
        "id": 503,
        "billing": {"first_name": "mohammad", "last_name": "ahmadi", "phone": "09121234567"},
    }
    respx.get("https://farazhonar.com/wp-json/wc/v3/orders/503").mock(
        return_value=httpx.Response(200, json=raw_order)
    )
    adapter = FarazHonarAdapter(config=_CFG)
    order = adapter.fetch_order_detail("503")
    assert order.customer_full_name == "محمد احمدی"


@respx.mock
def test_price_unit_rial_config_does_not_multiply():
    """
    Regression test for the Toman->Rial conversion (src/currency.py):
    a store explicitly configured with price_unit="rial" must NOT have
    its prices multiplied by 10 - only "toman" (FarazHonarConfig's
    default, confirmed for the real Faraz Honar store) does.
    """
    rial_cfg = FarazHonarConfig(
        base_url="https://farazhonar.com",
        consumer_key="ck_test",
        consumer_secret="cs_test",
        price_unit="rial",
    )
    respx.get("https://farazhonar.com/wp-json/wc/v3/orders/501").mock(
        return_value=httpx.Response(200, json=_RAW_ORDER)
    )
    adapter = FarazHonarAdapter(config=rial_cfg)
    order = adapter.fetch_order_detail("501")
    assert order.total_price == 480000


@respx.mock
def test_normalize_resolves_image_url_for_each_line_item():
    """
    The first image URL from each line item's WooCommerce product must
    end up on that item's product_image_url field, so the
    "ارسال محصول" (ship) Activity in Didar can attach it. The lookup
    goes through /wp-json/wc/v3/products/{id} - ONE request per unique
    product_id (category AND image come from the same response - see
    _resolve_product_meta()) - and is cached per id.
    """
    raw_order = {
        **_RAW_ORDER,
        "id": 601,
        "number": "601",
        "line_items": [
            {
                "product_id": 111,
                "sku": "SKU-A",
                "name": "جعبه خاتم",
                "quantity": 2,
                "price": "150000",
                "total": "300000",
            },
            {
                "product_id": 222,
                "sku": "SKU-B",
                "name": "تخته نرد",
                "quantity": 1,
                "price": "180000",
                "total": "180000",
            },
        ],
    }
    respx.get("https://farazhonar.com/wp-json/wc/v3/orders/601").mock(
        return_value=httpx.Response(200, json=raw_order)
    )
    products_route = respx.get(
        url__regex=r"https://farazhonar\.com/wp-json/wc/v3/products/\d+"
    ).mock(
        side_effect=[
            # _resolve_product_meta(product_id=111) - ONE call, category + image together
            httpx.Response(200, json={"categories": [{"name": "خاتم"}], "images": [{"src": "https://cdn.farazhonar.com/111.jpg"}]}),
            # _resolve_product_meta(product_id=222) - ONE call, category + image together
            httpx.Response(200, json={"categories": [{"name": "-NC"}], "images": [{"src": "https://cdn.farazhonar.com/222.jpg"}]}),
        ]
    )

    adapter = FarazHonarAdapter(config=_CFG)
    order = adapter.fetch_order_detail("601")

    assert products_route.call_count == 2  # ONE request per unique product_id (2 products) - not 2 per product
    assert order.items[0].product_image_url == "https://cdn.farazhonar.com/111.jpg"
    assert order.items[1].product_image_url == "https://cdn.farazhonar.com/222.jpg"
    # Order-level field - now just a last-resort fallback for
    # DidarSyncService._fetch_product_images; each item's OWN image
    # above (product_image_url) is what actually gets attached per
    # line item to the "ارسال محصول" Activity.
    assert order.product_image_url == "https://cdn.farazhonar.com/111.jpg"


@respx.mock
def test_normalize_image_url_is_none_when_product_has_no_image():
    """
    A WooCommerce product with no images (or an empty images array) must
    resolve to None - the ship Activity simply has no attachment, rather
    than the sync failing.
    """
    raw_order = {
        **_RAW_ORDER,
        "id": 602,
        "number": "602",
        "line_items": [
            {
                "product_id": 333,
                "sku": "SKU-NOIMG",
                "name": "محصول بدون تصویر",
                "quantity": 1,
                "price": "100000",
                "total": "100000",
            },
        ],
    }
    respx.get("https://farazhonar.com/wp-json/wc/v3/orders/602").mock(
        return_value=httpx.Response(200, json=raw_order)
    )
    respx.get("https://farazhonar.com/wp-json/wc/v3/products/333").mock(
        return_value=httpx.Response(200, json={"images": []})
    )

    adapter = FarazHonarAdapter(config=_CFG)
    order = adapter.fetch_order_detail("602")

    assert order.items[0].product_image_url is None


@respx.mock
def test_normalize_image_lookup_failure_does_not_break_order():
    """
    A transient HTTP failure on the per-product image lookup must NOT
    propagate - _resolve_image_url() swallows HTTPError and returns None,
    so the order still syncs with no product image attached to the ship
    Activity. Confirms the "image is best-effort" contract.
    """
    raw_order = {
        **_RAW_ORDER,
        "id": 603,
        "number": "603",
        "line_items": [
            {
                "product_id": 444,
                "sku": "SKU-ERR",
                "name": "محصول با خطا",
                "quantity": 1,
                "price": "100000",
                "total": "100000",
            },
        ],
    }
    respx.get("https://farazhonar.com/wp-json/wc/v3/orders/603").mock(
        return_value=httpx.Response(200, json=raw_order)
    )
    respx.get("https://farazhonar.com/wp-json/wc/v3/products/444").mock(
        return_value=httpx.Response(500, text="boom")
    )

    adapter = FarazHonarAdapter(config=_CFG)
    order = adapter.fetch_order_detail("603")

    assert order.items[0].product_image_url is None
    assert order.source_order_id == "603"


@respx.mock
def test_normalize_caches_image_lookup_per_product_id():
    """
    If two line items reference the same product_id, the lookup for
    that id (_resolve_product_meta - category + image together) must
    happen ONCE total, not once per line item, avoiding extra requests
    per repeated product on multi-item orders.
    """
    raw_order = {
        **_RAW_ORDER,
        "id": 604,
        "number": "604",
        "line_items": [
            {
                "product_id": 555,
                "sku": "SKU-X",
                "name": "محصول تکراری ۱",
                "quantity": 1,
                "price": "100000",
                "total": "100000",
            },
            {
                "product_id": 555,
                "sku": "SKU-X",
                "name": "محصول تکراری ۲",
                "quantity": 1,
                "price": "100000",
                "total": "100000",
            },
        ],
    }
    respx.get("https://farazhonar.com/wp-json/wc/v3/orders/604").mock(
        return_value=httpx.Response(200, json=raw_order)
    )
    products_route = respx.get("https://farazhonar.com/wp-json/wc/v3/products/555").mock(
        return_value=httpx.Response(
            200,
            json={"categories": [{"name": "خاتم"}], "images": [{"src": "https://cdn.farazhonar.com/555.jpg"}]},
        )
    )

    adapter = FarazHonarAdapter(config=_CFG)
    order = adapter.fetch_order_detail("604")

    # Same product_id on two line items -> _resolve_product_meta() runs once
    # total (category + image fetched together, then cached) - only 1 call,
    # not 2 (previously 2 for the first item's lookups + cached for the
    # second, now merged into a single call per unique product_id).
    assert products_route.call_count == 1
    assert order.items[0].product_image_url == "https://cdn.farazhonar.com/555.jpg"
    assert order.items[1].product_image_url == "https://cdn.farazhonar.com/555.jpg"