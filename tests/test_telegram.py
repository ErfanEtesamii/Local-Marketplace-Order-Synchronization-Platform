"""
Tests for the Telegram notification feature (src/telegram.py).

Covers: per-order message formatting (Requirement 1), report-message
formatting and money aggregation (Requirements 2-4), the day/week/
month rollover detection that drives when reports fire, and the
plain-synchronous-HTTP send path + retry queue introduced in the
2026-09 rewrite (see src/telegram.py's module docstring for the
"orders sync to Didar but Telegram never fires" incident this
replaces the old asyncio/python-telegram-bot implementation for).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import httpx
import jdatetime
import pytest
import respx

from src.db.repository import Repository
from src.marketplaces.base import NormalizedOrder, OrderItem
from src.telegram import (
    IRAN_TZ,
    TelegramError,
    TelegramNotifier,
    _emoji_number,
    _format_rial,
    _iranian_weekday,
    _jalali_key,
)

# Same fake token used across every test below - real-looking shape
# (Telegram bot tokens are "<numeric id>:<35-char secret>") but never a
# real credential. Tests build the exact URL TelegramNotifier itself
# builds (`https://api.telegram.org/bot<token>/<method>`) and mock it
# with respx, the same way every other httpx-based client in this
# project is tested (see tests/test_didar_activity.py etc.) - nothing
# python-telegram-bot-specific is mocked anymore since src/telegram.py
# no longer depends on that package at all.
_TOKEN = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
_API = f"https://api.telegram.org/bot{_TOKEN}"


def _ok_get_me():
    return respx.post(f"{_API}/getMe").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"id": 1, "is_bot": True}})
    )


def _order_with_items(
    source: str,
    order_id: str,
    total: str = "100000",
    shipping_cost: str = "0",
    customer_full_name: str | None = "علی رضایی",
    items: list[OrderItem] | None = None,
    shipping_method: str | None = None,
) -> NormalizedOrder:
    return NormalizedOrder(
        source=source,
        source_order_id=order_id,
        order_number=order_id,
        created_at=datetime(2026, 8, 31, 15, 12, 0, tzinfo=timezone.utc),
        total_price=Decimal(total),
        status="confirmed",
        customer_full_name=customer_full_name,
        items=items if items is not None else [
            OrderItem(
                sku="TEST001",
                title="Test Product",
                quantity=2,
                unit_price=Decimal("50000"),
                final_price=Decimal("100000"),
            )
        ],
        shipping_cost=Decimal(shipping_cost),
        shipping_method=shipping_method,
    )


@pytest.fixture
def repo(tmp_path):
    return Repository(db_path=str(tmp_path / "test.db"))


# ---------------------------------------------------------------------
# is_configured()
# ---------------------------------------------------------------------

@respx.mock
def test_is_configured_true_with_numeric_chat_id(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", _TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "775753176")
    route = _ok_get_me()

    notifier = TelegramNotifier()
    assert notifier.is_configured() is True
    assert route.called


@respx.mock
def test_is_configured_true_with_channel_username(monkeypatch):
    """TELEGRAM_CHAT_ID as "@channel_username" must work, not just a
    numeric id - this was the exact bug flagged in review."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", _TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "@my_channel")
    _ok_get_me()

    notifier = TelegramNotifier()
    assert notifier.is_configured() is True
    assert notifier._chat_ids == ["@my_channel"]


@respx.mock
def test_is_configured_true_with_multiple_numbered_chat_ids(monkeypatch):
    """TELEGRAM_CHAT_ID_1..TELEGRAM_CHAT_ID_10 (plus the legacy single
    TELEGRAM_CHAT_ID) all merge into one recipient list."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", _TOKEN)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setenv("TELEGRAM_CHAT_ID_1", "111")
    monkeypatch.setenv("TELEGRAM_CHAT_ID_2", "@second_channel")
    monkeypatch.setenv("TELEGRAM_CHAT_ID_3", "333")
    _ok_get_me()

    notifier = TelegramNotifier()
    assert notifier.is_configured() is True
    assert notifier._chat_ids == [111, "@second_channel", 333]


@respx.mock
def test_is_configured_skips_invalid_recipient_but_keeps_valid_ones(monkeypatch):
    """One bad id among several must not disable the whole feature."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", _TOKEN)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setenv("TELEGRAM_CHAT_ID_1", "111")
    monkeypatch.setenv("TELEGRAM_CHAT_ID_2", "not_a_number")
    _ok_get_me()

    notifier = TelegramNotifier()
    assert notifier.is_configured() is True
    assert notifier._chat_ids == [111]


