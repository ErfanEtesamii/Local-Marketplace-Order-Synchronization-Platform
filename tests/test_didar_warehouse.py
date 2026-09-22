"""
Tests for the Didar side of the Digikala FBD feature
("ارسال به انبار دیجی‌کالا") - stage 4: the Deal itself.

Deliberately a separate file from tests/test_didar_deal_and_service.py:
none of the existing Deal/Contact/Service behaviour changes for this
feature, and these tests must not be able to keep passing by accident if
create_deal()/find_existing_deal_id()/_build_deal_item() are refactored.

The ship Activity (stage 5) and the sync service/engine wiring (stage 6)
are covered separately once they exist.
"""
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import respx

from src.config import DidarConfig
from src.didar.deal_client import (
    WAREHOUSE_DEAL_TITLE,
    DidarDealClient,
    _build_warehouse_description,
    _warehouse_reference,
)
from src.didar.activity_client import (
    POST_SALE_CHECKLIST,
    SHIP_ACTIVITY_TITLE,
    DidarActivityClient,
)
from src.didar.product_client import DidarProductClient
from src.didar.warehouse_service import DidarWarehouseSyncService
from src.marketplaces.warehouse_base import WarehouseShipmentItem

_CFG = DidarConfig(
    base_url="https://app.didar.me/api", api_key="test-key",
    pipeline_id="p1", pipeline_stage_id="stage-1",
    # CLIENT DECISION: FBD deals carry the first store's existing
    # "دیجی کالا" label, not a label of their own.
    deal_label_title_digikala="دیجی کالا",
    # Required: create_warehouse_shipment_deal() now raises before ever
    # calling Didar if this is blank.
    warehouse_placeholder_person_id="placeholder-person-1",
    default_product_category_id="cat-default",
    # Explicitly blank (not left to the field's default_factory, which
    # reads the real .env) so these tests never load the client's actual
    # Excel catalog - otherwise a real catalog entry could resolve the
    # test item's title to a real Code and quietly change what
    # _build_warehouse_deal_item() sends.
    product_catalog_xlsx="",
)

_ITEM = WarehouseShipmentItem(
    source="digikala_warehouse",
    source_shipment_id="55123",
    product_title="قاب بشقاب 25 میناکاری",
    quantity=2,
    unit_price=Decimal("3000000"),
    created_at=datetime(2026, 9, 11, 5, 19, tzinfo=timezone.utc),
    product_image_url="https://dkstatics-public.digikala.com/example.jpg",
    commitment_date=datetime(2026, 9, 14, tzinfo=timezone.utc),
    order_id="382920341",
    supplier_code="25",
)


def _client():
    return DidarDealClient(config=_CFG, product_client=DidarProductClient(config=_CFG))


def _mock_categories():
    return respx.post("https://app.didar.me/api/product/categories").mock(
        return_value=httpx.Response(
            200, json={"Response": [{"Id": "cat-default", "Title": "متفرقه"}]}
        )
    )


def _mock_deal_labels():
    return respx.get("https://app.didar.me/api/Label/GetDealLabels").mock(
        return_value=httpx.Response(
            200,
            json={
                "Response": [
                    {"Id": "label-digikala-guid", "Title": "دیجی کالا", "Code": 1, "Type": "Deal"},
                ]
            },
        )
    )


def _mock_product_lookup_no_match():
    respx.post("https://app.didar.me/api/product/getproductbycodes").mock(
        return_value=httpx.Response(200, json={"Response": {"Total": 0, "Products": []}})
    )
    respx.post("https://app.didar.me/api/product/search").mock(
        return_value=httpx.Response(200, json={"Response": []})
    )


def _mock_product_save():
    return respx.post("https://app.didar.me/api/product/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Product": {"Id": "prod-1"}}})
    )


def _mock_deal_save():
    return respx.post("https://app.didar.me/api/deal/save_v2").mock(
        return_value=httpx.Response(200, json={"Response": {"Deal": {"Id": "deal-1"}}})
    )


def _mock_create_path():
    _mock_categories()
    _mock_deal_labels()
    _mock_product_lookup_no_match()
    return _mock_product_save(), _mock_deal_save()


# --- the Deal body ---------------------------------------------------------

