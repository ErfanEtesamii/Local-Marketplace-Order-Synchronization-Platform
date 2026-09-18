"""
Tests for the Digikala FBD ("ارسال به انبار دیجی‌کالا") adapter.

Same conventions as tests/test_digikala.py: the config object is built
directly here rather than read from the environment (tests/conftest.py
only isolates TELEGRAM_*, and src.config.settings is a frozen singleton
built at import time), and the token cache path is pointed at tmp_path
so no test ever reads or overwrites the real data/digikala_tokens.json
that the live adapter shares.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
import respx

from src.config import DigikalaConfig
from src.db.repository import Repository
from src.marketplaces.digikala_warehouse import DigikalaWarehouseAdapter

_CFG = DigikalaConfig(base_url="https://seller.digikala.com", access_token="test-token")

_ORDERS_URL = "https://seller.digikala.com/open-api/v1/orders"

_FLOOR = datetime(2026, 9, 1, tzinfo=timezone.utc)
_NEWER = "2026-09-11T08:49:01.000000+03:30"
_OLDER = "2026-08-11T08:49:01.000000+03:30"


@pytest.fixture
def repo(tmp_path):
    return Repository(db_path=str(tmp_path / "test.db"))


def _adapter(repo, tmp_path, config=None):
    return DigikalaWarehouseAdapter(
        config=config or _CFG,
        repository=repo,
        token_cache_path=tmp_path / "digikala_tokens.json",
    )


def _row(order_item_id, created_at=_NEWER, **overrides):
    """One row shaped like the stage-0 probe's real payload."""
    row = {
        "order_item_id": order_item_id,
        "order_id": 382920341,
        "product_variant_title": "قاب بشقاب 25 میناکاری",
        "quantity": 2,
        "selling_price": 3000000,
        "total_price": 6000000,
        "order_created_at": created_at,
        "commitment_date": "2026-09-14T00:00:00.000000+03:30",
        "warehouse_status_at": "2026-09-11T09:00:00.000000+03:30",
        "supplier_code": "",
        "product_image_url": "https://dkstatics-public.digikala.com/example.jpg",
    }
    row.update(overrides)
    return row


def _list_response(items, page=1, total_pages=1):
    return httpx.Response(
        200,
        json={
            "status": "ok",
            "data": {
                "pager": {
                    "page": page,
                    "item_per_page": 50,
                    "total_pages": total_pages,
                    "total_rows": len(items),
                },
                "items": items,
            },
        },
    )


# --- cold start ------------------------------------------------------------

@respx.mock
def test_cold_start_seeds_the_floor_and_syncs_nothing(repo, tmp_path):
    """The whole point of the floor: on a fresh database the currently
    active FBD list is pre-existing backlog and must NOT be pushed to
    Didar (the 2026-08-31 "43 old orders" incident, in its FBD form)."""
    route = respx.get(_ORDERS_URL).mock(return_value=_list_response([_row(1), _row(2)]))

    items = _adapter(repo, tmp_path).fetch_new_warehouse_shipments(since=None)

    assert items == []
    assert repo.get_last_sync_time("digikala_warehouse") is not None
    # Not even fetched - there is nothing a first run could legitimately do
    # with the answer.
    assert route.call_count == 0


@respx.mock
def test_second_run_after_cold_start_returns_only_items_newer_than_the_floor(repo, tmp_path):
    respx.get(_ORDERS_URL).mock(
        return_value=_list_response([_row(11, created_at=_NEWER), _row(12, created_at=_OLDER)])
    )
    repo.set_last_sync_time("digikala_warehouse", _FLOOR)

    items = _adapter(repo, tmp_path).fetch_new_warehouse_shipments(since=None)

    assert [item.source_shipment_id for item in items] == ["11"]


@respx.mock
def test_floor_is_not_advanced_by_a_poll(repo, tmp_path):
    """Advancing the floor each poll would silently drop any item
    Digikala recorded a few seconds late - see the module docstring."""
    respx.get(_ORDERS_URL).mock(return_value=_list_response([_row(11)]))
    repo.set_last_sync_time("digikala_warehouse", _FLOOR)

    _adapter(repo, tmp_path).fetch_new_warehouse_shipments(since=None)

    assert repo.get_last_sync_time("digikala_warehouse") == _FLOOR


