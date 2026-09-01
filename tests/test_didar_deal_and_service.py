from datetime import datetime, timezone
from decimal import Decimal

import respx
import httpx

from src.config import DidarConfig
from src.didar.activity_client import DidarActivityClient
from src.didar.contact_client import DidarContactClient
from src.didar.deal_client import DidarDealClient
from src.didar.product_client import DidarProductClient
from src.didar.service import DidarSyncService, _customer_code_for, _fetch_product_images
from src.marketplaces.base import NormalizedOrder, OrderItem

_CFG = DidarConfig(
    base_url="https://app.didar.me/api", api_key="test-key",
    pipeline_id="p1", pipeline_stage_id="stage-1",
    deal_label_title_tapsishop="تپسی",
    default_product_category_id="cat-default",
    # Explicitly blank (not left to field default_factory / real .env) so
    # these tests are hermetic: the post-sale checklist should be a no-op
    # unless a test deliberately configures real activity type Ids - see
    # test_sync_service_creates_post_sale_checklist_after_a_new_deal.
    activity_type_new_call_id="", activity_type_sms1_id="", activity_type_sms2_id="",
    activity_type_sms3_id="", activity_type_ship_id="", activity_type_satisfaction_call_id="",
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


# Shared Deal Labels list - Title "تپسی" matches _CFG's
# deal_label_title_tapsishop, resolving to "label-tapsishop-guid".
_DEAL_LABELS_RESPONSE = {
    "Response": [
        {"Id": "label-tapsishop-guid", "Title": "تپسی", "Code": 1, "Type": "Deal"},
    ]
}


def _mock_deal_labels():
    return respx.get("https://app.didar.me/api/Label/GetDealLabels").mock(
        return_value=httpx.Response(200, json=_DEAL_LABELS_RESPONSE)
    )


def _mock_deal_search_no_match():
    """sync_order() now calls DidarDealClient.find_existing_deal_id()
    (POST /search/search) before ever touching Contact/Deal creation -
    see service.py and deal_client.py. Tests that exercise the create
    path (i.e. don't care about the dedupe check itself) mock this to
    return no results, forcing the normal create flow they already
    expect."""
    return respx.post("https://app.didar.me/api/search/search").mock(
        return_value=httpx.Response(200, json={"Response": {"Total": 0, "List": []}})
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
def test_fetch_product_images_filename_ignores_query_string():
    """Regression test for a real production bug: Digikala's CDN URLs
    append image-transform params to the query string using "/" as a
    separator (e.g. "...?x-oss-process=image/resize,m_lfit/quality,q_60"),
    so naively taking the text after the LAST "/" in the full URL grabs a
    piece of the query string instead of the real filename. Confirmed
    live - every "ارسال محصول" attachment in production was named
    literally "quality,q_60" instead of the actual image filename.
    _fetch_product_images must derive the filename from the URL's path
    only, ignoring anything after "?"."""
    url = (
        "https://dkstatics-public.digikala.com/digikala-products/113noe.jpg"
        "?x-oss-process=image/resize,m_lfit/quality,q_60"
    )
    respx.get(url).mock(
        return_value=httpx.Response(200, content=b"fake-bytes", headers={"content-type": "image/jpeg"})
    )
    item = OrderItem(
        sku="SKU-1", title="Product A", quantity=2, unit_price=Decimal("100000"),
        final_price=Decimal("200000"), product_image_url=url,
    )
    order = NormalizedOrder(**{**_ORDER.__dict__, "items": [item]})

    result = _fetch_product_images(order)

    assert len(result) == 1
    file_bytes, filename, content_type = result[0]
    assert filename == "113noe.jpg"
    assert file_bytes == b"fake-bytes"
    assert content_type == "image/jpeg"


@respx.mock
def test_fetch_product_images_downloads_one_photo_per_line_item():
    """Regression test for the real production bug this fixes (client
    feedback, 2026-09): an order with more than one product must get a
    photo downloaded for EVERY item that has its own image URL, not
    just the first - previously only order.product_image_url (set from
    items[0] alone by every adapter) was ever read here."""
    url1 = "https://cdn.example.com/a.jpg"
    url2 = "https://cdn.example.com/b.jpg"
    respx.get(url1).mock(
        return_value=httpx.Response(200, content=b"bytes-a", headers={"content-type": "image/jpeg"})
    )
    respx.get(url2).mock(
        return_value=httpx.Response(200, content=b"bytes-b", headers={"content-type": "image/png"})
    )
    items = [
        OrderItem(sku="SKU-1", title="Product A", quantity=1, unit_price=Decimal("100000"),
                  final_price=Decimal("100000"), product_image_url=url1),
        OrderItem(sku="SKU-2", title="Product B", quantity=1, unit_price=Decimal("50000"),
                  final_price=Decimal("50000"), product_image_url=url2),
    ]
    order = NormalizedOrder(**{**_ORDER.__dict__, "items": items})

    result = _fetch_product_images(order)

    assert len(result) == 2
    assert result[0][0] == b"bytes-a"
    assert result[1][0] == b"bytes-b"


@respx.mock
def test_fetch_product_images_skips_duplicate_urls():
    """Two items pointing at the exact same photo must not download or
    attach it twice."""
    url = "https://cdn.example.com/same.jpg"
    route = respx.get(url).mock(
        return_value=httpx.Response(200, content=b"bytes", headers={"content-type": "image/jpeg"})
    )
    items = [
        OrderItem(sku="SKU-1", title="Product A", quantity=1, unit_price=Decimal("100000"),
                  final_price=Decimal("100000"), product_image_url=url),
        OrderItem(sku="SKU-2", title="Product B", quantity=1, unit_price=Decimal("50000"),
                  final_price=Decimal("50000"), product_image_url=url),
    ]
    order = NormalizedOrder(**{**_ORDER.__dict__, "items": items})

    result = _fetch_product_images(order)

    assert len(result) == 1
    assert route.call_count == 1


@respx.mock
def test_fetch_product_images_one_failed_download_does_not_block_others():
    """A broken/missing photo for one item must not prevent the other
    items' photos from being downloaded and attached."""
    good_url = "https://cdn.example.com/good.jpg"
    bad_url = "https://cdn.example.com/bad.jpg"
    respx.get(bad_url).mock(return_value=httpx.Response(404))
    respx.get(good_url).mock(
        return_value=httpx.Response(200, content=b"bytes-good", headers={"content-type": "image/jpeg"})
    )
    items = [
        OrderItem(sku="SKU-1", title="Product A", quantity=1, unit_price=Decimal("100000"),
                  final_price=Decimal("100000"), product_image_url=bad_url),
        OrderItem(sku="SKU-2", title="Product B", quantity=1, unit_price=Decimal("50000"),
                  final_price=Decimal("50000"), product_image_url=good_url),
    ]
    order = NormalizedOrder(**{**_ORDER.__dict__, "items": items})

    result = _fetch_product_images(order)

    assert len(result) == 1
    assert result[0][0] == b"bytes-good"


def test_fetch_product_images_falls_back_to_order_level_url_when_items_have_none():
    """An order whose items carry no image URL of their own (e.g. an
    adapter that hasn't been wired up yet) still falls back to the
    order-level product_image_url, same as before this fix."""
    with respx.mock:
        url = "https://cdn.example.com/order-level.jpg"
        respx.get(url).mock(
            return_value=httpx.Response(200, content=b"bytes", headers={"content-type": "image/jpeg"})
        )
        order = NormalizedOrder(**{**_ORDER.__dict__, "product_image_url": url})

        result = _fetch_product_images(order)

        assert len(result) == 1
        assert result[0][0] == b"bytes"


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
    route = respx.post("https://app.didar.me/api/deal/save_v2").mock(
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
    _mock_deal_labels()
    _mock_product_search_no_match()
    respx.post("https://app.didar.me/api/product/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Product": {"Id": "p-1"}}})
    )
    route = respx.post("https://app.didar.me/api/deal/save_v2").mock(
        return_value=httpx.Response(200, json={"Response": {"Deal": {"Id": "d-1"}}})
    )

    client = DidarDealClient(config=_CFG)
    client.create_deal(contact_id="c-1", display_name="Someone", order=_ORDER)

    body = route.calls[0].request.content
    assert b'"PersonId":"c-1"' in body  # confirmed via live testing - not ContactId
    assert b'"PipelineId":"p1"' in body  # matches _CFG.pipeline_id above
    assert b'"PipelineStageId":"stage-1"' in body
    assert b'"LabelIds":["label-tapsishop-guid"]' in body


