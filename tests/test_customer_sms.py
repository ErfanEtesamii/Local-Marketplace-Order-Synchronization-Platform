"""Tests for src/customer_sms.py (first customer SMS, sent via the phone's
local SMS-gateway API). Real Repository on a tmp sqlite file, respx for
the gateway."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
import respx

from src.customer_sms import MESSAGE_TEXT, CustomerSmsNotifier, normalize_iran_mobile
from src.db.repository import Repository
from src.marketplaces.base import NormalizedOrder

GATEWAY = "http://192.168.1.50:8080"
TEHRAN = timezone(timedelta(hours=3, minutes=30))
# 10:00 Tehran - safely outside quiet hours.
CREATED = datetime(2026, 11, 21, 10, 0, tzinfo=TEHRAN)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    # The developer's real .env is loaded at import time (src.config), so
    # SIM number, cloud credentials, limits etc. would leak into the tests.
    # Start from a clean slate: drop EVERY SMS-related variable.
    import os
    for k in list(os.environ):
        if k.startswith(("CUSTOMER_SMS_", "SMS_GATEWAY_")):
            monkeypatch.delenv(k, raising=False)
    # Queue pacing is covered by dedicated tests below; the older tests
    # exercise other behaviour and assume messages go out back to back.
    monkeypatch.setenv("CUSTOMER_SMS_MIN_INTERVAL_MINUTES", "0")
    monkeypatch.setenv("CUSTOMER_SMS_INFLIGHT_BLOCK_HOURS", "0")
    monkeypatch.setenv("CUSTOMER_SMS_MODE", "auto")   # old tests cover the auto-send mode
    monkeypatch.setenv("CUSTOMER_SMS_ENABLED", "true")
    monkeypatch.setenv("SMS_GATEWAY_URL", GATEWAY)
    monkeypatch.setenv("SMS_GATEWAY_USERNAME", "u")
    monkeypatch.setenv("SMS_GATEWAY_PASSWORD", "p")
    monkeypatch.setattr("src.customer_sms.settings", type("S", (), {"dry_run": False})())


@pytest.fixture
def repo(tmp_path):
    return Repository(str(tmp_path / "t.db"))


def _order(oid="1", mobile="09121234567", created=CREATED, source="basalam"):
    return NormalizedOrder(
        source=source, source_order_id=oid, order_number=oid, created_at=created,
        total_price=Decimal("1"), status="x", customer_mobile=mobile,
    )


@pytest.mark.parametrize("raw,expected", [
    ("09121234567", "+989121234567"),
    ("9121234567", "+989121234567"),
    ("+989121234567", "+989121234567"),
    ("00989121234567", "+989121234567"),
    ("۰۹۱۲۱۲۳۴۵۶۷", "+989121234567"),
    ("02122334455", None),
    ("0912123", None),
    ("", None),
    (None, None),
])
def test_normalize(raw, expected):
    assert normalize_iran_mobile(raw) == expected


def test_enqueue_schedules_five_hours_after_and_is_idempotent(repo):
    n = CustomerSmsNotifier()
    n.enqueue(_order(), repo, now=CREATED)
    n.enqueue(_order(), repo, now=CREATED)
    assert repo.get_due_customer_sms(CREATED + timedelta(hours=4, minutes=59), 8, 10) == []
    rows = repo.get_due_customer_sms(CREATED + timedelta(hours=5), 8, 10)
    assert len(rows) == 1 and rows[0][2] == "+989121234567"


def test_enqueue_skips_bad_phone_old_order_platform_and_disabled(repo, monkeypatch):
    n = CustomerSmsNotifier()
    n.enqueue(_order("a", mobile="021123"), repo, now=CREATED)
    n.enqueue(_order("b"), repo, now=CREATED + timedelta(hours=30))
    monkeypatch.setenv("CUSTOMER_SMS_PLATFORMS", "digikala")
    n.enqueue(_order("c"), repo, now=CREATED)
    monkeypatch.delenv("CUSTOMER_SMS_PLATFORMS")
    monkeypatch.setenv("CUSTOMER_SMS_ENABLED", "false")
    n.enqueue(_order("d"), repo, now=CREATED)
    assert repo.get_due_customer_sms(CREATED + timedelta(days=9), 8, 10) == []


def test_quiet_hours_push_to_morning(repo):
    late = datetime(2026, 11, 21, 18, 0, tzinfo=TEHRAN)  # +5h = 23:00
    CustomerSmsNotifier().enqueue(_order(created=late), repo, now=late)
    nine = datetime(2026, 11, 22, 9, 0, tzinfo=TEHRAN)
    assert repo.get_due_customer_sms(nine - timedelta(minutes=1), 8, 10) == []
    assert len(repo.get_due_customer_sms(nine, 8, 10)) == 1


@respx.mock
def test_process_due_sends_text_once(repo):
    route = respx.post(f"{GATEWAY}/message").mock(return_value=httpx.Response(202, json={}))
    n = CustomerSmsNotifier()
    n.enqueue(_order(), repo, now=CREATED)
    now = CREATED + timedelta(hours=5, minutes=1)
    n.process_due(repo, now=now)
    n.process_due(repo, now=now + timedelta(minutes=2))
    assert route.call_count == 1
    import json
    body = json.loads(route.calls[0].request.content)
    assert body == {"textMessage": {"text": MESSAGE_TEXT}, "phoneNumbers": ["+989121234567"]}
    assert route.calls[0].request.headers["authorization"].startswith("Basic ")


@respx.mock
def test_failure_backs_off_then_retries_and_gives_up(repo, monkeypatch):
    monkeypatch.setenv("CUSTOMER_SMS_MAX_ATTEMPTS", "2")
    # The gateway ANSWERS with an error (a real failure that counts as an
    # attempt); plain lack of connectivity is covered by the offline tests.
    route = respx.post(f"{GATEWAY}/message").mock(return_value=httpx.Response(500))
    alerts: list[str] = []
    n = CustomerSmsNotifier()
    n.enqueue(_order(), repo, now=CREATED)
    t = CREATED + timedelta(hours=5, minutes=1)
    n.process_due(repo, alert=alerts.append, now=t)
    assert route.call_count == 1
    n.process_due(repo, alert=alerts.append, now=t + timedelta(minutes=1))   # backoff
    assert route.call_count == 1
    n.process_due(repo, alert=alerts.append, now=t + timedelta(minutes=11))  # 2nd & last
    assert route.call_count == 2
    n.process_due(repo, alert=alerts.append, now=t + timedelta(hours=1))
    assert route.call_count == 2
    assert any("رها شد" in a for a in alerts)


@respx.mock
def test_caps_and_dry_run(repo, monkeypatch):
    route = respx.post(f"{GATEWAY}/message").mock(return_value=httpx.Response(200))
    n = CustomerSmsNotifier()
    for i in range(5):
        n.enqueue(_order(str(i)), repo, now=CREATED)
    t = CREATED + timedelta(hours=5, minutes=1)
    monkeypatch.setenv("CUSTOMER_SMS_MAX_PER_CYCLE", "2")
    n.process_due(repo, now=t)
    assert route.call_count == 2
    monkeypatch.setenv("CUSTOMER_SMS_DAILY_CAP", "2")
    n.process_due(repo, now=t + timedelta(minutes=2))
    assert route.call_count == 2
    monkeypatch.setenv("CUSTOMER_SMS_DAILY_CAP", "150")
    monkeypatch.setattr("src.customer_sms.settings", type("S", (), {"dry_run": True})())
    n.process_due(repo, now=t + timedelta(minutes=4))
    assert route.call_count == 2


def test_overdue_alert_once(repo):
    n = CustomerSmsNotifier()
    n.enqueue(_order(), repo, now=CREATED)
    alerts: list[str] = []
    with respx.mock:
        respx.post(f"{GATEWAY}/message").mock(side_effect=httpx.ConnectError("down"))
        t = CREATED + timedelta(hours=7)
        n.process_due(repo, alert=alerts.append, now=t)
        n.process_due(repo, alert=alerts.append, now=t + timedelta(minutes=12))
    assert sum("ارسال نشده" in a for a in alerts) == 1


# --- send-time status re-check -------------------------------------------

@respx.mock
def test_status_check_skips_cancelled_and_never_retries(repo):
    route = respx.post(f"{GATEWAY}/message").mock(return_value=httpx.Response(202))
    n = CustomerSmsNotifier()
    n.enqueue(_order(), repo, now=CREATED)
    t = CREATED + timedelta(hours=5, minutes=1)
    n.process_due(repo, now=t, status_check=lambda p, o: "status canceled is cancelled/failed")
    n.process_due(repo, now=t + timedelta(hours=1), status_check=lambda p, o: None)
    assert route.call_count == 0


@respx.mock
def test_status_check_ok_sends(repo):
    route = respx.post(f"{GATEWAY}/message").mock(return_value=httpx.Response(202))
    n = CustomerSmsNotifier()
    n.enqueue(_order(), repo, now=CREATED)
    n.process_due(repo, now=CREATED + timedelta(hours=5, minutes=1), status_check=lambda p, o: None)
    assert route.call_count == 1


@respx.mock
def test_status_check_error_does_not_send_and_retries_later(repo):
    route = respx.post(f"{GATEWAY}/message").mock(return_value=httpx.Response(202))
    n = CustomerSmsNotifier()
    n.enqueue(_order(), repo, now=CREATED)
    t = CREATED + timedelta(hours=5, minutes=1)

    def boom(p, o):
        raise RuntimeError("marketplace down")

    n.process_due(repo, now=t, status_check=boom)
    assert route.call_count == 0
    n.process_due(repo, now=t + timedelta(minutes=11), status_check=lambda p, o: None)
    assert route.call_count == 1


def test_engine_status_check_rules():
    from src.sync_engine import SyncEngine

    class FakeAdapter:
        def __init__(self, source, status):
            self.name, self._o = source, _order(source=source)
            self._o = NormalizedOrder(**{**self._o.__dict__, "status": status})

        def fetch_order_detail(self, oid):
            return self._o

    def check(source, status):
        e = SyncEngine.__new__(SyncEngine)
        e._adapters = {source: FakeAdapter(source, status)}
        return e._customer_sms_status_check(source, "1")

    assert check("snappshop", "canceled") is not None
    assert check("snappshop", "confirmed") is None
    assert check("farazhonar", "completed") is None      # shipped within 5h: still send
    assert check("farazhonar", "cancelled") is not None
    assert check("farazhonar", "refunded") is not None
    with pytest.raises(LookupError):
        SyncEngine.__new__(SyncEngine).__class__._customer_sms_status_check(
            type("E", (), {"_adapters": {}})(), "x", "1"
        )


# --- local-first, cloud-fallback routing ---------------------------------

CLOUD = "https://api.sms-gate.app"


@pytest.fixture
def both_routes(monkeypatch):
    monkeypatch.setenv("SMS_GATEWAY_CLOUD_USERNAME", "cu")
    monkeypatch.setenv("SMS_GATEWAY_CLOUD_PASSWORD", "cp")
    monkeypatch.delenv("SMS_GATEWAY_CLOUD_URL", raising=False)
    monkeypatch.delenv("SMS_GATEWAY_CLOUD_SEND_PATH", raising=False)


@respx.mock
def test_local_used_when_reachable(repo, both_routes):
    local = respx.post(f"{GATEWAY}/message").mock(return_value=httpx.Response(202))
    cloud = respx.post(f"{CLOUD}/3rdparty/v1/messages").mock(return_value=httpx.Response(202))
    n = CustomerSmsNotifier()
    n.enqueue(_order(), repo, now=CREATED)
    n.process_due(repo, now=CREATED + timedelta(hours=5, minutes=1))
    assert local.call_count == 1 and cloud.call_count == 0


@respx.mock
def test_falls_back_to_cloud_and_skips_local_for_a_while(repo, both_routes):
    local = respx.post(f"{GATEWAY}/message").mock(side_effect=httpx.ConnectError("away"))
    cloud = respx.post(f"{CLOUD}/3rdparty/v1/messages").mock(return_value=httpx.Response(202))
    n = CustomerSmsNotifier()
    for i in range(3):
        n.enqueue(_order(str(i)), repo, now=CREATED)
    t = CREATED + timedelta(hours=5, minutes=1)
    n.process_due(repo, now=t)
    assert cloud.call_count == 3
    assert local.call_count == 1                 # local not retried within 5 min
    import json
    req = cloud.calls[0].request
    assert json.loads(req.content)["phoneNumbers"] == ["+989121234567"]
    assert req.headers["authorization"].startswith("Basic ")


@respx.mock
def test_local_retried_after_skip_window(repo, both_routes, monkeypatch):
    monkeypatch.setenv("CUSTOMER_SMS_MAX_PER_CYCLE", "1")
    local = respx.post(f"{GATEWAY}/message").mock(side_effect=httpx.ConnectError("away"))
    respx.post(f"{CLOUD}/3rdparty/v1/messages").mock(return_value=httpx.Response(202))
    n = CustomerSmsNotifier()
    for i in range(2):
        n.enqueue(_order(str(i)), repo, now=CREATED)
    t = CREATED + timedelta(hours=5, minutes=1)
    n.process_due(repo, now=t)
    n.process_due(repo, now=t + timedelta(minutes=6))
    assert local.call_count == 2


@respx.mock
def test_local_rejection_falls_back_but_read_timeout_does_not(repo, both_routes):
    local = respx.post(f"{GATEWAY}/message").mock(return_value=httpx.Response(401))
    cloud = respx.post(f"{CLOUD}/3rdparty/v1/messages").mock(return_value=httpx.Response(202))
    n = CustomerSmsNotifier()
    n._send("+989121234567", "x")
    assert cloud.call_count == 1

    local.mock(side_effect=httpx.ReadTimeout("slow"))
    n2 = CustomerSmsNotifier()
    from src.customer_sms import CustomerSmsError
    with pytest.raises(CustomerSmsError):
        n2._send("+989121234567", "x")
    assert cloud.call_count == 1                 # no double-send risk


@respx.mock
def test_both_routes_down_queues_retry(repo, both_routes):
    respx.post(f"{GATEWAY}/message").mock(side_effect=httpx.ConnectError("away"))
    respx.post(f"{CLOUD}/3rdparty/v1/messages").mock(return_value=httpx.Response(503))
    n = CustomerSmsNotifier()
    n.enqueue(_order(), repo, now=CREATED)
    t = CREATED + timedelta(hours=5, minutes=1)
    n.process_due(repo, now=t)
    assert repo.get_due_customer_sms(t, 8, 10) == []                       # backed off
    assert len(repo.get_due_customer_sms(t + timedelta(minutes=11), 8, 10)) == 1


@respx.mock
def test_cloud_only_config_is_enough(repo, monkeypatch, both_routes):
    for k in ("SMS_GATEWAY_URL", "SMS_GATEWAY_USERNAME", "SMS_GATEWAY_PASSWORD"):
        monkeypatch.delenv(k)
    cloud = respx.post(f"{CLOUD}/3rdparty/v1/messages").mock(return_value=httpx.Response(202))
    n = CustomerSmsNotifier()
    assert n.is_enabled()
    n.enqueue(_order(), repo, now=CREATED)
    n.process_due(repo, now=CREATED + timedelta(hours=5, minutes=1))
    assert cloud.call_count == 1


@respx.mock
def test_requests_carry_explicit_user_agent(monkeypatch):
    route = respx.post(f"{GATEWAY}/message").mock(return_value=httpx.Response(202))
    CustomerSmsNotifier()._send("+989121234567", "x")
    assert route.calls[0].request.headers["user-agent"] == "FarazHonar-OrderSync/1.0"
    monkeypatch.setenv("SMS_GATEWAY_USER_AGENT", "Custom/2")
    CustomerSmsNotifier()._send("+989121234567", "x")
    assert route.calls[1].request.headers["user-agent"] == "Custom/2"


@respx.mock
def test_sim_number_sent_only_when_configured(monkeypatch):
    import json
    route = respx.post(f"{GATEWAY}/message").mock(return_value=httpx.Response(202))
    CustomerSmsNotifier()._send("+989121234567", "x")
    assert "simNumber" not in json.loads(route.calls[0].request.content)
    monkeypatch.setenv("SMS_GATEWAY_SIM_NUMBER", "2")
    CustomerSmsNotifier()._send("+989121234567", "x")
    assert json.loads(route.calls[1].request.content)["simNumber"] == 2


@respx.mock
def test_feature_dry_run_sends_nothing_and_keeps_rows_pending(repo, monkeypatch):
    route = respx.post(f"{GATEWAY}/message").mock(return_value=httpx.Response(202))
    n = CustomerSmsNotifier()
    n.enqueue(_order(), repo, now=CREATED)
    t = CREATED + timedelta(hours=5, minutes=1)
    monkeypatch.setenv("CUSTOMER_SMS_DRY_RUN", "true")
    n.process_due(repo, now=t)
    assert route.call_count == 0
    monkeypatch.setenv("CUSTOMER_SMS_DRY_RUN", "false")
    n.process_due(repo, now=t + timedelta(minutes=2))
    assert route.call_count == 1


# --- queue survives long outages (default: never give up on attempts) ------

@respx.mock
def test_message_waits_through_long_outage_then_sends(repo, monkeypatch):
    monkeypatch.setenv("CUSTOMER_SMS_QUIET_START", "0")
    monkeypatch.setenv("CUSTOMER_SMS_QUIET_END", "0")      # no quiet hours in this test
    gw = respx.post(f"{GATEWAY}/message").mock(side_effect=httpx.ConnectError("phone offline"))
    n = CustomerSmsNotifier()
    n.enqueue(_order(), repo, now=CREATED)
    t = CREATED + timedelta(hours=5, minutes=1)           # 15:01 Tehran
    for i in range(40):                                    # ~6.5h of failures, every 10 min
        n.process_due(repo, now=t + timedelta(minutes=10 * i))
    assert gw.call_count >= 1
    sent_before = gw.call_count
    # phone is back online
    gw.mock(return_value=httpx.Response(202))
    n.process_due(repo, now=t + timedelta(minutes=10 * 41))
    assert gw.call_count == sent_before + 1
    n.process_due(repo, now=t + timedelta(minutes=10 * 42))
    assert gw.call_count == sent_before + 1                # sent exactly once


@respx.mock
def test_old_undeliverable_message_expires(repo, monkeypatch):
    monkeypatch.setenv("CUSTOMER_SMS_QUIET_START", "0")
    monkeypatch.setenv("CUSTOMER_SMS_QUIET_END", "0")      # disable quiet hours
    gw = respx.post(f"{GATEWAY}/message").mock(side_effect=httpx.ConnectError("down"))
    alerts: list[str] = []
    n = CustomerSmsNotifier()
    n.enqueue(_order(), repo, now=CREATED)
    due = CREATED + timedelta(hours=5)
    n.process_due(repo, alert=alerts.append, now=due + timedelta(minutes=1))
    gw.mock(return_value=httpx.Response(202))
    # 49h after the original due time -> dropped, never sent
    n.process_due(repo, alert=alerts.append, now=due + timedelta(hours=49))
    assert gw.call_count == 1
    assert any("منقضی" in a for a in alerts)


@respx.mock
def test_give_up_after_zero_disables_expiry(repo, monkeypatch):
    monkeypatch.setenv("CUSTOMER_SMS_QUIET_START", "0")
    monkeypatch.setenv("CUSTOMER_SMS_QUIET_END", "0")
    monkeypatch.setenv("CUSTOMER_SMS_GIVE_UP_AFTER_HOURS", "0")
    gw = respx.post(f"{GATEWAY}/message").mock(return_value=httpx.Response(202))
    n = CustomerSmsNotifier()
    n.enqueue(_order(), repo, now=CREATED)
    n.process_due(repo, now=CREATED + timedelta(days=10))
    assert gw.call_count == 1


# --- final-state tracking: what did the PHONE do with it? -------------------

def _status(repo, oid="1", platform="basalam"):
    with repo._connect() as conn:
        row = conn.execute(
            "SELECT status, last_error FROM customer_sms_queue "
            "WHERE platform = ? AND source_order_id = ?", (platform, oid),
        ).fetchone()
    return row


def _sent_ids(*ids):
    return [httpx.Response(202, json={"id": i, "state": "Pending"}) for i in ids]


@respx.mock
def test_confirmed_sent_is_final_and_never_resent(repo):
    post = respx.post(f"{GATEWAY}/message").mock(side_effect=_sent_ids("m1"))
    respx.get(f"{GATEWAY}/message/m1").mock(
        return_value=httpx.Response(200, json={"id": "m1", "state": "Delivered"}))
    n = CustomerSmsNotifier()
    n.enqueue(_order(), repo, now=CREATED)
    t = CREATED + timedelta(hours=5, minutes=1)
    n.process_due(repo, now=t)
    assert _status(repo)[0] == "accepted"
    n.process_due(repo, now=t + timedelta(minutes=2))
    assert _status(repo)[0] == "sent"
    n.process_due(repo, now=t + timedelta(hours=1))
    assert post.call_count == 1


@respx.mock
def test_phone_failure_goes_back_in_queue_and_is_retried(repo):
    post = respx.post(f"{GATEWAY}/message").mock(side_effect=_sent_ids("m1", "m2"))
    respx.get(f"{GATEWAY}/message/m1").mock(return_value=httpx.Response(200, json={
        "id": "m1", "state": "Failed",
        "recipients": [{"phoneNumber": "+98912", "state": "Failed",
                        "error": "RESULT_ERROR_GENERIC_FAILURE"}]}))
    respx.get(f"{GATEWAY}/message/m2").mock(
        return_value=httpx.Response(200, json={"id": "m2", "state": "Sent"}))
    alerts: list[str] = []
    n = CustomerSmsNotifier()
    n.enqueue(_order(), repo, now=CREATED)
    t = CREATED + timedelta(hours=5, minutes=1)
    n.process_due(repo, alert=alerts.append, now=t)                         # handed over
    n.process_due(repo, alert=alerts.append, now=t + timedelta(minutes=2))  # Failed -> requeued
    status, err = _status(repo)
    assert status == "pending" and "GENERIC_FAILURE" in err
    assert post.call_count == 1                                             # backoff: not yet
    n.process_due(repo, alert=alerts.append, now=t + timedelta(minutes=13)) # resent
    assert post.call_count == 2
    n.process_due(repo, alert=alerts.append, now=t + timedelta(minutes=15)) # Sent
    assert _status(repo)[0] == "sent"
    assert any("سیم‌کارت" in a for a in alerts)


@respx.mock
def test_in_flight_message_is_not_resent(repo):
    post = respx.post(f"{GATEWAY}/message").mock(side_effect=_sent_ids("m1"))
    respx.get(f"{GATEWAY}/message/m1").mock(
        return_value=httpx.Response(200, json={"id": "m1", "state": "Pending"}))
    n = CustomerSmsNotifier()
    n.enqueue(_order(), repo, now=CREATED)
    t = CREATED + timedelta(hours=5, minutes=1)
    n.process_due(repo, now=t)
    for i in range(1, 6):
        n.process_due(repo, now=t + timedelta(minutes=12 * i))
    assert post.call_count == 1 and _status(repo)[0] == "accepted"


@respx.mock
def test_repeated_phone_failures_eventually_give_up(repo, monkeypatch):
    monkeypatch.setenv("CUSTOMER_SMS_MAX_APP_FAILURES", "2")
    post = respx.post(f"{GATEWAY}/message").mock(
        side_effect=_sent_ids("m1", "m2", "m3", "m4"))
    for mid in ("m1", "m2", "m3", "m4"):
        respx.get(f"{GATEWAY}/message/{mid}").mock(
            return_value=httpx.Response(200, json={"id": mid, "state": "Failed"}))
    n = CustomerSmsNotifier()
    n.enqueue(_order(), repo, now=CREATED)
    t = CREATED + timedelta(hours=5, minutes=1)
    for i in range(12):
        n.process_due(repo, now=t + timedelta(minutes=6 * i))
    assert post.call_count == 2 and _status(repo)[0] == "failed"


@respx.mock
def test_unreadable_state_waits_then_closes_unverified(repo):
    respx.post(f"{GATEWAY}/message").mock(side_effect=_sent_ids("m1"))
    respx.get(f"{GATEWAY}/message/m1").mock(return_value=httpx.Response(404))
    n = CustomerSmsNotifier()
    n.enqueue(_order(), repo, now=CREATED)
    t = CREATED + timedelta(hours=5, minutes=1)
    n.process_due(repo, now=t)
    n.process_due(repo, now=t + timedelta(hours=1))
    assert _status(repo)[0] == "accepted"
    n.process_due(repo, now=t + timedelta(hours=25))
    status, note = _status(repo)
    assert status == "sent" and note.startswith("unverified")


@respx.mock
def test_response_without_id_is_finalized_immediately(repo):
    post = respx.post(f"{GATEWAY}/message").mock(return_value=httpx.Response(202))
    n = CustomerSmsNotifier()
    n.enqueue(_order(), repo, now=CREATED)
    t = CREATED + timedelta(hours=5, minutes=1)
    n.process_due(repo, now=t)
    n.process_due(repo, now=t + timedelta(minutes=2))
    assert _status(repo)[0] == "sent" and post.call_count == 1


@respx.mock
def test_cloud_route_state_is_read_from_cloud(repo, both_routes, monkeypatch):
    respx.post(f"{GATEWAY}/message").mock(side_effect=httpx.ConnectError("away"))
    respx.post(f"{CLOUD}/3rdparty/v1/messages").mock(side_effect=_sent_ids("c1"))
    cloud_state = respx.get(f"{CLOUD}/3rdparty/v1/messages/c1").mock(
        return_value=httpx.Response(200, json={"id": "c1", "state": "Sent"}))
    n = CustomerSmsNotifier()
    n.enqueue(_order(), repo, now=CREATED)
    t = CREATED + timedelta(hours=5, minutes=1)
    n.process_due(repo, now=t)
    n.process_due(repo, now=t + timedelta(minutes=2))
    assert cloud_state.call_count == 1 and _status(repo)[0] == "sent"


def test_lan_gateway_ignores_environment_proxy():
    from src.customer_sms import _is_lan
    assert _is_lan("http://192.168.1.8:8080") and _is_lan("http://127.0.0.1:8080")
    assert not _is_lan("https://api.sms-gate.app") and not _is_lan("http://8.8.8.8")


@respx.mock
def test_dropped_local_connection_falls_back_to_cloud(repo, both_routes):
    respx.post(f"{GATEWAY}/message").mock(side_effect=httpx.RemoteProtocolError("Server disconnected"))
    cloud = respx.post(f"{CLOUD}/3rdparty/v1/messages").mock(side_effect=_sent_ids("c1"))
    n = CustomerSmsNotifier()
    assert n._send("+989121234567", "x")[0] == "cloud" and cloud.call_count == 1


@respx.mock
def test_local_read_timeout_still_does_not_fall_back(repo, both_routes):
    respx.post(f"{GATEWAY}/message").mock(side_effect=httpx.ReadTimeout("slow"))
    cloud = respx.post(f"{CLOUD}/3rdparty/v1/messages").mock(side_effect=_sent_ids("c1"))
    with pytest.raises(Exception):
        CustomerSmsNotifier()._send("+989121234567", "x")
    assert cloud.call_count == 0



# --- queue pacing: in order, one at a time, 5 minutes apart, hold when offline

def _enqueue_three(repo, n):
    for i, minutes in enumerate((0, 1, 2), start=1):
        n.enqueue(_order(oid=str(i), mobile=f"0912123456{i}",
                         created=CREATED + timedelta(minutes=minutes)),
                  repo, now=CREATED + timedelta(minutes=minutes))


@respx.mock
def test_messages_go_out_in_order_one_every_five_minutes(repo, monkeypatch):
    monkeypatch.setenv("CUSTOMER_SMS_MIN_INTERVAL_MINUTES", "5")
    post = respx.post(f"{GATEWAY}/message").mock(return_value=httpx.Response(202))
    n = CustomerSmsNotifier()
    _enqueue_three(repo, n)
    t = CREATED + timedelta(hours=6)            # all three are due
    n.process_due(repo, now=t)
    assert post.call_count == 1                  # only ONE per cycle
    n.process_due(repo, now=t + timedelta(minutes=2))
    n.process_due(repo, now=t + timedelta(minutes=4))
    assert post.call_count == 1                  # still inside the 5-minute gap
    n.process_due(repo, now=t + timedelta(minutes=5))
    assert post.call_count == 2
    n.process_due(repo, now=t + timedelta(minutes=10))
    assert post.call_count == 3
    sent_to = [__import__("json").loads(c.request.content)["phoneNumbers"][0] for c in post.calls]
    assert sent_to == ["+989121234561", "+989121234562", "+989121234563"]  # registration order


@respx.mock
def test_offline_does_not_count_attempts_and_keeps_order(repo, monkeypatch):
    monkeypatch.setenv("CUSTOMER_SMS_MIN_INTERVAL_MINUTES", "5")
    monkeypatch.setenv("CUSTOMER_SMS_MAX_ATTEMPTS", "2")
    gw = respx.post(f"{GATEWAY}/message").mock(side_effect=httpx.ConnectError("no network"))
    n = CustomerSmsNotifier()
    _enqueue_three(repo, n)
    t = CREATED + timedelta(hours=6)
    for i in range(10):                          # a long outage, many cycles
        n.process_due(repo, now=t + timedelta(minutes=2 * i))
    assert gw.call_count == 10                   # one cheap try per cycle, no hammering
    with repo._connect() as c:
        rows = c.execute("SELECT status, attempt_count FROM customer_sms_queue").fetchall()
    assert all(r[0] == "pending" and r[1] == 0 for r in rows)   # nothing burned or dropped
    gw.mock(return_value=httpx.Response(202))    # connection is back
    t2 = t + timedelta(minutes=30)
    n.process_due(repo, now=t2)
    n.process_due(repo, now=t2 + timedelta(minutes=5))
    n.process_due(repo, now=t2 + timedelta(minutes=10))
    sent_to = [__import__("json").loads(c.request.content)["phoneNumbers"][0] for c in gw.calls[10:]]
    assert sent_to == ["+989121234561", "+989121234562", "+989121234563"]


@respx.mock
def test_unconfirmed_message_holds_the_queue_until_the_phone_sends_it(repo, monkeypatch):
    monkeypatch.setenv("CUSTOMER_SMS_MIN_INTERVAL_MINUTES", "5")
    monkeypatch.setenv("CUSTOMER_SMS_INFLIGHT_BLOCK_HOURS", "6")
    post = respx.post(f"{GATEWAY}/message").mock(side_effect=_sent_ids("m1", "m2", "m3"))
    state = respx.get(f"{GATEWAY}/message/m1").mock(
        return_value=httpx.Response(200, json={"id": "m1", "state": "Pending"}))  # phone offline
    n = CustomerSmsNotifier()
    _enqueue_three(repo, n)
    t = CREATED + timedelta(hours=6)
    n.process_due(repo, now=t)
    for i in range(1, 15):                       # hours pass, phone still offline (stay before 21:00 quiet hours)
        n.process_due(repo, now=t + timedelta(minutes=10 * i))
    assert post.call_count == 1                  # nothing newer is handed over
    state.mock(return_value=httpx.Response(200, json={"id": "m1", "state": "Sent"}))
    respx.get(f"{GATEWAY}/message/m2").mock(
        return_value=httpx.Response(200, json={"id": "m2", "state": "Sent"}))
    back = t + timedelta(minutes=170)
    n.process_due(repo, now=back)                # m1 confirmed now
    assert post.call_count == 1
    n.process_due(repo, now=back + timedelta(minutes=3))
    assert post.call_count == 1                  # 5-minute gap counts from the confirmation
    n.process_due(repo, now=back + timedelta(minutes=5))
    assert post.call_count == 2