def test_is_configured_false_when_missing_token(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "775753176")
    assert TelegramNotifier().is_configured() is False


def test_is_configured_false_when_missing_chat_id(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", _TOKEN)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert TelegramNotifier().is_configured() is False


def test_is_configured_false_when_chat_id_invalid(monkeypatch):
    """Neither numeric nor "@..." -> config error, not a crash."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", _TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "not_a_number")
    assert TelegramNotifier().is_configured() is False


@respx.mock
def test_is_configured_false_when_bot_unreachable(monkeypatch):
    """A non-2xx/`ok:false` response from getMe (bad token, revoked
    credentials, etc.) must disable the feature, not crash it. Uses a
    401 (non-retryable per http_utils.is_retryable_http_error) so this
    fails immediately instead of sitting through default_retry's real
    exponential-backoff sleeps."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", _TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "775753176")
    respx.post(f"{_API}/getMe").mock(
        return_value=httpx.Response(401, json={"ok": False, "description": "Unauthorized"})
    )

    assert TelegramNotifier().is_configured() is False


# ---------------------------------------------------------------------
# Per-order message formatting (Requirement 1)
# ---------------------------------------------------------------------

def test_format_new_order_message_matches_exact_template():
    """Digikala's "هزینه ارسال" line shows the client's flat 2,390,000
    Rial fee (see src/shipping_fees.py: 239,000 Toman * 10), not the
    order's real shipping_cost (here 30,000 Rial) - client request,
    2026-09. The "مبلغ کل" grand total is products_total + this fee
    (100,000 + 2,390,000 = 2,490,000), not the order's own total_price
    (130,000)."""
    notifier = TelegramNotifier()
    order = _order_with_items(
        "digikala", "12345", total="130000", shipping_cost="30000",
        customer_full_name="علی رضایی",
    )

    message = notifier._format_new_order_message(order)

    # The date line's exact Jalali value is a calendar-conversion detail
    # of jdatetime itself, not this module's logic - compute it the same
    # way _format_jalali_datetime() does rather than hardcoding a literal
    # that could silently drift from whatever jdatetime actually returns.
    # 15:12 UTC + 03:30 (Iran, fixed offset) = 18:42 local, which IS this
    # module's own arithmetic and is safe to assert literally.
    local_date = order.created_at.astimezone(IRAN_TZ).date()
    jalali = jdatetime.date.fromgregorian(date=local_date)
    expected_date_str = notifier._to_persian_digits(f"{jalali.year:04d}/{jalali.month:02d}/{jalali.day:02d}")

    assert message == (
        "🟢 سفارش جدید ثبت شد\n"
        "🛍 پلتفرم: 🟣 دیجی‌کالا\n"
        "👤 مشتری:\n"
        "علی رضایی\n"
        "📦 محصولات:\n"
        "1️⃣ Test Product\n"
        "   └─ 50,000 ریال × 2\n"
        "🚚 هزینه ارسال:\n"
        "2,390,000 ریال\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "💰 مبلغ محصولات:\n"
        "100,000 ریال\n"
        "🚚 ارسال:\n"
        "2,390,000 ریال\n"
        "💳 مبلغ کل:\n"
        "2,490,000 ریال\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 {expected_date_str} — ۱۸:۴۲\n"
        "🟢 ثبت موفق در دیدار"
    )