@respx.mock
def test_deal_has_the_fixed_title_and_the_configured_placeholder_person_id():
    """No real customer exists on this endpoint, so the Title is a
    constant and PersonId must be the configured placeholder Contact -
    Didar's Deal.save_v2 rejects a Deal with neither PersonId nor
    CompanyId ("person and company both are empty")."""
    _, deal_route = _mock_create_path()

    deal_id = _client().create_warehouse_shipment_deal(_ITEM)

    assert deal_id == "deal-1"
    deal = deal_route.calls[0].request.content
    body = json.loads(deal)
    assert body["Deal"]["Title"] == WAREHOUSE_DEAL_TITLE
    assert body["Deal"]["PersonId"] == "placeholder-person-1"


def test_deal_creation_requires_placeholder_person_id():
    """Without DIDAR_WAREHOUSE_PLACEHOLDER_PERSON_ID configured, fail
    fast - before ever calling Didar - rather than let every poll hit
    the same "person and company both are empty" 400 forever."""
    cfg = DidarConfig(
        base_url="https://app.didar.me/api", api_key="test-key",
        pipeline_id="p1", pipeline_stage_id="stage-1",
        deal_label_title_digikala="دیجی کالا",
        warehouse_placeholder_person_id="",
        default_product_category_id="cat-default",
        product_catalog_xlsx="",
    )
    client = DidarDealClient(config=cfg, product_client=DidarProductClient(config=cfg))

    try:
        client.create_warehouse_shipment_deal(_ITEM)
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "DIDAR_WAREHOUSE_PLACEHOLDER_PERSON_ID" in str(exc)


@respx.mock
def test_deal_uses_the_configured_pipeline_and_the_digikala_label():
    _, deal_route = _mock_create_path()

    _client().create_warehouse_shipment_deal(_ITEM)

    body = json.loads(deal_route.calls[0].request.content)
    assert body["Deal"]["PipelineId"] == "p1"
    assert body["Deal"]["PipelineStageId"] == "stage-1"
    assert body["Deal"]["LabelIds"] == ["label-digikala-guid"]


@respx.mock
def test_deal_description_carries_the_exact_reference_line():
    """This line is what find_existing_warehouse_deal_id() matches back -
    if its format drifts, duplicate protection silently stops working."""
    _, deal_route = _mock_create_path()

    _client().create_warehouse_shipment_deal(_ITEM)

    body = json.loads(deal_route.calls[0].request.content)
    lines = body["Deal"]["Description"].splitlines()
    assert "شناسه یکتای هماهنگ‌سازی: digikala_warehouse:55123" in lines


@respx.mock
def test_tax_is_zero_on_both_the_deal_and_the_item():
    _, deal_route = _mock_create_path()

    _client().create_warehouse_shipment_deal(_ITEM)

    body = json.loads(deal_route.calls[0].request.content)
    assert body["Deal"]["TaxPercent"] == "0"
    assert body["DealItems"][0]["TaxPercent"] == "0"


@respx.mock
def test_exactly_one_deal_item_with_quantity_and_unit_price():
    _, deal_route = _mock_create_path()

    _client().create_warehouse_shipment_deal(_ITEM)

    items = json.loads(deal_route.calls[0].request.content)["DealItems"]
    assert len(items) == 1
    assert items[0]["ProductId"] == "prod-1"
    assert items[0]["Quantity"] == 2
    assert items[0]["UnitPrice"] == 3000000
    # No discount FIELD exists on this endpoint - see
    # _build_warehouse_deal_item()'s docstring.
    assert items[0]["Discount"] == 0


@respx.mock
def test_supplier_code_is_never_used_as_a_didar_product_code():
    """Regression guard for the 2026-09 wrong-product incident (order
    382920341, sellerCode "25" -> the unrelated catalog product "نبات 6").
    supplier_code is Description text only; the Code is the full title."""
    _mock_categories()
    _mock_deal_labels()
    _mock_product_lookup_no_match()
    product_route = _mock_product_save()
    _mock_deal_save()

    _client().create_warehouse_shipment_deal(_ITEM)

    saved = json.loads(product_route.calls[0].request.content)["Product"]
    assert saved["Code"] == "قاب بشقاب 25 میناکاری"
    assert saved["Code"] != "25"


