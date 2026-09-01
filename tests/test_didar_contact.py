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
    assert b'"LastName":"tapsishop-999"' in body
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
    assert b'"LastName":"basalam-42"' in body


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
def test_find_existing_contact_id_falls_back_to_mobile_phone_search():
    """
    Core regression test for the second production incident (2026-08-27,
    Faraz Honar orders 41905/42001): a Contact can already exist in
    Didar under a DIFFERENT CustomerCode than what this sync generates
    (order.customer_mobile), while sharing the same MobilePhone. The
    CustomerCode-keyed search finds nothing, so a second search by
    MobilePhone must be tried before giving up.
    """
    search_route = respx.post("https://app.didar.me/api/search/search")
    search_route.mock(
        side_effect=[
            # 1st call: search by CustomerCode ("09121234567") - no match,
            # since the existing Contact was filed under a different code.
            httpx.Response(200, json={"Response": {"Total": 0, "List": []}}),
            # 2nd call: search by MobilePhone - finds it.
            httpx.Response(
                200,
                json={
                    "Response": {
                        "Total": 1,
                        "List": [
                            {
                                "_tp": "contact",
                                "Id": "c-by-phone",
                                "CustomerCode": "some-older-different-code",
                                "MobilePhone": "09121234567",
                            }
                        ],
                    }
                },
            ),
        ]
    )
    client = DidarContactClient(config=_CFG)
    result = client.find_existing_contact_id(
        "09121234567", mobile_phone="09121234567"
    )
    assert result == "c-by-phone"
    assert search_route.call_count == 2


@respx.mock
def test_upsert_contact_finds_existing_by_mobile_phone_before_ever_saving():
    """
    End-to-end version of the incident above: upsert_contact() must
    resolve the existing Contact via the MobilePhone fallback BEFORE
    attempting /contact/save at all, so the call becomes a clean edit -
    no 400 "Duplicate contacts is not allowed" should happen in the
    first place for a Contact that already exists under another code.
    """
    respx.post("https://app.didar.me/api/search/search").mock(
        side_effect=[
            httpx.Response(200, json={"Response": {"Total": 0, "List": []}}),
            httpx.Response(
                200,
                json={
                    "Response": {
                        "Total": 1,
                        "List": [
                            {
                                "_tp": "contact",
                                "Id": "c-by-phone",
                                "CustomerCode": "some-older-different-code",
                                "MobilePhone": "09121234567",
                            }
                        ],
                    }
                },
            ),
        ]
    )
    save_route = respx.post("https://app.didar.me/api/contact/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Contact": {"Id": "c-by-phone"}}})
    )

    client = DidarContactClient(config=_CFG)
    result = client.upsert_contact(customer_code="09121234567", mobile_phone="09121234567")

    assert result.id == "c-by-phone"
    assert save_route.call_count == 1  # no duplicate 400, no retry needed
    body = save_route.calls[0].request.content
    assert b'"Id":"c-by-phone"' in body


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
    """Race: BOTH the CustomerCode and MobilePhone searches find nothing
    initially, but the Contact was created by something else between
    our search and this save - one recovery search+retry (now finding
    it via MobilePhone) instead of failing the whole order for a
    timing issue, same pattern as product_client.py's duplicate-code
    recovery."""
    search_route = respx.post("https://app.didar.me/api/search/search")
    search_route.mock(
        side_effect=[
            httpx.Response(200, json={"Response": {"Total": 0, "List": []}}),  # initial: by code
            httpx.Response(200, json={"Response": {"Total": 0, "List": []}}),  # initial: by phone
            httpx.Response(200, json={"Response": {"Total": 0, "List": []}}),  # recovery: by code
            httpx.Response(  # recovery: by phone - now it exists
                200,
                json={
                    "Response": {
                        "Total": 1,
                        "List": [
                            {
                                "_tp": "contact",
                                "Id": "c-race",
                                "CustomerCode": "some-other-code",
                                "MobilePhone": "09121234567",
                            }
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
    assert search_route.call_count == 4
    assert save_route.call_count == 2
    assert b'"Id":"c-race"' in save_route.calls[1].request.content


def _mock_get_locations(provinces=None, cities=None):
    """Mocks POST /shared/GetLocations - see contact_client.py's
    list_locations() docstring for the confirmed shape. No apikey
    query param and no body, unlike every other Didar endpoint."""
    return respx.post("https://app.didar.me/api/shared/GetLocations").mock(
        return_value=httpx.Response(
            200,
            json={
                "Response": {
                    "Countries": [{"Id": "ir", "Level": 0, "Title": "ایران", "ParentId": None}],
                    "Provinces": provinces or [],
                    "Cities": cities or [],
                }
            },
        )
    )


@respx.mock
def test_upsert_contact_sends_email_phone_zipcode_and_address_when_given():
    _mock_contact_search_no_match()
    route = respx.post("https://app.didar.me/api/contact/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Contact": {"Id": "c-1"}}})
    )
    client = DidarContactClient(config=_CFG)
    client.upsert_contact(
        customer_code="tapsishop-999",
        mobile_phone="09121234567",
        full_name="علی رضایی",
        email="ali@example.com",
        work_phone="02112345678",
        address="خیابان ولیعصر پلاک ۱۲",
        postal_code="1234567890",
    )

    body = route.calls[0].request.content
    assert b'"Email":"ali@example.com"' in body
    assert b'"Phone":"02112345678"' in body
    assert b'"ZipCode":"1234567890"' in body
    assert b'"Addresses"' in body
    assert b'"KeyValues"' in body
    assert "خیابان ولیعصر پلاک ۱۲".encode() in body


@respx.mock
def test_upsert_contact_omits_email_phone_zipcode_address_when_absent():
    """A source that provides none of these must not send empty
    placeholders - an update should never blank out a value Didar
    already has just because this order's source didn't supply it."""
    _mock_contact_search_no_match()
    route = respx.post("https://app.didar.me/api/contact/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Contact": {"Id": "c-1"}}})
    )
    client = DidarContactClient(config=_CFG)
    client.upsert_contact(customer_code="tapsishop-999", mobile_phone="09121234567")

    body = route.calls[0].request.content
    assert b'"Email"' not in body
    assert b'"Phone"' not in body
    assert b'"ZipCode"' not in body
    assert b'"Addresses"' not in body
    assert b'"ProvinceId"' not in body
    assert b'"CityId"' not in body


