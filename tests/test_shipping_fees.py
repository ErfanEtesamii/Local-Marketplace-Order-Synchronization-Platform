"""Tests for src/shipping_fees.py - the fixed, client-specified
shipping-fee display amounts for Digikala / Faraz Honar (client
request, 2026-09; corrected 2026-09 to 1,000x the original figures -
see the module docstring). Toman feeds Didar's Description text,
Rial (shipping_fee_rial) feeds Telegram's display and grand total."""
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

from src.marketplaces.base import NormalizedOrder, OrderItem
from src.shipping_fees import format_toman, shipping_fee_rial, shipping_fee_toman

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


def test_digikala_always_returns_flat_239000_toman():
    order = replace(_ORDER, source="digikala")
    assert shipping_fee_toman(order) == Decimal("239000")


def test_digikala_ignores_real_shipping_cost():
    order = replace(_ORDER, source="digikala", shipping_cost=Decimal("999999"))
    assert shipping_fee_toman(order) == Decimal("239000")


def test_farazhonar_pishtaz_returns_225000_toman():
    order = replace(_ORDER, source="farazhonar", shipping_method="پیشتاز")
    assert shipping_fee_toman(order) == Decimal("225000")


def test_farazhonar_tipax_returns_250000_toman():
    order = replace(_ORDER, source="farazhonar", shipping_method="تیپاکس")
    assert shipping_fee_toman(order) == Decimal("250000")


def test_farazhonar_method_matching_is_normalized():
    """Arabic yeh/kaf variants and surrounding text must still match -
    see src/didar/category_mapping.py's _normalize_fa()."""
    order = replace(_ORDER, source="farazhonar", shipping_method="ارسال با پیشتاز پست")
    assert shipping_fee_toman(order) == Decimal("225000")


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
    assert format_toman(Decimal("239000")) == "239,000"
    assert format_toman(Decimal("12500")) == "12,500"


# ---------------------------------------------------------------------
# shipping_fee_rial() - Telegram's Rial equivalent of the same fee
# ---------------------------------------------------------------------

def test_digikala_rial_fee_is_toman_fee_times_ten():
    order = replace(_ORDER, source="digikala")
    assert shipping_fee_rial(order) == Decimal("2390000")


def test_farazhonar_pishtaz_rial_fee():
    order = replace(_ORDER, source="farazhonar", shipping_method="پیشتاز")
    assert shipping_fee_rial(order) == Decimal("2250000")


def test_farazhonar_tipax_rial_fee():
    order = replace(_ORDER, source="farazhonar", shipping_method="تیپاکس")
    assert shipping_fee_rial(order) == Decimal("2500000")


def test_rial_fee_is_none_when_toman_fee_is_none():
    order = replace(_ORDER, source="farazhonar", shipping_method="پست عادی")
    assert shipping_fee_rial(order) is None