def test_format_new_order_message_numbers_multiple_products():
    notifier = TelegramNotifier()
    items = [
        OrderItem(sku="A", title="Product A", quantity=1,
                  unit_price=Decimal("10000"), final_price=Decimal("10000")),
        OrderItem(sku="B", title="Product B", quantity=3,
                  unit_price=Decimal("20000"), final_price=Decimal("60000")),
    ]
    order = _order_with_items("basalam", "999", total="70000", items=items)

    message = notifier._format_new_order_message(order)

    assert "1️⃣ Product A" in message
    assert "   └─ 10,000 ریال × 1" in message
    assert "2️⃣ Product B" in message
    assert "   └─ 20,000 ریال × 3" in message
    assert "🛍 پلتفرم: 🟢 باسلام" in message


def test_format_new_order_message_missing_customer_name_shows_placeholder():
    notifier = TelegramNotifier()
    order = _order_with_items("tapsishop", "1", customer_full_name=None)

    message = notifier._format_new_order_message(order)

    assert "👤 مشتری:\nنامشخص\n" in message
    assert "🛍 پلتفرم: 🟠 تپسی‌شاپ" in message


def test_format_new_order_message_farazhonar_platform_emoji():
    notifier = TelegramNotifier()
    order = _order_with_items("farazhonar", "1")
    message = notifier._format_new_order_message(order)
    assert "🛍 پلتفرم: 🔵 فرازهنر" in message


# ---------------------------------------------------------------------
# Fixed shipping-fee override for Digikala / Faraz Honar
# (client request, 2026-09 - see src/shipping_fees.py). Telegram shows
# this fee in RIAL (Didar's Description text stays Toman) and the grand
# total is products_total + this fee, not order.total_price.
# ---------------------------------------------------------------------

def test_digikala_always_shows_flat_2390000_rial_shipping_fee():
    """Digikala shows the flat 2,390,000 Rial fee (239,000 Toman * 10)
    regardless of the order's real shipping_cost, and the grand total
    is products_total (100,000) + this fee."""
    notifier = TelegramNotifier()
    order = _order_with_items("digikala", "1", shipping_cost="999999")
    message = notifier._format_new_order_message(order)
    assert "🚚 هزینه ارسال:\n2,390,000 ریال\n" in message
    assert "💳 مبلغ کل:\n2,490,000 ریال\n" in message


def test_farazhonar_pishtaz_shows_2250000_rial_shipping_fee():
    notifier = TelegramNotifier()
    order = _order_with_items("farazhonar", "1", shipping_method="پیشتاز")
    message = notifier._format_new_order_message(order)
    assert "🚚 هزینه ارسال:\n2,250,000 ریال\n" in message
    assert "💳 مبلغ کل:\n2,350,000 ریال\n" in message


def test_farazhonar_tipax_shows_2500000_rial_shipping_fee():
    notifier = TelegramNotifier()
    order = _order_with_items("farazhonar", "1", shipping_method="تیپاکس")
    message = notifier._format_new_order_message(order)
    assert "🚚 هزینه ارسال:\n2,500,000 ریال\n" in message
    assert "💳 مبلغ کل:\n2,600,000 ریال\n" in message


def test_farazhonar_unknown_shipping_method_falls_back_to_real_cost():
    """An unrecognized courier must never be guessed as Pishtaz/Tipax -
    falls back to the order's real shipping_cost in Rial instead."""
    notifier = TelegramNotifier()
    order = _order_with_items(
        "farazhonar", "1", shipping_cost="40000", shipping_method="پست عادی",
    )
    message = notifier._format_new_order_message(order)
    assert "🚚 هزینه ارسال:\n40,000 ریال\n" in message


def test_other_platforms_unaffected_by_fixed_shipping_fee():
    """Tapsi Shop (and Basalam/SnappShop) have no fixed fee - keep
    showing the real shipping_cost (0 by default) in Rial."""
    notifier = TelegramNotifier()
    order = _order_with_items("tapsishop", "1")
    message = notifier._format_new_order_message(order)
    assert "🚚 هزینه ارسال:\n0 ریال\n" in message


