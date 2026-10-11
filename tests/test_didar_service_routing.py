from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock

from src.didar.service import DidarSyncService
from src.marketplaces.base import NormalizedOrder

_T = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)


def _order(source):
    import dataclasses
    fields = {f.name: f for f in dataclasses.fields(NormalizedOrder)}
    kwargs = {}
    for name, f in fields.items():
        if f.default is not dataclasses.MISSING or f.default_factory is not dataclasses.MISSING:
            continue
        t = str(f.type)
        kwargs[name] = (
            _T if "datetime" in t else Decimal(0) if "Decimal" in t else
            [] if "list" in t else 0 if "int" in t else "x"
        )
    kwargs.update(source=source, source_order_id="1", created_at=_T, ship_time=None, items=[])
    return NormalizedOrder(**kwargs)


def _svc():
    contacts, deals, acts = MagicMock(), MagicMock(), MagicMock()
    deals.find_existing_deal_id.return_value = None
    deals.create_deal.return_value = "deal-1"
    return DidarSyncService(contacts, deals, acts), acts


def test_digikala_ship_activity_advanced_one_day():
    for src in ("digikala", "digikala2"):
        svc, acts = _svc()
        svc.sync_order(_order(src))
        assert acts.create_post_sale_checklist.call_args.kwargs["ship_activity_advance"] == timedelta(days=1)


def test_other_sources_not_advanced():
    svc, acts = _svc()
    svc.sync_order(_order("basalam"))
    assert acts.create_post_sale_checklist.call_args.kwargs["ship_activity_advance"] is None


def _snapp(method, has_addr, source="snappshop"):
    import dataclasses
    return dataclasses.replace(_order(source), shipping_method=method, has_delivery_address=has_addr)


def test_snappshop_warehouse_orders_get_only_ship_activity():
    for method, has_addr in (("FBM", False), ("NORMAL", False)):
        svc, acts = _svc()
        svc.sync_order(_snapp(method, has_addr))
        acts.create_post_sale_checklist.assert_not_called()
        kw = acts.create_ship_only_activity.call_args.kwargs
        assert kw["deal_id"] == "deal-1"
        assert kw["due_date"] == _T + timedelta(days=2)


def test_snappshop_express_vendor_delivery_and_account2_keep_full_checklist():
    for order in (
        _snapp("EXPRESS", False),
        _snapp("NORMAL", True),            # vendor delivery (address populated)
        _snapp("NORMAL", None),            # unknown -> old behaviour
        _snapp("FBM", False, "snappshop2"),  # account 2 has no warehouse modes
    ):
        svc, acts = _svc()
        svc.sync_order(order)
        acts.create_ship_only_activity.assert_not_called()
        acts.create_post_sale_checklist.assert_called_once()


def test_snappshop_warehouse_kind_and_note_texts():
    from src.snappshop_order_type import (
        URGENT_WAREHOUSE, WAREHOUSE, snappshop_warehouse_kind,
    )
    assert snappshop_warehouse_kind(_snapp("FBM", False)) == URGENT_WAREHOUSE
    assert snappshop_warehouse_kind(_snapp("NORMAL", False)) == WAREHOUSE
    assert snappshop_warehouse_kind(_snapp("EXPRESS", False)) is None
    assert snappshop_warehouse_kind(_snapp("NORMAL", True)) is None
    assert snappshop_warehouse_kind(_snapp("FBM", False, "snappshop2")) is None
