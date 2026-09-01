"""
Tests for the Telegram notification feature.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from src.db.repository import Repository
from src.marketplaces.base import NormalizedOrder, OrderItem
from src.telegram import TelegramNotifier


def _order_with_items(
    source: str,
    order_id: str,
    total: str = "100000",
    shipping_cost: str = "0"
) -> NormalizedOrder:
    """Helper to create a NormalizedOrder with items for testing."""
    return NormalizedOrder(
        source=source,
        source_order_id=order_id,
        order_number=order_id,
        created_at=datetime.now(timezone.utc),
        total_price=Decimal(total),
        status="confirmed",
        items=[OrderItem(
            sku="TEST001",
            title="Test Product",
            quantity=2,
            unit_price=Decimal("50000"),
            final_price=Decimal("50000")
        )],
        shipping_cost=Decimal(shipping_cost)
    )


def test_telegram_notifier_is_configured_when_credentials_set(monkeypatch):
    """Test that is_configured returns True when credentials are properly set."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "775753176")

    notifier = TelegramNotifier()
    # Bot.get_me is a coroutine function (python-telegram-bot v20+); patch()
    # auto-detects that and uses an AsyncMock, so setting return_value here
    # is what the mock resolves to once is_configured() actually awaits it.
    with patch('telegram.Bot.get_me') as mock_get_me:
        mock_get_me.return_value = MagicMock()
        assert notifier.is_configured() is True
        mock_get_me.assert_called_once()


def test_telegram_notifier_is_configured_false_when_missing_token(monkeypatch):
    """Test that is_configured returns False when bot token is missing."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "775753176")

    notifier = TelegramNotifier()
    assert notifier.is_configured() is False


def test_telegram_notifier_is_configured_false_when_missing_chat_id(monkeypatch):
    """Test that is_configured returns False when chat ID is missing."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    notifier = TelegramNotifier()
    assert notifier.is_configured() is False


def test_telegram_notifier_is_configured_false_when_invalid_chat_id(monkeypatch):
    """Test that is_configured returns False when chat ID is not numeric."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "not_a_number")

    notifier = TelegramNotifier()
    assert notifier.is_configured() is False


def test_telegram_notifier_is_configured_false_when_bot_unreachable(monkeypatch):
    """Test that is_configured returns False when bot cannot be reached."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "775753176")

    notifier = TelegramNotifier()
    # Bot.get_me is a coroutine function (python-telegram-bot v20+), so
    # patch() auto-detects that and uses an AsyncMock - side_effect fires
    # when the code actually awaits it (via asyncio.run() in
    # is_configured()), same as it would against the real Telegram API.
    with patch('telegram.Bot.get_me') as mock_get_me:
        from telegram.error import TelegramError
        mock_get_me.side_effect = TelegramError("Bot unreachable")
        assert notifier.is_configured() is False


def test_format_new_order_message_creates_proper_persian_rtl():
    """Test that _format_new_order_message creates properly formatted Persian RTL message."""
    notifier = TelegramNotifier()

    order = _order_with_items("digikala", "12345", "250000", "30000")

    # We'll test the private method by patching is_configured to return True
    with patch.object(notifier, 'is_configured', return_value=True):
        message = notifier._format_new_order_message(order, "deal-12345")

        # Check that key components are present
        assert "🆕 *سفارش جدید در دیدار*" in message
        assert "فروشگاه: دیجی‌کالا 📦" in message  # platform name with emoji
        assert "`12345`" in message  # order ID
        assert "`deal-12345`" in message  # deal ID
        assert "📦 تعداد آیتم: ۲" in message  # Persian digit for quantity
        assert "💰 مبلغ کل: *۲۵۰٬۰۰۰*" in message  # Persian formatted price
        assert "🚚 هزینه ارسال: ۳۰٬۰۰۰" in message  # shipping cost line
        assert "📋 *اقلام سفارش:*" in message
        assert "▫️ Test Product × ۲ = ۵۰٬۰۰۰ ریال" in message


