from datetime import datetime, timezone
from decimal import Decimal

import respx
import httpx

from src.config import DidarConfig
from src.didar.contact_client import DidarContactClient
from src.didar.deal_client import DidarDealClient
from src.didar.product_client import DidarProductClient
from src.didar.service import DidarSyncService, _customer_code_for
from src.marketplaces.base import NormalizedOrder, OrderItem

_CFG = DidarConfig(
    base_url="https://app.didar.me/api", api_key="test-key",
    pipeline_id="p1", pipeline_stage_id="stage-1",
    label_tapsishop="label-tapsishop-guid",
    default_product_category_id="cat-default",
)

# Shared category list for tests that don't care about category resolution
# specifics - just needs /product/categories mocked so
# DidarProductClient._category_by_title_map() doesn't hit a real endpoint.
# None of these titles are expected to keyword-match "Product A" (the
# _ORDER fixture's item title), so those tests fall through to
# default_product_category_id="cat-default" as before this feature existed.
_CATEGORIES_RESPONSE = {
    "Response": [{"Id": "cat-default", "Title": "متفرقه"}]
}


def _mock_categories():
    return respx.post("https://app.didar.me/api/product/categories").mock(
        return_value=httpx.Response(200, json=_CATEGORIES_RESPONSE)
    )


def _mock_product_search_no_match():
    """upsert_product() now searches (POST /product/search) before
    ever calling /product/save - see product_client.py's module
    docstring. None of these tests care about that lookup finding
    anything, so it's always mocked to return no results, forcing the
    create path these tests were already written to expect."""
    return respx.post("https://app.didar.me/api/product/search").mock(
        return_value=httpx.Response(200, json={"Response": []})
    )


_ORDER = NormalizedOrder(
    source="tapsishop",
    source_order_id="999",
    order_number="ORD-999",
    created_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
    total_price=Decimal("250000"),
    status="confirmed",
    items=[OrderItem(sku="SKU-1", title="Product A", quantity=2, unit_price=Decimal("100000"),
                      final_price=Decimal("200000"))],
    customer_mobile=None,
    customer_full_name=None,
)


@respx.mock
def test_create_deal_title_uses_didar_default_convention():
    """
    Regression test: Title was originally "{order_number} - {source}",
    which the client asked to be replaced with Didar's own default
    convention: "معامله {display_name}".
    """
    _mock_categories()
    _mock_product_search_no_match()
    respx.post("https://app.didar.me/api/product/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Product": {"Id": "p-1"}}})
    )
    route = respx.post("https://app.didar.me/api/deal/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Deal": {"Id": "d-1"}}})
    )

    client = DidarDealClient(config=_CFG)
    deal_id = client.create_deal(contact_id="c-1", display_name="مشتری تپسی-999", order=_ORDER)

    assert deal_id == "d-1"
    body = route.calls[0].request.content
    assert "معامله مشتری تپسی-999".encode() in body


@respx.mock
def test_create_deal_sends_person_id_pipeline_stage_and_label():
    _mock_categories()
    _mock_product_search_no_match()
    respx.post("https://app.didar.me/api/product/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Product": {"Id": "p-1"}}})
    )
    route = respx.post("https://app.didar.me/api/deal/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Deal": {"Id": "d-1"}}})
    )

    client = DidarDealClient(config=_CFG)
    client.create_deal(contact_id="c-1", display_name="Someone", order=_ORDER)

    body = route.calls[0].request.content
    assert b'"PersonId":"c-1"' in body  # confirmed via live testing - not ContactId
    assert b'"PipelineStageId":"stage-1"' in body
    assert b'"LabelId":"label-tapsishop-guid"' in body


@respx.mock
def test_create_deal_omits_label_when_source_not_mapped():
    _mock_categories()
    _mock_product_search_no_match()
    unmapped_order = NormalizedOrder(**{**_ORDER.__dict__, "source": "unmapped_source"})
    respx.post("https://app.didar.me/api/product/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Product": {"Id": "p-1"}}})
    )
    route = respx.post("https://app.didar.me/api/deal/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Deal": {"Id": "d-1"}}})
    )

    client = DidarDealClient(config=_CFG)
    client.create_deal(contact_id="c-1", display_name="Someone", order=unmapped_order)

    body = route.calls[0].request.content
    assert b"LabelId" not in body


