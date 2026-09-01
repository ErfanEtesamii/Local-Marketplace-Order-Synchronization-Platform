import respx
import httpx
import pytest

from src.config import DidarConfig
from src.didar.contact_client import DidarApiError
from src.didar.product_client import DidarProductClient

_CATEGORIES_RESPONSE = {
    "Response": [
        {"Id": "cat-khatam", "Title": "خاتم"},
        {"Id": "cat-mina", "Title": "مینا"},
        {"Id": "cat-default", "Title": "متفرقه"},
    ]
}

_CFG = DidarConfig(
    base_url="https://app.didar.me/api",
    api_key="test-key",
    default_product_category_id="cat-default",
)


def _mock_categories():
    return respx.post("https://app.didar.me/api/product/categories").mock(
        return_value=httpx.Response(200, json=_CATEGORIES_RESPONSE)
    )


def _mock_getproductbycodes_no_match():
    """search_by_code() now tries POST /product/getproductbycodes
    (an exact Code lookup) FIRST - see product_client.py's module
    docstring for the 2026-09 production incident (order 364139925)
    that replaced the old GetProductsList-based cache with this call.
    Every test that expects the create (/product/save) path must mock
    this to return no match, since it's consulted before /product/save
    is ever attempted."""
    return respx.post("https://app.didar.me/api/product/getproductbycodes").mock(
        return_value=httpx.Response(200, json={"Response": {"Total": 0, "Products": []}})
    )


def _mock_search_no_match():
    """/product/search is now only reached as a fallback when
    getproductbycodes itself raises (network/server error) - see
    search_by_code(). Kept for that fallback path's own tests."""
    return respx.post("https://app.didar.me/api/product/search").mock(
        return_value=httpx.Response(200, json={"Response": []})
    )


@respx.mock
def test_upsert_product_includes_confirmed_required_fields():
    """
    Regression test for the real "Product Not Exist" incident: the
    create call was missing TitleForInvoice and Unit, which Didar's own
    docs confirm are required (not the ProductCategoryId that was
    originally suspected - that was already being sent correctly).
    """
    _mock_categories()
    _mock_getproductbycodes_no_match()
    route = respx.post("https://app.didar.me/api/product/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Product": {"Id": "p-1"}}})
    )

    client = DidarProductClient(config=_CFG)
    client.upsert_product(code="SKU-A", title="گلدان خاتم ۳", unit_price=20000, final_price=15000)

    body = route.calls[0].request.content
    # Original price (before discount) is preserved in TitleForInvoice so
    # it's visible in the Didar catalog; the discounted final price is
    # what Didar uses as the product's own UnitPrice.
    assert b"\xd8\xae\xd8\xa7\xd8\xaa\xd9\x85 \xdb\xb3 - 20000" in body
    assert b'"Unit":"\xd8\xb9\xd8\xaf\xd8\xaf"' in body or b'"Unit":"\xd8\xb9\xd8\xaf\xd8\xaf"' in body or "\"Unit\":\"عدد\"".encode() in body
    assert b'"UnitPrice":15000' in body


@respx.mock
def test_upsert_product_creates_when_lookup_finds_nothing():
    _mock_categories()
    _mock_getproductbycodes_no_match()
    route = respx.post("https://app.didar.me/api/product/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Product": {"Id": "p-1"}}})
    )

    client = DidarProductClient(config=_CFG)
    product_id = client.upsert_product(code="SKU-A", title="گلدان خاتم ۳")

    assert product_id == "p-1"
    body = route.calls[0].request.content
    assert b'"Code":"SKU-A"' in body
    assert "گلدان خاتم ۳".encode() in body
    # keyword fallback should have matched "خاتم" from the title
    assert b'"ProductCategoryId":"cat-khatam"' in body


@respx.mock
def test_upsert_product_uses_existing_id_from_getproductbycodes_without_calling_save():
    _mock_categories()
    respx.post("https://app.didar.me/api/product/getproductbycodes").mock(
        return_value=httpx.Response(
            200,
            json={
                "Response": {
                    "Total": 1,
                    "Products": [{"Id": "existing-p-1", "Code": "SKU-A", "Title": "گلدان خاتم ۳"}],
                }
            },
        )
    )
    save_route = respx.post("https://app.didar.me/api/product/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Product": {"Id": "should-not-be-used"}}})
    )

    client = DidarProductClient(config=_CFG)
    product_id = client.upsert_product(code="SKU-A", title="گلدان خاتم ۳")

    assert product_id == "existing-p-1"
    assert not save_route.called  # exact lookup must skip create entirely


@respx.mock
def test_search_falls_back_to_product_search_and_ignores_non_exact_matches():
    """If getproductbycodes itself fails (network/server error),
    search_by_code() falls back to /product/search - but Keywords
    there is a full-text search, not an exact filter, so a result
    whose Code only partially matches must still not be used."""
    _mock_categories()
    respx.post("https://app.didar.me/api/product/getproductbycodes").mock(
        return_value=httpx.Response(500, json={"Error": "internal error"})
    )
    respx.post("https://app.didar.me/api/product/search").mock(
        return_value=httpx.Response(
            200,
            json={"Response": [{"Id": "wrong-product", "Code": "SKU-A-VARIANT", "Title": "چیز دیگر"}]},
        )
    )
    route = respx.post("https://app.didar.me/api/product/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Product": {"Id": "p-created"}}})
    )

    client = DidarProductClient(config=_CFG)
    product_id = client.upsert_product(code="SKU-A", title="یک محصول کاملا نامرتبط")

    assert product_id == "p-created"
    assert route.called


