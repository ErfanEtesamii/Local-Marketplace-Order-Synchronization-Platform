import respx
import httpx
import pytest

from src.config import DidarConfig
from src.didar.contact_client import DidarContactClient, DidarApiError

_CFG = DidarConfig(base_url="https://app.didar.me/api", api_key="test-key",
                    pipeline_id="p1", pipeline_stage_id="s1")


def _mock_contact_search_no_match():
    """upsert_contact() now searches (POST /search/search, Types=["contact"])
    before ever calling /contact/save - see contact_client.py's module
    docstring. Tests that only care about the create path mock this to
    return no results, forcing the create-without-Id flow they already
    expect."""
    return respx.post("https://app.didar.me/api/search/search").mock(
        return_value=httpx.Response(200, json={"Response": {"Total": 0, "List": []}})
    )


@respx.mock
def test_upsert_contact_returns_id_and_display_name():
    _mock_contact_search_no_match()
    route = respx.post("https://app.didar.me/api/contact/save").mock(
        return_value=httpx.Response(
            200,
            json={"Response": {"Contact": {"Id": "c-123", "DisplayName": "علی رضایی"}}},
        )
    )

    client = DidarContactClient(config=_CFG)
    result = client.upsert_contact(
        customer_code="tapsishop-999", mobile_phone="09121234567", full_name="علی رضایی"
    )

    assert result.id == "c-123"
    assert result.display_name == "علی رضایی"
    request = route.calls[0].request
    assert request.url.params["apikey"] == "test-key"
    body = request.content
    assert b"tapsishop-999" in body
    assert b"09121234567" in body
    assert b'"Id"' not in body  # no existing contact found -> plain create


@respx.mock
def test_upsert_contact_handles_alternate_response_shape():
    """
    Response envelope shape has a confirmed primary shape, but this
    locks in that a flatter shape is also handled defensively as a
    safety net, without needing to guess which one Didar actually
    returns in every edge case.
    """
    _mock_contact_search_no_match()
    respx.post("https://app.didar.me/api/contact/save").mock(
        return_value=httpx.Response(200, json={"Id": "c-flat", "DisplayName": "Flat Name"})
    )
    client = DidarContactClient(config=_CFG)
    result = client.upsert_contact(customer_code="x")
    assert result.id == "c-flat"
    assert result.display_name == "Flat Name"


@respx.mock
def test_upsert_contact_raises_clear_error_on_unrecognized_shape():
    _mock_contact_search_no_match()
    respx.post("https://app.didar.me/api/contact/save").mock(
        return_value=httpx.Response(200, json={"SomethingElse": True})
    )
    client = DidarContactClient(config=_CFG)
    with pytest.raises(DidarApiError):
        client.upsert_contact(customer_code="x")


@respx.mock
def test_missing_full_name_falls_back_to_customer_code_as_lastname():
    """
    Regression test: Didar rejects an empty LastName with a 400
    ("LastName can not be empty"). Tapsi Shop and Digikala never
    provide a customer name at all (see their adapters' docstrings),
    so full_name=None must not result in an empty Lastname.
    """
    _mock_contact_search_no_match()
    route = respx.post("https://app.didar.me/api/contact/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Contact": {"Id": "c-1"}}})
    )
    client = DidarContactClient(config=_CFG)
    client.upsert_contact(customer_code="tapsishop-999")

    body = route.calls[0].request.content
    assert b'"Lastname":"tapsishop-999"' in body
    assert b'"FirstName":"' in body and b'""FirstName":""' not in body  # non-empty placeholder


@respx.mock
def test_single_word_full_name_also_falls_back_to_customer_code_as_lastname():
    """Same problem, different trigger: a one-word name (no space) also
    leaves _split_name's last_name empty."""
    _mock_contact_search_no_match()
    route = respx.post("https://app.didar.me/api/contact/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Contact": {"Id": "c-1"}}})
    )
    client = DidarContactClient(config=_CFG)
    client.upsert_contact(customer_code="basalam-42", full_name="Cher")

    body = route.calls[0].request.content
    assert b'"FirstName":"Cher"' in body
    assert b'"Lastname":"basalam-42"' in body


@respx.mock
def test_find_existing_contact_id_returns_none_when_no_match():
    respx.post("https://app.didar.me/api/search/search").mock(
        return_value=httpx.Response(200, json={"Response": {"Total": 0, "List": []}})
    )
    client = DidarContactClient(config=_CFG)
    assert client.find_existing_contact_id("09121234567") is None


@respx.mock
def test_find_existing_contact_id_matches_exact_customer_code():
    respx.post("https://app.didar.me/api/search/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "Response": {
                    "Total": 1,
                    "List": [
                        {"_tp": "contact", "Id": "c-existing", "CustomerCode": "09121234567"}
                    ],
                }
            },
        )
    )
    client = DidarContactClient(config=_CFG)
    assert client.find_existing_contact_id("09121234567") == "c-existing"


@respx.mock
def test_upsert_contact_includes_existing_id_so_didar_edits_not_creates():
    """
    Core regression test for a real production incident (2026-08): a
    repeat customer's second order failed with a 400 'Duplicate contacts
    is not allowed' because /contact/save does NOT reliably auto-upsert
    by CustomerCode alone. Finding the existing Contact first and
    including its Id turns the call into an edit.
    """
    respx.post("https://app.didar.me/api/search/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "Response": {
                    "Total": 1,
                    "List": [
                        {"_tp": "contact", "Id": "c-existing", "CustomerCode": "09121234567"}
                    ],
                }
            },
        )
    )
    route = respx.post("https://app.didar.me/api/contact/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Contact": {"Id": "c-existing"}}})
    )

    client = DidarContactClient(config=_CFG)
    result = client.upsert_contact(customer_code="09121234567", mobile_phone="09121234567")

    assert result.id == "c-existing"
    body = route.calls[0].request.content
    assert b'"Id":"c-existing"' in body


@respx.mock
def test_upsert_contact_recovers_via_search_on_duplicate_race():
    """Race: search found nothing, but the Contact was created by
    something else between our search and this save - one recovery
    search+retry instead of failing the whole order for a timing issue,
    same pattern as product_client.py's duplicate-code recovery."""
    search_route = respx.post("https://app.didar.me/api/search/search")
    search_route.mock(
        side_effect=[
            httpx.Response(200, json={"Response": {"Total": 0, "List": []}}),
            httpx.Response(
                200,
                json={
                    "Response": {
                        "Total": 1,
                        "List": [
                            {"_tp": "contact", "Id": "c-race", "CustomerCode": "09121234567"}
                        ],
                    }
                },
            ),
        ]
    )
    save_route = respx.post("https://app.didar.me/api/contact/save")
    save_route.mock(
        side_effect=[
            httpx.Response(400, json={"Message": "Duplicate contacts is not allowed"}),
            httpx.Response(200, json={"Response": {"Contact": {"Id": "c-race"}}}),
        ]
    )

    client = DidarContactClient(config=_CFG)
    result = client.upsert_contact(customer_code="09121234567", mobile_phone="09121234567")

    assert result.id == "c-race"
    assert save_route.call_count == 2
    assert b'"Id":"c-race"' in save_route.calls[1].request.content

