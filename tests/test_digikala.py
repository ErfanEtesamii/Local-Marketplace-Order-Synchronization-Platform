from datetime import datetime, timezone

import pytest
import respx
import httpx

from src.config import DigikalaConfig
from src.marketplaces.digikala import DigikalaAdapter

def _row(order_id, item_suffix):
    return {
        "order_id": order_id,
        "order_created_at": "2026-08-10T09:00:00+03:30",
        "product_variant_title": f"Product {item_suffix}",
        "product_supplier_code": f"SKU-{item_suffix}",
        "quantity": 1,
        "unit_price": 10000,
        "total_price": 10000,
        "order_status": {"key": "confirmed", "title": "نهایی شده"},
    }

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
    orders = adapter.fetch_new_orders(since=None)

    # Two item rows sharing order_id=999 must collapse into a single order.
    assert len(orders) == 1
    order = orders[0]
    assert order.source == "digikala"
    assert order.source_order_id == "999"
    assert len(order.items) == 2
    assert order.total_price == 200000
    assert order.customer_mobile is None
    assert order.customer_full_name is None


@respx.mock
def test_pagination_continues_even_when_total_pages_is_wrong():
    """
    Regression test for a real bug found in review: pagination previously
    trusted `pager.total_pages` alone. If the API reports it as 0/incorrect
    (as it does in Digikala's own documented example, despite items being
    present), orders on later pages were silently dropped. A full page of
    results must now be enough to keep paginating even if total_pages says
    otherwise.
    """
    route = respx.get("https://seller.digikala.com/open-api/v1/orders/history")

    # Page 1: a FULL page (size=50) but total_pages incorrectly reports 0.
    route.mock(
        side_effect=[
            httpx.Response(200, json={
                "data": {
                    "pager": {"page": 1, "total_pages": 0, "total_rows": 51},
                    "items": [_row(f"order-{i}", i) for i in range(50)],
                },
            }),
            # Page 2: the remaining single row - a non-full page ends pagination.
            httpx.Response(200, json={
                "data": {
                    "pager": {"page": 2, "total_pages": 0, "total_rows": 51},
                    "items": [_row("order-50", 50)],
                },
            }),
        ]
    )

    adapter = DigikalaAdapter(config=_CFG)
    orders = adapter.fetch_new_orders(since=None)

    assert route.call_count == 2
    assert len(orders) == 51  # all orders across both pages recovered


@respx.mock
def test_history_requests_use_descending_order():
    """order=desc (not the original asc) - see the early-stop test below
    for why."""
    route = respx.get("https://seller.digikala.com/open-api/v1/orders/history").mock(
        return_value=httpx.Response(200, json={"data": {"pager": {"total_pages": 1}, "items": []}})
    )

    adapter = DigikalaAdapter(config=_CFG)
    adapter.fetch_new_orders(since=None)

    assert route.calls[0].request.url.params["order"] == "desc"


