"""
Tests for src/express_alert.py - the single source-agnostic answer to
"is this order an EXPRESS shipment?".

Two layers, deliberately kept apart in this file:

  1. is_express_order() on a hand-built NormalizedOrder - the keyword /
     normalization / never-guess contract itself, independent of any
     marketplace.
  2. Each adapter's own normalization step feeding that contract - the
     part that would silently regress if someone changed a payload
     field name: Basalam's shipping_method.default.title, SnappShop's
     delivery_type, Digikala's and Digikala2's isDigiExpress, and
     Tapsi Shop, which has no such field at all.

Layer 2 goes through the real _normalize_* methods (pure row ->
NormalizedOrder transforms, no network - same way tests/test_digikala2.py
calls _normalize_sbs_row directly) rather than asserting on
shipping_method in isolation, because the thing worth locking in is the
whole path from raw payload to express/not-express, not the helper in
the middle.

SOURCE ISOLATION is asserted explicitly here (see the Digikala/Tapsi Shop
sections): a source that does not report a shipping method must come out
as "not express", never as an accidental True inherited from another
source's conventions, and never as a raised exception.

The SMS side of this feature - ModirPayamakNotifier, the dedup guard and
the retry queue - is covered in tests/test_modir_payamak.py; nothing in
this file sends anything or touches the Repository.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.config import BasalamConfig, DigikalaConfig, SnappShopConfig, TapsiShopConfig
from src.db.repository import Repository
from src.express_alert import is_express_order
from src.marketplaces.base import NormalizedOrder, OrderItem
from src.marketplaces.basalam import BasalamAdapter
from src.marketplaces.digikala import DigikalaAdapter
from src.marketplaces.digikala2 import Digikala2Adapter
from src.marketplaces.snappshop import SnappShopAdapter
from src.marketplaces.tapsishop import TapsiShopAdapter

# Pinned explicitly rather than relying on each Config's own default:
# src/config.py calls load_dotenv() at import time, so an unset field
# would silently inherit whatever the developer's local .env happens to
# hold - same reasoning as tests/test_basalam.py's _CFG.price_unit.
_BASALAM_CFG = BasalamConfig(
    base_url="https://order-processing.basalam.com",
    access_token="test-pat",
    price_unit="toman",
)
_SNAPP_CFG = SnappShopConfig(
    base_url="https://apix.snappshop.ir",
    auth_token="test-token",
    agent_user="agent-123",
    vendor_id="v1",
    price_unit="toman",
)
_DIGIKALA_CFG = DigikalaConfig(
    base_url="https://seller.digikala.com", access_token="test-token"
)
_TAPSI_CFG = TapsiShopConfig(
    base_url="https://vendorgw.tapsi.shop", auth_token="test-token"
)

_ORDER = NormalizedOrder(
    source="basalam",
    source_order_id="1",
    order_number="1",
    created_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
    total_price=Decimal("100000"),
    status="confirmed",
    items=[
        OrderItem(
            sku="A",
            title="A",
            quantity=1,
            unit_price=Decimal("100000"),
            final_price=Decimal("100000"),
        )
    ],
)


@pytest.fixture
def repo(tmp_path):
    return Repository(db_path=str(tmp_path / "test.db"))


# ---------------------------------------------------------------------
# Layer 1 - is_express_order()'s own contract
# ---------------------------------------------------------------------

@pytest.mark.parametrize(
    "shipping_method",
    [
        "EXPRESS",
        "express",
        "  Express  ",
        "اکسپرس",
        "پست اکسپرس",
        "ارسال اکسپرس تهران",
        "3198",
    ],
)
def test_express_keywords_are_detected(shipping_method):
    """Each of the three documented keywords, on its own and embedded in
    a longer human-written title, and case-insensitively - the real
    values are free text, not an enum."""
    assert is_express_order(replace(_ORDER, shipping_method=shipping_method)) is True


def test_arabic_yeh_and_kaf_variants_still_match():
    """«اکسپرس» written with Arabic ي/ك instead of Persian ی/ک must still
    match, via _normalize_fa() - the same folding
    tests/test_shipping_fees.py locks in for its courier keywords."""
    assert is_express_order(replace(_ORDER, shipping_method="پست اكسپرس")) is True


def test_zwnj_inside_the_keyword_still_matches():
    """A zero-width non-joiner between words is a rendering detail of
    Persian text, not a different value - _normalize_fa() turns it into
    a space before matching."""
    assert is_express_order(replace(_ORDER, shipping_method="ارسال\u200cاکسپرس")) is True


def test_persian_and_arabic_indic_digits_match_the_numeric_code():
    """_fold_digits(): "۳۱۹۸" / "٣١٩٨" are the same delivery-type code as
    "3198", just in a different digit shape (defensive - no source has
    been observed sending it that way, see the module docstring)."""
    assert is_express_order(replace(_ORDER, shipping_method="۳۱۹۸")) is True
    assert is_express_order(replace(_ORDER, shipping_method="٣١٩٨")) is True


def test_non_string_shipping_method_does_not_raise():
    """SnappShop's delivery_type is a raw JSON value that may arrive as a
    number; is_express_order() str()s it rather than assuming text."""
    assert is_express_order(replace(_ORDER, shipping_method=3198)) is True


@pytest.mark.parametrize(
    "shipping_method",
    [None, "", "   ", "NORMAL", "normal", "پست پیشتاز", "تیپاکس", "پست عادی"],
)
def test_non_express_and_missing_values_return_false(shipping_method):
    """Never guess: an unknown, empty or absent shipping method is False,
    not an optimistic "probably express" and not an exception. Note
    "NORMAL" is Digikala's own sentinel for an explicitly-not-express
    shipment (stage 2) and must read as False here."""
    assert is_express_order(replace(_ORDER, shipping_method=shipping_method)) is False


def test_express_substring_of_an_unrelated_word_is_not_a_false_positive():
    """Sanity check that the keyword list is what is doing the work: a
    value that merely looks Persian-ish and shares no keyword must not
    match."""
    assert is_express_order(replace(_ORDER, shipping_method="پست سفارشی")) is False


# ---------------------------------------------------------------------
# Layer 2 - Basalam (shipping_method.default.title / .current.title)
# ---------------------------------------------------------------------

def _basalam_parcel(shipping_method: dict | None = None) -> dict:
    raw = {
        "id": 555,
        "total_items_price": 480000,
        "created_at": "2026-08-10T10:00:00Z",
        "status": {"id": 3739, "title": "جدید"},
        "order": {"id": 9001, "paid_at": "2026-08-10T09:59:00Z"},
        "estimate_send_at": "2026-08-12T18:00:00Z",
    }
    if shipping_method is not None:
        raw["shipping_method"] = shipping_method
    return raw


def test_basalam_express_from_default_title():
    adapter = BasalamAdapter(config=_BASALAM_CFG)
    order = adapter._normalize_list_item(
        _basalam_parcel({"default": {"title": "پست اکسپرس"}})
    )

    assert order.shipping_method == "پست اکسپرس"
    assert is_express_order(order) is True


def test_basalam_falls_back_to_current_title_when_default_absent():
    adapter = BasalamAdapter(config=_BASALAM_CFG)
    order = adapter._normalize_list_item(
        _basalam_parcel({"current": {"title": "ارسال اکسپرس"}})
    )

    assert order.shipping_method == "ارسال اکسپرس"
    assert is_express_order(order) is True


def test_basalam_detail_endpoint_normalizes_the_same_field():
    """_normalize_detail and _normalize_list_item share
    _basalam_shipping_method - both paths must agree, since an order can
    reach the SMS notifier through either."""
    adapter = BasalamAdapter(config=_BASALAM_CFG)
    raw = _basalam_parcel({"default": {"title": "پست اکسپرس"}})
    raw["items"] = []

    assert is_express_order(adapter._normalize_detail(raw)) is True


def test_basalam_non_express_title_is_not_express():
    adapter = BasalamAdapter(config=_BASALAM_CFG)
    order = adapter._normalize_list_item(
        _basalam_parcel({"default": {"title": "پست پیشتاز"}})
    )

    assert order.shipping_method == "پست پیشتاز"
    assert is_express_order(order) is False


def test_basalam_missing_shipping_method_stays_none():
    adapter = BasalamAdapter(config=_BASALAM_CFG)
    order = adapter._normalize_list_item(_basalam_parcel())

    assert order.shipping_method is None
    assert is_express_order(order) is False


# ---------------------------------------------------------------------
# Layer 2 - SnappShop (delivery_type)
# ---------------------------------------------------------------------

def _snapp_order(**overrides) -> dict:
    """Minimal variant of the v2.1.2 doc's order-detail sample (section
    2-3-2), the same shape tests/test_snappshop.py uses."""
    raw = {
        "order_number": "1216515253",
        "created_at": "2025-11-01 11:39:54",
        "order_status": "CONFIRMED",
        "item_origin": "VENDOR",
        "pickup_time": {"start": "2025-11-01 11:00:00", "end": "2025-11-01 18:00:00"},
        "customer": {"first_name": "مهسا", "last_name": "رضایی", "phone": None},
        "items": [
            {
                "sku": None,
                "product_number": 1564841615164,
                "item_status": "CONFIRMED",
                "quantity": 1,
                "canceled_quantity": 0,
                "final_price": 13799990,
            }
        ],
    }
    raw.update(overrides)
    return raw


def test_snappshop_express_delivery_type():
    adapter = SnappShopAdapter(config=_SNAPP_CFG)
    order = adapter._normalize_order(_snapp_order(delivery_type="EXPRESS"))

    assert order.shipping_method == "EXPRESS"
    assert is_express_order(order) is True


def test_snappshop_normal_delivery_type_is_not_express():
    adapter = SnappShopAdapter(config=_SNAPP_CFG)
    order = adapter._normalize_order(_snapp_order(delivery_type="NORMAL"))

    assert order.shipping_method == "NORMAL"
    assert is_express_order(order) is False


def test_snappshop_absent_delivery_type_stays_none():
    """delivery_type is documented only for GET /orders/ and
    GET /orders/{order_number}; when it is absent nothing is guessed."""
    adapter = SnappShopAdapter(config=_SNAPP_CFG)
    order = adapter._normalize_order(_snapp_order())

    assert order.shipping_method is None
    assert is_express_order(order) is False


# ---------------------------------------------------------------------
# Layer 2 - Digikala and Digikala2 (isDigiExpress)
#
# The two adapters are deliberately kept as independent copies of each
# other (project convention), so every case below runs against BOTH -
# a fix applied to only one of them fails here.
# ---------------------------------------------------------------------

_DIGIKALA_ADAPTERS = [
    pytest.param(DigikalaAdapter, id="digikala"),
    pytest.param(Digikala2Adapter, id="digikala2"),
]


def _sbs_row(**overrides) -> dict:
    """Minimal /ship-by-seller-orders row - same shape as
    tests/test_digikala2.py's helper, trimmed to the fields
    _normalize_sbs_row actually needs here."""
    row = {
        "orderId": 9,
        "shipmentId": 1,
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
                "price": 1000000,
                "count": 1,
            }
        ],
    }
    row.update(overrides)
    return row


@pytest.mark.parametrize("adapter_cls", _DIGIKALA_ADAPTERS)
def test_digikala_is_digi_express_true_is_express(adapter_cls, repo):
    adapter = adapter_cls(config=_DIGIKALA_CFG, repository=repo)
    order = adapter._normalize_sbs_row(_sbs_row(isDigiExpress=True))

    assert order.shipping_method == "EXPRESS"
    assert is_express_order(order) is True


@pytest.mark.parametrize("adapter_cls", _DIGIKALA_ADAPTERS)
def test_digikala_is_digi_express_false_is_not_express(adapter_cls, repo):
    """False is an explicit "not express" from the API - mapped to the
    "NORMAL" sentinel, which is distinguishable from "field absent" and
    must read as not-express."""
    adapter = adapter_cls(config=_DIGIKALA_CFG, repository=repo)
    order = adapter._normalize_sbs_row(_sbs_row(isDigiExpress=False))

    assert order.shipping_method == "NORMAL"
    assert is_express_order(order) is False


@pytest.mark.parametrize("adapter_cls", _DIGIKALA_ADAPTERS)
def test_digikala_missing_is_digi_express_key_stays_none(adapter_cls, repo):
    """A missing key means the API did not report it for this row - not
    "not express". It must stay None (never guessed, never thrown), and
    still be treated as not-express downstream."""
    adapter = adapter_cls(config=_DIGIKALA_CFG, repository=repo)
    order = adapter._normalize_sbs_row(_sbs_row())

    assert order.shipping_method is None
    assert is_express_order(order) is False


@pytest.mark.parametrize("adapter_cls", _DIGIKALA_ADAPTERS)
def test_digikala_neighbouring_digiexpress_fields_are_not_parsed(adapter_cls, repo):
    """digiexpress_ability / digiexpressData /
    isDigiexpressShippingServiceActive describe seller capability and
    config, not this shipment - they must never stand in for
    isDigiExpress."""
    adapter = adapter_cls(config=_DIGIKALA_CFG, repository=repo)
    order = adapter._normalize_sbs_row(
        _sbs_row(
            digiexpress_ability=True,
            digiexpressData={"enabled": True},
            isDigiexpressShippingServiceActive=True,
        )
    )

    assert order.shipping_method is None
    assert is_express_order(order) is False


# ---------------------------------------------------------------------
# Layer 2 - Tapsi Shop (no shipping-method field at all)
# ---------------------------------------------------------------------

def test_tapsishop_has_no_shipping_method_and_is_never_express():
    """Source isolation: Tapsi Shop's payload has no equivalent field, so
    its orders come out with shipping_method=None and are never express -
    the adapter is not expected to grow a field just because other
    sources have one."""
    adapter = TapsiShopAdapter(config=_TAPSI_CFG)
    order = adapter._normalize_list_item(
        {
            "id": 77,
            "orderNumber": "TS-77",
            "createdOn": "2026-08-10T10:00:00Z",
            "finalPrice": 480000,
            "stateTitle": "تایید شده",
        }
    )

    assert order.shipping_method is None
    assert is_express_order(order) is False