@respx.mock
def test_upsert_contact_resolves_province_and_city_ids():
    _mock_contact_search_no_match()
    _mock_get_locations(
        provinces=[{"Id": "prov-thr", "Level": 1, "Title": "تهران", "ParentId": "ir"}],
        cities=[{"Id": "city-thr", "Level": 2, "Title": "تهران", "ParentId": "prov-thr"}],
    )
    route = respx.post("https://app.didar.me/api/contact/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Contact": {"Id": "c-1"}}})
    )
    client = DidarContactClient(config=_CFG)
    client.upsert_contact(
        customer_code="x", province="تهران", city="تهران",
    )

    body = route.calls[0].request.content
    assert b'"ProvinceId":"prov-thr"' in body
    assert b'"CityId":"city-thr"' in body


@respx.mock
def test_upsert_contact_falls_back_to_city_name_when_province_unresolved():
    """A city can still resolve via the province-agnostic flat lookup
    even when the province name itself doesn't match anything."""
    _mock_contact_search_no_match()
    _mock_get_locations(
        provinces=[{"Id": "prov-thr", "Level": 1, "Title": "تهران", "ParentId": "ir"}],
        cities=[{"Id": "city-krj", "Level": 2, "Title": "کرج", "ParentId": "prov-alborz-unmatched"}],
    )
    route = respx.post("https://app.didar.me/api/contact/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Contact": {"Id": "c-1"}}})
    )
    client = DidarContactClient(config=_CFG)
    client.upsert_contact(customer_code="x", province="البرز", city="کرج")

    body = route.calls[0].request.content
    assert b'"ProvinceId"' not in body  # "البرز" doesn't match any Province title above
    assert b'"CityId":"city-krj"' in body  # still found by city name alone


@respx.mock
def test_upsert_contact_omits_location_ids_when_get_locations_fails():
    """A failed/unreachable GetLocations call must never break the
    Contact upsert - ProvinceId/CityId are just omitted."""
    _mock_contact_search_no_match()
    respx.post("https://app.didar.me/api/shared/GetLocations").mock(
        return_value=httpx.Response(500, json={"Message": "error"})
    )
    route = respx.post("https://app.didar.me/api/contact/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Contact": {"Id": "c-1"}}})
    )
    client = DidarContactClient(config=_CFG)
    client.upsert_contact(customer_code="x", province="تهران", city="تهران")

    body = route.calls[0].request.content
    assert b'"ProvinceId"' not in body
    assert b'"CityId"' not in body


@respx.mock
def test_get_locations_does_not_send_apikey_query_param():
    """Confirmed via client-supplied Didar docs: unlike every other
    endpoint in this module, GetLocations takes no apikey query param
    and no request body."""
    _mock_contact_search_no_match()
    locations_route = _mock_get_locations(
        provinces=[{"Id": "prov-thr", "Level": 1, "Title": "تهران", "ParentId": "ir"}],
    )
    respx.post("https://app.didar.me/api/contact/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Contact": {"Id": "c-1"}}})
    )
    client = DidarContactClient(config=_CFG)
    client.upsert_contact(customer_code="x", province="تهران")

    request = locations_route.calls[0].request
    assert "apikey" not in request.url.params
    assert request.content in (b"", b"{}") or request.content is None