def test_emoji_number_keycaps():
    assert _emoji_number(1) == "1️⃣"
    assert _emoji_number(9) == "9️⃣"
    assert _emoji_number(10) == "🔟"
    assert _emoji_number(11) == "1️⃣1️⃣"
    assert _emoji_number(23) == "2️⃣3️⃣"


def test_format_rial_uses_ascii_digits_and_comma():
    assert _format_rial("1250000") == "1,250,000"
    assert _format_rial(1000) == "1,000"
    assert _format_rial(0) == "0"
    assert _format_rial(None) == "0"
    assert _format_rial(Decimal("12500000")) == "12,500,000"


@respx.mock
def test_notify_new_order_actually_sends_message(monkeypatch, repo):
    """End-to-end regression test for the whole 2026-09 send path:
    the sendMessage HTTP call must actually go out with the right
    chat_id/text, and nothing should be queued for retry when it
    succeeds."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", _TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "775753176")
    _ok_get_me()
    send_route = respx.post(f"{_API}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {}})
    )

    notifier = TelegramNotifier()
    order = _order_with_items("digikala", "12345", "250000")
    notifier.notify_new_order(order, "deal-12345", repo)

    assert send_route.called
    body = json.loads(send_route.calls[0].request.content)
    assert body["chat_id"] == 775753176
    assert "سفارش جدید ثبت شد" in body["text"]
    # No parse_mode key - messages are sent as literal plain text so the
    # exact template can't be corrupted by Markdown escaping.
    assert "parse_mode" not in body
    assert repo.get_pending_notification_failures() == []


@respx.mock
def test_notify_new_deal_actually_sends_message(monkeypatch, repo):
    """New-deal notifications (client request, 2026-09 - every Deal
    registered in Didar, manual or automatic - see
    src/didar/deal_poller.py) go through the same send path as
    notify_new_order() and must actually reach the Telegram API."""
    from src.didar.deal_poller import NewDealInfo

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", _TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "775753176")
    _ok_get_me()
    send_route = respx.post(f"{_API}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {}})
    )

    notifier = TelegramNotifier()
    deal = NewDealInfo(
        deal_id="deal-1",
        code=4242,
        title="معامله علی رضایی",
        customer_name="علی رضایی",
        price=Decimal("250000"),
        owner_name="نگین عابدیان",
        stage_name="مذاکرات اولیه",
        register_time=datetime(2026, 9, 3, 8, 0, 0, tzinfo=timezone.utc),
    )
    notifier.notify_new_deal(deal, repo)

    assert send_route.called
    body = json.loads(send_route.calls[0].request.content)
    assert body["chat_id"] == 775753176
    text = body["text"]
    assert "معامله جدید در دیدار" in text
    assert "علی رضایی" in text
    assert "250,000" in text
    assert "نگین عابدیان" in text
    assert "مذاکرات اولیه" in text
    assert "4242" in text
    assert "parse_mode" not in body


def test_notify_new_deal_noops_when_not_configured(repo):
    from src.didar.deal_poller import NewDealInfo

    notifier = TelegramNotifier()
    deal = NewDealInfo(
        deal_id="deal-1", code=None, title="t", customer_name=None,
        price=Decimal("0"), owner_name=None, stage_name=None, register_time=None,
    )
    with patch.object(notifier, "is_configured", return_value=False), \
         patch.object(notifier, "_send") as mock_send:
        notifier.notify_new_deal(deal, repo)
    mock_send.assert_not_called()


@respx.mock
def test_send_fans_out_to_every_configured_chat_id():
    """A single _send() call must reach every recipient, not just one."""
    notifier = TelegramNotifier()
    notifier._client = httpx.Client(base_url=_API)
    notifier._chat_ids = [111, "@second_channel", 333]
    route = respx.post(f"{_API}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {}})
    )

    notifier._send("hello")

    assert route.call_count == 3
    sent_to = {json.loads(call.request.content)["chat_id"] for call in route.calls}
    assert sent_to == {111, "@second_channel", 333}


@respx.mock
def test_send_one_bad_recipient_does_not_block_the_others():
    """A blocked/kicked bot on one chat must not stop delivery to the
    rest of the recipients. Uses a 400 (non-retryable per
    http_utils.is_retryable_http_error) for the bad chat so this
    doesn't sit through default_retry's real exponential-backoff
    sleeps."""
    notifier = TelegramNotifier()
    notifier._client = httpx.Client(base_url=_API)
    notifier._chat_ids = [111, 222, 333]

    def _responder(request):
        body = json.loads(request.content)
        if body["chat_id"] == 222:
            return httpx.Response(
                400, json={"ok": False, "description": "bot was blocked by the user"}
            )
        return httpx.Response(200, json={"ok": True, "result": {}})

    route = respx.post(f"{_API}/sendMessage").mock(side_effect=_responder)

    notifier._send("hello")  # must not raise - 111 and 333 still succeeded

    assert route.call_count == 3


@respx.mock
def test_send_raises_only_when_every_recipient_fails():
    notifier = TelegramNotifier()
    notifier._client = httpx.Client(base_url=_API)
    notifier._chat_ids = [111, 222]
    respx.post(f"{_API}/sendMessage").mock(
        return_value=httpx.Response(400, json={"ok": False, "description": "boom"})
    )

    with pytest.raises(TelegramError):
        notifier._send("hello")


# ---------------------------------------------------------------------
# Report aggregation (Requirements 2-4)
# ---------------------------------------------------------------------

def test_repository_aggregates_amounts_for_reports(repo):
    repo.mark_synced("digikala", "1", "deal-1",
                      products_amount=Decimal("100000"),
                      shipping_amount=Decimal("20000"),
                      total_amount=Decimal("120000"))
    repo.mark_synced("digikala", "2", "deal-2",
                      products_amount=Decimal("50000"),
                      shipping_amount=Decimal("0"),
                      total_amount=Decimal("50000"))
    # Order synced before this feature existed - NULL amounts, must not
    # break aggregation or be treated as zero-inflating the count wrongly.
    repo.mark_synced("digikala", "3", "deal-3")

    since = datetime.now(timezone.utc) - timedelta(hours=1)
    products, shipping, total, count = repo.get_amount_stats_since("digikala", since)

    assert products == 150000
    assert shipping == 20000
    assert total == 170000
    assert count == 3


def test_repository_amount_stats_respects_until_bound(repo):
    repo.mark_synced("basalam", "1", "deal-1",
                      products_amount=Decimal("10000"),
                      shipping_amount=Decimal("0"),
                      total_amount=Decimal("10000"))

    far_future_since = datetime.now(timezone.utc) - timedelta(hours=1)
    far_past_until = datetime.now(timezone.utc) - timedelta(hours=1)

    _, _, _, count = repo.get_amount_stats_since("basalam", far_future_since, far_past_until)
    assert count == 0


def test_format_report_message_matches_expected_shape():
    notifier = TelegramNotifier()
    message = notifier._format_report_message(
        "📊 گزارش پایان روز", "📊 گزارش روزانه", "📅 شنبه ۱۴۰۵/۰۶/۰۷",
        products=1000000, shipping=50000, total=1050000, count=4,
    )

    assert message.startswith("📊 گزارش پایان روز\n╔")
    assert "📅 شنبه ۱۴۰۵/۰۶/۰۷" in message
    assert "└─ 4 سفارش" in message
    assert "└─ 1,000,000 ریال" in message  # products
    assert "└─ 50,000 ریال" in message      # shipping
    assert "└─ 1,050,000 ریال" in message   # total
    assert "└─ 262,500 ریال" in message     # average (1,050,000 / 4)
    assert message.endswith("🟢 همه سفارش‌ها با موفقیت\nدر دیدار ثبت شده‌اند.")


def test_format_report_message_handles_zero_orders():
    notifier = TelegramNotifier()
    message = notifier._format_report_message(
        "📊 گزارش پایان روز", "📊 گزارش روزانه", "📅 شنبه ۱۴۰۵/۰۶/۰۷",
        products=0, shipping=0, total=0, count=0,
    )
    assert "└─ 0 سفارش" in message
    assert "└─ 0 ریال" in message


# ---------------------------------------------------------------------
# Day/week/month rollover detection
# ---------------------------------------------------------------------

def test_daily_rollover_first_run_sets_marker_without_sending(repo):
    notifier = TelegramNotifier()
    today = jdatetime.date(1405, 6, 9)

    with patch.object(notifier, "_send_daily_report") as mock_send:
        notifier._check_daily_rollover(repo, ["digikala"], today)

    mock_send.assert_not_called()
    assert repo.get_report_marker("day") == _jalali_key(today)


def test_daily_rollover_same_day_does_not_resend(repo):
    notifier = TelegramNotifier()
    today = jdatetime.date(1405, 6, 9)
    repo.set_report_marker("day", _jalali_key(today))

    with patch.object(notifier, "_send_daily_report") as mock_send:
        notifier._check_daily_rollover(repo, ["digikala"], today)

    mock_send.assert_not_called()


def test_daily_rollover_sends_report_for_day_that_ended(repo):
    notifier = TelegramNotifier()
    yesterday = jdatetime.date(1405, 6, 9)
    today = jdatetime.date(1405, 6, 10)
    repo.set_report_marker("day", _jalali_key(yesterday))

    with patch.object(notifier, "_send_daily_report") as mock_send:
        notifier._check_daily_rollover(repo, ["digikala"], today)

    mock_send.assert_called_once_with(repo, ["digikala"], yesterday)
    assert repo.get_report_marker("day") == _jalali_key(today)


def test_weekly_rollover_fires_only_when_week_anchor_changes(repo):
    notifier = TelegramNotifier()
    # Derive this week's Saturday anchor and the following week's from an
    # arbitrary reference date, rather than hardcoding which real-world
    # weekday a specific Jalali date falls on - only jdatetime.date
    # arithmetic is relied on here, not its own weekday() convention
    # (see _iranian_weekday's docstring in src/telegram.py for why).
    reference = jdatetime.date(1405, 6, 15)
    this_saturday = reference - timedelta(days=_iranian_weekday(reference))
    last_day_of_week = this_saturday + timedelta(days=6)  # this week's Friday
    next_saturday = this_saturday + timedelta(days=7)

    with patch.object(notifier, "_send_weekly_report") as mock_send:
        notifier._check_weekly_rollover(repo, ["digikala"], last_day_of_week)
        mock_send.assert_not_called()  # first run - just sets the marker

        notifier._check_weekly_rollover(repo, ["digikala"], last_day_of_week)
        mock_send.assert_not_called()  # same week, no rollover yet

        notifier._check_weekly_rollover(repo, ["digikala"], next_saturday)
        mock_send.assert_called_once()


def test_monthly_rollover_fires_when_jalali_month_changes(repo):
    notifier = TelegramNotifier()
    end_of_month = jdatetime.date(1405, 6, 31)
    start_of_next_month = jdatetime.date(1405, 7, 1)

    with patch.object(notifier, "_send_monthly_report") as mock_send:
        notifier._check_monthly_rollover(repo, ["digikala"], end_of_month)
        mock_send.assert_not_called()

        notifier._check_monthly_rollover(repo, ["digikala"], start_of_next_month)
        mock_send.assert_called_once_with(repo, ["digikala"], jdatetime.date(1405, 6, 1))


def test_yearly_rollover_fires_when_jalali_year_changes(repo):
    notifier = TelegramNotifier()
    end_of_year = jdatetime.date(1405, 12, 29)
    start_of_next_year = jdatetime.date(1406, 1, 1)

    with patch.object(notifier, "_send_yearly_report") as mock_send:
        notifier._check_yearly_rollover(repo, ["digikala"], end_of_year)
        mock_send.assert_not_called()  # first run - just sets the marker

        notifier._check_yearly_rollover(repo, ["digikala"], end_of_year)
        mock_send.assert_not_called()  # same year, no rollover yet

        notifier._check_yearly_rollover(repo, ["digikala"], start_of_next_year)
        mock_send.assert_called_once_with(repo, ["digikala"], jdatetime.date(1405, 1, 1))


def test_send_yearly_report_noops_when_not_configured(repo, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    notifier = TelegramNotifier()

    with patch.object(notifier, "_send") as mock_send:
        notifier._send_yearly_report(repo, ["digikala"], jdatetime.date(1405, 1, 1))

    mock_send.assert_not_called()


# ---------------------------------------------------------------------
# Retry queue for failed sends (2026-09 regression tests for the
# "orders sync to Didar but Telegram never fires" production incident -
# see src/telegram.py's module docstring). Before this, a send that
# failed after mark_synced()/mark_deal_notified() had already run was
# just logged and lost forever - nothing in the system ever retried it.
# ---------------------------------------------------------------------

@respx.mock
def test_deliver_queues_notification_on_failure(repo):
    """_deliver() must persist a failed send to the repository instead
    of only logging it, so it isn't gone the moment this process moves
    on to the next order."""
    notifier = TelegramNotifier()
    notifier._client = httpx.Client(base_url=_API)
    notifier._chat_ids = [775753176]
    respx.post(f"{_API}/sendMessage").mock(
        return_value=httpx.Response(400, json={"ok": False, "description": "boom"})
    )

    notifier._deliver("order:digikala:12345", "test message", repo, "test notification")

    pending = repo.get_pending_notification_failures()
    assert len(pending) == 1
    assert pending[0].ref_id == "order:digikala:12345"
    assert pending[0].message_text == "test message"


@respx.mock
def test_deliver_queues_nothing_on_success(repo):
    notifier = TelegramNotifier()
    notifier._client = httpx.Client(base_url=_API)
    notifier._chat_ids = [775753176]
    respx.post(f"{_API}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {}})
    )

    notifier._deliver("order:digikala:12345", "test message", repo, "test notification")

    assert repo.get_pending_notification_failures() == []


@respx.mock
def test_retry_pending_notifications_resends_and_clears_queue(monkeypatch, repo):
    """The exact scenario from the incident: a send fails once (queued),
    then a later poll cycle's retry succeeds and the queue is cleared -
    instead of the message being gone forever."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", _TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "775753176")
    _ok_get_me()
    repo.record_notification_failure("order:digikala:12345", "queued message", "boom")

    notifier = TelegramNotifier()
    send_route = respx.post(f"{_API}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {}})
    )

    notifier.retry_pending_notifications(repo)

    assert send_route.called
    body = json.loads(send_route.calls[0].request.content)
    assert body["text"] == "queued message"
    assert repo.get_pending_notification_failures() == []