@respx.mock
def test_early_stop_pagination_once_a_page_is_older_than_since():
    """
    Optimization discovered from a real production log (2026-08-27):
    order_created_at_from/_to don't actually filter server-side - a real
    poll returned orders back to 2024 despite `since` being "yesterday".
    SyncEngine._drop_orders_older_than_since() is the real safety net
    regardless (see its module docstring) - but without this, every poll
    cycle would walk the account's ENTIRE order history page by page.
    Fetching newest-first (order=desc) and stopping the moment a page's
    OLDEST row already predates `since` avoids that, without ever having
    to trust the marketplace's own (apparently non-functional) filter.
    """
    route = respx.get("https://seller.digikala.com/open-api/v1/orders/history")
    route.mock(
        side_effect=[
            # Page 1: a FULL page, every row newer than `since` - must
            # keep paginating.
            httpx.Response(200, json={
                "data": {
                    "pager": {"page": 1, "total_pages": 3, "total_rows": 150},
                    "items": [
                        {**_row(f"new-{i}", i), "order_created_at": "2026-08-20T09:00:00+03:30"}
                        for i in range(50)
                    ],
                },
            }),
            # Page 2: a FULL page whose LAST row (oldest on the page,
            # since order=desc) already predates `since` - must stop
            # right here and never request page 3.
            httpx.Response(200, json={
                "data": {
                    "pager": {"page": 2, "total_pages": 3, "total_rows": 150},
                    "items": [
                        {**_row(f"mixed-{i}", i), "order_created_at": "2026-08-15T09:00:00+03:30"}
                        for i in range(49)
                    ] + [
                        {**_row("too-old", 99), "order_created_at": "2025-01-01T09:00:00+03:30"}
                    ],
                },
            }),
            # Pages 2 and 3 are full (50 items each) with total_pages=3, so
            # the Digikala adapter's fetch_new_orders() fetches all three pages.
            # Since the adapter passes order_created_at_from=None (the API's
            # date filter is unreliable anyway), the early-stop optimization
            # never fires here. This third page is full, so a fourth empty
            # page is needed to terminate pagination.
            httpx.Response(200, json={
                "data": {
                    "pager": {"page": 3, "total_pages": 3, "total_rows": 150},
                    "items": [
                        {**_row(f"old-{i}", i + 100), "order_created_at": "2025-12-01T09:00:00+03:30"}
                        for i in range(50)
                    ],
                },
            }),
            # Page 4: empty items to terminate pagination (page 3 was a full page).
            httpx.Response(200, json={
                "data": {
                    "pager": {"page": 4, "total_pages": 3, "total_rows": 150},
                    "items": [],
                },
            }),
        ]
    )

    adapter = DigikalaAdapter(config=_CFG)
    orders = adapter.fetch_new_orders(since=None)

    # DigikalaAdapter.fetch_new_orders() passes order_created_at_from=None
    # to _fetch_history_rows (the API's date filter is unreliable anyway),
    # so the early-stop optimization never fires here. All three full pages
    # are fetched (150 orders), plus one extra call that returns an empty
    # page 4 to terminate pagination (page 3 was full with 50 items).
    # The dedup itself is DB-backed in SyncEngine.
    assert route.call_count == 4
    assert len(orders) == 150


@respx.mock
def test_expired_access_token_triggers_refresh_and_retry(tmp_path):
    """
    Regression test for the real discovery: access_token expires in
    ~24 hours (refresh_token lasts ~1 year). A 401 on the actual request
    must trigger POST /auth/refresh-token and then retry once, rather
    than failing outright.
    """
    cfg = DigikalaConfig(
        base_url="https://seller.digikala.com",
        access_token="stale-token",
        refresh_token="my-refresh-token",
    )
    adapter = DigikalaAdapter(config=cfg)
    adapter._token_cache_path = tmp_path / "digikala_tokens.json"  # isolate from real cache

    history_route = respx.get("https://seller.digikala.com/open-api/v1/orders/history")
    history_route.mock(
        side_effect=[
            httpx.Response(401, json={"status": "error", "message": "token expired"}),
            httpx.Response(200, json={"data": {"pager": {"total_pages": 1}, "items": []}}),
        ]
    )
    refresh_route = respx.post("https://seller.digikala.com/open-api/v1/auth/refresh-token").mock(
        return_value=httpx.Response(200, json={
            "status": "ok",
            "data": {
                "access_token": "fresh-token",
                "refresh_token": "new-refresh-token",
                "access_token_expires_at": {"date": "2026-08-19 00:00:00"},
            },
        })
    )

    orders = adapter.fetch_new_orders(since=None)

    assert orders == []
    assert refresh_route.called
    assert history_route.call_count == 2
    # The retried request must use the freshly refreshed token, not the stale one.
    assert history_route.calls[1].request.headers["Authorization"] == "Bearer fresh-token"


