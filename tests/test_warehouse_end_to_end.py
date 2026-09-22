"""
Stage 7 - end-to-end check of the Digikala FBD feature
("ارسال به انبار دیجی‌کالا"), with NO fakes between the pieces.

Every other FBD test file exercises one layer with the next one stubbed
out. This one wires the REAL adapter, the REAL Repository, the REAL
Didar deal/activity/product clients and the REAL SyncEngine together,
mocking only the network (respx), so that a mismatch between two layers
- a renamed field, a changed method signature, a dedupe key that doesn't
round-trip - fails here even though each layer's own tests still pass.

It also locks down the two things the client is most exposed to if this
feature misbehaves: the cold-start guard (no backlog flood on the first
run) and the per-poll duplicate guard (one Deal per item, ever).
"""
import json
from datetime import datetime, timezone

import httpx
import pytest
import respx

from src.config import DidarConfig, DigikalaConfig
from src.db.repository import Repository
from src.didar.activity_client import DidarActivityClient
from src.didar.deal_client import DidarDealClient
from src.didar.product_client import DidarProductClient
from src.didar.warehouse_service import DidarWarehouseSyncService
from src.marketplaces.digikala_warehouse import DigikalaWarehouseAdapter
from src.sync_engine import SyncEngine

_DIGIKALA_CFG = DigikalaConfig(
    base_url="https://seller.digikala.com", access_token="test-token"
)
_DIDAR_CFG = DidarConfig(
    base_url="https://app.didar.me/api", api_key="test-key",
    pipeline_id="p1", pipeline_stage_id="stage-1",
    deal_label_title_digikala="دیجی کالا",
    default_product_category_id="cat-default",
    activity_type_ship_id="ship-type-id",
    # Required: create_warehouse_shipment_deal() now raises before ever
    # calling Didar if this is blank.
    warehouse_placeholder_person_id="placeholder-person-1",
    # Blank on purpose so nothing here depends on the developer's .env.
    product_catalog_xlsx="", default_owner_id="",
)

_ORDERS_URL = "https://seller.digikala.com/open-api/v1/orders"
_IMAGE_URL = "https://dkstatics-public.digikala.com/digikala-products/113noe.jpg"


@pytest.fixture
def repo(tmp_path):
    return Repository(db_path=str(tmp_path / "e2e.db"))


def _row(order_item_id=55123, created_at="2026-09-11T08:49:01.000000+03:30"):
    return {
        "order_item_id": order_item_id,
        "order_id": 382920341,
        "product_variant_title": "قاب بشقاب 25 میناکاری",
        "quantity": 2,
        "selling_price": 3000000,
        "total_price": 6000000,
        "order_created_at": created_at,
        "commitment_date": "2026-09-14T00:00:00.000000+03:30",
        "supplier_code": "25",
        "product_image_url": _IMAGE_URL,
    }


def _orders_response(items):
    return httpx.Response(
        200,
        json={
            "status": "ok",
            "data": {"pager": {"page": 1, "total_pages": 1}, "items": items},
        },
    )


def _mock_didar():
    """Every Didar endpoint this flow touches, each returning the
    minimum real-shaped payload."""
    respx.post("https://app.didar.me/api/search/search").mock(
        return_value=httpx.Response(200, json={"Response": {"Total": 0, "List": []}})
    )
    respx.get("https://app.didar.me/api/Label/GetDealLabels").mock(
        return_value=httpx.Response(
            200,
            json={
                "Response": [
                    {"Id": "label-digikala", "Title": "دیجی کالا", "Code": 1, "Type": "Deal"}
                ]
            },
        )
    )
    respx.post("https://app.didar.me/api/product/categories").mock(
        return_value=httpx.Response(
            200, json={"Response": [{"Id": "cat-default", "Title": "متفرقه"}]}
        )
    )
    respx.post("https://app.didar.me/api/product/getproductbycodes").mock(
        return_value=httpx.Response(200, json={"Response": {"Total": 0, "Products": []}})
    )
    respx.post("https://app.didar.me/api/product/search").mock(
        return_value=httpx.Response(200, json={"Response": []})
    )
    respx.post("https://app.didar.me/api/product/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Product": {"Id": "prod-1"}}})
    )
    respx.get(_IMAGE_URL).mock(
        return_value=httpx.Response(
            200, content=b"img", headers={"content-type": "image/jpeg"}
        )
    )
    return {
        "deal": respx.post("https://app.didar.me/api/deal/save_v2").mock(
            return_value=httpx.Response(200, json={"Response": {"Deal": {"Id": "deal-1"}}})
        ),
        "activity": respx.post("https://app.didar.me/api/activity/save").mock(
            return_value=httpx.Response(200, json={"Response": {"Id": "act-1"}})
        ),
        "attach": respx.post(
            "https://app.didar.me/api/activity/AttachFilesToActivity"
        ).mock(return_value=httpx.Response(200, json={"Response": {"Key": "k"}})),
    }