@respx.mock
def test_retry_pending_notifications_requeues_on_repeated_failure(monkeypatch, repo):
    """A retry that fails again must stay queued (with an incremented
    attempt count) rather than being dropped or retried forever with no
    limit."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", _TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "775753176")
    _ok_get_me()
    repo.record_notification_failure("order:digikala:12345", "queued message", "boom")

    notifier = TelegramNotifier()
    respx.post(f"{_API}/sendMessage").mock(
        return_value=httpx.Response(400, json={"ok": False, "description": "still failing"})
    )

    notifier.retry_pending_notifications(repo)

    pending = repo.get_pending_notification_failures()
    assert len(pending) == 1
    assert pending[0].attempt_count == 2


def test_retry_pending_notifications_noops_when_not_configured(repo, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    repo.record_notification_failure("order:digikala:12345", "queued message", "boom")

    notifier = TelegramNotifier()
    with patch.object(notifier, "_send") as mock_send:
        notifier.retry_pending_notifications(repo)

    mock_send.assert_not_called()
    assert len(repo.get_pending_notification_failures()) == 1


def test_send_daily_report_noops_when_not_configured(repo, monkeypatch):
    """Rollover math must still be safe with Telegram unconfigured -
    the marker check in check_and_send_reports doesn't gate on
    is_configured(), so the send methods themselves must."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    notifier = TelegramNotifier()

    with patch.object(notifier, "_send") as mock_send:
        notifier._send_daily_report(repo, ["digikala"], jdatetime.date(1405, 6, 9))

    mock_send.assert_not_called()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])   