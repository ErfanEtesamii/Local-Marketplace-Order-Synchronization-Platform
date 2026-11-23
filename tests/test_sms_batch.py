"""Manual customer-SMS flow (src/sms_batch.py): orders are only saved; an
operator's Telegram button sends the saved queue one message per minute."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
import respx

from src.customer_sms import CustomerSmsNotifier
from src.db.repository import Repository
from src.marketplaces.base import NormalizedOrder
from src.sms_batch import (
    BUTTON_BACK, BUTTON_CLEAR, BUTTON_PANEL, BUTTON_RETRY, BUTTON_STATUS, BUTTON_TEST,
    BUTTON_TEXT, SmsBatchController, parse_ip, parse_ip_checked,
)

PR, BOSS, OTHER = 222, 111, 999          # IP/PR account, second operator, stranger
IP = "192.168.1.43"
LOCAL = f"http://{IP}:8080"
CLOUD = "https://api.sms-gate.app"
T0 = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)


class FakeTelegram:
    def __init__(self):
        self.sent: list[tuple[int, str, dict | None]] = []

    def is_configured(self):
        return True

    def _send_plain_message(self, chat_id, text):
        self.sent.append((chat_id, text, None))

    def _send_message_with_keyboard(self, chat_id, text, kb):
        self.sent.append((chat_id, text, kb))

    def _reply_keyboard_for(self, chat_id):
        return {"keyboard": []}

    def to(self, chat_id):
        return [t for c, t, _ in self.sent if c == chat_id]


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
    monkeypatch.setenv("SMS_GATEWAY_CLOUD_USERNAME", "cu")
    monkeypatch.setenv("SMS_GATEWAY_CLOUD_PASSWORD", "cp")
    monkeypatch.setattr("src.customer_sms.settings", type("S", (), {"dry_run": False})())


@pytest.fixture
def repo(tmp_path):
    return Repository(str(tmp_path / "t.db"))


@pytest.fixture
def world(repo):
    tg = FakeTelegram()
    sms = CustomerSmsNotifier()
    checks: dict[str, str] = {}   # order_id -> skip reason

    def status_check(platform, oid):
        return checks.get(oid)

    ctl = SmsBatchController(sms, tg, status_check)
    return tg, sms, ctl, checks


def _order(oid, mobile, minutes=0):
    t = T0 + timedelta(minutes=minutes)
    return NormalizedOrder(
        source="digikala", source_order_id=oid, order_number=oid, created_at=t,
        total_price=Decimal("1"), status="x", customer_mobile=mobile,
    )


def _enqueue(sms, repo, n=3, start=1):
    for i in range(start, start + n):
        sms.enqueue(_order(str(i), f"0912123456{i}", i), repo, now=T0 + timedelta(minutes=i))


def _bodies(route):
    return [json.loads(c.request.content)["phoneNumbers"][0] for c in route.calls]


def _go(ctl, repo, ip=IP):
    """operator presses the button, PR answers with the IP."""
    assert ctl.handle_message(BOSS, BUTTON_TEXT, repo)
    assert ctl.handle_message(PR, ip, repo)


def _run(ctl, repo, minutes=10):
    for i in range(0, minutes * 6 + 1):          # a tick every 10 s
        ctl.tick(repo, T0 + timedelta(hours=1, seconds=10 * i))


# ------------------------------------------------------------------ parsing
@pytest.mark.parametrize("raw,expected", [
    ("192.168.1.43", "192.168.1.43:8080"),
    ("192.168.1.43:9090", "192.168.1.43:9090"),
    ("http://192.168.1.43:8080/", "192.168.1.43:8080"),
    ("۱۹۲.۱۶۸.۱.۴۳", "192.168.1.43:8080"),
    ("192.168.1.300", None), ("hello", None), ("192.168.1", None), ("", None),
])
def test_parse_ip(raw, expected):
    assert parse_ip(raw) == expected


# ------------------------------------------------------------------ saving
@respx.mock
def test_orders_are_only_saved_nothing_is_sent_automatically(repo, world):
    tg, sms, ctl, _ = world
    route = respx.post(url__regex=r".*").mock(return_value=httpx.Response(202))
    _enqueue(sms, repo)
    sms.process_due(repo, now=T0 + timedelta(hours=30))
    assert route.call_count == 0
    assert repo.count_pending_customer_sms() == 3


# ------------------------------------------------------------------ who sees the button
def test_button_only_for_the_two_operators(world):
    _, _, ctl, _ = world
    assert ctl.keyboard_rows(BOSS) == [[{"text": BUTTON_PANEL}]]   # ONE entry in the main menu
    assert ctl.keyboard_rows(PR) == [[{"text": BUTTON_PANEL}]]
    assert ctl.keyboard_rows(OTHER) == []
    panel = [b["text"] for row in ctl.panel_keyboard()["keyboard"] for b in row]
    assert panel == [BUTTON_TEXT, BUTTON_STATUS, BUTTON_RETRY, BUTTON_TEST, BUTTON_CLEAR, BUTTON_BACK]


def test_panel_button_opens_the_three_buttons_and_back_returns(repo, world):
    tg, _, ctl, _ = world
    assert ctl.handle_message(BOSS, BUTTON_PANEL, repo)
    chat, text, kb = tg.sent[-1]
    assert chat == BOSS and "پنل پیامکی" in text
    assert [b["text"] for row in kb["keyboard"] for b in row][:3] == [BUTTON_TEXT, BUTTON_STATUS, BUTTON_RETRY]
    assert ctl.handle_message(PR, BUTTON_BACK, repo)
    assert tg.sent[-1][0] == PR and tg.sent[-1][2] == {"keyboard": []}   # main keyboard again
    assert ctl.handle_message(OTHER, BUTTON_PANEL, repo) is False        # strangers: nothing


def test_stranger_pressing_the_button_does_nothing(repo, world):
    tg, sms, ctl, _ = world
    _enqueue(sms, repo)
    assert ctl.handle_message(OTHER, BUTTON_TEXT, repo) is False
    assert tg.sent == [] and repo.count_pending_customer_sms() == 3


def test_menu_is_pushed_once_to_each_operator(repo, world):
    tg, _, ctl, _ = world
    ctl.ensure_menu(repo)
    ctl.ensure_menu(repo)
    assert sorted(c for c, _, _ in tg.sent) == [BOSS, PR]


# ------------------------------------------------------------------ request flow
def test_press_asks_pr_for_the_ip_and_acknowledges_the_requester(repo, world):
    tg, sms, ctl, _ = world
    _enqueue(sms, repo)
    assert ctl.handle_message(BOSS, BUTTON_TEXT, repo)
    prompt = [m for m in tg.sent if m[0] == PR]
    assert len(prompt) == 1 and "3 پیامک" in prompt[0][1] and "آی‌پی" in prompt[0][1]
    buttons = [b["callback_data"] for row in prompt[0][2]["inline_keyboard"] for b in row]
    assert "sms:cloud" in buttons and "sms:cancel" in buttons and "sms:same" not in buttons
    assert any("برای روابط عمومی" in t for t in tg.to(BOSS))


def test_empty_queue_says_so(repo, world):
    tg, _, ctl, _ = world
    ctl.handle_message(BOSS, BUTTON_TEXT, repo)
    assert any("صف خالی" in t for t in tg.to(BOSS))


# ------------------------------------------------------------------ the batch
@respx.mock
def test_batch_sends_in_order_one_per_minute_local_first_and_reports(repo, world):
    tg, sms, ctl, _ = world
    local = respx.post(f"{LOCAL}/message").mock(side_effect=[
        httpx.Response(202, json={"id": f"m{i}", "state": "Pending"}) for i in (1, 2, 3)])
    for i in (1, 2, 3):
        respx.get(f"{LOCAL}/message/m{i}").mock(
            return_value=httpx.Response(200, json={"id": f"m{i}", "state": "Sent"}))
    cloud = respx.post(f"{CLOUD}/3rdparty/v1/messages").mock(return_value=httpx.Response(202))
    _enqueue(sms, repo)
    _go(ctl, repo)
    sent_times: list[datetime] = []
    for i in range(0, 200):
        before = local.call_count
        now = T0 + timedelta(hours=1, seconds=10 * i)
        ctl.tick(repo, now)
        if local.call_count > before:
            sent_times.append(now)
    assert _bodies(local) == ["+989121234561", "+989121234562", "+989121234563"]  # in order
    gaps = [(b - a).total_seconds() for a, b in zip(sent_times, sent_times[1:])]
    assert all(g >= 60 for g in gaps)                                          # >= 1 minute apart
    assert cloud.call_count == 0                                               # local worked
    assert repo.count_pending_customer_sms() == 0                              # queue emptied
    for chat in (BOSS, PR):
        report = tg.to(chat)[-1]
        assert "گزارش ارسال" in report and "ارسال‌شده: 3" in report and "ناموفق: 0" in report
        assert "Local 3" in report and "تمام شد" in report


@respx.mock
def test_orders_arriving_after_the_press_wait_for_the_next_press(repo, world):
    tg, sms, ctl, _ = world
    local = respx.post(f"{LOCAL}/message").mock(
        return_value=httpx.Response(202, json={"id": "m", "state": "Pending"}))
    respx.get(url__regex=rf"{LOCAL}/message/.*").mock(
        return_value=httpx.Response(200, json={"state": "Sent"}))
    _enqueue(sms, repo, n=2)
    _go(ctl, repo)
    _enqueue(sms, repo, n=1, start=3)               # arrives mid-batch
    _run(ctl, repo)
    assert _bodies(local) == ["+989121234561", "+989121234562"]
    assert repo.count_pending_customer_sms() == 1   # waits for the next cycle


@respx.mock
def test_cloud_takes_over_when_the_phone_is_not_reachable_on_that_ip(repo, world):
    tg, sms, ctl, _ = world
    respx.post(f"{LOCAL}/message").mock(side_effect=httpx.ConnectError("no route"))
    cloud = respx.post(f"{CLOUD}/3rdparty/v1/messages").mock(side_effect=[
        httpx.Response(202, json={"id": f"c{i}", "state": "Pending"}) for i in (1, 2)])
    respx.get(url__regex=rf"{CLOUD}/3rdparty/v1/messages/.*").mock(
        return_value=httpx.Response(200, json={"state": "Sent"}))
    _enqueue(sms, repo, n=2)
    _go(ctl, repo)
    _run(ctl, repo)
    assert cloud.call_count == 2
    assert "Cloud 2" in tg.to(BOSS)[-1] and "ارسال‌شده: 2" in tg.to(PR)[-1]


@respx.mock
def test_cloud_only_button_never_touches_the_local_route(repo, world):
    tg, sms, ctl, _ = world
    local = respx.post(url__regex=r"http://.*:8080/message").mock(return_value=httpx.Response(202))
    cloud = respx.post(f"{CLOUD}/3rdparty/v1/messages").mock(
        return_value=httpx.Response(202, json={"id": "c", "state": "Pending"}))
    respx.get(url__regex=rf"{CLOUD}/3rdparty/v1/messages/.*").mock(
        return_value=httpx.Response(200, json={"state": "Sent"}))
    _enqueue(sms, repo, n=1)
    ctl.handle_message(BOSS, BUTTON_TEXT, repo)
    ctl.handle_callback(PR, "sms:cloud", repo)
    _run(ctl, repo)
    assert local.call_count == 0 and cloud.call_count == 1


@respx.mock
def test_same_ip_button_reuses_the_last_ip(repo, world):
    tg, sms, ctl, _ = world
    local = respx.post(f"{LOCAL}/message").mock(
        return_value=httpx.Response(202, json={"id": "m", "state": "Pending"}))
    respx.get(url__regex=rf"{LOCAL}/message/.*").mock(
        return_value=httpx.Response(200, json={"state": "Sent"}))
    _enqueue(sms, repo, n=1)
    _go(ctl, repo)                                  # first batch: IP typed
    _run(ctl, repo)
    _enqueue(sms, repo, n=1, start=5)
    ctl.handle_message(BOSS, BUTTON_TEXT, repo)     # second batch
    prompt = [m for m in tg.sent if m[0] == PR][-1]
    labels = [b["callback_data"] for row in prompt[2]["inline_keyboard"] for b in row]
    assert "sms:same" in labels and IP in str(prompt[2])
    ctl.handle_callback(PR, "sms:same", repo)
    _run(ctl, repo)
    assert local.call_count == 2


# ------------------------------------------------------------------ failures
@respx.mock
def test_both_routes_down_stops_the_batch_and_reports_failure(repo, world):
    tg, sms, ctl, _ = world
    respx.post(f"{LOCAL}/message").mock(side_effect=httpx.ConnectError("x"))
    respx.post(f"{CLOUD}/3rdparty/v1/messages").mock(side_effect=httpx.ConnectError("x"))
    _enqueue(sms, repo)
    _go(ctl, repo)
    _run(ctl, repo)
    assert repo.count_pending_customer_sms() == 3          # nothing lost, nothing sent
    for chat in (BOSS, PR):
        report = tg.to(chat)[-1]
        assert "متوقف شد" in report and "باقی‌مانده در صف: 3" in report
    assert ctl._load(repo) is None                          # ready for the next press


@respx.mock
def test_phone_reporting_failed_stops_and_keeps_the_message_queued(repo, world):
    tg, sms, ctl, _ = world
    respx.post(f"{LOCAL}/message").mock(
        return_value=httpx.Response(202, json={"id": "m1", "state": "Pending"}))
    respx.get(f"{LOCAL}/message/m1").mock(return_value=httpx.Response(200, json={
        "state": "Failed", "recipients": [{"error": "RESULT_ERROR_GENERIC_FAILURE"}]}))
    _enqueue(sms, repo)
    _go(ctl, repo)
    _run(ctl, repo)
    assert repo.count_pending_customer_sms() == 3          # failed one is back in the queue
    report = tg.to(PR)[-1]
    assert "ناموفق: 1" in report and "GENERIC_FAILURE" in report and "متوقف شد" in report


@respx.mock
def test_cancelled_orders_are_skipped_and_the_batch_continues(repo, world):
    tg, sms, ctl, checks = world
    checks["2"] = "status cancelled"
    local = respx.post(f"{LOCAL}/message").mock(side_effect=[
        httpx.Response(202, json={"id": "a", "state": "Pending"}),
        httpx.Response(202, json={"id": "b", "state": "Pending"})])
    respx.get(url__regex=rf"{LOCAL}/message/.*").mock(
        return_value=httpx.Response(200, json={"state": "Sent"}))
    _enqueue(sms, repo)
    _go(ctl, repo)
    _run(ctl, repo)
    assert _bodies(local) == ["+989121234561", "+989121234563"]
    assert "ردشده" in tg.to(BOSS)[-1] and "ارسال‌شده: 2" in tg.to(BOSS)[-1]


@respx.mock
def test_phone_that_never_sends_stalls_the_batch_with_a_report(repo, world):
    tg, sms, ctl, _ = world
    respx.post(f"{LOCAL}/message").mock(
        return_value=httpx.Response(202, json={"id": "m1", "state": "Pending"}))
    respx.get(f"{LOCAL}/message/m1").mock(
        return_value=httpx.Response(200, json={"state": "Pending"}))
    _enqueue(sms, repo, n=2)
    _go(ctl, repo)
    _run(ctl, repo, minutes=15)
    assert "تأیید نشده توسط گوشی: 1" in tg.to(BOSS)[-1] and "Pending" in tg.to(BOSS)[-1]


# ------------------------------------------------------------------ misc
def test_cancel_button_and_ip_timeout(repo, world):
    tg, sms, ctl, _ = world
    _enqueue(sms, repo)
    ctl.handle_message(BOSS, BUTTON_TEXT, repo)
    ctl.handle_callback(PR, "sms:cancel", repo)
    assert ctl._load(repo) is None and repo.count_pending_customer_sms() == 3
    ctl.handle_message(BOSS, BUTTON_TEXT, repo)
    ctl.tick(repo, datetime.now(timezone.utc) + timedelta(hours=2))
    assert ctl._load(repo) is None and "لغو شد" in tg.to(BOSS)[-1]


def test_an_ip_typed_by_a_stranger_or_without_a_request_is_ignored(repo, world):
    tg, sms, ctl, _ = world
    _enqueue(sms, repo)
    assert ctl.handle_message(PR, IP, repo) is False        # no request pending
    ctl.handle_message(BOSS, BUTTON_TEXT, repo)
    assert ctl.handle_message(OTHER, IP, repo) is False     # not an operator
    assert ctl._load(repo)["state"] == "awaiting_ip"


@respx.mock
def test_batch_survives_a_service_restart_mid_way(repo, world):
    tg, sms, ctl, _ = world
    local = respx.post(f"{LOCAL}/message").mock(side_effect=[
        httpx.Response(202, json={"id": f"m{i}", "state": "Pending"}) for i in (1, 2, 3)])
    respx.get(url__regex=rf"{LOCAL}/message/.*").mock(
        return_value=httpx.Response(200, json={"state": "Sent"}))
    _enqueue(sms, repo)
    _go(ctl, repo)
    for i in range(0, 3):                                   # first message goes out...
        ctl.tick(repo, T0 + timedelta(hours=1, seconds=10 * i))
    assert local.call_count == 1
    # ...then the service restarts: brand-new objects, same database
    sms2 = CustomerSmsNotifier()
    ctl2 = SmsBatchController(sms2, tg, None)
    for i in range(3, 200):
        ctl2.tick(repo, T0 + timedelta(hours=1, seconds=10 * i))
    assert _bodies(local) == ["+989121234561", "+989121234562", "+989121234563"]
    assert "ارسال‌شده: 3" in tg.to(BOSS)[-1]


def test_pressing_again_while_running_or_waiting_does_not_start_a_second_batch(repo, world):
    tg, sms, ctl, _ = world
    _enqueue(sms, repo)
    ctl.handle_message(BOSS, BUTTON_TEXT, repo)
    ctl.handle_message(PR, BUTTON_TEXT, repo)               # PR presses while waiting for the IP
    assert ctl._load(repo)["state"] == "awaiting_ip"
    ctl.handle_message(PR, IP, repo)
    batch = ctl._load(repo)["batch_id"]
    ctl.handle_message(BOSS, BUTTON_TEXT, repo)             # while running
    assert ctl._load(repo)["batch_id"] == batch
    assert any("در حال انجام" in t for t in tg.to(BOSS))


def test_dry_run_and_disabled_refuse_to_send(repo, world, monkeypatch):
    tg, sms, ctl, _ = world
    _enqueue(sms, repo)
    monkeypatch.setenv("CUSTOMER_SMS_DRY_RUN", "true")
    ctl.handle_message(BOSS, BUTTON_TEXT, repo)
    assert ctl._load(repo) is None and any("DRY_RUN" in t for t in tg.to(BOSS))
    monkeypatch.setenv("CUSTOMER_SMS_DRY_RUN", "false")
    monkeypatch.setenv("CUSTOMER_SMS_ENABLED", "false")
    ctl.handle_message(BOSS, BUTTON_TEXT, repo)
    assert ctl._load(repo) is None and any("غیرفعال" in t for t in tg.to(BOSS))


def test_manual_mode_works_with_only_the_local_login_and_no_url(repo, world, monkeypatch):
    _, sms, _, _ = world
    monkeypatch.delenv("SMS_GATEWAY_CLOUD_USERNAME", raising=False)
    monkeypatch.delenv("SMS_GATEWAY_URL", raising=False)
    assert sms.is_enabled()                      # login is enough; the IP comes from Telegram


# ====================================================================
# Round 2: Cloud default, private IPs only, preview, age/quiet hours,
# status + retry buttons, richer report
# ====================================================================
def _now():
    return datetime.now(timezone.utc)


def _cb(ctl, repo, who, data):
    return ctl.handle_callback(who, data, repo)


def _buttons(kb):
    return [b["callback_data"] for row in kb["inline_keyboard"] for b in row]


@pytest.mark.parametrize("raw,reason", [
    ("8.8.8.8", "not_private"), ("37.255.247.252:8080", "not_private"),
    ("127.0.0.1", "not_private"), ("169.254.10.1", "not_private"),
    ("0.0.0.0", "not_private"), ("172.32.0.1", "not_private"),
    ("192.168.1.300", "invalid"), ("hello", "invalid"),
])
def test_only_private_lan_addresses_are_accepted(raw, reason):
    assert parse_ip_checked(raw) == (None, reason)


@pytest.mark.parametrize("raw", ["10.0.0.5", "172.16.3.4", "172.31.255.1:9000", "192.168.0.7"])
def test_private_ranges_are_accepted(raw):
    assert parse_ip(raw) is not None


def test_a_public_ip_typed_in_telegram_is_refused_and_the_request_stays_open(repo, world):
    tg, sms, ctl, _ = world
    _enqueue(sms, repo)
    ctl.handle_message(BOSS, BUTTON_TEXT, repo)
    assert ctl.handle_message(PR, "8.8.8.8", repo) is True
    assert "آی‌پی شبکهٔ داخلی" in tg.to(PR)[-1]
    assert ctl._load(repo)["state"] == "awaiting_ip"
    assert repo.count_pending_customer_sms() == 3          # nothing claimed / sent


@respx.mock
def test_cloud_is_the_default_route_and_nobody_is_asked_for_an_ip(repo, world, monkeypatch):
    tg, sms, ctl, _ = world
    monkeypatch.delenv("CUSTOMER_SMS_ROUTE_MODE")
    monkeypatch.setenv("SMS_GATEWAY_URL", LOCAL)
    local = respx.post(url__regex=r"http://.*:8080/message").mock(return_value=httpx.Response(202))
    cloud = respx.post(f"{CLOUD}/3rdparty/v1/messages").mock(
        return_value=httpx.Response(202, json={"id": "c", "state": "Pending"}))
    respx.get(url__regex=rf"{CLOUD}/3rdparty/v1/messages/.*").mock(
        return_value=httpx.Response(200, json={"state": "Sent"}))
    _enqueue(sms, repo, n=2)
    assert ctl.route_mode() == "cloud"
    ctl.handle_message(BOSS, BUTTON_TEXT, repo)
    assert not any("آی‌پی Local" in t for t in tg.to(PR))     # no IP question
    assert any("فقط Cloud" in t for t in tg.to(PR))           # PR is still told to get the phone ready
    _run(ctl, repo)
    assert local.call_count == 0 and cloud.call_count == 2


def test_without_cloud_credentials_the_default_falls_back_to_asking_for_the_ip(repo, world, monkeypatch):
    tg, sms, ctl, _ = world
    monkeypatch.delenv("CUSTOMER_SMS_ROUTE_MODE")
    monkeypatch.delenv("SMS_GATEWAY_CLOUD_USERNAME")
    monkeypatch.delenv("SMS_GATEWAY_CLOUD_PASSWORD")
    assert ctl.route_mode() == "ask"
    monkeypatch.setenv("CUSTOMER_SMS_ROUTE_MODE", "local")     # local without a fixed URL
    assert ctl.route_mode() == "ask"


@respx.mock
def test_local_mode_uses_the_fixed_address_from_env_without_asking(repo, world, monkeypatch):
    tg, sms, ctl, _ = world
    monkeypatch.setenv("CUSTOMER_SMS_ROUTE_MODE", "local")
    monkeypatch.setenv("SMS_GATEWAY_URL", "http://192.168.1.50:8080")
    local = respx.post("http://192.168.1.50:8080/message").mock(
        return_value=httpx.Response(202, json={"id": "m", "state": "Pending"}))
    respx.get(url__regex=r"http://192.168.1.50:8080/message/.*").mock(
        return_value=httpx.Response(200, json={"state": "Sent"}))
    _enqueue(sms, repo, n=1)
    ctl.handle_message(BOSS, BUTTON_TEXT, repo)
    assert not any("آی‌پی Local" in t for t in tg.to(PR))
    _run(ctl, repo)
    assert local.call_count == 1


def test_preview_must_be_confirmed_before_anything_starts(repo, world, monkeypatch):
    tg, sms, ctl, _ = world
    monkeypatch.setenv("CUSTOMER_SMS_CONFIRM", "true")
    _enqueue(sms, repo, n=3)
    ctl.handle_message(BOSS, BUTTON_TEXT, repo)
    chat, text, kb = tg.sent[-1]
    assert chat == BOSS and "آمادهٔ ارسال: 3" in text and "حدود 3 دقیقه" in text
    assert "+989***561" in text and "+989121234561" not in text      # masked sample
    assert "sms:go" in _buttons(kb) and "sms:cancel" in _buttons(kb)
    assert tg.to(PR) == []                                          # PR not bothered yet
    assert repo.count_pending_customer_sms() == 3
    _cb(ctl, repo, OTHER, "sms:go")                                 # stranger: nothing happens
    assert ctl._load(repo)["state"] == "awaiting_confirm"
    _cb(ctl, repo, BOSS, "sms:go")                                  # confirmed -> now PR is asked for the IP
    assert ctl._load(repo)["state"] == "awaiting_ip"
    assert any("آی‌پی" in t for t in tg.to(PR))


def test_preview_can_be_cancelled_and_expires(repo, world, monkeypatch):
    tg, sms, ctl, _ = world
    monkeypatch.setenv("CUSTOMER_SMS_CONFIRM", "true")
    _enqueue(sms, repo)
    ctl.handle_message(BOSS, BUTTON_TEXT, repo)
    _cb(ctl, repo, BOSS, "sms:cancel")
    assert ctl._load(repo) is None and repo.count_pending_customer_sms() == 3
    ctl.handle_message(BOSS, BUTTON_TEXT, repo)
    ctl.tick(repo, _now() + timedelta(hours=2))
    assert ctl._load(repo) is None and "تأیید دریافت نشد" in tg.to(BOSS)[-1]


@respx.mock
def test_old_orders_are_skipped_and_only_fresh_ones_are_sent(repo, world, monkeypatch):
    tg, sms, ctl, _ = world
    monkeypatch.setenv("CUSTOMER_SMS_MAX_AGE_HOURS", "72")
    monkeypatch.setenv("CUSTOMER_SMS_ROUTE_MODE", "cloud")
    cloud = respx.post(f"{CLOUD}/3rdparty/v1/messages").mock(
        return_value=httpx.Response(202, json={"id": "c", "state": "Pending"}))
    respx.get(url__regex=rf"{CLOUD}/3rdparty/v1/messages/.*").mock(
        return_value=httpx.Response(200, json={"state": "Sent"}))
    repo.enqueue_customer_sms("digikala", "old1", "09120000001", _now() - timedelta(hours=100))
    repo.enqueue_customer_sms("digikala", "old2", "09120000002", _now() - timedelta(hours=80))
    repo.enqueue_customer_sms("digikala", "new1", "09120000003", _now() - timedelta(hours=1))
    ctl.handle_message(BOSS, BUTTON_TEXT, repo)
    _run(ctl, repo)
    assert [json.loads(c.request.content)["phoneNumbers"][0] for c in cloud.calls] == ["09120000003"]
    assert repo.get_customer_sms_row("digikala", "old1")[0] == "skipped"
    report = tg.to(BOSS)[-1]
    assert "قدیمی بودن: 2" in report and "ارسال‌شده: 1" in report


def test_queue_that_is_entirely_old_is_cleared_with_a_message(repo, world, monkeypatch):
    tg, sms, ctl, _ = world
    monkeypatch.setenv("CUSTOMER_SMS_MAX_AGE_HOURS", "72")
    repo.enqueue_customer_sms("digikala", "old1", "09120000001", _now() - timedelta(hours=100))
    ctl.handle_message(BOSS, BUTTON_TEXT, repo)
    assert "قدیمی‌تر از 72 ساعت" in tg.to(BOSS)[-1]
    assert repo.count_pending_customer_sms() == 0 and ctl._load(repo) is None


def test_preview_warns_about_old_orders(repo, world, monkeypatch):
    tg, sms, ctl, _ = world
    monkeypatch.setenv("CUSTOMER_SMS_CONFIRM", "true")
    monkeypatch.setenv("CUSTOMER_SMS_MAX_AGE_HOURS", "72")
    repo.enqueue_customer_sms("digikala", "old1", "09120000001", _now() - timedelta(hours=100))
    repo.enqueue_customer_sms("digikala", "new1", "09120000003", _now() - timedelta(hours=1))
    ctl.handle_message(BOSS, BUTTON_TEXT, repo)
    assert "آمادهٔ ارسال: 1" in tg.to(BOSS)[-1] and "1 پیام قدیمی‌تر از 72 ساعت" in tg.to(BOSS)[-1]


def test_inside_quiet_hours_the_preview_is_forced_with_a_warning(repo, world, monkeypatch):
    tg, sms, ctl, _ = world                                  # CONFIRM=false in the fixture
    monkeypatch.setattr(ctl, "_in_quiet_hours", lambda now: True)
    _enqueue(sms, repo)
    ctl.handle_message(BOSS, BUTTON_TEXT, repo)
    chat, text, kb = tg.sent[-1]
    assert chat == BOSS and "ساعت سکوت" in text
    assert ctl._load(repo)["state"] == "awaiting_confirm"
    assert "با این حال" in kb["inline_keyboard"][0][0]["text"]


@respx.mock
def test_batch_stops_when_quiet_hours_begin_mid_way(repo, world, monkeypatch):
    tg, sms, ctl, _ = world
    monkeypatch.setenv("CUSTOMER_SMS_ROUTE_MODE", "cloud")
    quiet = {"on": False}
    monkeypatch.setattr(ctl, "_in_quiet_hours", lambda now: quiet["on"])
    cloud = respx.post(f"{CLOUD}/3rdparty/v1/messages").mock(
        return_value=httpx.Response(202, json={"id": "c", "state": "Pending"}))
    respx.get(url__regex=rf"{CLOUD}/3rdparty/v1/messages/.*").mock(
        return_value=httpx.Response(200, json={"state": "Sent"}))
    _enqueue(sms, repo, n=3)
    ctl.handle_message(BOSS, BUTTON_TEXT, repo)
    for i in range(0, 3):
        ctl.tick(repo, T0 + timedelta(hours=1, seconds=10 * i))
    assert cloud.call_count == 1
    quiet["on"] = True                                       # 21:00 arrives
    for i in range(3, 40):
        ctl.tick(repo, T0 + timedelta(hours=1, seconds=10 * i))
    assert cloud.call_count == 1                             # nothing more went out
    assert "ساعت سکوت" in tg.to(BOSS)[-1] and "باقی‌مانده در صف: 2" in tg.to(BOSS)[-1]
    assert repo.count_pending_customer_sms() == 2


@respx.mock
def test_explicit_confirmation_inside_quiet_hours_lets_the_batch_run(repo, world, monkeypatch):
    tg, sms, ctl, _ = world
    monkeypatch.setenv("CUSTOMER_SMS_ROUTE_MODE", "cloud")
    monkeypatch.setattr(ctl, "_in_quiet_hours", lambda now: True)
    cloud = respx.post(f"{CLOUD}/3rdparty/v1/messages").mock(
        return_value=httpx.Response(202, json={"id": "c", "state": "Pending"}))
    respx.get(url__regex=rf"{CLOUD}/3rdparty/v1/messages/.*").mock(
        return_value=httpx.Response(200, json={"state": "Sent"}))
    _enqueue(sms, repo, n=2)
    ctl.handle_message(BOSS, BUTTON_TEXT, repo)
    _cb(ctl, repo, BOSS, "sms:go")
    _run(ctl, repo)
    assert cloud.call_count == 2 and "تمام شد" in tg.to(BOSS)[-1]


def test_moved_on_orders_are_rejected_by_the_batch_status_check(monkeypatch):
    from src.sync_engine import SyncEngine

    class Adapter:
        def __init__(self, status, source="farazhonar"):
            self.status, self.source = status, source

        def fetch_order_detail(self, oid):
            return NormalizedOrder(
                source=self.source, source_order_id=oid, order_number=oid, created_at=T0,
                total_price=Decimal("1"), status=self.status, customer_mobile="09121234567",
            )

    engine = SyncEngine.__new__(SyncEngine)
    engine._adapters = {}
    for status, expected in [("processing", False), ("completed", True), ("cancelled", True)]:
        engine._adapters = {"farazhonar": Adapter(status)}
        reason = engine.customer_sms_batch_status_check("farazhonar", "1")
        assert bool(reason) is expected, (status, reason)
    # the old (auto-mode) check still lets a completed order through
    engine._adapters = {"farazhonar": Adapter("completed")}
    assert engine.customer_sms_status_check("farazhonar", "1") is None
    monkeypatch.setenv("CUSTOMER_SMS_MOVED_ON_STATUSES", "")
    assert engine.customer_sms_batch_status_check("farazhonar", "1") is None


def test_status_button_summarises_the_queue(repo, world):
    tg, sms, ctl, _ = world
    _enqueue(sms, repo, n=3)
    repo.mark_customer_sms_accepted("digikala", "1", "cloud", "g1", _now())
    ctl.handle_message(BOSS, BUTTON_STATUS, repo)
    text = tg.to(BOSS)[-1]
    assert "در صف: 2" in text and "تأییدنشده توسط گوشی: 1" in text and "ارسال‌شده در ۲۴ ساعت اخیر: 1" in text
    assert ctl.handle_message(OTHER, BUTTON_STATUS, repo) is False


@respx.mock
def test_status_button_shows_progress_of_a_running_batch(repo, world):
    tg, sms, ctl, _ = world
    respx.post(f"{LOCAL}/message").mock(return_value=httpx.Response(202, json={"id": "m", "state": "Pending"}))
    respx.get(url__regex=rf"{LOCAL}/message/.*").mock(return_value=httpx.Response(200, json={"state": "Sent"}))
    _enqueue(sms, repo, n=3)
    _go(ctl, repo)
    for i in range(0, 3):
        ctl.tick(repo, T0 + timedelta(hours=1, seconds=10 * i))
    ctl.handle_message(PR, BUTTON_STATUS, repo)
    assert "در حال انجام: 1 از 3" in tg.to(PR)[-1]


def _make_unconfirmed(sms, repo, n=2):
    _enqueue(sms, repo, n=n)
    for i in range(1, n + 1):
        repo.mark_customer_sms_accepted("digikala", str(i), "local", f"g{i}", _now())


def test_retry_button_lists_unconfirmed_and_needs_confirmation(repo, world):
    tg, sms, ctl, _ = world
    _make_unconfirmed(sms, repo)
    ctl.handle_message(BOSS, BUTTON_RETRY, repo)
    chat, text, kb = tg.sent[-1]
    assert "2 پیام" in text and "+989121234561" in text and "دو بار" in text
    assert _buttons(kb) == ["sms:retry", "sms:retrycancel"]
    assert repo.count_pending_customer_sms() == 0            # nothing moved yet
    _cb(ctl, repo, OTHER, "sms:retry")                        # stranger: swallowed
    assert repo.count_pending_customer_sms() == 0
    _cb(ctl, repo, BOSS, "sms:retrycancel")
    assert repo.count_pending_customer_sms() == 0
    _cb(ctl, repo, BOSS, "sms:retry")
    assert repo.count_pending_customer_sms() == 2
    assert any("2 پیام تأییدنشده به صف برگشت" in t for t in tg.to(PR))


def test_retry_with_nothing_unconfirmed_and_while_running(repo, world):
    tg, sms, ctl, _ = world
    ctl.handle_message(BOSS, BUTTON_RETRY, repo)
    assert "تأییدنشده‌ای نداریم" in tg.to(BOSS)[-1]
    _make_unconfirmed(sms, repo, n=1)
    _enqueue(sms, repo, n=1, start=5)
    _go(ctl, repo)                                           # a batch is now running
    ctl.handle_message(BOSS, BUTTON_RETRY, repo)
    assert "در حال انجام" in tg.to(BOSS)[-1]
    _cb(ctl, repo, BOSS, "sms:retry")
    assert repo.get_customer_sms_row("digikala", "1")[0] == "accepted"   # untouched


@respx.mock
def test_report_names_the_failed_and_unconfirmed_orders_with_phones(repo, world):
    tg, sms, ctl, _ = world
    respx.post(f"{LOCAL}/message").mock(
        return_value=httpx.Response(202, json={"id": "m1", "state": "Pending"}))
    respx.get(f"{LOCAL}/message/m1").mock(return_value=httpx.Response(200, json={"state": "Pending"}))
    _enqueue(sms, repo, n=3)
    _go(ctl, repo)
    _run(ctl, repo, minutes=15)
    report = tg.to(BOSS)[-1]
    assert "digikala 1 — +989121234561" in report and "تأییدنشده توسط گوشی (پیام در Cloud لغو" in report



# ====================================================================
# Round 3: ttl on every send, cancel at the Cloud when a batch stops,
# test button
# ====================================================================
def _payloads(route):
    return [json.loads(c.request.content) for c in route.calls]


@respx.mock
def test_every_batch_message_carries_a_ttl_equal_to_the_stall_time(repo, world, monkeypatch):
    tg, sms, ctl, _ = world
    local = respx.post(f"{LOCAL}/message").mock(
        return_value=httpx.Response(202, json={"id": "m", "state": "Pending"}))
    respx.get(url__regex=rf"{LOCAL}/message/.*").mock(return_value=httpx.Response(200, json={"state": "Sent"}))
    _enqueue(sms, repo, n=1)
    _go(ctl, repo)
    _run(ctl, repo)
    assert _payloads(local)[0]["ttl"] == 600                     # default stall = 10 min
    monkeypatch.setenv("CUSTOMER_SMS_TTL_MINUTES", "0")          # 0 = no ttl
    assert ctl._ttl_seconds() is None
    monkeypatch.setenv("CUSTOMER_SMS_TTL_MINUTES", "3")
    assert ctl._ttl_seconds() == 180


@respx.mock
def test_stalled_cloud_messages_are_cancelled_at_the_cloud(repo, world, monkeypatch):
    tg, sms, ctl, _ = world
    monkeypatch.setenv("CUSTOMER_SMS_ROUTE_MODE", "cloud")
    respx.post(f"{CLOUD}/3rdparty/v1/messages").mock(
        return_value=httpx.Response(202, json={"id": "c1", "state": "Pending"}))
    respx.get(f"{CLOUD}/3rdparty/v1/messages/c1").mock(
        return_value=httpx.Response(200, json={"state": "Pending"}))
    cancel = respx.delete(f"{CLOUD}/3rdparty/v1/messages/c1").mock(return_value=httpx.Response(204))
    _enqueue(sms, repo, n=2)
    ctl.handle_message(BOSS, BUTTON_TEXT, repo)
    _run(ctl, repo, minutes=15)
    assert cancel.call_count == 1
    report = tg.to(BOSS)[-1]
    assert "لغو شد؛ خودکار به صف برمی‌گردد" in report and "متوقف شد" in report


@respx.mock
def test_if_the_phone_already_took_it_the_report_warns_it_may_have_been_sent(repo, world, monkeypatch):
    tg, sms, ctl, _ = world
    monkeypatch.setenv("CUSTOMER_SMS_ROUTE_MODE", "cloud")
    respx.post(f"{CLOUD}/3rdparty/v1/messages").mock(
        return_value=httpx.Response(202, json={"id": "c1", "state": "Pending"}))
    respx.get(f"{CLOUD}/3rdparty/v1/messages/c1").mock(
        return_value=httpx.Response(200, json={"state": "Processed"}))
    respx.delete(f"{CLOUD}/3rdparty/v1/messages/c1").mock(return_value=httpx.Response(409))
    _enqueue(sms, repo, n=1)
    ctl.handle_message(BOSS, BUTTON_TEXT, repo)
    _run(ctl, repo, minutes=15)
    assert "ممکن است ارسال شده باشد" in tg.to(BOSS)[-1]
    assert repo.get_customer_sms_row("digikala", "1")[0] == "accepted"   # not requeued blindly


def test_cancel_outcomes_and_local_route_is_unsupported(monkeypatch):
    sms = CustomerSmsNotifier()
    assert sms.cancel("local", "x") == "unsupported"
    with respx.mock:
        respx.delete(f"{CLOUD}/3rdparty/v1/messages/a").mock(return_value=httpx.Response(204))
        respx.delete(f"{CLOUD}/3rdparty/v1/messages/b").mock(return_value=httpx.Response(409))
        respx.delete(f"{CLOUD}/3rdparty/v1/messages/c").mock(return_value=httpx.Response(404))
        respx.delete(f"{CLOUD}/3rdparty/v1/messages/d").mock(return_value=httpx.Response(500))
        respx.delete(f"{CLOUD}/3rdparty/v1/messages/e").mock(side_effect=httpx.ConnectError("x"))
        got = [sms.cancel("cloud", k) for k in "abcde"]
    assert got[:3] == ["requested", "not_pending", "missing"]
    assert got[3].startswith("error: HTTP 500") and got[4].startswith("error:")


# ---------------------------------------------------------------- test button
@respx.mock
def test_test_button_sends_one_message_to_the_admin_number_only(repo, world, monkeypatch):
    tg, sms, ctl, _ = world
    monkeypatch.setenv("CUSTOMER_SMS_ROUTE_MODE", "cloud")
    monkeypatch.setenv("CUSTOMER_SMS_TEST_PHONE", "09921190031")
    cloud = respx.post(f"{CLOUD}/3rdparty/v1/messages").mock(
        return_value=httpx.Response(202, json={"id": "t1", "state": "Pending"}))
    respx.get(f"{CLOUD}/3rdparty/v1/messages/t1").mock(
        return_value=httpx.Response(200, json={"state": "Delivered"}))
    watched = []
    ctl._start_watch = lambda chat, route, gid: watched.append(
        ctl._watch_test(chat, route, gid, sleep=lambda s: None))
    _enqueue(sms, repo, n=2)                                      # real customers stay untouched
    assert ctl.handle_message(BOSS, BUTTON_TEST, repo)
    body = _payloads(cloud)[0]
    assert body["phoneNumbers"] == ["+989921190031"] and body["ttl"] == 180
    assert "آزمایشی" in body["textMessage"]["text"]
    assert repo.count_pending_customer_sms() == 2 and cloud.call_count == 1
    assert any("به گوشی داده شد" in t for t in tg.to(BOSS))
    assert any("ارسال شد (Delivered)" in t for t in tg.to(BOSS))
    assert ctl.handle_message(OTHER, BUTTON_TEST, repo) is False


@respx.mock
def test_test_button_reports_a_phone_that_never_sends_and_cancels(repo, world, monkeypatch):
    tg, sms, ctl, _ = world
    monkeypatch.setenv("CUSTOMER_SMS_ROUTE_MODE", "cloud")
    monkeypatch.setenv("CUSTOMER_SMS_TEST_PHONE", "09921190031")
    respx.post(f"{CLOUD}/3rdparty/v1/messages").mock(
        return_value=httpx.Response(202, json={"id": "t1", "state": "Pending"}))
    respx.get(f"{CLOUD}/3rdparty/v1/messages/t1").mock(
        return_value=httpx.Response(200, json={"state": "Pending"}))
    cancel = respx.delete(f"{CLOUD}/3rdparty/v1/messages/t1").mock(return_value=httpx.Response(204))
    ctl._start_watch = lambda chat, route, gid: ctl._watch_test(chat, route, gid, sleep=lambda s: None, tries=3)
    ctl.handle_message(BOSS, BUTTON_TEST, repo)
    assert cancel.call_count == 1
    assert "نفرستاد" in tg.to(BOSS)[-1] and "لغو شد" in tg.to(BOSS)[-1]


def test_test_button_refuses_without_phone_dry_run_or_while_running(repo, world, monkeypatch):
    tg, sms, ctl, _ = world
    ctl.handle_message(BOSS, BUTTON_TEST, repo)
    assert "CUSTOMER_SMS_TEST_PHONE" in tg.to(BOSS)[-1]
    monkeypatch.setenv("CUSTOMER_SMS_TEST_PHONE", "09921190031")
    monkeypatch.setenv("CUSTOMER_SMS_DRY_RUN", "true")
    ctl.handle_message(BOSS, BUTTON_TEST, repo)
    assert "DRY_RUN" in tg.to(BOSS)[-1]
    monkeypatch.delenv("CUSTOMER_SMS_DRY_RUN")
    _enqueue(sms, repo, n=1)
    _go(ctl, repo)                                                # a batch is running
    ctl.handle_message(BOSS, BUTTON_TEST, repo)
    assert "در حال انجام" in tg.to(BOSS)[-1]



# ---------------------------------------------------------------- clear queue
def test_clear_queue_needs_confirmation_then_skips_everything(repo, world):
    tg, sms, ctl, _ = world
    _enqueue(sms, repo, n=3)
    assert ctl.handle_message(BOSS, BUTTON_CLEAR, repo)
    chat, text, kb = tg.sent[-1]
    assert chat == BOSS and "3 پیام" in text and "+989***561" in text
    assert _buttons(kb) == ["sms:clear", "sms:clearcancel"]
    assert repo.count_pending_customer_sms() == 3                 # nothing happened yet
    _cb(ctl, repo, OTHER, "sms:clear")                            # stranger: swallowed
    assert repo.count_pending_customer_sms() == 3
    _cb(ctl, repo, BOSS, "sms:clearcancel")
    assert repo.count_pending_customer_sms() == 3
    _cb(ctl, repo, BOSS, "sms:clear")
    assert repo.count_pending_customer_sms() == 0
    assert repo.get_customer_sms_row("digikala", "1")[0] == "skipped"
    for chat in (BOSS, PR):
        assert "صف پیامک مشتریان خالی شد (3 پیام" in tg.to(chat)[-1]
    assert ctl.handle_message(OTHER, BUTTON_CLEAR, repo) is False


@respx.mock
def test_cleared_orders_are_never_sent_or_queued_again_but_new_ones_are(repo, world):
    tg, sms, ctl, _ = world
    route = respx.post(url__regex=r".*").mock(return_value=httpx.Response(202, json={"id": "m"}))
    _enqueue(sms, repo, n=2)
    ctl.handle_message(BOSS, BUTTON_CLEAR, repo)
    _cb(ctl, repo, BOSS, "sms:clear")
    _enqueue(sms, repo, n=2)                       # the SAME orders arrive again -> dedup lock holds
    assert repo.count_pending_customer_sms() == 0
    _enqueue(sms, repo, n=1, start=9)              # a genuinely new order is queued normally
    assert repo.count_pending_customer_sms() == 1
    ctl.handle_message(BOSS, BUTTON_TEXT, repo)
    assert "9" not in str(route.calls)  # no request made by clearing itself
    assert route.call_count == 0


def test_clear_empty_queue_and_while_running_and_voids_a_pending_preview(repo, world, monkeypatch):
    tg, sms, ctl, _ = world
    ctl.handle_message(BOSS, BUTTON_CLEAR, repo)
    assert "از قبل خالی" in tg.to(BOSS)[-1]
    monkeypatch.setenv("CUSTOMER_SMS_CONFIRM", "true")
    _enqueue(sms, repo, n=2)
    ctl.handle_message(BOSS, BUTTON_TEXT, repo)                   # preview waiting
    assert ctl._load(repo)["state"] == "awaiting_confirm"
    ctl.handle_message(BOSS, BUTTON_CLEAR, repo)
    _cb(ctl, repo, BOSS, "sms:clear")
    assert ctl._load(repo) is None and repo.count_pending_customer_sms() == 0
    monkeypatch.setenv("CUSTOMER_SMS_CONFIRM", "false")
    _enqueue(sms, repo, n=1, start=8)
    _go(ctl, repo)                                                # batch running
    ctl.handle_message(BOSS, BUTTON_CLEAR, repo)
    assert "وسط ارسال" in tg.to(BOSS)[-1]
    _cb(ctl, repo, BOSS, "sms:clear")                             # confirm tap is refused too
    assert repo.count_pending_customer_sms() == 1 and ctl._load(repo)["state"] == "running"