@respx.mock
def test_create_deal_omits_label_when_source_not_mapped():
    _mock_categories()
    _mock_product_search_no_match()
    unmapped_order = NormalizedOrder(**{**_ORDER.__dict__, "source": "unmapped_source"})
    respx.post("https://app.didar.me/api/product/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Product": {"Id": "p-1"}}})
    )
    route = respx.post("https://app.didar.me/api/deal/save_v2").mock(
        return_value=httpx.Response(200, json={"Response": {"Deal": {"Id": "d-1"}}})
    )

    client = DidarDealClient(config=_CFG)
    client.create_deal(contact_id="c-1", display_name="Someone", order=unmapped_order)

    body = route.calls[0].request.content
    assert b"LabelId" not in body


@respx.mock
def test_create_deal_omits_label_when_title_not_found_in_didar():
    """Source has a configured Title, but GetDealLabels doesn't return a
    matching Deal Label (e.g. wrong/stale Title) - the deal must still
    be created, just without a label."""
    _mock_categories()
    _mock_product_search_no_match()
    respx.get("https://app.didar.me/api/Label/GetDealLabels").mock(
        return_value=httpx.Response(200, json={"Response": []})
    )
    respx.post("https://app.didar.me/api/product/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Product": {"Id": "p-1"}}})
    )
    route = respx.post("https://app.didar.me/api/deal/save_v2").mock(
        return_value=httpx.Response(200, json={"Response": {"Deal": {"Id": "d-1"}}})
    )

    client = DidarDealClient(config=_CFG)
    client.create_deal(contact_id="c-1", display_name="Someone", order=_ORDER)

    body = route.calls[0].request.content
    assert b"LabelId" not in body