@respx.mock
def test_description_includes_source_label_and_panel_link_for_marketplaces():
    _mock_categories()
    _mock_product_search_no_match()
    respx.post("https://app.didar.me/api/product/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Product": {"Id": "p-1"}}})
    )
    route = respx.post("https://app.didar.me/api/deal/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Deal": {"Id": "d-1"}}})
    )

    client = DidarDealClient(config=_CFG)
    client.create_deal(contact_id="c-1", display_name="Someone", order=_ORDER)

    body = route.calls[0].request.content
    assert "تپسی‌شاپ".encode() in body
    assert b"ORD-999" in body
    assert b"vendor.tapsi.shop" in body


@respx.mock
def test_description_includes_direct_order_link_for_farazhonar():
    """
    Faraz Honar is the one source with a confirmed real per-order deep
    link (standard WooCommerce wp-admin URL), unlike the four
    marketplaces which only get a link to the vendor panel's home page.
    """
    _mock_categories()
    _mock_product_search_no_match()
    faraz_order = NormalizedOrder(**{**_ORDER.__dict__, "source": "farazhonar", "source_order_id": "555"})
    respx.post("https://app.didar.me/api/product/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Product": {"Id": "p-1"}}})
    )
    route = respx.post("https://app.didar.me/api/deal/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Deal": {"Id": "d-1"}}})
    )

    client = DidarDealClient(config=_CFG)
    client.create_deal(contact_id="c-1", display_name="Someone", order=faraz_order)

    body = route.calls[0].request.content
    assert b"post=555&action=edit" in body


@respx.mock
def test_create_deal_builds_structured_deal_items_not_description_text():
    """
    Regression test for the core requirement of this rewrite: line
    items must be structured DealItems with a real ProductId, not text
    dumped into Description.
    """
    _mock_categories()
    _mock_product_search_no_match()
    product_route = respx.post("https://app.didar.me/api/product/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Product": {"Id": "p-999"}}})
    )
    deal_route = respx.post("https://app.didar.me/api/deal/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Deal": {"Id": "d-1"}}})
    )

    client = DidarDealClient(config=_CFG)
    client.create_deal(contact_id="c-1", display_name="Someone", order=_ORDER)

    # Product was upserted using the item's SKU and exact platform title.
    product_body = product_route.calls[0].request.content
    assert b'"Code":"SKU-1"' in product_body
    assert b'"Title":"Product A"' in product_body

    deal_body = deal_route.calls[0].request.content
    assert b'"ProductId":"p-999"' in deal_body
    assert b'"Quantity":2' in deal_body
    assert b'"UnitPrice":100000' in deal_body


def test_customer_code_prefers_mobile_then_falls_back_to_synthetic():
    with_mobile = NormalizedOrder(**{**_ORDER.__dict__, "customer_mobile": "0912"})
    assert _customer_code_for(with_mobile) == "0912"
    assert _customer_code_for(_ORDER) == "tapsishop-999"  # no mobile -> synthetic


@respx.mock
def test_sync_service_calls_contact_then_deal_in_order():
    _mock_categories()
    _mock_product_search_no_match()
    contact_route = respx.post("https://app.didar.me/api/contact/save").mock(
        return_value=httpx.Response(
            200, json={"Response": {"Contact": {"Id": "c-42", "DisplayName": "مشتری تست"}}}
        )
    )
    respx.post("https://app.didar.me/api/product/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Product": {"Id": "p-1"}}})
    )
    deal_route = respx.post("https://app.didar.me/api/deal/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Deal": {"Id": "d-42"}}})
    )

    service = DidarSyncService(
        contact_client=DidarContactClient(config=_CFG),
        deal_client=DidarDealClient(config=_CFG, product_client=DidarProductClient(config=_CFG)),
    )
    deal_id = service.sync_order(_ORDER)

    assert deal_id == "d-42"
    assert contact_route.called
    assert deal_route.called
    # Deal must be created with the Id AND display name from the Contact call.
    deal_body = deal_route.calls[0].request.content
    assert b"c-42" in deal_body
    assert "مشتری تست".encode() in deal_body