def test_format_new_order_message_handles_zero_shipping():
    """Test that _format_new_order_message hides shipping line when cost is zero."""
    notifier = TelegramNotifier()

    order = _order_with_items("basalam", "67890", "150000", "0")

    with patch.object(notifier, 'is_configured', return_value=True):
        message = notifier._format_new_order_message(order, "deal-67890")

        # Shipping line should not appear when cost is 0
        assert "🚚 هزینه ارسال" not in message
        assert "💰 مبلغ کل: *۱۵۰٬۰۰۰*" in message


def test_escape_md_handles_special_characters():
    """Test that _escape_md properly escapes Telegram MarkdownV2 special characters."""
    notifier = TelegramNotifier()

    # Test string with all special characters that need escaping
    # Backtick ` is intentionally NOT escaped - used for inline code blocks in MarkdownV2
    test_string = r"_*[]()~`>#+-=|{}.!"
    escaped = notifier._escape_md(test_string)

    # Per Telegram's MarkdownV2 spec, these chars MUST be escaped:
    # ! # * + - . [ ] ( ) ~ > | = { } % \
    # Backtick ` is NOT in the list - it's used for inline code blocks
    expected = r'_\*\[\]\(\)\~`\>\#\+\-\=\|\{\}\.\!'
    assert escaped == expected


def test_format_price_converts_to_persian_digits_and_separators():
    """Test that _format_price correctly formats numbers with Persian digits and separators."""
    notifier = TelegramNotifier()

    # Test various amounts
    assert notifier._format_rial("1250000") == "۱٬۲۵۰٬۰۰۰"
    assert notifier._format_rial(1000) == "۱٬۰۰۰"
    assert notifier._format_rial(0) == "۰"
    assert notifier._format_rial("999999999") == "۹۹۹٬۹۹۹٬۹۹۹"


def test_format_jalali_date_handles_none_and_valid_dates():
    """Test that _format_jalali_date handles None and valid dates correctly."""
    notifier = TelegramNotifier()

    # Test None case
    assert notifier._format_jalali_date(None) == "نامشخص"

    # Test valid date (we'll check it returns a string, exact format depends on system locale)
    test_date = datetime(2026, 8, 31, 10, 30, 0, tzinfo=timezone.utc)
    result = notifier._format_jalali_date(test_date)
    assert isinstance(result, str)
    assert len(result) > 0
    assert result != "نامشخص"


def test_to_persian_digits_converts_ascii_to_persian():
    """Test that _to_persian_digits converts ASCII digits to Persian digits."""
    notifier = TelegramNotifier()

    assert notifier._to_persian_digits("0123456789") == "۰۱۲۳۴۵۶۷۸۹"
    assert notifier._to_persian_digits("Test 123 abc 456") == "Test ۱۲۳ abc ۴۵۶"
    assert notifier._to_persian_digits("No digits here!") == "No digits here!"
    assert notifier._to_persian_digits("") == ""


def test_notify_new_order_actually_sends_message(monkeypatch):
    """Regression test for the coroutine-never-awaited bug: before the fix,
    self._bot.send_message(...) built a coroutine and immediately discarded
    it without ever calling Telegram's API - notify_new_order would report
    success while sending nothing. Bot.send_message is a coroutine function,
    so patch() auto-detects that and uses an AsyncMock; asserting it was
    called only proves something if the code actually awaits it."""
    notifier = TelegramNotifier()
    notifier._bot = MagicMock()
    notifier._chat_id = 775753176

    order = _order_with_items("digikala", "12345", "250000")

    with patch.object(notifier, "is_configured", return_value=True), \
         patch.object(notifier._bot, "send_message") as mock_send:
        notifier.notify_new_order(order, "deal-12345")

    mock_send.assert_called_once()
    _, kwargs = mock_send.call_args
    assert kwargs["chat_id"] == 775753176
    assert "12345" in kwargs["text"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])