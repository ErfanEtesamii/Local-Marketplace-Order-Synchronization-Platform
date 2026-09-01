"""Tests for src/shipping_fees.py - the fixed, client-specified Toman
shipping-fee display amounts for Digikala / Faraz Honar (client
request, 2026-09)."""
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

from src.marketplaces.base import NormalizedOrder, OrderItem
from src.shipping_fees import format_toman, shipping_fee_toman

_ORDER = NormalizedOrder(
    source="tapsishop",
    source_order_id="1",
    order_number="1",
    created_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
    total_price=Decimal("100000"),
    status="confirmed",
    items=[OrderItem(sku="A", title="A", quantity=1, unit_price=Decimal("100000"),
                      final_price=Decimal("100000"))],
)


def test_digikala_always_returns_flat_239_toman():
    order = replace(_ORDER, source="digikala")
    assert shipping_fee_toman(order) == Decimal("239")


def test_digikala_ignores_real_shipping_cost():
    order = replace(_ORDER, source="digikala", shipping_cost=Decimal("999999"))
    assert shipping_fee_toman(order) == Decimal("239")


def test_farazhonar_pishtaz_returns_225_toman():
    order = replace(_ORDER, source="farazhonar", shipping_method="پیشتاز")
    assert shipping_fee_toman(order) == Decimal("225")


def test_farazhonar_tipax_returns_250_toman():
    order = replace(_ORDER, source="farazhonar", shipping_method="تیپاکس")
    assert shipping_fee_toman(order) == Decimal("250")


def test_farazhonar_method_matching_is_normalized():
    """Arabic yeh/kaf variants and surrounding text must still match -
    see src/didar/category_mapping.py's _normalize_fa()."""
    order = replace(_ORDER, source="farazhonar", shipping_method="ارسال با پیشتاز پست")
    assert shipping_fee_toman(order) == Decimal("225")


def test_farazhonar_unknown_method_returns_none():
    order = replace(_ORDER, source="farazhonar", shipping_method="پست عادی")
    assert shipping_fee_toman(order) is None


def test_farazhonar_no_method_returns_none():
    order = replace(_ORDER, source="farazhonar", shipping_method=None)
    assert shipping_fee_toman(order) is None


def test_other_sources_return_none():
    for source in ("tapsishop", "basalam", "snappshop"):
        order = replace(_ORDER, source=source)
        assert shipping_fee_toman(order) is None


def test_format_toman_uses_ascii_digits_and_comma():
    assert format_toman(Decimal("239")) == "239"
    assert format_toman(Decimal("12500")) == "12,500"