@respx.mock
def test_deal_item_description_mentions_the_order_and_supplier_code():
    _, deal_route = _mock_create_path()

    _client().create_warehouse_shipment_deal(_ITEM)

    text = json.loads(deal_route.calls[0].request.content)["DealItems"][0][
        "Description"
    ]
    assert "شماره سفارش: 382920341" in text
    assert "کد فروشنده: 25" in text


@respx.mock
def test_a_missing_label_does_not_block_the_deal():
    """_label_id_for_source() is fire-and-forget: no label must never be
    the reason an item fails to sync."""
    _mock_categories()
    respx.get("https://app.didar.me/api/Label/GetDealLabels").mock(
        return_value=httpx.Response(500, json={"error": "boom"})
    )
    _mock_product_lookup_no_match()
    _mock_product_save()
    deal_route = _mock_deal_save()

    deal_id = _client().create_warehouse_shipment_deal(_ITEM)

    assert deal_id == "deal-1"
    assert "LabelIds" not in json.loads(deal_route.calls[0].request.content)["Deal"]


# --- description building (no network) -------------------------------------

def test_description_omits_every_field_the_api_did_not_return():
    bare = WarehouseShipmentItem(
        source="digikala_warehouse",
        source_shipment_id="9",
        product_title="کالا",
        quantity=1,
        unit_price=Decimal("1000"),
        created_at=datetime(2026, 9, 11, tzinfo=timezone.utc),
    )

    text = _build_warehouse_description(bare)

    assert "None" not in text
    assert "شماره سفارش" not in text
    assert "کد فروشنده" not in text
    assert "تاریخ تعهد" not in text
    assert "شناسه یکتای هماهنگ‌سازی: digikala_warehouse:9" in text.splitlines()


def test_reference_is_namespaced_away_from_customer_orders():
    """An FBD item and a customer order can share an id - the source
    prefix is what keeps their references distinct."""
    assert _warehouse_reference(_ITEM) == "digikala_warehouse:55123"


# --- duplicate protection --------------------------------------------------

@respx.mock
def test_find_existing_warehouse_deal_id_matches_the_exact_reference_line():
    respx.post("https://app.didar.me/api/search/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "Response": {
                    "List": [
                        {
                            "_tp": "deal",
                            "Id": "existing-deal",
                            "Description": (
                                "منبع: دیجی‌کالا (ارسال به انبار)\n"
                                "شناسه یکتای هماهنگ‌سازی: digikala_warehouse:55123"
                            ),
                        }
                    ]
                }
            },
        )
    )

    assert _client().find_existing_warehouse_deal_id(_ITEM) == "existing-deal"


@respx.mock
def test_a_longer_id_is_not_treated_as_a_match():
    """/search/search is fuzzy: "digikala_warehouse:55123" is a substring
    of "...:551234", a different item. A false match would silently skip
    an item that was never synced - worse than the duplicate this
    prevents."""
    respx.post("https://app.didar.me/api/search/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "Response": {
                    "List": [
                        {
                            "_tp": "deal",
                            "Id": "other-deal",
                            "Description": "شناسه یکتای هماهنگ‌سازی: digikala_warehouse:551234",
                        }
                    ]
                }
            },
        )
    )

    assert _client().find_existing_warehouse_deal_id(_ITEM) is None


@respx.mock
def test_non_deal_results_are_ignored():
    respx.post("https://app.didar.me/api/search/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "Response": {
                    "List": [
                        {
                            "_tp": "contact",
                            "Id": "some-contact",
                            "Description": "شناسه یکتای هماهنگ‌سازی: digikala_warehouse:55123",
                        }
                    ]
                }
            },
        )
    )

    assert _client().find_existing_warehouse_deal_id(_ITEM) is None


@respx.mock
def test_no_results_means_not_found():
    respx.post("https://app.didar.me/api/search/search").mock(
        return_value=httpx.Response(200, json={"Response": {"Total": 0, "List": []}})
    )

    assert _client().find_existing_warehouse_deal_id(_ITEM) is None


# --- existing behaviour must be untouched ----------------------------------

def test_customer_order_entry_points_still_exist_unchanged():
    """Stage 4 adds methods, it does not change any signature the
    existing tests/service code binds to."""
    import inspect

    assert list(inspect.signature(DidarDealClient.create_deal).parameters) == [
        "self", "contact_id", "display_name", "order",
    ]
    assert list(inspect.signature(DidarDealClient.find_existing_deal_id).parameters) == [
        "self", "order",
    ]


