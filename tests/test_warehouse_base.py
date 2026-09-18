"""
Invariants of WarehouseShipmentItem (stage 1 of the Digikala FBD
feature - "ارسال به انبار دیجی‌کالا").

Small on purpose: this is a dataclass, not logic. What IS worth locking
down is the set of decisions encoded in its shape, because every other
stage of the feature depends on them - that optional fields default to
None (meaning "the API returned nothing", never zero/empty), that it is
frozen like NormalizedOrder, and that it deliberately carries no
customer field and no derived total_price.
"""
from dataclasses import FrozenInstanceError, fields, replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.marketplaces.warehouse_base import WarehouseShipmentItem


def _item(**overrides) -> WarehouseShipmentItem:
    base = dict(
        source="digikala_warehouse",
        source_shipment_id="55123",
        product_title="کالا",
        quantity=2,
        unit_price=Decimal("3000000"),
        created_at=datetime(2026, 9, 11, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return WarehouseShipmentItem(**base)


def test_optional_fields_default_to_none():
    """None means "Digikala did not return this for this row" - never a
    stand-in for zero, an empty string or a guessed default."""
    item = _item()

    assert item.product_image_url is None
    assert item.commitment_date is None
    assert item.order_id is None
    assert item.supplier_code is None


def test_the_item_is_frozen():
    item = _item()

    with pytest.raises(FrozenInstanceError):
        item.quantity = 5  # type: ignore[misc]


def test_replace_still_works_for_callers_that_need_a_modified_copy():
    item = replace(_item(), quantity=7)

    assert item.quantity == 7


def test_no_customer_field_exists_on_this_source():
    """The FBD endpoint exposes no customer of any kind - modelling one
    here would invite a fabricated Contact downstream (explicitly
    rejected by the client)."""
    names = {f.name for f in fields(WarehouseShipmentItem)}

    assert not [name for name in names if "customer" in name or "person" in name]


def test_no_total_price_field():
    """unit_price is CONFIRMED per-unit and total is derived
    (unit_price * quantity) - storing both invites the two silently
    drifting apart."""
    names = {f.name for f in fields(WarehouseShipmentItem)}

    assert "total_price" not in names


def test_normalized_order_is_untouched_by_this_feature():
    """warehouse_base.py exists precisely so base.py's contract for
    customer orders never grows FBD-only optional fields."""
    from src.marketplaces.base import NormalizedOrder

    names = {f.name for f in fields(NormalizedOrder)}

    assert "source_shipment_id" not in names
    assert "commitment_date" not in names
    assert "supplier_code" not in names
