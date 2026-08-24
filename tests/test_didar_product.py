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


@respx.mock
def test_upsert_product_sends_code_and_title():
    respx.post("https://app.didar.me/api/product/categories").mock(
        return_value=httpx.Response(200, json=_CATEGORIES_RESPONSE)
    )
    route = respx.post("https://app.didar.me/api/product/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Product": {"Id": "p-1"}}})
    )

    cfg = DidarConfig(
        base_url="https://app.didar.me/api",
        api_key="test-key",
        default_product_category_id="cat-default",
    )
    client = DidarProductClient(config=cfg)
    product_id = client.upsert_product(code="SKU-A", title="گلدان خاتم ۳")

    assert product_id == "p-1"
    body = route.calls[0].request.content
    assert b'"Code":"SKU-A"' in body
    assert "گلدان خاتم ۳".encode() in body
    # keyword fallback should have matched "خاتم" from the title
    assert b'"ProductCategoryId":"cat-khatam"' in body


@respx.mock
def test_upsert_product_raises_clear_error_on_unrecognized_shape():
    respx.post("https://app.didar.me/api/product/categories").mock(
        return_value=httpx.Response(200, json=_CATEGORIES_RESPONSE)
    )
    respx.post("https://app.didar.me/api/product/save").mock(
        return_value=httpx.Response(200, json={"SomethingElse": True})
    )
    cfg = DidarConfig(
        base_url="https://app.didar.me/api",
        api_key="test-key",
        default_product_category_id="cat-default",
    )
    client = DidarProductClient(config=cfg)
    with pytest.raises(DidarApiError):
        client.upsert_product(code="x", title="محصول بدون کلیدواژه مرتبط")


@respx.mock
def test_upsert_product_exact_category_match_wins_over_keyword():
    respx.post("https://app.didar.me/api/product/categories").mock(
        return_value=httpx.Response(200, json=_CATEGORIES_RESPONSE)
    )
    route = respx.post("https://app.didar.me/api/product/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Product": {"Id": "p-2"}}})
    )
    cfg = DidarConfig(
        base_url="https://app.didar.me/api",
        api_key="test-key",
        default_product_category_id="cat-default",
    )
    client = DidarProductClient(config=cfg)
    # title would keyword-match "خاتم", but an exact marketplace category
    # name ("مینا") is provided and must win.
    client.upsert_product(code="SKU-B", title="جعبه خاتم", category="مینا")

    body = route.calls[0].request.content
    assert b'"ProductCategoryId":"cat-mina"' in body


@respx.mock
def test_upsert_product_falls_back_to_default_when_nothing_matches():
    respx.post("https://app.didar.me/api/product/categories").mock(
        return_value=httpx.Response(200, json=_CATEGORIES_RESPONSE)
    )
    route = respx.post("https://app.didar.me/api/product/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Product": {"Id": "p-3"}}})
    )
    cfg = DidarConfig(
        base_url="https://app.didar.me/api",
        api_key="test-key",
        default_product_category_id="cat-default",
    )
    client = DidarProductClient(config=cfg)
    client.upsert_product(code="SKU-C", title="یک محصول کاملا نامرتبط")

    body = route.calls[0].request.content
    assert b'"ProductCategoryId":"cat-default"' in body