def _engine(repo, tmp_path):
    adapter = DigikalaWarehouseAdapter(
        config=_DIGIKALA_CFG,
        repository=repo,
        token_cache_path=tmp_path / "digikala_tokens.json",
    )
    deals = DidarDealClient(
        config=_DIDAR_CFG, product_client=DidarProductClient(config=_DIDAR_CFG)
    )
    service = DidarWarehouseSyncService(
        deal_client=deals, activity_client=DidarActivityClient(config=_DIDAR_CFG)
    )
    return SyncEngine(
        adapters=[],
        repository=repo,
        # Never reached (there are no customer-order adapters here), but
        # passed explicitly so the engine doesn't build a real
        # DidarSyncService against the developer's own .env.
        didar_service=object(),
        synced_ids_file_path=str(tmp_path / "synced_ids.json"),
        warehouse_adapters=[adapter],
        warehouse_service=service,
    )


@respx.mock
def test_first_ever_run_syncs_nothing_and_arms_the_floor(repo, tmp_path):
    """The one failure the client has already lived through once (43 old
    orders, 2026-08-31), in its FBD form: a fresh DB must NOT push the
    existing active list into Didar."""
    respx.get(_ORDERS_URL).mock(return_value=_orders_response([_row(), _row(55124)]))
    routes = _mock_didar()

    _engine(repo, tmp_path).run_once()

    assert routes["deal"].call_count == 0
    assert routes["activity"].call_count == 0
    assert repo.get_last_sync_time("digikala_warehouse") is not None


@respx.mock
def test_an_item_created_after_the_floor_flows_all_the_way_into_didar(repo, tmp_path):
    repo.set_last_sync_time("digikala_warehouse", datetime(2026, 9, 1, tzinfo=timezone.utc))
    respx.get(_ORDERS_URL).mock(return_value=_orders_response([_row()]))
    routes = _mock_didar()

    _engine(repo, tmp_path).run_once()

    assert routes["deal"].call_count == 1
    # Exactly one activity, and it carries the product photo.
    assert routes["activity"].call_count == 1
    assert routes["attach"].call_count == 1
    # Dedupe row written under the adapter's own name + order_item_id.
    assert repo.is_warehouse_shipment_synced("digikala_warehouse", "55123") is True
    # ...and nothing landed in the customer-order tables.
    assert repo.is_already_synced("digikala_warehouse", "55123") is False

    deal_body = json.loads(routes["deal"].calls[0].request.content)
    assert deal_body["Deal"]["PersonId"] == "placeholder-person-1"
    assert (
        "شناسه یکتای هماهنگ‌سازی: digikala_warehouse:55123"
        in deal_body["Deal"]["Description"].splitlines()
    )
    assert len(deal_body["DealItems"]) == 1


@respx.mock
def test_warehouse_deal_is_marked_notified_so_the_deal_poller_skips_it(repo, tmp_path):
    """Regression test for a confirmed production incident (deal #6108,
    2026-09): an FBD deal lives in the same pipeline as customer orders
    (see create_warehouse_shipment_deal()'s docstring), so if it's never
    marked notified, DidarDealPoller later discovers it as an
    unrecognized deal and sends it through notify_new_deal()'s
    "ثبت دستی در دیدار" (manual-entry) Telegram template - wrong, since
    nobody typed it into Didar by hand. Marking it notified up front
    (mirroring what _sync_one_order() already does for customer orders)
    is what makes the poller skip it, same as any other program-created
    deal."""
    repo.set_last_sync_time("digikala_warehouse", datetime(2026, 9, 1, tzinfo=timezone.utc))
    respx.get(_ORDERS_URL).mock(return_value=_orders_response([_row()]))
    _mock_didar()

    _engine(repo, tmp_path).run_once()

    assert repo.is_deal_notified("deal-1") is True


@respx.mock
def test_polling_twice_creates_exactly_one_deal(repo, tmp_path):
    """The endpoint keeps returning an item while it is active, so the
    second poll must be a no-op - this is the guard that would otherwise
    produce a duplicate Deal every two minutes, forever."""
    repo.set_last_sync_time("digikala_warehouse", datetime(2026, 9, 1, tzinfo=timezone.utc))
    respx.get(_ORDERS_URL).mock(return_value=_orders_response([_row()]))
    routes = _mock_didar()

    engine = _engine(repo, tmp_path)
    engine.run_once()
    engine.run_once()

    assert routes["deal"].call_count == 1
    assert routes["activity"].call_count == 1


@respx.mock
def test_a_didar_failure_leaves_the_item_unsynced_and_the_next_poll_recovers(repo, tmp_path):
    """No retry queue exists for this source by design - recovery is
    simply "it wasn't marked, so the next poll sees it again"."""
    repo.set_last_sync_time("digikala_warehouse", datetime(2026, 9, 1, tzinfo=timezone.utc))
    respx.get(_ORDERS_URL).mock(return_value=_orders_response([_row()]))
    routes = _mock_didar()
    routes["deal"].mock(
        side_effect=[
            httpx.Response(500, json={"error": "boom"}),
            httpx.Response(500, json={"error": "boom"}),
            httpx.Response(500, json={"error": "boom"}),
            httpx.Response(200, json={"Response": {"Deal": {"Id": "deal-1"}}}),
        ]
    )

    engine = _engine(repo, tmp_path)
    engine.run_once()
    assert repo.is_warehouse_shipment_synced("digikala_warehouse", "55123") is False

    engine.run_once()
    assert repo.is_warehouse_shipment_synced("digikala_warehouse", "55123") is True