import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest
import respx
import httpx

from src.config import DigikalaConfig
from src.db.repository import Repository
from src.marketplaces.digikala import DigikalaAdapter

_CFG = DigikalaConfig(base_url="https://seller.digikala.com", access_token="test-token")

_SBS_URL = "https://seller.digikala.com/open-api/v1/ship-by-seller-orders"


@pytest.fixture
def repo(tmp_path):
    return Repository(db_path=str(tmp_path / "test.db"))


def _sbs_row(shipment_id, order_id=None, **overrides):
    row = {
        "orderId": order_id if order_id is not None else shipment_id,
        "shipmentId": shipment_id,
        "orderDate": "1403/11/07",
        "address": {"state": "تهران", "city": "تهران", "district": "ونک"},
        "trackingCode": "11234",
        "shippingCost": 650000,
        "status": {"text": "processing", "text_fa": "در حال پردازش"},
        "isCancelled": False,
        "hasFailedDeliveryBefore": False,
        "customer_name": "علی علیایی",
        "customer_address": "تهران، تهران، ونک، خدامی",
        "customer_postal_code": "111111111",
        "customer_phone_number": "09121212121",
        "variants": [
            {
                "image_url": "https://dkstatics-public.digikala.com/example.jpg",
                "title": "تیشرت مردانه",
                "productId": "123",
                "sellerCode": 1,
                "count": 1,
                "price": 1200000,
            }
        ],
    }
    row.update(overrides)
    return row


def _sbs_list_response(items, page=1, total_pages=1):
    return httpx.Response(
        200,
        json={
            "status": "ok",
            "data": {
                "pager": {"page": page, "item_per_page": 50, "total_pages": total_pages, "total_rows": len(items)},
                "items": items,
            },
        },
    )


# --- cold start / watermark ------------------------------------------------

@respx.mock
def test_cold_start_seeds_watermark_without_syncing_anything(repo):
    """No watermark yet -> a single size=1/sort=id/order=desc request seeds
    it to the account's current highest shipmentId, and NO order is synced
    (Decision 5, digikala-sbs-migration-prompt.md)."""
    route = respx.get(_SBS_URL).mock(
        return_value=_sbs_list_response([_sbs_row(shipment_id=777)])
    )

    adapter = DigikalaAdapter(config=_CFG, repository=repo)
    orders = adapter.fetch_new_orders(since=None)

    assert orders == []
    assert repo.get_last_shipment_id("digikala") == 777
    assert route.calls[0].request.url.params["sort"] == "id"
    assert route.calls[0].request.url.params["order"] == "desc"
    assert route.calls[0].request.url.params["size"] == "1"


@respx.mock
def test_cold_start_seeds_zero_when_account_has_no_shipments(repo):
    respx.get(_SBS_URL).mock(return_value=_sbs_list_response([]))

    adapter = DigikalaAdapter(config=_CFG, repository=repo)
    orders = adapter.fetch_new_orders(since=None)

    assert orders == []
    assert repo.get_last_shipment_id("digikala") == 0


@respx.mock
def test_fetch_new_orders_uses_watermark_plus_one_and_advances_it(repo):
    """With a watermark already set, fetch_new_orders must request
    search[min_shipment_id]=watermark+1 and, once new rows come back,
    persist the new max shipmentId as the watermark."""
    repo.set_last_shipment_id("digikala", 100)
    route = respx.get(_SBS_URL).mock(
        return_value=_sbs_list_response([_sbs_row(shipment_id=101), _sbs_row(shipment_id=105)])
    )

    adapter = DigikalaAdapter(config=_CFG, repository=repo)
    orders = adapter.fetch_new_orders(since=None)

    assert len(orders) == 2
    assert route.calls[0].request.url.params["search[min_shipment_id]"] == "101"
    assert route.calls[0].request.url.params["sort"] == "shipment_id"
    assert route.calls[0].request.url.params["order"] == "asc"
    assert repo.get_last_shipment_id("digikala") == 105


@respx.mock
def test_fetch_new_orders_returns_nothing_when_no_new_shipments(repo):
    """Immediately re-polling with no new shipments must return zero
    orders and leave the watermark unchanged."""
    repo.set_last_shipment_id("digikala", 500)
    respx.get(_SBS_URL).mock(return_value=_sbs_list_response([]))

    adapter = DigikalaAdapter(config=_CFG, repository=repo)
    orders = adapter.fetch_new_orders(since=None)

    assert orders == []
    assert repo.get_last_shipment_id("digikala") == 500