@respx.mock
def test_search_by_code_finds_product_missed_by_keyword_search():
    """Regression test for the 2026-09 production incident: a real,
    pre-existing product with a short/generic catalog Code (e.g. "38")
    didn't rank within /product/search's Limit among thousands of
    products, so the old GetProductsList-based lookup wrongly reported
    "not found" - and then /product/save correctly failed with
    "duplicate product code", failing the whole order.

    Fix: search_by_code() now uses the exact POST
    /product/getproductbycodes lookup and must find the product
    WITHOUT ever needing /product/search - so /product/search is
    mocked here too but asserted as never called, and /product/save
    is not mocked at all (calling it would fail the test), proving
    upsert_product never even attempts a duplicate create.
    """
    _mock_categories()
    respx.post("https://app.didar.me/api/product/getproductbycodes").mock(
        return_value=httpx.Response(
            200,
            json={
                "Response": {
                    "Total": 1,
                    "Products": [{"Id": "existing-38", "Code": "38", "Title": "چکامه 12"}],
                }
            },
        )
    )
    search_route = respx.post("https://app.didar.me/api/product/search").mock(
        return_value=httpx.Response(200, json={"Response": []})
    )

    client = DidarProductClient(config=_CFG)
    product_id = client.upsert_product(code="38", title="چکامه 12")

    assert product_id == "existing-38"
    assert not search_route.called


@respx.mock
def test_upsert_product_recovers_via_getproductbycodes_on_duplicate_code_race():
    """If save fails with "duplicate product code" (another writer
    created it between our lookup and this save call), recover by
    looking it up again instead of failing the whole order."""
    _mock_categories()
    lookup_route = respx.post("https://app.didar.me/api/product/getproductbycodes").mock(
        side_effect=[
            httpx.Response(200, json={"Response": {"Total": 0, "Products": []}}),  # first: nothing yet
            httpx.Response(  # recovery lookup: now it exists
                200,
                json={
                    "Response": {
                        "Total": 1,
                        "Products": [{"Id": "recovered-id", "Code": "SKU-RACE"}],
                    }
                },
            ),
        ]
    )
    respx.post("https://app.didar.me/api/product/save").mock(
        return_value=httpx.Response(400, json={"Error": "duplicate product code."})
    )

    client = DidarProductClient(config=_CFG)
    product_id = client.upsert_product(code="SKU-RACE", title="یک محصول کاملا نامرتبط")

    assert product_id == "recovered-id"
    assert lookup_route.call_count == 2


@respx.mock
def test_upsert_product_reraises_other_save_errors():
    """A save failure that ISN'T "duplicate product code" must not be
    swallowed - it needs to surface so the order lands in retry/failure
    tracking rather than silently vanishing."""
    _mock_categories()
    _mock_getproductbycodes_no_match()
    respx.post("https://app.didar.me/api/product/save").mock(
        return_value=httpx.Response(400, json={"Error": "Product Not Exist"})
    )

    client = DidarProductClient(config=_CFG)
    with pytest.raises(httpx.HTTPStatusError):
        client.upsert_product(code="SKU-B", title="یک محصول کاملا نامرتبط")


@respx.mock
def test_upsert_product_raises_clear_error_on_unrecognized_shape():
    _mock_categories()
    _mock_getproductbycodes_no_match()
    respx.post("https://app.didar.me/api/product/save").mock(
        return_value=httpx.Response(200, json={"SomethingElse": True})
    )
    client = DidarProductClient(config=_CFG)
    with pytest.raises(DidarApiError):
        client.upsert_product(code="x", title="محصول بدون کلیدواژه مرتبط")


@respx.mock
def test_upsert_product_exact_category_match_wins_over_keyword():
    _mock_categories()
    _mock_getproductbycodes_no_match()
    route = respx.post("https://app.didar.me/api/product/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Product": {"Id": "p-2"}}})
    )
    client = DidarProductClient(config=_CFG)
    # title would keyword-match "خاتم", but an exact marketplace category
    # name ("مینا") is provided and must win.
    client.upsert_product(code="SKU-C", title="جعبه خاتم", category="مینا")

    body = route.calls[0].request.content
    assert b'"ProductCategoryId":"cat-mina"' in body


@respx.mock
def test_upsert_product_falls_back_to_default_when_nothing_matches():
    _mock_categories()
    _mock_getproductbycodes_no_match()
    route = respx.post("https://app.didar.me/api/product/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Product": {"Id": "p-3"}}})
    )
    client = DidarProductClient(config=_CFG)
    client.upsert_product(code="SKU-D", title="یک محصول کاملا نامرتبط")

    body = route.calls[0].request.content
    assert b'"ProductCategoryId":"cat-default"' in body