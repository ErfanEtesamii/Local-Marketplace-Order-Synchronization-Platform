import respx
import httpx
import pytest

from src.config import DidarConfig
from src.didar.contact_client import DidarApiError
from src.didar.product_client import DidarProductClient

_CFG = DidarConfig(base_url="https://app.didar.me/api", api_key="test-key")


@respx.mock
def test_upsert_product_sends_code_and_title():
    route = respx.post("https://app.didar.me/api/product/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Product": {"Id": "p-1"}}})
    )

    client = DidarProductClient(config=_CFG)
    product_id = client.upsert_product(code="SKU-A", title="گلدان خاتم ۳")

    assert product_id == "p-1"
    body = route.calls[0].request.content
    assert b'"Code":"SKU-A"' in body
    assert "گلدان خاتم ۳".encode() in body


@respx.mock
def test_upsert_product_raises_clear_error_on_unrecognized_shape():
    respx.post("https://app.didar.me/api/product/save").mock(
        return_value=httpx.Response(200, json={"SomethingElse": True})
    )
    client = DidarProductClient(config=_CFG)
    with pytest.raises(DidarApiError):
        client.upsert_product(code="x", title="y")