@respx.mock
def test_watermark_persisted_after_every_page_not_just_at_the_end(repo):
    """Regression guard for Decision 4: a crash between pages must not
    lose progress from pages already fetched. We simulate this by
    asserting the watermark reflects page 1's max even though we can
    inspect it (via the route side_effect) before page 2 is requested."""
    repo.set_last_shipment_id("digikala", 0)
    route = respx.get(_SBS_URL)
    seen_watermark_before_page_2 = {}

    def _responder(request):
        params = dict(request.url.params)
        page = int(params["page"])
        if page == 1:
            return _sbs_list_response(
                [_sbs_row(shipment_id=i) for i in range(1, 51)], page=1, total_pages=2
            )
        # By the time page 2 is requested, page 1's max must already be persisted.
        seen_watermark_before_page_2["value"] = repo.get_last_shipment_id("digikala")
        return _sbs_list_response([_sbs_row(shipment_id=51)], page=2, total_pages=2)

    route.mock(side_effect=_responder)

    adapter = DigikalaAdapter(config=_CFG, repository=repo)
    orders = adapter.fetch_new_orders(since=None)

    assert seen_watermark_before_page_2["value"] == 50
    assert len(orders) == 51
    assert repo.get_last_shipment_id("digikala") == 51


@respx.mock
def test_fetch_new_orders_paginates_via_full_page_guard(repo):
    """Same double-signal pagination guard as the old /orders/history
    fetch: a full page keeps paginating even if total_pages under-reports."""
    repo.set_last_shipment_id("digikala", 0)
    route = respx.get(_SBS_URL)
    route.mock(
        side_effect=[
            _sbs_list_response([_sbs_row(shipment_id=i) for i in range(1, 51)], page=1, total_pages=0),
            _sbs_list_response([_sbs_row(shipment_id=51)], page=2, total_pages=0),
        ]
    )

    adapter = DigikalaAdapter(config=_CFG, repository=repo)
    orders = adapter.fetch_new_orders(since=None)

    assert route.call_count == 2
    assert len(orders) == 51


def test_since_argument_is_ignored(repo):
    """fetch_new_orders must not raise or behave differently regardless of
    what `since` is passed - it's watermark-driven, not time-driven."""
    with respx.mock:
        respx.get(_SBS_URL).mock(return_value=_sbs_list_response([]))
        adapter = DigikalaAdapter(config=_CFG, repository=repo)
        adapter.fetch_new_orders(since=datetime.now(timezone.utc))
    assert repo.get_last_shipment_id("digikala") is not None


# --- row normalization -------------------------------------------------

def test_normalize_sbs_row_maps_customer_and_address_fields(repo):
    adapter = DigikalaAdapter(config=_CFG, repository=repo)
    order = adapter._normalize_sbs_row(_sbs_row(shipment_id=1, order_id=9))

    assert order.source == "digikala"
    assert order.source_order_id == "1"
    assert order.order_number == "9"
    assert order.shipment_id == "1"
    assert order.customer_full_name == "علی علیایی"
    assert order.customer_mobile == "09121212121"
    assert order.customer_address == "تهران، تهران، ونک، خدامی"
    assert order.customer_postal_code == "111111111"
    assert order.customer_province == "تهران"
    assert order.customer_city == "تهران"
    assert order.shipment_tracking_code == "11234"
    assert order.shipping_cost == Decimal("650000")


def test_normalize_sbs_row_builds_items_from_variants_price_times_count(repo):
    """Decision 2 (unconfirmed assumption): price is per-unit, no discount
    field exists, final_price = price * count."""
    adapter = DigikalaAdapter(config=_CFG, repository=repo)
    row = _sbs_row(
        shipment_id=1,
        variants=[
            {
                "title": "Product A",
                "sellerCode": 42,
                "count": 3,
                "price": 100000,
                "image_url": "https://example.com/a.jpg",
            }
        ],
    )
    order = adapter._normalize_sbs_row(row)

    assert len(order.items) == 1
    item = order.items[0]
    assert item.sku == "42"
    assert item.title == "Product A"
    assert item.quantity == 3
    assert item.unit_price == Decimal("100000")
    assert item.final_price == Decimal("300000")
    assert item.product_image_url == "https://example.com/a.jpg"
    assert order.total_price == Decimal("300000")
    assert order.product_image_url == "https://example.com/a.jpg"