@respx.mock
def test_since_argument_is_ignored(repo, tmp_path):
    """`since` exists only for symmetry with fetch_new_orders - honouring
    it on top of the floor would drop items with no retry path."""
    respx.get(_ORDERS_URL).mock(return_value=_list_response([_row(11, created_at=_NEWER)]))
    repo.set_last_sync_time("digikala_warehouse", _FLOOR)

    items = _adapter(repo, tmp_path).fetch_new_warehouse_shipments(
        since=datetime.now(timezone.utc) + timedelta(days=30)
    )

    assert len(items) == 1


# --- normalization ---------------------------------------------------------

@respx.mock
def test_row_is_normalized_into_a_warehouse_shipment_item(repo, tmp_path):
    respx.get(_ORDERS_URL).mock(return_value=_list_response([_row(55123)]))
    repo.set_last_sync_time("digikala_warehouse", _FLOOR)

    item = _adapter(repo, tmp_path).fetch_new_warehouse_shipments(since=None)[0]

    assert item.source == "digikala_warehouse"
    assert item.source_shipment_id == "55123"
    assert item.product_title == "قاب بشقاب 25 میناکاری"
    assert item.quantity == 2
    assert item.order_id == "382920341"
    assert item.product_image_url == "https://dkstatics-public.digikala.com/example.jpg"
    assert item.created_at == datetime.fromisoformat("2026-09-11T08:49:01.000000+03:30")
    assert item.commitment_date == datetime.fromisoformat("2026-09-14T00:00:00.000000+03:30")


def test_price_unit_rial_is_not_converted(repo, tmp_path):
    adapter = _adapter(repo, tmp_path)
    item = adapter._normalize_row(_row(1))

    # selling_price is CONFIRMED per-unit; rial -> no conversion.
    assert item.unit_price == Decimal("3000000")


def test_price_unit_toman_is_converted_to_rial(repo, tmp_path):
    cfg = DigikalaConfig(
        base_url="https://seller.digikala.com", access_token="t", price_unit="toman"
    )
    adapter = _adapter(repo, tmp_path, config=cfg)

    item = adapter._normalize_row(_row(1))

    assert item.unit_price == Decimal("30000000")


def test_empty_supplier_code_becomes_none_not_empty_string(repo, tmp_path):
    """The probe saw supplier_code as "" on every sample row - that means
    "Digikala gave us nothing", not a code whose value is blank."""
    item = _adapter(repo, tmp_path)._normalize_row(_row(1, supplier_code=""))

    assert item.supplier_code is None


def test_supplier_code_is_kept_when_present(repo, tmp_path):
    item = _adapter(repo, tmp_path)._normalize_row(_row(1, supplier_code="25"))

    assert item.supplier_code == "25"


def test_missing_optional_fields_become_none_not_defaults(repo, tmp_path):
    row = _row(1)
    del row["commitment_date"]
    del row["product_image_url"]
    del row["order_id"]

    item = _adapter(repo, tmp_path)._normalize_row(row)

    assert item.commitment_date is None
    assert item.product_image_url is None
    assert item.order_id is None


@pytest.mark.parametrize(
    "missing_key",
    ["order_item_id", "product_variant_title", "quantity", "selling_price", "order_created_at"],
)
def test_row_missing_a_required_field_is_skipped_not_guessed(repo, tmp_path, missing_key):
    """No fabricated title/price/date ever reaches a real Didar deal -
    and no exception either, so one bad row can't break the poll."""
    row = _row(1)
    del row[missing_key]

    assert _adapter(repo, tmp_path)._normalize_row(row) is None


@respx.mock
def test_one_unusable_row_does_not_drop_the_good_ones(repo, tmp_path):
    bad = _row(2)
    del bad["selling_price"]
    respx.get(_ORDERS_URL).mock(return_value=_list_response([_row(1), bad, _row(3)]))
    repo.set_last_sync_time("digikala_warehouse", _FLOOR)

    items = _adapter(repo, tmp_path).fetch_new_warehouse_shipments(since=None)

    assert [item.source_shipment_id for item in items] == ["1", "3"]


