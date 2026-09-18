"""
Tests for src/modir_payamak.py, in two halves.

PART 1 (first half of this file) - everything below the express-alert
business logic: the configuration gate, the warehouse message text, the
exact HTTP request sent to Modir Payamak's IPPanel Edge API
(POST https://edge.ippanel.com/v1/api/send), and the conversion of every
failure shape into ModirPayamakError.

PART 2 (second half, from the "notify_if_express" banner onwards) - the
behaviour built on top of those primitives: notify_if_express's dedup
guard against a duplicate SMS for the same (source, source_order_id),
the record_sms_failure() retry queue, retry_pending_notifications(), and
a small set of wiring tests against src/sync_engine.py. Those use a real
Repository against a tmp_path sqlite file rather than a mock, because
the dedup guard IS a database fact (express_alerts_sent) - a fake
repository would be asserting on the test's own bookkeeping instead of
on the thing that actually stops three people being paged twice.

Two conventions inherited from tests/test_telegram.py, both deliberate:

  * respx mocks the real URL ModirPayamakNotifier itself builds, rather
    than the notifier's internals being stubbed - the point is to lock in
    the on-the-wire shape documented in the module docstring, since a
    wrong field name there fails silently in production (the API answers
    200 with meta.status: false).
  * Error-path tests use NON-retryable statuses (400/401) wherever the
    retry itself is not what is being tested, so they fail immediately
    instead of sitting through default_retry's real exponential backoff.
    The one test that IS about retrying 5xx neutralizes that sleep
    explicitly (see _no_retry_sleep).

NOTE ON ENV ISOLATION: tests/conftest.py's autouse fixture strips
TELEGRAM_* only, and src/config.py calls load_dotenv() at import time, so
a developer machine with real MODIR_PAYAMAK_* / EXPRESS_ALERT_SMS_*
values in its .env would otherwise leak live credentials and real phone
numbers into this suite (and, worse, into a test that reaches the send
path). _isolate_sms_env below clears all five vars before every test in
this file; each test then sets only what it means to exercise.
"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest
import respx

from src.db.repository import Repository
from src.marketplaces.base import MarketplaceAdapter, NormalizedOrder, OrderItem
from src.modir_payamak import ModirPayamakError, ModirPayamakNotifier
from src.sync_engine import SyncEngine

# Real-looking but fake credentials - never a live token, and the
# recipient numbers are the documented +98 E.164 shape from the API
# reference, not anyone's real line.
_TOKEN = "OTUyM2E1ZmItZmFrZS10b2tlbi1mb3ItdGVzdHM"
_FROM = "+983000505"
_RECIPIENTS = ["+989121111111", "+989122222222", "+989123333333"]

_API = "https://edge.ippanel.com/v1/api"
_SEND_URL = f"{_API}/send"

_ENV_VARS = (
    "MODIR_PAYAMAK_TOKEN",
    "MODIR_PAYAMAK_FROM_NUMBER",
    "EXPRESS_ALERT_SMS_RECIPIENT_1",
    "EXPRESS_ALERT_SMS_RECIPIENT_2",
    "EXPRESS_ALERT_SMS_RECIPIENT_3",
    "EXPRESS_ALERT_SMS_RECIPIENT_4",
    "EXPRESS_ALERT_SMS_RECIPIENT_5",
)


@pytest.fixture(autouse=True)
def _isolate_sms_env(monkeypatch):
    """See the NOTE ON ENV ISOLATION in the module docstring."""
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def repo(tmp_path):
    return Repository(db_path=str(tmp_path / "test.db"))


@pytest.fixture
def _no_retry_sleep(monkeypatch):
    """Neutralize default_retry()'s exponential backoff for the one test
    that deliberately triggers it. Opt-in (not autouse) so no other test
    can accidentally depend on retries being free.

    Both spellings are patched because which one tenacity uses to sleep
    has moved between versions (`tenacity.nap.sleep` vs. plain
    `time.sleep`); `raising=False` keeps this harmless on whichever
    version is installed.
    """
    monkeypatch.setattr("tenacity.nap.sleep", lambda seconds: None, raising=False)
    monkeypatch.setattr("time.sleep", lambda seconds: None, raising=False)


def _set_full_config(monkeypatch, recipients=_RECIPIENTS):
    monkeypatch.setenv("MODIR_PAYAMAK_TOKEN", _TOKEN)
    monkeypatch.setenv("MODIR_PAYAMAK_FROM_NUMBER", _FROM)
    for index, value in enumerate(recipients, start=1):
        monkeypatch.setenv(f"EXPRESS_ALERT_SMS_RECIPIENT_{index}", value)


def _configured_notifier(monkeypatch, **kwargs) -> ModirPayamakNotifier:
    """A notifier whose is_configured() has already run, so _client /
    _from_number / _recipients are populated and _send() may be called
    directly."""
    _set_full_config(monkeypatch, **kwargs)
    notifier = ModirPayamakNotifier()
    assert notifier.is_configured() is True
    return notifier


def _ok_send_response(outbox_ids=(123456,)):
    """The documented success shape: data.message_outbox_ids plus
    meta.status true."""
    return httpx.Response(
        200,
        json={
            "data": {"message_outbox_ids": list(outbox_ids)},
            "meta": {"status": True, "message": "success", "message_code": "ok"},
        },
    )


def _error_send_response(message="invalid token", code="ErrInvalidToken", status_code=200):
    """The documented failure shape - note it is normally an HTTP 200
    carrying meta.status false, which is exactly why this module can
    never rely on the status code alone."""
    return httpx.Response(
        status_code,
        json={"data": None, "meta": {"status": False, "message": message, "message_code": code}},
    )


def _order(source="digikala", order_id="12345", order_number="98765") -> NormalizedOrder:
    return NormalizedOrder(
        source=source,
        source_order_id=order_id,
        order_number=order_number,
        created_at=datetime(2026, 8, 31, 15, 12, tzinfo=timezone.utc),
        total_price=Decimal("100000"),
        status="confirmed",
        items=[
            OrderItem(
                sku="TEST001",
                title="Test Product",
                quantity=1,
                unit_price=Decimal("100000"),
                final_price=Decimal("100000"),
            )
        ],
        shipping_method="EXPRESS",
    )


# ---------------------------------------------------------------------
# is_configured()
# ---------------------------------------------------------------------

@respx.mock
def test_is_configured_true_and_makes_no_network_call(monkeypatch):
    """The deliberate difference from TelegramNotifier.is_configured():
    no reachability probe. respx is active with NO routes registered, so
    any outbound request here would fail the test outright; the explicit
    assertion on respx.calls documents the intent rather than relying on
    that side effect."""
    _set_full_config(monkeypatch)

    notifier = ModirPayamakNotifier()

    assert notifier.is_configured() is True
    assert len(respx.calls) == 0


def test_is_configured_collects_all_three_recipients(monkeypatch):
    notifier = _configured_notifier(monkeypatch)
    assert notifier._recipients == _RECIPIENTS


def test_is_configured_skips_empty_recipient_slots(monkeypatch):
    """Only non-empty values are used, and the configured ones keep their
    order - a site with a single warehouse number must not end up sending
    to an empty string."""
    _set_full_config(monkeypatch, recipients=[])
    monkeypatch.setenv("EXPRESS_ALERT_SMS_RECIPIENT_1", _RECIPIENTS[0])
    monkeypatch.setenv("EXPRESS_ALERT_SMS_RECIPIENT_2", "   ")
    monkeypatch.setenv("EXPRESS_ALERT_SMS_RECIPIENT_3", _RECIPIENTS[2])

    notifier = ModirPayamakNotifier()

    assert notifier.is_configured() is True
    assert notifier._recipients == [_RECIPIENTS[0], _RECIPIENTS[2]]


def test_is_configured_false_without_token(monkeypatch):
    _set_full_config(monkeypatch)
    monkeypatch.delenv("MODIR_PAYAMAK_TOKEN")

    assert ModirPayamakNotifier().is_configured() is False


def test_is_configured_false_without_from_number(monkeypatch):
    _set_full_config(monkeypatch)
    monkeypatch.delenv("MODIR_PAYAMAK_FROM_NUMBER")

    assert ModirPayamakNotifier().is_configured() is False


def test_is_configured_false_when_no_recipients_configured(monkeypatch):
    """Credentials alone are not enough: with nobody to page, the feature
    is off rather than sending to an empty recipient list (which the API
    would reject as a validation error on every single order)."""
    _set_full_config(monkeypatch, recipients=[])

    assert ModirPayamakNotifier().is_configured() is False


def test_is_configured_treats_whitespace_only_values_as_missing(monkeypatch):
    _set_full_config(monkeypatch)
    monkeypatch.setenv("MODIR_PAYAMAK_TOKEN", "   ")

    assert ModirPayamakNotifier().is_configured() is False


def test_is_configured_is_cached_after_the_first_success(monkeypatch):
    """Called on every poll cycle, so the second call must be a cheap
    no-op reusing the same httpx.Client (and its connection pool) rather
    than building a new one each time."""
    notifier = _configured_notifier(monkeypatch)
    client = notifier._client

    monkeypatch.delenv("MODIR_PAYAMAK_TOKEN")

    assert notifier.is_configured() is True
    assert notifier._client is client


def test_close_releases_the_client(monkeypatch):
    notifier = _configured_notifier(monkeypatch)
    notifier.close()

    assert notifier._client is None


# ---------------------------------------------------------------------
# Message text
# ---------------------------------------------------------------------

def test_format_message_names_the_platform_and_order_number(monkeypatch):
    notifier = ModirPayamakNotifier()
    text = notifier._format_message(_order(source="basalam", order_number="98765"))

    assert "سفارش اکسپرس" in text
    assert "basalam" in text
    assert "98765" in text


def test_format_message_falls_back_to_source_order_id(monkeypatch):
    """order_number is optional on NormalizedOrder; the warehouse still
    needs an identifier they can look the shipment up by."""
    notifier = ModirPayamakNotifier()
    text = notifier._format_message(_order(order_id="777", order_number=None))

    assert "777" in text


# ---------------------------------------------------------------------
# _send() - the on-the-wire request
# ---------------------------------------------------------------------

@respx.mock
def test_send_posts_the_documented_body_to_the_send_endpoint(monkeypatch):
    """Locks in the exact field names from the "Send Webservice SMS"
    reference. A typo in any of them would still return HTTP 200 (with
    meta.status false), so nothing but an assertion on the request body
    catches it."""
    route = respx.post(_SEND_URL).mock(return_value=_ok_send_response())
    notifier = _configured_notifier(monkeypatch)

    notifier._send("متن تست")

    assert route.called
    body = json.loads(route.calls[0].request.content)
    assert body == {
        "sending_type": "webservice",
        "from_number": _FROM,
        "message": "متن تست",
        "params": {"recipients": _RECIPIENTS},
    }


@respx.mock
def test_send_sets_the_authorization_and_content_type_headers(monkeypatch):
    """Authorization is the bare token - no "Bearer " prefix - per the
    API reference."""
    route = respx.post(_SEND_URL).mock(return_value=_ok_send_response())
    notifier = _configured_notifier(monkeypatch)

    notifier._send("متن تست")

    request = route.calls[0].request
    assert request.headers["Authorization"] == _TOKEN
    assert request.headers["Content-Type"] == "application/json"


@respx.mock
def test_send_fans_out_to_all_recipients_in_one_request(monkeypatch):
    """Unlike Telegram (one call per chat id), IPPanel takes the whole
    recipient list itself - three numbers must mean one HTTP call, not
    three."""
    route = respx.post(_SEND_URL).mock(return_value=_ok_send_response())
    notifier = _configured_notifier(monkeypatch)

    notifier._send("متن تست")

    assert route.call_count == 1
    body = json.loads(route.calls[0].request.content)
    assert body["params"]["recipients"] == _RECIPIENTS


@respx.mock
def test_send_succeeds_quietly_on_meta_status_true(monkeypatch):
    respx.post(_SEND_URL).mock(return_value=_ok_send_response())
    notifier = _configured_notifier(monkeypatch)

    assert notifier._send("متن تست") is None


# ---------------------------------------------------------------------
# Failure shapes -> ModirPayamakError
#
# Every one of these is what the retry queue (part 2 of this file) is
# built on: _deliver() catches exactly this one exception type, so a
# failure shape that escapes as something else would propagate into the
# SyncEngine's success path instead of being queued.
# ---------------------------------------------------------------------

@respx.mock
def test_meta_status_false_raises_with_message_and_code(monkeypatch):
    """The API's own validation error - HTTP 200, meta.status false. The
    message and code go into the exception text because that string is
    what gets persisted as the queued row's error_message and is the only
    diagnostic anyone sees later."""
    respx.post(_SEND_URL).mock(
        return_value=_error_send_response(message="invalid token", code="ErrInvalidToken")
    )
    notifier = _configured_notifier(monkeypatch)

    with pytest.raises(ModirPayamakError) as exc_info:
        notifier._send("متن تست")

    assert "invalid token" in str(exc_info.value)
    assert "ErrInvalidToken" in str(exc_info.value)


@respx.mock
def test_missing_meta_block_is_treated_as_failure(monkeypatch):
    """Never guess: a response without meta.status is not assumed to have
    succeeded just because it parsed as JSON and returned 200."""
    respx.post(_SEND_URL).mock(return_value=httpx.Response(200, json={"data": {}}))
    notifier = _configured_notifier(monkeypatch)

    with pytest.raises(ModirPayamakError):
        notifier._send("متن تست")


@respx.mock
def test_http_4xx_raises_and_is_not_retried(monkeypatch):
    """A 4xx needs human intervention (bad credentials, malformed
    payload), so is_retryable_http_error() must leave it at a single
    attempt - re-sending it would only burn quota."""
    route = respx.post(_SEND_URL).mock(
        return_value=httpx.Response(401, json={"meta": {"status": False, "message": "unauthorized"}})
    )
    notifier = _configured_notifier(monkeypatch)

    with pytest.raises(ModirPayamakError):
        notifier._send("متن تست")

    assert route.call_count == 1


@respx.mock
def test_network_error_raises_modir_payamak_error(monkeypatch):
    """A transport failure must surface as this module's own exception
    type, not as a raw httpx error - see the comment block above."""
    respx.post(_SEND_URL).mock(side_effect=httpx.ConnectError("connection refused"))
    notifier = _configured_notifier(monkeypatch)

    with pytest.raises(ModirPayamakError) as exc_info:
        notifier._send("متن تست")

    assert "network error" in str(exc_info.value)


@respx.mock
def test_non_json_response_raises_modir_payamak_error(monkeypatch):
    """An HTML error page from a proxy in front of the API still parses
    as a 200 at the HTTP level - it must not blow up as a bare
    ValueError."""
    respx.post(_SEND_URL).mock(
        return_value=httpx.Response(200, text="<html>gateway error</html>")
    )
    notifier = _configured_notifier(monkeypatch)

    with pytest.raises(ModirPayamakError) as exc_info:
        notifier._send("متن تست")

    assert "non-JSON" in str(exc_info.value)


@respx.mock
def test_server_error_is_retried_then_raises(monkeypatch, _no_retry_sleep):
    """5xx IS transient, so default_retry()'s 3 attempts apply before the
    failure is handed on to the retry queue - the only test here that
    exercises the backoff path (with its sleeps neutralized)."""
    route = respx.post(_SEND_URL).mock(return_value=httpx.Response(502, text="bad gateway"))
    notifier = _configured_notifier(monkeypatch)

    with pytest.raises(ModirPayamakError):
        notifier._send("متن تست")

    assert route.call_count == 3


@respx.mock
def test_server_error_that_recovers_mid_retry_succeeds(monkeypatch, _no_retry_sleep):
    """A single transient 5xx must not reach the retry queue at all - it
    is absorbed by default_retry()."""
    route = respx.post(_SEND_URL).mock(
        side_effect=[httpx.Response(503, text="unavailable"), _ok_send_response()]
    )
    notifier = _configured_notifier(monkeypatch)

    notifier._send("متن تست")

    assert route.call_count == 2


# =====================================================================
# PART 2 - notify_if_express, the retry queue, and sync_engine wiring
#
# Everything below uses the real Repository (the `repo` fixture above)
# and the real _send() path mocked at the HTTP layer with respx, so a
# test that says "no second SMS went out" is asserting on outbound HTTP
# calls, not on a stubbed method having been called.
# =====================================================================

def _express_order(source="basalam", order_id="555", method="پست اکسپرس") -> NormalizedOrder:
    """An express order whose created_at is NOW, so it survives
    SyncEngine's sliding FETCH_WINDOW_HOURS drop in the wiring tests at
    the end of this file (the part-1 `_order()` helper deliberately uses
    a fixed past date, which is fine for the notifier in isolation but
    would be window-dropped by the engine)."""
    return replace(
        _order(source=source, order_id=order_id, order_number=order_id),
        created_at=datetime.now(timezone.utc),
        shipping_method=method,
    )


def _non_express_order(source="basalam", order_id="556", method="پست پیشتاز") -> NormalizedOrder:
    return replace(_express_order(source=source, order_id=order_id), shipping_method=method)


def _ref_id(order: NormalizedOrder) -> str:
    """The queue key ModirPayamakNotifier builds - duplicated here on
    purpose: these tests pin the ref_id format, because it is what makes
    a re-queued failure bump the SAME sms_notification_failures row
    (attempt_count) instead of piling up a new row per attempt."""
    return f"express_sms:{order.source}:{order.source_order_id}"


# ---------------------------------------------------------------------
# notify_if_express() - the happy path and the "not express" no-op
# ---------------------------------------------------------------------

@respx.mock
def test_notify_if_express_sends_the_alert_for_an_express_order(monkeypatch, repo):
    route = respx.post(_SEND_URL).mock(return_value=_ok_send_response())
    notifier = _configured_notifier(monkeypatch)
    order = _express_order()

    notifier.notify_if_express(order, repo)

    assert route.call_count == 1
    body = json.loads(route.calls[0].request.content)
    assert body["message"] == notifier._format_message(order)
    assert body["params"]["recipients"] == _RECIPIENTS
    # Sent cleanly, so nothing should be sitting in the retry queue.
    assert repo.get_pending_sms_failures() == []


@respx.mock
def test_notify_if_express_records_the_dedup_guard_after_sending(monkeypatch, repo):
    respx.post(_SEND_URL).mock(return_value=_ok_send_response())
    notifier = _configured_notifier(monkeypatch)
    order = _express_order()

    assert repo.has_express_alert_been_sent(order.source, order.source_order_id) is False

    notifier.notify_if_express(order, repo)

    assert repo.has_express_alert_been_sent(order.source, order.source_order_id) is True


@respx.mock
def test_notify_if_express_is_a_no_op_for_a_non_express_order(monkeypatch, repo):
    """Called for EVERY synced order by sync_engine.py, so the common
    case is this one: no HTTP call, no dedup row, no queued message."""
    route = respx.post(_SEND_URL).mock(return_value=_ok_send_response())
    notifier = _configured_notifier(monkeypatch)
    order = _non_express_order()

    notifier.notify_if_express(order, repo)

    assert route.call_count == 0
    assert repo.has_express_alert_been_sent(order.source, order.source_order_id) is False
    assert repo.get_pending_sms_failures() == []


@respx.mock
@pytest.mark.parametrize("method", [None, "", "   ", "NORMAL"])
def test_notify_if_express_never_guesses_from_a_missing_shipping_method(
    monkeypatch, repo, method
):
    """The four values that mean "not known to be express" - including
    Digikala's "NORMAL" sentinel and a source that never reports a
    shipping method at all (Tapsi Shop -> None). None of them may page
    the warehouse."""
    route = respx.post(_SEND_URL).mock(return_value=_ok_send_response())
    notifier = _configured_notifier(monkeypatch)

    notifier.notify_if_express(_non_express_order(method=method), repo)

    assert route.call_count == 0


# ---------------------------------------------------------------------
# notify_if_express() - the duplicate-SMS guard
#
# The reason this method is allowed to be called twice for one order at
# all: sync_engine.py calls it from BOTH _sync_one_order() and
# retry_pending_failures() (see src/modir_payamak.py's docstring). A
# duplicate Telegram message is mildly annoying; a duplicate here is a
# second page to three warehouse phones.
# ---------------------------------------------------------------------

@respx.mock
def test_second_call_for_the_same_order_sends_nothing(monkeypatch, repo):
    route = respx.post(_SEND_URL).mock(return_value=_ok_send_response())
    notifier = _configured_notifier(monkeypatch)
    order = _express_order()

    notifier.notify_if_express(order, repo)
    notifier.notify_if_express(order, repo)

    assert route.call_count == 1


@respx.mock
def test_dedup_survives_a_fresh_notifier_instance(monkeypatch, repo):
    """The guard has to be the database row, not in-process state: the
    retry path can reach the same order on a later poll cycle, or after
    a restart, with a brand-new notifier object."""
    route = respx.post(_SEND_URL).mock(return_value=_ok_send_response())
    order = _express_order()

    _configured_notifier(monkeypatch).notify_if_express(order, repo)
    _configured_notifier(monkeypatch).notify_if_express(order, repo)

    assert route.call_count == 1


@respx.mock
def test_dedup_is_keyed_on_source_and_order_id_together(monkeypatch, repo):
    """Order ids are only unique WITHIN a marketplace - two sources can
    legitimately both have an order "555", and each deserves its own
    alert. Isolation between sources, same as everywhere else in this
    project."""
    route = respx.post(_SEND_URL).mock(return_value=_ok_send_response())
    notifier = _configured_notifier(monkeypatch)

    notifier.notify_if_express(_express_order(source="basalam", order_id="555"), repo)
    notifier.notify_if_express(_express_order(source="digikala", order_id="555"), repo)
    notifier.notify_if_express(_express_order(source="basalam", order_id="556"), repo)

    assert route.call_count == 3


@respx.mock
def test_a_failed_send_still_blocks_a_second_send_attempt(monkeypatch, repo):
    """The documented split of responsibilities: the dedup row is
    written BEFORE the send, so a delivery failure is owned by the retry
    queue and never turns into "notify_if_express tries again from
    scratch next cycle" (which would be a second SMS if the first one
    had in fact gone out and only the response was lost)."""
    route = respx.post(_SEND_URL).mock(return_value=_error_send_response())
    notifier = _configured_notifier(monkeypatch)
    order = _express_order()

    notifier.notify_if_express(order, repo)
    notifier.notify_if_express(order, repo)

    assert route.call_count == 1
    assert repo.has_express_alert_been_sent(order.source, order.source_order_id) is True
    assert len(repo.get_pending_sms_failures()) == 1


# ---------------------------------------------------------------------
# notify_if_express() - failures are queued, never raised
#
# notify_if_express() runs after the order's Didar deal already exists,
# so an exception escaping here would be recorded by sync_engine.py as a
# SYNC failure and the whole order would be retried - which is exactly
# what the fire-and-forget contract exists to prevent.
# ---------------------------------------------------------------------

@respx.mock
def test_api_error_queues_the_message_instead_of_raising(monkeypatch, repo):
    respx.post(_SEND_URL).mock(
        return_value=_error_send_response(message="invalid token", code="ErrInvalidToken")
    )
    notifier = _configured_notifier(monkeypatch)
    order = _express_order()

    notifier.notify_if_express(order, repo)  # must not raise

    pending = repo.get_pending_sms_failures()
    assert len(pending) == 1
    assert pending[0].ref_id == _ref_id(order)
    assert pending[0].message_text == notifier._format_message(order)
    assert "invalid token" in pending[0].error_message


@respx.mock
def test_network_error_queues_the_message_instead_of_raising(monkeypatch, repo):
    respx.post(_SEND_URL).mock(side_effect=httpx.ConnectError("connection refused"))
    notifier = _configured_notifier(monkeypatch)
    order = _express_order()

    notifier.notify_if_express(order, repo)

    pending = repo.get_pending_sms_failures()
    assert len(pending) == 1
    assert "network error" in pending[0].error_message


@respx.mock
def test_unexpected_error_is_queued_too(monkeypatch, repo):
    """The defensive `except Exception` in _deliver(): even a bug that
    isn't a ModirPayamakError must not escape into the sync path."""
    notifier = _configured_notifier(monkeypatch)
    monkeypatch.setattr(
        notifier, "_send", lambda text: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    order = _express_order()

    notifier.notify_if_express(order, repo)

    pending = repo.get_pending_sms_failures()
    assert len(pending) == 1
    assert "boom" in pending[0].error_message


@respx.mock
def test_unconfigured_notifier_queues_the_message_without_any_http_call(repo):
    """Mirrors the 2026-09 Telegram gap documented in
    TelegramNotifier.notify_new_order: returning early when the
    notifier isn't configured would lose the message with nothing for
    retry_pending_notifications() to pick up once credentials are
    fixed. No env vars are set here (the autouse fixture stripped them),
    so is_configured() is False.

    respx is active with no routes registered, so any outbound request
    would fail this test outright."""
    notifier = ModirPayamakNotifier()
    order = _express_order()

    assert notifier.is_configured() is False

    notifier.notify_if_express(order, repo)

    pending = repo.get_pending_sms_failures()
    assert len(pending) == 1
    assert pending[0].message_text == notifier._format_message(order)
    assert "not configured" in pending[0].error_message
    assert len(respx.calls) == 0


# ---------------------------------------------------------------------
# retry_pending_notifications()
# ---------------------------------------------------------------------

@respx.mock
def test_retry_sends_the_queued_message_and_clears_it(monkeypatch, repo):
    """The full round trip this feature's retry story depends on: a send
    that failed on one poll cycle goes out on the next one, with the
    exact text that was queued, and then leaves the queue."""
    route = respx.post(_SEND_URL).mock(return_value=_error_send_response())
    notifier = _configured_notifier(monkeypatch)
    order = _express_order()

    notifier.notify_if_express(order, repo)
    assert len(repo.get_pending_sms_failures()) == 1
    queued_text = repo.get_pending_sms_failures()[0].message_text

    route.mock(return_value=_ok_send_response())
    notifier.retry_pending_notifications(repo)

    assert repo.get_pending_sms_failures() == []
    assert json.loads(route.calls[-1].request.content)["message"] == queued_text


@respx.mock
def test_retry_requeues_and_bumps_attempt_count_on_repeated_failure(monkeypatch, repo):
    """A still-failing message stays in the queue as the SAME row with a
    higher attempt_count - that counter is the only thing that ever lets
    the retry loop give up, so a re-queue that inserted a second row
    would mean retrying forever."""
    respx.post(_SEND_URL).mock(return_value=_error_send_response())
    notifier = _configured_notifier(monkeypatch)
    order = _express_order()

    notifier.notify_if_express(order, repo)          # attempt 1
    notifier.retry_pending_notifications(repo)       # attempt 2

    pending = repo.get_pending_sms_failures()
    assert len(pending) == 1
    assert pending[0].ref_id == _ref_id(order)
    assert pending[0].attempt_count == 2


@respx.mock
def test_retry_gives_up_after_max_attempts(monkeypatch, repo):
    """Rows at or over the attempt budget are left in place (inspectable)
    but no longer re-sent - otherwise a permanently invalid token would
    have this loop hammering the API once per poll cycle forever."""
    route = respx.post(_SEND_URL).mock(return_value=_ok_send_response())
    notifier = _configured_notifier(monkeypatch)
    order = _express_order()
    for _ in range(5):
        repo.record_sms_failure(_ref_id(order), "متن تست", "previous failure")

    notifier.retry_pending_notifications(repo, max_attempts=5)

    assert route.call_count == 0


@respx.mock
def test_retry_is_a_no_op_when_the_notifier_is_not_configured(repo):
    """Credentials removed (or never set) must not drain the queue -
    the messages have to survive until the config is fixed."""
    order = _express_order()
    repo.record_sms_failure(_ref_id(order), "متن تست", "previous failure")

    ModirPayamakNotifier().retry_pending_notifications(repo)

    assert len(repo.get_pending_sms_failures()) == 1
    assert len(respx.calls) == 0


@respx.mock
def test_retry_does_not_reopen_the_dedup_guard(monkeypatch, repo):
    """clear_sms_failure() has no matching un-mark on
    express_alerts_sent: after a successful retry the alert is committed
    AND delivered, so a later call for the same order must still send
    nothing."""
    route = respx.post(_SEND_URL).mock(return_value=_error_send_response())
    notifier = _configured_notifier(monkeypatch)
    order = _express_order()

    notifier.notify_if_express(order, repo)
    route.mock(return_value=_ok_send_response())
    notifier.retry_pending_notifications(repo)
    calls_after_retry = route.call_count

    notifier.notify_if_express(order, repo)

    assert route.call_count == calls_after_retry
    assert repo.has_express_alert_been_sent(order.source, order.source_order_id) is True


@respx.mock
def test_retry_drains_each_queued_message_independently(monkeypatch, repo):
    """One message still failing must not stop the others going out -
    the loop catches per message, and (as elsewhere in this project) one
    source's problem never breaks another's.

    The mock keys off the message body rather than call order, because
    get_pending_sms_failures() makes no promise about row order.
    """
    stuck_order = _express_order(source="basalam", order_id="111")
    other_order = _express_order(source="digikala", order_id="222")

    def _respond(request):
        message = json.loads(request.content)["message"]
        if stuck_order.source_order_id in message:
            return _error_send_response(message="still failing")
        return _ok_send_response()

    respx.post(_SEND_URL).mock(side_effect=_respond)
    notifier = _configured_notifier(monkeypatch)

    notifier.notify_if_express(stuck_order, repo)
    notifier.notify_if_express(other_order, repo)
    assert len(repo.get_pending_sms_failures()) == 1

    notifier.retry_pending_notifications(repo)

    pending = repo.get_pending_sms_failures()
    assert [failure.ref_id for failure in pending] == [_ref_id(stuck_order)]


# =====================================================================
# Wiring: src/sync_engine.py
#
# Narrow on purpose - the engine's own orchestration is already covered
# by tests/test_sync_engine.py. All that is checked here is stage 6 of
# this feature: that notify_if_express() is actually reached from BOTH
# call sites, and that an SMS problem cannot damage the sync.
# =====================================================================

class _StubAdapter(MarketplaceAdapter):
    """Minimal in-memory adapter - the engine only needs these two
    methods for an order that already carries its line items."""

    def __init__(self, name: str, orders: list[NormalizedOrder]):
        self.name = name
        self._orders = orders

    def fetch_new_orders(self, since):
        return self._orders

    def fetch_order_detail(self, source_order_id):
        return next(o for o in self._orders if o.source_order_id == source_order_id)


class _StubDidarService:
    """Returns a deal id; optionally fails the first push for an order so
    the engine's retry path can be exercised."""

    def __init__(self, fail_once_for: set[str] | None = None):
        self._fail_once_for = set(fail_once_for or ())

    def sync_order(self, order: NormalizedOrder) -> str:
        key = f"{order.source}:{order.source_order_id}"
        if key in self._fail_once_for:
            self._fail_once_for.remove(key)
            raise RuntimeError("simulated Didar failure")
        return f"deal-{order.source_order_id}"


class _SpyNotifier:
    """Stands in for ModirPayamakNotifier to record the exact
    (order, repository) pairs the engine hands it."""

    def __init__(self):
        self.calls: list[tuple[NormalizedOrder, object]] = []

    def notify_if_express(self, order, repository) -> None:
        self.calls.append((order, repository))

    def retry_pending_notifications(self, repository, max_attempts: int = 5) -> None:
        pass


def _engine(repo, tmp_path, order, sms_notifier, fail_once=False):
    didar = _StubDidarService(
        fail_once_for={f"{order.source}:{order.source_order_id}"} if fail_once else None
    )
    adapter = _StubAdapter(order.source, [order])
    engine = SyncEngine(
        adapters=[adapter],
        repository=repo,
        didar_service=didar,
        synced_ids_file_path=str(tmp_path / "synced_ids.json"),
        sms_notifier=sms_notifier,
    )
    return engine, adapter


def test_sync_engine_notifies_on_the_first_sync_path(repo, tmp_path):
    order = _express_order()
    spy = _SpyNotifier()
    engine, adapter = _engine(repo, tmp_path, order, spy)

    engine._sync_source(adapter)

    assert [(o.source, o.source_order_id) for o, _ in spy.calls] == [
        (order.source, order.source_order_id)
    ]
    # Given the repository, not a fresh one - the dedup guard and the
    # retry queue both live in the engine's own database.
    assert spy.calls[0][1] is repo


def test_sync_engine_notifies_on_the_retry_path(repo, tmp_path):
    """An order whose first sync attempt failed is the one most likely to
    be running late, so it must not lose its express alert."""
    order = _express_order()
    spy = _SpyNotifier()
    engine, adapter = _engine(repo, tmp_path, order, spy, fail_once=True)

    # Deliberately _sync_source() and not run_once(): the latter ends
    # with its own retry pass, which would blur the two phases (same
    # note as in tests/test_sync_engine.py).
    engine._sync_source(adapter)
    assert spy.calls == []

    engine.retry_pending_failures()

    assert len(spy.calls) == 1


@respx.mock
def test_sync_engine_sends_exactly_one_sms_end_to_end(monkeypatch, repo, tmp_path):
    """Real notifier, real repository, HTTP mocked at the edge: one
    express order through the engine means one POST to Modir Payamak,
    and the engine's second call site for the same order means no
    second one."""
    route = respx.post(_SEND_URL).mock(return_value=_ok_send_response())
    notifier = _configured_notifier(monkeypatch)
    order = _express_order()
    engine, adapter = _engine(repo, tmp_path, order, notifier)

    engine._sync_source(adapter)
    engine._sms_notifier.notify_if_express(order, repo)  # the retry-path call site

    assert route.call_count == 1


@respx.mock
def test_sync_engine_sends_nothing_for_a_non_express_order(monkeypatch, repo, tmp_path):
    route = respx.post(_SEND_URL).mock(return_value=_ok_send_response())
    notifier = _configured_notifier(monkeypatch)
    engine, adapter = _engine(repo, tmp_path, _non_express_order(), notifier)

    engine._sync_source(adapter)

    assert route.call_count == 0


@respx.mock
def test_sms_failure_never_breaks_the_sync(monkeypatch, repo, tmp_path):
    """The point of the whole fire-and-forget contract: with the SMS API
    returning an error, the order is still synced and marked, NO sync
    failure is recorded (which would re-push the order to Didar on the
    next cycle), and the SMS itself waits in its own queue."""
    respx.post(_SEND_URL).mock(return_value=_error_send_response())
    notifier = _configured_notifier(monkeypatch)
    order = _express_order()
    engine, adapter = _engine(repo, tmp_path, order, notifier)

    engine._sync_source(adapter)

    assert repo.is_already_synced(order.source, order.source_order_id) is True
    assert repo.get_pending_failures() == []
    assert len(repo.get_pending_sms_failures()) == 1