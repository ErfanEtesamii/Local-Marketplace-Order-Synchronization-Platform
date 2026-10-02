"""Telegram notification for Digikala warehouse (FBD) items (2026-10)."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock

from src.marketplaces.warehouse_base import WarehouseShipmentItem
from src.telegram import TelegramNotifier


def _item(**kw):
    base = dict(
        source="digikala_warehouse",
        source_shipment_id="1166319283",
        product_title="جعبه دستمال کاغذی",
        quantity=2,
        unit_price=Decimal("3000000"),
        created_at=datetime(2026, 9, 30, 23, 30, tzinfo=timezone(timedelta(hours=3, minutes=30))),
        order_id="377721560",
    )
    base.update(kw)
    return WarehouseShipmentItem(**base)


def test_message_contents():
    text = TelegramNotifier()._format_new_warehouse_message(_item())
    assert "ارسال به انبار دیجی‌کالا" in text
    assert "جعبه دستمال کاغذی" in text
    assert "3,000,000 ریال × 2" in text
    assert "6,000,000 ریال" in text
    assert "377721560" in text
    assert "۲۳:۳۰" in text  # Iran-local time, Persian digits
    assert text.endswith("#ارسال_به_انبار_دیجی‌کالا")


def test_message_without_order_id():
    text = TelegramNotifier()._format_new_warehouse_message(_item(order_id=None))
    assert "شماره سفارش" not in text


def test_notify_delivers_with_stable_ref_id():
    n = TelegramNotifier()
    n.is_configured = MagicMock(return_value=True)
    n._deliver = MagicMock()
    repo = MagicMock()
    n.notify_new_warehouse_shipment(_item(), "deal-1", repo)
    ref_id = n._deliver.call_args[0][0]
    assert ref_id == "warehouse:digikala_warehouse:1166319283"


def test_notify_queues_when_unreachable_but_credentials_present():
    n = TelegramNotifier()
    n.is_configured = MagicMock(return_value=False)
    n._has_credentials = MagicMock(return_value=True)
    repo = MagicMock()
    n.notify_new_warehouse_shipment(_item(), "deal-1", repo)
    repo.record_notification_failure.assert_called_once()


def test_notify_noop_without_credentials():
    n = TelegramNotifier()
    n.is_configured = MagicMock(return_value=False)
    n._has_credentials = MagicMock(return_value=False)
    repo = MagicMock()
    n.notify_new_warehouse_shipment(_item(), "deal-1", repo)
    repo.record_notification_failure.assert_not_called()


def test_notify_never_raises():
    n = TelegramNotifier()
    n.is_configured = MagicMock(side_effect=RuntimeError("boom"))
    n.notify_new_warehouse_shipment(_item(), "deal-1", MagicMock())