# ===========================================================================
# Stage 5 - the single "ارسال محصول" Activity
# ===========================================================================

_ACTIVITY_CFG = DidarConfig(
    base_url="https://app.didar.me/api", api_key="test-key",
    # Explicit, not read from the real .env, so these tests are hermetic.
    activity_type_ship_id="ship-type-id",
    activity_type_new_call_id="", activity_type_sms1_id="",
    activity_type_sms2_id="", activity_type_sms3_id="",
    activity_type_satisfaction_call_id="",
    default_owner_id="",
)

_DUE = datetime(2026, 9, 14, tzinfo=timezone.utc)


def _mock_activity_save():
    return respx.post("https://app.didar.me/api/activity/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Id": "act-1"}})
    )


def _mock_attach():
    return respx.post("https://app.didar.me/api/activity/AttachFilesToActivity").mock(
        return_value=httpx.Response(200, json={"Response": {"Key": "k"}})
    )


@respx.mock
def test_exactly_one_activity_is_created_and_it_is_the_ship_one():
    """Client instruction for this feature: "فقط فعالیت ارسال محصول" -
    the other five post-sale checklist items are a CUSTOMER follow-up
    sequence and there is no customer here."""
    route = _mock_activity_save()

    DidarActivityClient(config=_ACTIVITY_CFG).create_ship_only_activity(
        deal_id="deal-1", due_date=_DUE
    )

    assert route.call_count == 1
    body = json.loads(route.calls[0].request.content)["Activity"]
    assert body["Title"] == SHIP_ACTIVITY_TITLE
    assert body["ActivityTypeId"] == "ship-type-id"
    assert body["DealId"] == "deal-1"
    assert body["IsDone"] is False


@respx.mock
def test_photos_are_attached_to_the_ship_activity():
    _mock_activity_save()
    attach_route = _mock_attach()

    DidarActivityClient(config=_ACTIVITY_CFG).create_ship_only_activity(
        deal_id="deal-1",
        due_date=_DUE,
        ship_attachments=[(b"bytes", "product.jpg", "image/jpeg")],
    )

    assert attach_route.call_count == 1


@respx.mock
def test_a_failed_photo_upload_does_not_raise():
    """Fire-and-forget: the Deal is already created by the time this
    runs, so a photo problem must never surface as an exception."""
    _mock_activity_save()
    respx.post("https://app.didar.me/api/activity/AttachFilesToActivity").mock(
        return_value=httpx.Response(500, json={"error": "boom"})
    )

    DidarActivityClient(config=_ACTIVITY_CFG).create_ship_only_activity(
        deal_id="deal-1",
        due_date=_DUE,
        ship_attachments=[(b"bytes", "product.jpg", "image/jpeg")],
    )


@respx.mock
def test_missing_ship_activity_type_skips_without_raising():
    """Same rule as the post-sale checklist's missing_types guard: a
    config gap warns and skips, it never fails the sync."""
    cfg = DidarConfig(
        base_url="https://app.didar.me/api", api_key="test-key", activity_type_ship_id=""
    )
    route = _mock_activity_save()

    DidarActivityClient(config=cfg).create_ship_only_activity(deal_id="d", due_date=_DUE)

    assert route.call_count == 0


@respx.mock
def test_a_failed_activity_create_does_not_raise():
    respx.post("https://app.didar.me/api/activity/save").mock(
        return_value=httpx.Response(500, json={"error": "boom"})
    )

    DidarActivityClient(config=_ACTIVITY_CFG).create_ship_only_activity(
        deal_id="deal-1", due_date=_DUE
    )


def test_post_sale_checklist_is_untouched():
    """Stage 5 must not change the customer-order checklist in any way."""
    assert [title for title, _ in POST_SALE_CHECKLIST] == [
        "تماس جدید", "پیامک 2", "پیامک 1", "ارسال محصول", "پیامک 3", "تماس رضایت",
    ]


# ===========================================================================
# Stage 6 - DidarWarehouseSyncService
# ===========================================================================


