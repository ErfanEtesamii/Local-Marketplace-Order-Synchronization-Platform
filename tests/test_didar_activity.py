import json as _json
from datetime import datetime, timedelta, timezone

import respx
import httpx

from src.config import DidarConfig
from src.didar.activity_client import DidarActivityClient, POST_SALE_CHECKLIST
from src.didar.scheduling import compute_checklist_due_dates

_CFG_WITH_TYPES = DidarConfig(
    base_url="https://app.didar.me/api", api_key="test-key",
    activity_type_new_call_id="type-new-call",
    activity_type_sms1_id="type-sms1",
    activity_type_sms2_id="type-sms2",
    activity_type_sms3_id="type-sms3",
    activity_type_ship_id="type-ship",
    activity_type_satisfaction_call_id="type-satisfaction-call",
)
_CFG_MISSING_SMS2 = DidarConfig(
    base_url="https://app.didar.me/api", api_key="test-key",
    activity_type_new_call_id="type-new-call",
    activity_type_sms1_id="type-sms1",
    activity_type_sms2_id="",
    activity_type_sms3_id="type-sms3",
    activity_type_ship_id="type-ship",
    activity_type_satisfaction_call_id="type-satisfaction-call",
)
_DUE = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
_REGISTERED = datetime(2026, 8, 27, 6, 0, tzinfo=timezone.utc)
_SHIP = datetime(2026, 8, 29, 18, 0, tzinfo=timezone.utc)


@respx.mock
def test_create_activity_sends_confirmed_fields():
    route = respx.post("https://app.didar.me/api/activity/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Id": "a-1"}})
    )

    client = DidarActivityClient(config=_CFG_WITH_TYPES)
    activity_id = client.create_activity(
        deal_id="deal-1", title="تماس جدید", activity_type_id="type-new-call", due_date=_DUE,
    )

    assert activity_id == "a-1"
    body = route.calls[0].request.content
    assert b'"ActivityTypeId":"type-new-call"' in body
    assert "تماس جدید".encode() in body
    assert b'"DealId":"deal-1"' in body
    assert b'"IsDone":false' in body
    assert b"OwnerId" not in body  # omitted when default_owner_id is unset


@respx.mock
def test_create_activity_includes_owner_id_when_configured():
    cfg = DidarConfig(
        base_url="https://app.didar.me/api", api_key="test-key",
        default_owner_id="owner-1",
    )
    route = respx.post("https://app.didar.me/api/activity/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Id": "a-1"}})
    )

    client = DidarActivityClient(config=cfg)
    client.create_activity(deal_id="deal-1", title="x", activity_type_id="t", due_date=_DUE)

    body = route.calls[0].request.content
    assert b'"OwnerId":"owner-1"' in body


@respx.mock
def test_create_post_sale_checklist_creates_every_item_in_order():
    route = respx.post("https://app.didar.me/api/activity/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Id": "a-x"}})
    )

    client = DidarActivityClient(config=_CFG_WITH_TYPES)
    client.create_post_sale_checklist(
        deal_id="deal-1", order_registered_at=_REGISTERED, ship_time=_SHIP,
    )

    assert route.call_count == len(POST_SALE_CHECKLIST)
    sent = [_json.loads(call.request.content)["Activity"] for call in route.calls]
    sent_titles = [a["Title"] for a in sent]
    assert sent_titles == [title for title, _ in POST_SALE_CHECKLIST]

    expected_due_dates = compute_checklist_due_dates(
        order_registered_at=_REGISTERED, ship_time=_SHIP,
    )
    for activity in sent:
        expected = expected_due_dates[activity["Title"]]
        expected_str = (
            expected.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.")
            + f"{expected.microsecond // 1000:03d}Z"
        )
        assert activity["DueDate"] == expected_str


