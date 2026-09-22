"""Tests for src/shipping_fees.py. The fixed, client-specified
shipping-fee display amounts (originally client requests, 2026-09) for
Digikala and Faraz Honar have both been REMOVED (2026-09 - see the
module docstring): shipping_fee_toman()/shipping_fee_rial() now always
return None, for every source, so the two display call sites
(src/didar/deal_client.py, src/telegram.py) always fall back to the
real order.shipping_cost each adapter already reads from its own
API."""
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

_ALL_SOURCES = ("tapsishop", "basalam", "snappshop", "snappshop2", "digikala",
                "digikala2", "farazhonar")


def test_shipping_fee_toman_is_always_none():
    """No source has a fixed display fee anymore - every source's real
    shipping_cost is used instead (Digikala: SBS shippingCost; Basalam:
    vendor-parcels shipping_cost; Tapsi Shop: shipments[].
    operationalCost; Faraz Honar: WooCommerce shipping_total; SnappShop
    has no such field at all, so its order.shipping_cost stays None
    same as always)."""
    for source in _ALL_SOURCES:
        order = replace(_ORDER, source=source, shipping_cost=Decimal("999999"))
        assert shipping_fee_toman(order) is None


def test_farazhonar_shipping_method_no_longer_affects_the_fee():
    """REMOVED 2026-09: Faraz Honar's flat Pishtaz/Tipax fee is gone -
    shipping_method must not resurrect it."""
    for method in ("پیشتاز", "تیپاکس", "ارسال با پیشتاز پست", "پست عادی", None):
        order = replace(_ORDER, source="farazhonar", shipping_method=method)
        assert shipping_fee_toman(order) is None


def test_shipping_fee_rial_is_always_none():
    for source in _ALL_SOURCES:
        order = replace(_ORDER, source=source, shipping_cost=Decimal("999999"))
        assert shipping_fee_rial(order) is None


def test_format_toman_uses_ascii_digits_and_comma():
    assert format_toman(Decimal("239000")) == "239,000"
    assert format_toman(Decimal("12500")) == "12,500"