# --- pagination ------------------------------------------------------------

@respx.mock
def test_pagination_continues_on_a_full_page_even_when_total_pages_says_zero(repo, tmp_path):
    """Same two-signal guard as the SBS fetch: this API reports
    total_pages=0 even with items present, so a full page is itself a
    reason to keep going."""
    repo.set_last_sync_time("digikala_warehouse", _FLOOR)
    route = respx.get(_ORDERS_URL)
    route.mock(
        side_effect=[
            _list_response([_row(i) for i in range(1, 51)], page=1, total_pages=0),
            _list_response([_row(51)], page=2, total_pages=0),
        ]
    )

    items = _adapter(repo, tmp_path).fetch_new_warehouse_shipments(since=None)

    assert route.call_count == 2
    assert len(items) == 51
    assert route.calls[0].request.url.params["sort"] == "order_created_at"
    assert route.calls[0].request.url.params["order"] == "desc"


@respx.mock
def test_pagination_stops_early_once_a_page_reaches_the_floor(repo, tmp_path):
    """Rows arrive newest-first, so the first page containing an item
    older than the floor is the last page worth fetching - even though
    it is a full page."""
    repo.set_last_sync_time("digikala_warehouse", _FLOOR)
    page_1 = [_row(i, created_at=_NEWER) for i in range(1, 50)] + [_row(50, created_at=_OLDER)]
    route = respx.get(_ORDERS_URL)
    route.mock(side_effect=[_list_response(page_1, page=1, total_pages=0)])

    items = _adapter(repo, tmp_path).fetch_new_warehouse_shipments(since=None)

    assert route.call_count == 1
    assert len(items) == 49


# --- auth ------------------------------------------------------------------

@respx.mock
def test_401_triggers_a_token_refresh_and_one_retry(repo, tmp_path):
    cfg = DigikalaConfig(
        base_url="https://seller.digikala.com",
        access_token="stale-token",
        refresh_token="my-refresh-token",
    )
    repo.set_last_sync_time("digikala_warehouse", _FLOOR)
    orders_route = respx.get(_ORDERS_URL)
    orders_route.mock(
        side_effect=[
            httpx.Response(401, json={"status": "error", "message": "token expired"}),
            _list_response([]),
        ]
    )
    refresh_route = respx.post(
        "https://seller.digikala.com/open-api/v1/auth/refresh-token"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "ok",
                "data": {"access_token": "fresh-token", "refresh_token": "new-refresh-token"},
            },
        )
    )

    items = _adapter(repo, tmp_path, config=cfg).fetch_new_warehouse_shipments(since=None)

    assert items == []
    assert refresh_route.called
    assert orders_route.call_count == 2
    assert orders_route.calls[1].request.headers["Authorization"] == "Bearer fresh-token"
    # Both tokens in the body, despite the endpoint's name - see the
    # adapter's _refresh_access_token().
    assert refresh_route.calls[0].request.content.decode().find("access_token") != -1


@respx.mock
def test_transport_errors_propagate_instead_of_looking_like_no_new_items(repo, tmp_path):
    """An empty list must only ever mean "Digikala has nothing new" -
    never "the request failed" - so the caller can log/retry."""
    repo.set_last_sync_time("digikala_warehouse", _FLOOR)
    respx.get(_ORDERS_URL).mock(return_value=httpx.Response(500, json={"status": "error"}))

    with pytest.raises(httpx.HTTPStatusError):
        _adapter(repo, tmp_path).fetch_new_warehouse_shipments(since=None)


def test_token_cache_file_is_shared_with_the_customer_order_adapter(repo, tmp_path):
    """Regression guard for the note in the adapter's docstring: a
    separate cache file would let one adapter's refresh invalidate the
    other's rotated refresh_token."""
    adapter = DigikalaWarehouseAdapter(config=_CFG, repository=repo)

    assert adapter._token_cache_path.name == "digikala_tokens.json"