@respx.mock
def test_create_post_sale_checklist_defaults_ship_time_when_missing():
    # Client instruction (2026-08-29): a source whose adapter doesn't
    # expose a real ship_time (everything except Basalam right now)
    # must still get the checklist, anchored to
    # order_registered_at + 2 days instead of being skipped.
    activity_route = respx.post("https://app.didar.me/api/activity/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Id": "a-x"}})
    )

    client = DidarActivityClient(config=_CFG_WITH_TYPES)
    client.create_post_sale_checklist(
        deal_id="deal-1", order_registered_at=_REGISTERED, ship_time=None,
    )

    assert activity_route.call_count == 6
    expected_due_dates = compute_checklist_due_dates(
        order_registered_at=_REGISTERED,
        ship_time=_REGISTERED + timedelta(days=2),
    )
    for call in activity_route.calls:
        activity = _json.loads(call.request.content)["Activity"]
        expected = expected_due_dates[activity["Title"]]
        expected_str = (
            expected.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.")
            + f"{expected.microsecond // 1000:03d}Z"
        )
        assert activity["DueDate"] == expected_str


@respx.mock
def test_create_post_sale_checklist_attaches_uploaded_photo_to_ship_item_only():
    upload_route = respx.post("https://app.didar.me/api/UploadFile").mock(
        return_value=httpx.Response(
            200,
            json={"Response": [{
                "Key": "photo-key-1.jpg", "Size": 123,
                "Type": "image/jpeg", "Name": "photo.jpg",
            }]},
        )
    )
    save_route = respx.post("https://app.didar.me/api/activity/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Id": "a-x"}})
    )

    client = DidarActivityClient(config=_CFG_WITH_TYPES)
    client.create_post_sale_checklist(
        deal_id="deal-1",
        order_registered_at=_REGISTERED,
        ship_time=_SHIP,
        ship_attachment=(b"fake-bytes", "photo.jpg", "image/jpeg"),
    )

    assert upload_route.call_count == 1
    sent = [_json.loads(call.request.content)["Activity"]["Title"] for call in save_route.calls]
    bodies = [_json.loads(call.request.content) for call in save_route.calls]
    ship_body = bodies[sent.index("ارسال محصول")]
    assert ship_body.get("NewAttachments") == [{"First": "photo-key-1.jpg", "Second": "photo.jpg"}]
    # every other item gets no attachment
    for title, body in zip(sent, bodies):
        if title != "ارسال محصول":
            assert "NewAttachments" not in body


@respx.mock
def test_create_post_sale_checklist_continues_without_attachment_when_upload_fails():
    respx.post("https://app.didar.me/api/UploadFile").mock(return_value=httpx.Response(500))
    route = respx.post("https://app.didar.me/api/activity/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Id": "a-x"}})
    )

    client = DidarActivityClient(config=_CFG_WITH_TYPES)
    client.create_post_sale_checklist(
        deal_id="deal-1",
        order_registered_at=_REGISTERED,
        ship_time=_SHIP,
        ship_attachment=(b"fake-bytes", "photo.jpg", "image/jpeg"),
    )

    # the whole checklist still gets created despite the upload failure
    assert route.call_count == len(POST_SALE_CHECKLIST)


@respx.mock
def test_create_post_sale_checklist_skipped_entirely_when_a_type_is_unconfigured():
    """All-or-nothing on config (see module docstring) - a half-built
    checklist would be more confusing than none."""
    route = respx.post("https://app.didar.me/api/activity/save")

    client = DidarActivityClient(config=_CFG_MISSING_SMS2)
    client.create_post_sale_checklist(
        deal_id="deal-1", order_registered_at=_REGISTERED, ship_time=_SHIP,
    )

    assert not route.called


@respx.mock
def test_create_post_sale_checklist_continues_after_one_item_fails():
    """One item's request failing must not stop the rest of the
    checklist from being attempted. Uses a 400 (non-retryable per
    http_utils.is_retryable_http_error) so the failure is immediate,
    rather than a 5xx which would trigger default_retry's real
    exponential-backoff sleeps and slow this test down."""
    call_count = {"n": 0}

    def _responder(request):
        call_count["n"] += 1
        if call_count["n"] == 2:
            return httpx.Response(400, json={"error": "boom"})
        return httpx.Response(200, json={"Response": {"Id": "a-x"}})

    route = respx.post("https://app.didar.me/api/activity/save").mock(side_effect=_responder)

    client = DidarActivityClient(config=_CFG_WITH_TYPES)
    client.create_post_sale_checklist(
        deal_id="deal-1", order_registered_at=_REGISTERED, ship_time=_SHIP,
    )

    # Every item was attempted (6 calls) despite the one 400 in the middle.
    assert route.call_count == len(POST_SALE_CHECKLIST)