class _FakeDeals:
    def __init__(self, existing_id=None, fail=False):
        self._existing_id = existing_id
        self._fail = fail
        self.created: list = []
        self.searched: list = []

    def find_existing_warehouse_deal_id(self, item):
        self.searched.append(item)
        return self._existing_id

    def create_warehouse_shipment_deal(self, item):
        if self._fail:
            raise RuntimeError("simulated Didar failure")
        self.created.append(item)
        return "deal-1"


class _FakeActivities:
    def __init__(self):
        self.calls: list = []

    def create_ship_only_activity(self, deal_id, due_date, ship_attachments=None):
        self.calls.append((deal_id, due_date, ship_attachments))


def _service(deals, activities):
    return DidarWarehouseSyncService(deal_client=deals, activity_client=activities)


@respx.mock
def test_service_creates_a_deal_and_exactly_one_activity():
    respx.get("https://dkstatics-public.digikala.com/example.jpg").mock(
        return_value=httpx.Response(200, content=b"img", headers={"content-type": "image/jpeg"})
    )
    deals, activities = _FakeDeals(), _FakeActivities()

    deal_id = _service(deals, activities).sync_shipment(_ITEM)

    assert deal_id == "deal-1"
    assert len(deals.created) == 1
    assert len(activities.calls) == 1


def test_service_holds_no_contact_client_at_all():
    """No Contact is created for an FBD item - there is no customer, and
    a placeholder person was explicitly rejected by the client."""
    service = _service(_FakeDeals(), _FakeActivities())

    assert not hasattr(service, "_contacts")


def test_an_already_existing_deal_creates_nothing():
    deals, activities = _FakeDeals(existing_id="existing-deal"), _FakeActivities()

    deal_id = _service(deals, activities).sync_shipment(_ITEM)

    assert deal_id == "existing-deal"
    assert deals.created == []
    # Critically: no SECOND ship activity on a deal that already has one.
    assert activities.calls == []


@respx.mock
def test_due_date_comes_from_commitment_date_when_present():
    respx.get("https://dkstatics-public.digikala.com/example.jpg").mock(
        return_value=httpx.Response(200, content=b"img", headers={"content-type": "image/jpeg"})
    )
    deals, activities = _FakeDeals(), _FakeActivities()

    _service(deals, activities).sync_shipment(_ITEM)

    assert activities.calls[0][1] == _ITEM.commitment_date


def test_due_date_falls_back_to_created_at_plus_two_days():
    from dataclasses import replace

    item = replace(_ITEM, commitment_date=None, product_image_url=None)
    deals, activities = _FakeDeals(), _FakeActivities()

    _service(deals, activities).sync_shipment(item)

    assert activities.calls[0][1] == item.created_at + timedelta(days=2)


@respx.mock
def test_a_failed_photo_download_still_creates_the_deal():
    respx.get("https://dkstatics-public.digikala.com/example.jpg").mock(
        return_value=httpx.Response(404)
    )
    deals, activities = _FakeDeals(), _FakeActivities()

    deal_id = _service(deals, activities).sync_shipment(_ITEM)

    assert deal_id == "deal-1"
    assert activities.calls[0][2] == []


@respx.mock
def test_photo_filename_ignores_the_cdn_query_string():
    """Regression guard for the real production bug where every
    attachment was named "quality,q_60" - Digikala's CDN separates
    image-transform params with "/" inside the query string."""
    from dataclasses import replace

    url = (
        "https://dkstatics-public.digikala.com/digikala-products/113noe.jpg"
        "?x-oss-process=image/resize,m_lfit,h_600,w_600/quality,q_60"
    )
    respx.get(url).mock(
        return_value=httpx.Response(200, content=b"img", headers={"content-type": "image/jpeg"})
    )
    deals, activities = _FakeDeals(), _FakeActivities()

    _service(deals, activities).sync_shipment(replace(_ITEM, product_image_url=url))

    assert activities.calls[0][2][0][1] == "113noe.jpg"


def test_a_failed_deal_create_propagates_so_the_item_is_not_marked_synced():
    deals, activities = _FakeDeals(fail=True), _FakeActivities()

    try:
        _service(deals, activities).sync_shipment(_ITEM)
    except RuntimeError:
        pass
    else:
        raise AssertionError("a failed deal create must not be swallowed")

    assert activities.calls == []