@pytest.mark.parametrize(
    "overrides,expected_status",
    [
        ({"isCancelled": True, "status": {"text": "processing"}}, "cancelled"),
        ({"isCancelled": False, "status": {"text": "rejected"}}, "rejected"),
        ({"isCancelled": False, "status": {"text": "pending"}}, "pending"),
        ({"isCancelled": False, "status": {"text": "processing"}}, "processing"),
        ({"isCancelled": False, "status": {"text": "processed"}}, "processed"),
        ({"isCancelled": False, "status": {"text": "edited"}}, "edited"),
        ({"isCancelled": False, "status": {}}, "unknown"),
    ],
)
def test_normalize_sbs_row_status_mapping(repo, overrides, expected_status):
    """Decision 3: isCancelled wins over status.text; rejected is the other
    terminal state; everything else passes through as-is."""
    adapter = DigikalaAdapter(config=_CFG, repository=repo)
    order = adapter._normalize_sbs_row(_sbs_row(shipment_id=1, **overrides))
    assert order.status == expected_status


def test_normalize_sbs_row_isCancelled_wins_even_if_status_text_is_rejected(repo):
    adapter = DigikalaAdapter(config=_CFG, repository=repo)
    order = adapter._normalize_sbs_row(
        _sbs_row(shipment_id=1, isCancelled=True, status={"text": "rejected"})
    )
    assert order.status == "cancelled"


def test_normalize_sbs_row_handles_missing_variants(repo):
    adapter = DigikalaAdapter(config=_CFG, repository=repo)
    row = _sbs_row(shipment_id=1)
    row["variants"] = []
    order = adapter._normalize_sbs_row(row)

    assert order.items == []
    assert order.total_price == Decimal("0")
    assert order.product_image_url is None


def test_normalize_sbs_row_parses_jalali_order_date(repo):
    adapter = DigikalaAdapter(config=_CFG, repository=repo)
    order = adapter._normalize_sbs_row(_sbs_row(shipment_id=1, orderDate="1403/11/07"))

    # 1403/11/07 is 2025-01-26 on the Gregorian calendar.
    assert order.created_at.year == 2025
    assert order.created_at.month == 1
    assert order.created_at.day == 26


def test_normalize_sbs_row_missing_order_date_falls_back_to_now(repo):
    adapter = DigikalaAdapter(config=_CFG, repository=repo)
    row = _sbs_row(shipment_id=1)
    row.pop("orderDate")
    before = datetime.now(timezone.utc)
    order = adapter._normalize_sbs_row(row)
    assert order.created_at >= before


# --- fetch_order_detail --------------------------------------------------

@respx.mock
def test_fetch_order_detail_uses_single_shipment_endpoint(repo):
    respx.get(f"{_SBS_URL}/42").mock(
        return_value=httpx.Response(200, json={"status": "ok", "data": _sbs_row(shipment_id=42)})
    )

    adapter = DigikalaAdapter(config=_CFG, repository=repo)
    order = adapter.fetch_order_detail("42")

    assert order.source_order_id == "42"
    assert len(order.items) == 1


@respx.mock
def test_fetch_order_detail_supports_items_wrapped_shape(repo):
    """fetch_shipment_details observed a different real-payload shape
    ({"items": [...]}) for this same endpoint - fetch_order_detail must
    not break on it."""
    respx.get(f"{_SBS_URL}/42").mock(
        return_value=httpx.Response(
            200, json={"status": "ok", "data": {"items": [_sbs_row(shipment_id=42)]}}
        )
    )

    adapter = DigikalaAdapter(config=_CFG, repository=repo)
    order = adapter.fetch_order_detail("42")

    assert order.source_order_id == "42"


@respx.mock
def test_fetch_order_detail_raises_when_shipment_not_found(repo):
    respx.get(f"{_SBS_URL}/999").mock(
        return_value=httpx.Response(200, json={"status": "ok", "data": {}})
    )

    adapter = DigikalaAdapter(config=_CFG, repository=repo)
    with pytest.raises(ValueError, match="999"):
        adapter.fetch_order_detail("999")


# --- auto-confirm pending orders (client request, 2026-09) ---------------
# A "pending" row (سفارش جدید) has no customer data in the real panel until
# the seller confirms it (-> "processing", در حال پردازش). These tests
# cover _confirm_if_pending() and its two call sites.

_UPDATE_STATUS_URL = f"{_SBS_URL}/update-status"