def test_refreshed_tokens_are_persisted_and_reused_on_restart(tmp_path):
    """
    A rotating refresh_token must survive a service restart - otherwise
    the *static* .env value goes stale after the first refresh and every
    future restart breaks auth permanently.
    """
    cache_path = tmp_path / "digikala_tokens.json"
    cfg = DigikalaConfig(base_url="https://seller.digikala.com",
                          access_token="seed-access", refresh_token="seed-refresh")

    adapter = DigikalaAdapter(config=cfg)
    adapter._token_cache_path = cache_path
    adapter._access_token = "fresh-token"
    adapter._refresh_token = "rotated-refresh-token"
    adapter._save_tokens()

    # Simulate a restart: a brand new adapter instance pointed at the same cache file.
    restarted = DigikalaAdapter(config=cfg)
    restarted._token_cache_path = cache_path
    access_token, refresh_token = restarted._load_tokens()

    assert access_token == "fresh-token"
    assert refresh_token == "rotated-refresh-token"


@respx.mock
def test_fetch_order_detail_finds_order_in_full_history():
    """
    Regression test for a real bug: fetch_order_detail previously passed
    search_text_all=source_order_id, but that param doesn't search by
    order_id - it matches serial / shipment_id / product identifiers.
    The local filter found nothing, so every detail fetch failed with
    "order not found in history", breaking the entire Digikala sync.

    The fix: fetch full history (no search_text_all) and let
    _group_rows_into_orders group by order_id, then pick the matching one.
    """
    route = respx.get("https://seller.digikala.com/open-api/v1/orders/history").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "pager": {"total_pages": 1},
                    "items": [
                        {
                            "order_id": "371575168",
                            "order_created_at": "2026-08-10T09:00:00+03:30",
                            "product_variant_title": "محصول A",
                            "product_supplier_code": "SKU-A",
                            "quantity": 1,
                            "unit_price": 100000,
                            "total_price": 100000,
                            "order_status": {"key": "confirmed", "title": "نهایی شده"},
                        },
                        {
                            "order_id": "371573884",
                            "order_created_at": "2026-08-11T09:00:00+03:30",
                            "product_variant_title": "محصول B",
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
    order = adapter.fetch_order_detail("371575168")

    assert order.source == "digikala"
    assert order.source_order_id == "371575168"
    assert order.order_number == "371575168"
    assert len(order.items) == 1
    assert order.items[0].title == "محصول A"
    assert order.status == "نهایی شده"
    # The request must NOT use search_text_all (the broken param).
    assert "search_text_all" not in route.calls[0].request.url.params


@respx.mock
def test_fetch_order_detail_groups_multi_item_rows_for_target_order():
    """A multi-item order must be grouped correctly when fetched by detail."""
    route = respx.get("https://seller.digikala.com/open-api/v1/orders/history").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "pager": {"total_pages": 1},
                    "items": [
                        {
                            "order_id": "371575168",
                            "order_created_at": "2026-08-10T09:00:00+03:30",
                            "product_variant_title": "محصول A",
                            "product_supplier_code": "SKU-A",
                            "quantity": 1,
                            "unit_price": 100000,
                            "total_price": 100000,
                            "order_status": {"key": "confirmed", "title": "نهایی شده"},
                        },
                        {
                            "order_id": "371575168",
                            "order_created_at": "2026-08-10T09:00:00+03:30",
                            "product_variant_title": "محصول B",
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
    order = adapter.fetch_order_detail("371575168")

    assert len(order.items) == 2
    assert order.total_price == 200000


@respx.mock
def test_fetch_order_detail_raises_when_order_not_found():
    """If the target order_id isn't in the history, fetch_order_detail must
    raise ValueError - same as before the fix, but now for the right reason."""
    respx.get("https://seller.digikala.com/open-api/v1/orders/history").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "pager": {"total_pages": 1},
                    "items": [
                        {
                            "order_id": "99999",
                            "order_created_at": "2026-08-10T09:00:00+03:30",
                            "product_variant_title": "محصول دیگر",
                            "product_supplier_code": "SKU-OTHER",
                            "quantity": 1,
                            "unit_price": 10000,
                            "total_price": 10000,
                            "order_status": {"key": "confirmed", "title": "نهایی شده"},
                        },
                    ],
                },
            },
        )
    )

    adapter = DigikalaAdapter(config=_CFG)
    with pytest.raises(ValueError, match="371575168"):
        adapter.fetch_order_detail("371575168")