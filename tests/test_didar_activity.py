from datetime import datetime, timezone

import respx
import httpx

from src.config import DidarConfig
from src.didar.activity_client import DidarActivityClient, POST_SALE_CHECKLIST

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
    client.create_post_sale_checklist(deal_id="deal-1", due_date=_DUE)

    assert route.call_count == len(POST_SALE_CHECKLIST)
    sent_titles = []
    for call in route.calls:
        import json as _json
        sent_titles.append(_json.loads(call.request.content)["Activity"]["Title"])
    assert sent_titles == [title for title, _ in POST_SALE_CHECKLIST]


@respx.mock
def test_create_post_sale_checklist_skipped_entirely_when_a_type_is_unconfigured():
    """All-or-nothing on config (see module docstring) - a half-built
    checklist would be more confusing than none."""
    route = respx.post("https://app.didar.me/api/activity/save")

    client = DidarActivityClient(config=_CFG_MISSING_SMS2)
    client.create_post_sale_checklist(deal_id="deal-1", due_date=_DUE)

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
    client.create_post_sale_checklist(deal_id="deal-1", due_date=_DUE)

    # Every item was attempted (6 calls) despite the one 400 in the middle.
    assert route.call_count == len(POST_SALE_CHECKLIST)