def _pending_row(shipment_id, next_status="processing", verification_code="4242", **overrides):
    row = _sbs_row(
        shipment_id=shipment_id,
        status={"text": "pending"},
        nextStatus=next_status,
        verificationCode=verification_code,
        customer_name=None,
        customer_address=None,
        customer_postal_code=None,
        customer_phone_number=None,
    )
    row.update(overrides)
    return row


@respx.mock
def test_fetch_new_orders_confirms_pending_row_before_normalizing(repo):
    """A pending row must be confirmed via PUT update-status (using its
    own nextStatus/verificationCode) and re-fetched BEFORE normalization,
    so the resulting NormalizedOrder carries the post-confirm status and
    customer data - not the pending row's null fields."""
    repo.set_last_shipment_id("digikala", 0)
    respx.get(_SBS_URL).mock(
        return_value=_sbs_list_response([_pending_row(shipment_id=1)])
    )
    update_route = respx.put(_UPDATE_STATUS_URL).mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )
    respx.get(f"{_SBS_URL}/1").mock(
        return_value=httpx.Response(
            200, json={"status": "ok", "data": _sbs_row(shipment_id=1)}  # status=processing, full customer data
        )
    )

    adapter = DigikalaAdapter(config=_CFG, repository=repo)
    orders = adapter.fetch_new_orders(since=None)

    assert len(orders) == 1
    order = orders[0]
    assert order.status == "processing"
    assert order.customer_full_name == "علی علیایی"
    assert order.customer_mobile == "09121212121"
    assert order.customer_address == "تهران، تهران، ونک، خدامی"

    assert str(update_route.calls[0].request.url) == _UPDATE_STATUS_URL
    sent_body = json.loads(update_route.calls[0].request.content)
    assert sent_body == {
        "order_shipment_id": 1,
        "new_status": "processing",
        "verification_code": 4242,
    }


@respx.mock
def test_fetch_new_orders_skips_confirm_for_non_pending_rows(repo):
    """processing/processed/edited/rejected/cancelled rows must never
    trigger an update-status call - there's nothing to confirm."""
    repo.set_last_shipment_id("digikala", 0)
    respx.get(_SBS_URL).mock(
        return_value=_sbs_list_response([_sbs_row(shipment_id=1, status={"text": "processing"})])
    )
    update_route = respx.put(_UPDATE_STATUS_URL).mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )

    adapter = DigikalaAdapter(config=_CFG, repository=repo)
    orders = adapter.fetch_new_orders(since=None)

    assert len(orders) == 1
    assert update_route.call_count == 0


@respx.mock
def test_confirm_uses_next_status_and_falls_back_to_processing(repo):
    """`new_status` must come from the row's own `nextStatus` field when
    present, since that's Digikala's documented "what can this shipment
    become next" value - not a hardcoded "processing"."""
    repo.set_last_shipment_id("digikala", 0)
    respx.get(_SBS_URL).mock(
        return_value=_sbs_list_response([_pending_row(shipment_id=1, next_status="edited")])
    )
    update_route = respx.put(_UPDATE_STATUS_URL).mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )
    respx.get(f"{_SBS_URL}/1").mock(
        return_value=httpx.Response(200, json={"status": "ok", "data": _sbs_row(shipment_id=1)})
    )

    adapter = DigikalaAdapter(config=_CFG, repository=repo)
    adapter.fetch_new_orders(since=None)

    body = json.loads(update_route.calls[0].request.content)
    assert body["new_status"] == "edited"


@respx.mock
def test_confirm_omits_verification_code_when_row_has_none(repo):
    repo.set_last_shipment_id("digikala", 0)
    respx.get(_SBS_URL).mock(
        return_value=_sbs_list_response([_pending_row(shipment_id=1, verification_code=None)])
    )
    update_route = respx.put(_UPDATE_STATUS_URL).mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )
    respx.get(f"{_SBS_URL}/1").mock(
        return_value=httpx.Response(200, json={"status": "ok", "data": _sbs_row(shipment_id=1)})
    )

    adapter = DigikalaAdapter(config=_CFG, repository=repo)
    adapter.fetch_new_orders(since=None)

    body = json.loads(update_route.calls[0].request.content)
    assert "verification_code" not in body


