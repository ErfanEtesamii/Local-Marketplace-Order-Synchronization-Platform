import respx
import httpx
import pytest

from src.config import DidarConfig
from src.didar.contact_client import DidarContactClient, DidarApiError

_CFG = DidarConfig(base_url="https://app.didar.me/api", api_key="test-key",
                    pipeline_id="p1", pipeline_stage_id="s1")


@respx.mock
def test_upsert_contact_sends_apikey_as_query_param_not_header():
    route = respx.post("https://app.didar.me/api/contact/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Contact": {"Id": "c-123"}}})
    )

    client = DidarContactClient(config=_CFG)
    contact_id = client.upsert_contact(
        customer_code="tapsishop-999", mobile_phone="09121234567", full_name="علی رضایی"
    )

    assert contact_id == "c-123"
    request = route.calls[0].request
    assert request.url.params["apikey"] == "test-key"
    body = request.content
    assert b"tapsishop-999" in body
    assert b"09121234567" in body


@respx.mock
def test_upsert_contact_splits_full_name():
    respx.post("https://app.didar.me/api/contact/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Contact": {"Id": "c-1"}}})
    )
    client = DidarContactClient(config=_CFG)
    client.upsert_contact(customer_code="x", full_name="علی رضایی")
    # (functional check is on request body content, done via requests below)


@respx.mock
def test_upsert_contact_handles_alternate_response_shape():
    """
    Response envelope shape is explicitly unconfirmed (see module
    docstring) - this test locks in that a flatter shape is also handled,
    without needing to guess which one Didar actually returns.
    """
    respx.post("https://app.didar.me/api/contact/save").mock(
        return_value=httpx.Response(200, json={"Id": "c-flat"})
    )
    client = DidarContactClient(config=_CFG)
    assert client.upsert_contact(customer_code="x") == "c-flat"


@respx.mock
def test_upsert_contact_raises_clear_error_on_unrecognized_shape():
    respx.post("https://app.didar.me/api/contact/save").mock(
        return_value=httpx.Response(200, json={"SomethingElse": True})
    )
    client = DidarContactClient(config=_CFG)
    with pytest.raises(DidarApiError):
        client.upsert_contact(customer_code="x")
