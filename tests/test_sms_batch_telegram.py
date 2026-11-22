"""End-to-end through the REAL TelegramNotifier: getUpdates -> handlers ->
SmsBatchController -> gateway. Only the Telegram HTTP call is faked."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import httpx
import pytest
import respx

from src.customer_sms import CustomerSmsNotifier
from src.db.repository import Repository
from src.marketplaces.base import NormalizedOrder
from src.sms_batch import BUTTON_PANEL, BUTTON_TEXT, SmsBatchController
from src.telegram import TelegramNotifier

BOSS, PR, OTHER = 775753176, 6978174215, 555
T0 = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    import os
    for k in list(os.environ):
        if k.startswith(("CUSTOMER_SMS_", "SMS_GATEWAY_")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CUSTOMER_SMS_ENABLED", "true")
    monkeypatch.setenv("CUSTOMER_SMS_OPERATOR_CHAT_IDS", f"{BOSS},{PR}")
    monkeypatch.setenv("CUSTOMER_SMS_IP_CHAT_ID", str(PR))
    # the classic flow (ask for the IP, no preview, no age/quiet-hours limits);
    # the new behaviours have their own tests below
    monkeypatch.setenv("CUSTOMER_SMS_ROUTE_MODE", "ask")
    monkeypatch.setenv("CUSTOMER_SMS_CONFIRM", "false")
    monkeypatch.setenv("CUSTOMER_SMS_MAX_AGE_HOURS", "0")
    monkeypatch.setenv("CUSTOMER_SMS_QUIET_START", "0")
    monkeypatch.setenv("CUSTOMER_SMS_QUIET_END", "0")
    monkeypatch.setenv("SMS_GATEWAY_USERNAME", "u")
    monkeypatch.setenv("SMS_GATEWAY_PASSWORD", "p")
    monkeypatch.setattr("src.customer_sms.settings", type("S", (), {"dry_run": False})())


class Bot:
    """Fake Telegram API behind TelegramNotifier._request."""
    def __init__(self):
        self.updates: list[dict] = []
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, client, method, **body):
        self.calls.append((method, body))
        if method == "getUpdates":
            out, self.updates = self.updates, []
            return {"result": out}
        return {"result": {"message_id": 1}}

    def sent_to(self, chat):
        return [b for m, b in self.calls if m == "sendMessage" and b["chat_id"] == chat]


@pytest.fixture
def setup(tmp_path):
    repo = Repository(str(tmp_path / "t.db"))
    repo.set_report_marker("telegram_update_offset", "0")
    tg = TelegramNotifier()
    tg._chat_ids = [BOSS, PR, OTHER]
    tg._client = object()
    bot = Bot()
    sms = CustomerSmsNotifier()
    ctl = SmsBatchController(sms, tg, None)
    tg.sms_batch = ctl
    with patch.object(TelegramNotifier, "is_configured", return_value=True), \
            patch.object(tg, "_request", side_effect=bot):
        yield repo, tg, bot, sms, ctl


def _msg(uid, chat, text):
    return {"update_id": uid, "message": {"chat": {"id": chat}, "text": text}}


def _cb(uid, chat, data):
    return {"update_id": uid, "callback_query": {
        "id": f"q{uid}", "data": data, "message": {"chat": {"id": chat}, "message_id": 7}}}


def _buttons(markup):
    return [b["text"] for row in markup["keyboard"] for b in row]


def test_start_menu_shows_the_button_only_to_operators(setup):
    repo, tg, bot, *_ = setup
    bot.updates = [_msg(1, BOSS, "/start"), _msg(2, OTHER, "/start")]
    tg.poll_updates(repo, ["digikala"])
    boss_kb = [b["reply_markup"] for b in bot.sent_to(BOSS) if "keyboard" in b["reply_markup"]]
    other_kb = [b["reply_markup"] for b in bot.sent_to(OTHER) if "keyboard" in b["reply_markup"]]
    assert BUTTON_PANEL in _buttons(boss_kb[0]) and BUTTON_TEXT not in _buttons(boss_kb[0])
    assert BUTTON_PANEL not in _buttons(other_kb[0])
    assert "📊 گزارش دلخواه" in _buttons(other_kb[0])      # normal report menu untouched


@respx.mock
def test_full_flow_through_the_real_bot_handlers(setup):
    repo, tg, bot, sms, ctl = setup
    local = respx.post("http://192.168.1.43:8080/message").mock(
        return_value=httpx.Response(202, json={"id": "m1", "state": "Pending"}))
    respx.get("http://192.168.1.43:8080/message/m1").mock(
        return_value=httpx.Response(200, json={"state": "Sent"}))
    sms.enqueue(NormalizedOrder(
        source="digikala", source_order_id="1", order_number="1", created_at=T0,
        total_price=Decimal("1"), status="x", customer_mobile="09121234561"), repo, now=T0)

    # a stranger presses the button: ignored, no prompt anywhere
    bot.updates = [_msg(1, OTHER, BUTTON_TEXT)]
    tg.poll_updates(repo, ["digikala"])
    assert not any("آی‌پی" in b["text"] for b in bot.sent_to(PR))     # (the one-time menu push is fine)
    assert bot.sent_to(OTHER) == []

    # the operator presses it: PR is asked for the IP
    bot.updates = [_msg(2, BOSS, BUTTON_TEXT)]
    tg.poll_updates(repo, ["digikala"])
    ask = [b for b in bot.sent_to(PR) if "آی‌پی" in b["text"]]
    assert len(ask) == 1

    # PR types the IP: batch starts, both operators are told
    bot.updates = [_msg(3, PR, "192.168.1.43")]
    tg.poll_updates(repo, ["digikala"])
    assert any("شروع ارسال" in b["text"] for b in bot.sent_to(BOSS))
    assert any("شروع ارسال" in b["text"] for b in bot.sent_to(PR))

    for i in range(0, 20):
        ctl.tick(repo, T0 + timedelta(hours=1, seconds=10 * i))
    assert local.call_count == 1
    assert "ارسال‌شده: 1" in bot.sent_to(BOSS)[-1]["text"]
    assert "ارسال‌شده: 1" in bot.sent_to(PR)[-1]["text"]


def test_inline_buttons_from_a_stranger_are_swallowed(setup):
    repo, tg, bot, sms, ctl = setup
    bot.updates = [_cb(1, OTHER, "sms:cloud")]
    tg.poll_updates(repo, ["digikala"])
    assert ctl._load(repo) is None
    assert any(m == "answerCallbackQuery" for m, _ in bot.calls)   # spinner cleared


@respx.mock
def test_preview_status_and_retry_buttons_through_the_real_bot(setup, monkeypatch):
    from src.sms_batch import BUTTON_RETRY, BUTTON_STATUS
    repo, tg, bot, sms, ctl = setup
    monkeypatch.setenv("CUSTOMER_SMS_CONFIRM", "true")
    monkeypatch.setenv("CUSTOMER_SMS_ROUTE_MODE", "cloud")
    monkeypatch.setenv("SMS_GATEWAY_CLOUD_USERNAME", "cu")
    monkeypatch.setenv("SMS_GATEWAY_CLOUD_PASSWORD", "cp")
    cloud = respx.post("https://api.sms-gate.app/3rdparty/v1/messages").mock(
        return_value=httpx.Response(202, json={"id": "c1", "state": "Pending"}))
    respx.get("https://api.sms-gate.app/3rdparty/v1/messages/c1").mock(
        return_value=httpx.Response(200, json={"state": "Sent"}))
    sms.enqueue(NormalizedOrder(
        source="digikala", source_order_id="1", order_number="1", created_at=T0,
        total_price=Decimal("1"), status="x", customer_mobile="09121234561"), repo, now=T0)

    # the menu has the two new buttons for operators only
    bot.updates = [_msg(1, BOSS, "/start"), _msg(2, OTHER, "/start")]
    tg.poll_updates(repo, ["digikala"])
    boss_kb = [b["reply_markup"] for b in bot.sent_to(BOSS) if "keyboard" in b["reply_markup"]]
    other_kb = [b["reply_markup"] for b in bot.sent_to(OTHER) if "keyboard" in b["reply_markup"]]
    assert BUTTON_PANEL in _buttons(boss_kb[-1]) and BUTTON_STATUS not in _buttons(boss_kb[-1])
    assert BUTTON_PANEL not in _buttons(other_kb[-1])
    # opening the panel shows the three buttons, "back" restores the main menu
    from src.sms_batch import BUTTON_BACK
    bot.updates = [_msg(20, BOSS, BUTTON_PANEL)]
    tg.poll_updates(repo, ["digikala"])
    panel = [b["reply_markup"] for b in bot.sent_to(BOSS) if "keyboard" in b["reply_markup"]][-1]
    assert _buttons(panel) == [BUTTON_TEXT, BUTTON_STATUS, BUTTON_RETRY, "🧪 ارسال تست", BUTTON_BACK]
    bot.updates = [_msg(21, BOSS, BUTTON_BACK)]
    tg.poll_updates(repo, ["digikala"])
    main = [b["reply_markup"] for b in bot.sent_to(BOSS) if "keyboard" in b["reply_markup"]][-1]
    assert BUTTON_PANEL in _buttons(main)

    # press -> preview with inline buttons; nothing is sent yet
    bot.updates = [_msg(3, BOSS, BUTTON_TEXT)]
    tg.poll_updates(repo, ["digikala"])
    preview = [b for b in bot.sent_to(BOSS) if "آمادهٔ ارسال" in b["text"]]
    assert len(preview) == 1 and cloud.call_count == 0

    # status button works from the real bot
    bot.updates = [_msg(4, PR, BUTTON_STATUS)]
    tg.poll_updates(repo, ["digikala"])
    assert "وضعیت صف پیامک" in bot.sent_to(PR)[-1]["text"]

    # confirm via the inline callback -> Cloud batch runs, no IP question
    bot.updates = [_cb(5, BOSS, "sms:go")]
    tg.poll_updates(repo, ["digikala"])
    assert not any("Local Address" in b["text"] for b in bot.sent_to(PR))
    for i in range(0, 20):
        ctl.tick(repo, T0 + timedelta(hours=1, seconds=10 * i))
    assert cloud.call_count == 1
    assert "ارسال‌شده: 1" in bot.sent_to(BOSS)[-1]["text"]