@respx.mock
def test_confirm_failure_falls_back_to_original_pending_row(repo):
    """If update-status itself fails (4xx/5xx/network), the order must
    still sync - with whatever data the pending row already had - rather
    than being lost or raising out of fetch_new_orders."""
    repo.set_last_shipment_id("digikala", 0)
    respx.get(_SBS_URL).mock(
        return_value=_sbs_list_response([_pending_row(shipment_id=1)])
    )
    respx.put(_UPDATE_STATUS_URL).mock(
        return_value=httpx.Response(400, json={"status": "error"})
    )
    refetch_route = respx.get(f"{_SBS_URL}/1")

    adapter = DigikalaAdapter(config=_CFG, repository=repo)
    orders = adapter.fetch_new_orders(since=None)

    assert len(orders) == 1
    assert orders[0].status == "pending"
    assert orders[0].customer_full_name is None
    assert not refetch_route.called, "must not attempt re-fetch when confirm itself failed"


@respx.mock
def test_confirm_success_but_refetch_failure_falls_back_to_pending_row(repo):
    """If update-status succeeds but the follow-up re-fetch fails, the
    pre-confirmation row must still be used rather than blowing up the
    whole poll."""
    repo.set_last_shipment_id("digikala", 0)
    respx.get(_SBS_URL).mock(
        return_value=_sbs_list_response([_pending_row(shipment_id=1)])
    )
    respx.put(_UPDATE_STATUS_URL).mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )
    respx.get(f"{_SBS_URL}/1").mock(return_value=httpx.Response(500, json={"status": "error"}))

    adapter = DigikalaAdapter(config=_CFG, repository=repo)
    orders = adapter.fetch_new_orders(since=None)

    assert len(orders) == 1
    assert orders[0].status == "pending"


@respx.mock
def test_fetch_order_detail_also_confirms_pending_row(repo):
    """fetch_order_detail (used by the retry path) must apply the same
    auto-confirm as fetch_new_orders, since a shipment can still be
    pending the first time it's fetched through this path."""
    respx.get(f"{_SBS_URL}/1").mock(
        side_effect=[
            httpx.Response(200, json={"status": "ok", "data": _pending_row(shipment_id=1)}),
            httpx.Response(200, json={"status": "ok", "data": _sbs_row(shipment_id=1)}),
        ]
    )
    update_route = respx.put(_UPDATE_STATUS_URL).mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )

    adapter = DigikalaAdapter(config=_CFG, repository=repo)
    order = adapter.fetch_order_detail("1")

    assert order.status == "processing"
    assert order.customer_full_name == "علی علیایی"
    assert update_route.call_count == 1


@respx.mock
def test_confirm_skips_cancelled_pending_row(repo):
    """isCancelled=True must win over a "pending" status.text, same
    precedence as _normalize_sbs_row's own status mapping (Decision 3) -
    a cancelled order is never confirmed."""
    repo.set_last_shipment_id("digikala", 0)
    respx.get(_SBS_URL).mock(
        return_value=_sbs_list_response(
            [_pending_row(shipment_id=1, isCancelled=True)]
        )
    )
    update_route = respx.put(_UPDATE_STATUS_URL).mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )

    adapter = DigikalaAdapter(config=_CFG, repository=repo)
    orders = adapter.fetch_new_orders(since=None)

    assert len(orders) == 1
    assert orders[0].status == "cancelled"
    assert update_route.call_count == 0


# --- auth / token lifecycle (unaffected by the SBS migration) -----------