@respx.mock
def test_description_includes_source_label_and_panel_link_for_marketplaces():
    _mock_categories()
    _mock_product_search_no_match()
    respx.post("https://app.didar.me/api/product/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Product": {"Id": "p-1"}}})
    )
    route = respx.post("https://app.didar.me/api/deal/save_v2").mock(
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
    route = respx.post("https://app.didar.me/api/deal/save_v2").mock(
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
    deal_route = respx.post("https://app.didar.me/api/deal/save_v2").mock(
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


@respx.mock
def test_deal_item_uses_catalog_code_and_title_when_excel_match_found(tmp_path):
    """Regression test for the client's Excel-catalog feature: when the
    item's marketplace title has a confident match in the client's Excel
    product catalog (product_catalog.py), the product must be
    searched/created using the CATALOG's Code and title - not the
    marketplace SKU/title - so it resolves to the product that already
    exists in Didar rather than creating a wrongly-named duplicate."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["_type", "عنوان محصول", "دسته بندی محصول", "کد دیدار محصول", "کد محصول"])
    ws.append(["Product", "راستین 1", None, 0, "146"])
    xlsx_path = tmp_path / "catalog.xlsx"
    wb.save(xlsx_path)

    cfg = DidarConfig(
        base_url=_CFG.base_url, api_key=_CFG.api_key,
        pipeline_id=_CFG.pipeline_id, pipeline_stage_id=_CFG.pipeline_stage_id,
        default_product_category_id=_CFG.default_product_category_id,
        product_catalog_xlsx=str(xlsx_path),
    )
    order = NormalizedOrder(
        source="digikala", source_order_id="1", order_number="1",
        created_at=datetime.now(timezone.utc), total_price=Decimal("100000"), status="new",
        items=[OrderItem(
            sku="DK-SOME-SKU", quantity=1, unit_price=Decimal("100000"), final_price=Decimal("100000"),
            title="ست هدیه مسی فراز هنر مدل راستین کد 1 | چند رنگ | گارانتی اصالت و سلامت فیزیکی کالا",
        )],
    )

    _mock_categories()
    search_route = respx.post("https://app.didar.me/api/product/search").mock(
        return_value=httpx.Response(200, json={"Response": [{"Id": "p-existing", "Code": "146"}]})
    )
    save_route = respx.post("https://app.didar.me/api/product/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Product": {"Id": "should-not-be-used"}}})
    )
    deal_route = respx.post("https://app.didar.me/api/deal/save_v2").mock(
        return_value=httpx.Response(200, json={"Response": {"Deal": {"Id": "d-1"}}})
    )

    client = DidarDealClient(config=cfg)
    client.create_deal(contact_id="c-1", display_name="Someone", order=order)

    search_body = search_route.calls[0].request.content
    assert b'"Keywords":"146"' in search_body
    assert not save_route.called  # matched existing catalog product - no create needed

    deal_body = deal_route.calls[0].request.content
    assert b'"ProductId":"p-existing"' in deal_body


@respx.mock
def test_deal_item_description_includes_order_number():
    """Regression test (client feedback, 2026-08): manually-entered deals
    have the order number typed into each item's توضیحات; auto-created
    deals previously left it blank entirely."""
    _mock_categories()
    _mock_product_search_no_match()
    respx.post("https://app.didar.me/api/product/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Product": {"Id": "p-1"}}})
    )
    route = respx.post("https://app.didar.me/api/deal/save_v2").mock(
        return_value=httpx.Response(200, json={"Response": {"Deal": {"Id": "d-1"}}})
    )

    client = DidarDealClient(config=_CFG)
    client.create_deal(contact_id="c-1", display_name="Someone", order=_ORDER)

    deal_body = route.calls[0].request.content
    assert "شماره سفارش: ORD-999".encode() in deal_body


@respx.mock
def test_deal_item_discount_reflects_gap_between_unit_and_final_price():
    """Regression test (client feedback, 2026-08): Discount was
    previously hardcoded to 0 for every item regardless of the source
    data, silently losing real per-item discounts (Digikala, Tapsi
    Shop, Faraz Honar and SnappShop all distinguish an original
    unit_price from a possibly-discounted final_price)."""
    _mock_categories()
    _mock_product_search_no_match()
    respx.post("https://app.didar.me/api/product/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Product": {"Id": "p-1"}}})
    )
    route = respx.post("https://app.didar.me/api/deal/save_v2").mock(
        return_value=httpx.Response(200, json={"Response": {"Deal": {"Id": "d-1"}}})
    )
    discounted_order = NormalizedOrder(**{
        **_ORDER.__dict__,
        "items": [
            OrderItem(
                sku="SKU-1", title="Product A", quantity=2,
                unit_price=Decimal("100000"),
                final_price=Decimal("180000"),  # 90000/unit actually charged -> 10000/unit discount
            )
        ],
    })

    client = DidarDealClient(config=_CFG)
    client.create_deal(contact_id="c-1", display_name="Someone", order=discounted_order)

    deal_body = route.calls[0].request.content
    assert b'"Discount":10000' in deal_body


@respx.mock
def test_deal_item_discount_never_goes_negative_when_final_price_is_higher():
    """A final_price higher than unit_price*quantity (e.g. tax/fees
    added on top by the source, not a discount) must clamp to 0, not go
    negative."""
    _mock_categories()
    _mock_product_search_no_match()
    respx.post("https://app.didar.me/api/product/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Product": {"Id": "p-1"}}})
    )
    route = respx.post("https://app.didar.me/api/deal/save_v2").mock(
        return_value=httpx.Response(200, json={"Response": {"Deal": {"Id": "d-1"}}})
    )
    marked_up_order = NormalizedOrder(**{
        **_ORDER.__dict__,
        "items": [
            OrderItem(
                sku="SKU-1", title="Product A", quantity=1,
                unit_price=Decimal("50200000"),
                final_price=Decimal("55220000"),  # +10%, e.g. tax - not a discount
            )
        ],
    })

    client = DidarDealClient(config=_CFG)
    client.create_deal(contact_id="c-1", display_name="Someone", order=marked_up_order)

    deal_body = route.calls[0].request.content
    assert b'"Discount":0' in deal_body


@respx.mock
def test_description_includes_unique_order_reference_for_dedupe():
    """The exact anchor string find_existing_deal_id() later searches
    for must actually be written into Description, or the dedupe check
    can never match anything."""
    _mock_categories()
    _mock_product_search_no_match()
    respx.post("https://app.didar.me/api/product/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Product": {"Id": "p-1"}}})
    )
    route = respx.post("https://app.didar.me/api/deal/save_v2").mock(
        return_value=httpx.Response(200, json={"Response": {"Deal": {"Id": "d-1"}}})
    )

    client = DidarDealClient(config=_CFG)
    client.create_deal(contact_id="c-1", display_name="Someone", order=_ORDER)

    body = route.calls[0].request.content
    assert b"tapsishop:999" in body


@respx.mock
def test_find_existing_deal_id_returns_none_when_search_has_no_match():
    _mock_categories()
    route = respx.post("https://app.didar.me/api/search/search").mock(
        return_value=httpx.Response(200, json={"Response": {"Total": 0, "List": []}})
    )

    client = DidarDealClient(config=_CFG)
    assert client.find_existing_deal_id(_ORDER) is None
    assert b'"Keyword":"tapsishop:999"' in route.calls[0].request.content
    assert b'"Types":["deal"]' in route.calls[0].request.content


@respx.mock
def test_find_existing_deal_id_matches_on_exact_reference_in_description():
    _mock_categories()
    respx.post("https://app.didar.me/api/search/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "Response": {
                    "Total": 1,
                    "List": [
                        {
                            "_tp": "deal",
                            "Id": "d-existing",
                            "Title": "معامله مشتری تپسی-999",
                            "Description": "فروشگاه: تپسی‌شاپ\nشماره سفارش: ORD-999\n"
                            "شناسه یکتای هماهنگ‌سازی: tapsishop:999",
                        }
                    ],
                }
            },
        )
    )

    client = DidarDealClient(config=_CFG)
    assert client.find_existing_deal_id(_ORDER) == "d-existing"


@respx.mock
def test_find_existing_deal_id_ignores_non_deal_results_and_partial_matches():
    """Fuzzy keyword search can return unrelated hits (a contact whose
    name happens to contain the search text, or a different order whose
    reference merely overlaps) - only an exact reference match on an
    actual `deal`-typed result counts."""
    _mock_categories()
    respx.post("https://app.didar.me/api/search/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "Response": {
                    "Total": 2,
                    "List": [
                        {"_tp": "contact", "Id": "c-1", "Description": "tapsishop:999"},
                        {"_tp": "deal", "Id": "d-1", "Description": "tapsishop:9999"},
                    ],
                }
            },
        )
    )

    client = DidarDealClient(config=_CFG)
    assert client.find_existing_deal_id(_ORDER) is None


@respx.mock
def test_sync_service_skips_contact_and_deal_creation_when_already_in_didar():
    """Core regression test: if Didar already has a Deal for this order
    (created earlier, e.g. by a retry after a lost response), sync_order
    must NOT create a second Contact or Deal - it returns the existing
    Deal Id instead."""
    _mock_categories()
    respx.post("https://app.didar.me/api/search/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "Response": {
                    "Total": 1,
                    "List": [
                        {
                            "_tp": "deal",
                            "Id": "d-already-there",
                            "Description": "شناسه یکتای هماهنگ‌سازی: tapsishop:999",
                        }
                    ],
                }
            },
        )
    )
    contact_route = respx.post("https://app.didar.me/api/contact/save")
    deal_route = respx.post("https://app.didar.me/api/deal/save")

    service = DidarSyncService(
        contact_client=DidarContactClient(config=_CFG),
        deal_client=DidarDealClient(config=_CFG, product_client=DidarProductClient(config=_CFG)),
    )
    deal_id = service.sync_order(_ORDER)

    assert deal_id == "d-already-there"
    assert not contact_route.called
    assert not deal_route.called


def test_customer_code_prefers_mobile_then_falls_back_to_synthetic():
    with_mobile = NormalizedOrder(**{**_ORDER.__dict__, "customer_mobile": "0912"})
    assert _customer_code_for(with_mobile) == "0912"
    assert _customer_code_for(_ORDER) == "tapsishop-999"  # no mobile -> synthetic


@respx.mock
def test_sync_service_calls_contact_then_deal_in_order():
    _mock_categories()
    _mock_deal_search_no_match()
    _mock_product_search_no_match()
    contact_route = respx.post("https://app.didar.me/api/contact/save").mock(
        return_value=httpx.Response(
            200, json={"Response": {"Contact": {"Id": "c-42", "DisplayName": "مشتری تست"}}}
        )
    )
    respx.post("https://app.didar.me/api/product/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Product": {"Id": "p-1"}}})
    )
    deal_route = respx.post("https://app.didar.me/api/deal/save_v2").mock(
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


@respx.mock
def test_sync_service_creates_post_sale_checklist_after_a_new_deal():
    """Core regression test for the checklist feature (client feedback,
    2026-08): a successful new-deal sync must attach the standard
    post-sale checklist Activities to that deal - see
    DidarActivityClient.POST_SALE_CHECKLIST."""
    _mock_categories()
    _mock_deal_search_no_match()
    _mock_product_search_no_match()
    respx.post("https://app.didar.me/api/contact/save").mock(
        return_value=httpx.Response(
            200, json={"Response": {"Contact": {"Id": "c-42", "DisplayName": "مشتری تست"}}}
        )
    )
    respx.post("https://app.didar.me/api/product/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Product": {"Id": "p-1"}}})
    )
    respx.post("https://app.didar.me/api/deal/save_v2").mock(
        return_value=httpx.Response(200, json={"Response": {"Deal": {"Id": "d-42"}}})
    )
    activity_route = respx.post("https://app.didar.me/api/activity/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Id": "a-x"}})
    )

    activity_cfg = DidarConfig(
        base_url="https://app.didar.me/api", api_key="test-key",
        activity_type_new_call_id="type-new-call",
        activity_type_sms1_id="type-sms1", activity_type_sms2_id="type-sms2",
        activity_type_sms3_id="type-sms3", activity_type_ship_id="type-ship",
        activity_type_satisfaction_call_id="type-satisfaction-call",
    )
    service = DidarSyncService(
        contact_client=DidarContactClient(config=_CFG),
        deal_client=DidarDealClient(config=_CFG, product_client=DidarProductClient(config=_CFG)),
        activity_client=DidarActivityClient(config=activity_cfg),
    )
    order_with_ship_time = NormalizedOrder(
        **{**_ORDER.__dict__, "ship_time": datetime(2026, 8, 12, 18, 0, tzinfo=timezone.utc)}
    )
    service.sync_order(order_with_ship_time)

    assert activity_route.call_count == 6
    first_call_body = activity_route.calls[0].request.content
    assert b'"DealId":"d-42"' in first_call_body


@respx.mock
def test_sync_service_does_not_recreate_checklist_when_deal_already_exists():
    """Companion to the dedupe test above - finding an already-existing
    Deal must skip the checklist too, not just Contact/Deal creation,
    or a retry would duplicate all 6 activities every time."""
    _mock_categories()
    respx.post("https://app.didar.me/api/search/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "Response": {
                    "Total": 1,
                    "List": [
                        {
                            "_tp": "deal",
                            "Id": "d-already-there",
                            "Description": "شناسه یکتای هماهنگ‌سازی: tapsishop:999",
                        }
                    ],
                }
            },
        )
    )
    activity_route = respx.post("https://app.didar.me/api/activity/save")

    activity_cfg = DidarConfig(
        base_url="https://app.didar.me/api", api_key="test-key",
        activity_type_new_call_id="type-new-call",
        activity_type_sms1_id="type-sms1", activity_type_sms2_id="type-sms2",
        activity_type_sms3_id="type-sms3", activity_type_ship_id="type-ship",
        activity_type_satisfaction_call_id="type-satisfaction-call",
    )
    service = DidarSyncService(
        contact_client=DidarContactClient(config=_CFG),
        deal_client=DidarDealClient(config=_CFG, product_client=DidarProductClient(config=_CFG)),
        activity_client=DidarActivityClient(config=activity_cfg),
    )
    service.sync_order(_ORDER)

    assert not activity_route.called