@respx.mock
def test_expired_access_token_triggers_refresh_and_retry(tmp_path, repo):
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
    repo.set_last_shipment_id("digikala", 100)  # skip cold-start seeding for this test
    adapter = DigikalaAdapter(config=cfg, repository=repo)
    adapter._token_cache_path = tmp_path / "digikala_tokens.json"  # isolate from real cache

    sbs_route = respx.get(_SBS_URL)
    sbs_route.mock(
        side_effect=[
            httpx.Response(401, json={"status": "error", "message": "token expired"}),
            _sbs_list_response([]),
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
    assert sbs_route.call_count == 2
    # The retried request must use the freshly refreshed token, not the stale one.
    assert sbs_route.calls[1].request.headers["Authorization"] == "Bearer fresh-token"


def test_refreshed_tokens_are_persisted_and_reused_on_restart(tmp_path, repo):
    """
    A rotating refresh_token must survive a service restart - otherwise
    the *static* .env value goes stale after the first refresh and every
    future restart breaks auth permanently.
    """
    cache_path = tmp_path / "digikala_tokens.json"
    cfg = DigikalaConfig(base_url="https://seller.digikala.com",
                          access_token="seed-access", refresh_token="seed-refresh")

    adapter = DigikalaAdapter(config=cfg, repository=repo)
    adapter._token_cache_path = cache_path
    adapter._access_token = "fresh-token"
    adapter._refresh_token = "rotated-refresh-token"
    adapter._save_tokens()

    # Simulate a restart: a brand new adapter instance pointed at the same cache file.
    restarted = DigikalaAdapter(config=cfg, repository=repo)
    restarted._token_cache_path = cache_path
    access_token, refresh_token = restarted._load_tokens()

    assert access_token == "fresh-token"
    assert refresh_token == "rotated-refresh-token"


# --- SBS customer/shipment detail fallback endpoints (unchanged) --------

@respx.mock
def test_fetch_sbs_customer_details_returns_customer_data(repo):
    """fetch_sbs_customer_details calls the correct SBS endpoint and returns
    customer name and mobile."""
    route = respx.get(
        "https://seller.digikala.com/open-api/v1/ship-by-seller-orders/customer/12345"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "ok",
                "data": {
                    "name": "علی محمدی",
                    "phoneNumber": "09123456789",
                    "state": "تهران",
                    "city": "تهران",
                    "address": "خیابان ولیعصر",
                    "postalCode": "1234567890",
                },
            },
        )
    )

    adapter = DigikalaAdapter(config=_CFG, repository=repo)
    result = adapter.fetch_sbs_customer_details("12345")

    assert result["customer_full_name"] == "علی محمدی"
    assert result["customer_mobile"] == "09123456789"
    assert result["customer_province"] == "تهران"
    assert result["customer_city"] == "تهران"
    assert result["customer_address"] == "خیابان ولیعصر"
    assert result["customer_postal_code"] == "1234567890"
    assert route.called


@respx.mock
def test_fetch_sbs_customer_details_returns_none_on_failure(repo):
    """On any API error, fetch_sbs_customer_details returns both fields as None
    so the caller can fall back to a synthetic name."""
    respx.get(
        "https://seller.digikala.com/open-api/v1/ship-by-seller-orders/customer/99999"
    ).mock(return_value=httpx.Response(500, json={"status": "error"}))

    adapter = DigikalaAdapter(config=_CFG, repository=repo)
    result = adapter.fetch_sbs_customer_details("99999")

    assert result["customer_full_name"] is None
    assert result["customer_mobile"] is None
    assert result["customer_province"] is None
    assert result["customer_city"] is None
    assert result["customer_address"] is None
    assert result["customer_postal_code"] is None


@respx.mock
def test_fetch_shipment_details_returns_tracking_code_and_shipping_cost(repo):
    """fetch_shipment_details calls the confirmed /ship-by-seller-orders/
    {shipment_id} endpoint and extracts trackingCode + shippingCost from
    the first item - using the real payload shape the client supplied
    (2026-09)."""
    route = respx.get(
        "https://seller.digikala.com/open-api/v1/ship-by-seller-orders/1"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "ok",
                "data": {
                    "items": [
                        {
                            "orderId": 1,
                            "shipmentId": 1,
                            "trackingCode": "11234",
                            "shippingCost": 650000,
                        }
                    ],
                },
            },
        )
    )

    adapter = DigikalaAdapter(config=_CFG, repository=repo)
    result = adapter.fetch_shipment_details("1")

    assert result["tracking_code"] == "11234"
    assert result["shipping_cost"] == Decimal("650000")
    assert route.called


@respx.mock
def test_fetch_shipment_details_returns_none_when_no_items(repo):
    """An empty items list (e.g. an invalid shipment_id) must yield both
    fields as None, not an IndexError."""
    respx.get(
        "https://seller.digikala.com/open-api/v1/ship-by-seller-orders/999"
    ).mock(return_value=httpx.Response(200, json={"status": "ok", "data": {"items": []}}))

    adapter = DigikalaAdapter(config=_CFG, repository=repo)
    result = adapter.fetch_shipment_details("999")

    assert result["tracking_code"] is None
    assert result["shipping_cost"] is None


@respx.mock
def test_fetch_shipment_details_returns_none_on_failure(repo):
    """On any API error, fetch_shipment_details returns both fields as
    None so the caller can proceed without this data."""
    respx.get(
        "https://seller.digikala.com/open-api/v1/ship-by-seller-orders/2"
    ).mock(return_value=httpx.Response(500, json={"status": "error"}))

    adapter = DigikalaAdapter(config=_CFG, repository=repo)
    result = adapter.fetch_shipment_details("2")

    assert result["tracking_code"] is None
    assert result["shipping_cost"] is None