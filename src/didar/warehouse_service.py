"""
Didar sync service for Digikala FBD items ("ارسال به انبار دیجی‌کالا").

WHY A SEPARATE FILE FROM service.py: DidarSyncService.sync_order() is
the CUSTOMER-order flow - Contact upsert, then Deal, then the six-item
post-sale checklist. None of those three steps applies here: this source
has no customer to upsert (the endpoint exposes no customer field at
all), its Deal is built by different methods
(create_warehouse_shipment_deal, see deal_client.py), and it gets
exactly ONE activity instead of the checklist. Putting this in
service.py would mean branching that method on source inside a class the
whole customer pipeline depends on; a separate service keeps
service.py's tests and behaviour untouched.

NO DidarContactClient IS CONSTRUCTED OR CALLED ANYWHERE IN THIS FILE.
That is the point - a placeholder/fake Contact per FBD item was
explicitly rejected (it would pollute the CRM's contact list with people
who don't exist).

FLOW, in order:
    1. Ask Didar itself whether this item already has a Deal
       (find_existing_warehouse_deal_id) - the local dedupe table can't
       see a Deal created by a request whose response never came back.
       If found: return that Id and do NOTHING else - in particular, no
       second ship Activity is created for a deal that already has one.
    2. Create the Deal. This one is NOT fire-and-forget: if it raises,
       the caller must not mark the item as synced, so the next poll
       retries it (see sync_engine.py's _sync_warehouse_source).
    3. Create the single "ارسال محصول" Activity with the product photo
       attached - fire-and-forget, exactly like the post-sale checklist:
       a missing activity or photo is visible and fixable by hand in
       Didar, while a failed Deal would be invisible.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from urllib.parse import urlparse

import httpx

from src.didar.activity_client import DidarActivityClient
from src.didar.deal_client import DidarDealClient
from src.logger import get_logger
from src.marketplaces.warehouse_base import WarehouseShipmentItem

log = get_logger(__name__)

# Same 2-day fallback, and the same reasoning, as
# activity_client.py's _DEFAULT_SHIP_DELAY - copied rather than imported
# so that module's private constant stays private and a change there is
# a deliberate decision here too. Only used when Digikala didn't return
# a commitment_date for the item; a date is never invented beyond this
# documented, client-approved default.
_DEFAULT_SHIP_DELAY = timedelta(days=2)


class DidarWarehouseSyncService:
    def __init__(
        self,
        deal_client: DidarDealClient | None = None,
        activity_client: DidarActivityClient | None = None,
    ) -> None:
        self._deals = deal_client or DidarDealClient()
        self._activities = activity_client or DidarActivityClient()

    def sync_shipment(self, item: WarehouseShipmentItem) -> str:
        """Create (or find) this FBD item's Didar Deal and return its Id."""
        existing_deal_id = self._deals.find_existing_warehouse_deal_id(item)
        if existing_deal_id:
            log.info(
                "didar: FBD item %s already has deal %s - not creating a second "
                "deal or a second ship activity",
                item.source_shipment_id, existing_deal_id,
            )
            return existing_deal_id

        deal_id = self._deals.create_warehouse_shipment_deal(item)
        log.info(
            "didar: synced FBD item %s (order %s) -> deal=%s",
            item.source_shipment_id, item.order_id, deal_id,
        )

        # Fire-and-forget from here on - see this module's docstring.
        self._activities.create_ship_only_activity(
            deal_id=deal_id,
            due_date=_ship_due_date(item),
            ship_attachments=_fetch_warehouse_product_images(item),
        )
        return deal_id


def _ship_due_date(item: WarehouseShipmentItem) -> datetime:
    """commitment_date (the date Digikala itself expects the goods) is
    the real anchor whenever the API returned one. Otherwise the same
    created_at + 2 days fallback the post-sale checklist uses for
    sources with no real ship time - logged, so a due date that came
    from the fallback is never mistaken for one Digikala supplied."""
    if item.commitment_date is not None:
        return item.commitment_date
    due = item.created_at + _DEFAULT_SHIP_DELAY
    log.info(
        "didar: FBD item %s has no commitment_date - defaulting the ship "
        "activity's due date to created_at + %s",
        item.source_shipment_id, _DEFAULT_SHIP_DELAY,
    )
    return due


def _fetch_warehouse_product_images(
    item: WarehouseShipmentItem,
) -> list[tuple[bytes, str, str]]:
    """
    Downloads this item's product photo so it can be attached to the
    ship Activity. Copy of src/didar/service.py's
    _fetch_product_images() logic, narrowed to the single photo this
    source has (one row = one product), per the project's
    copy-don't-share convention for per-source code - INCLUDING its
    filename bugfix:

    the filename must come from the URL's PATH only, never the raw
    string. Digikala's CDN puts image-transform params in the query
    string using "/" as a separator
    (".../x.jpg?x-oss-process=image/resize,m_lfit/quality,q_60"), so
    taking the text after the last "/" of the whole URL yields
    "quality,q_60" - confirmed live in production, where every single
    "ارسال محصول" attachment was literally named that.

    Returns [] - never raises - when there is no image URL or the
    download fails: a photo must never be the reason an item fails to
    sync.
    """
    if not item.product_image_url:
        return []

    url = item.product_image_url
    try:
        resp = httpx.get(url, timeout=30.0, follow_redirects=True)
        resp.raise_for_status()
    except Exception:
        log.exception(
            "didar: failed to download product image %r for FBD item %s - "
            "continuing without a ship attachment",
            url, item.source_shipment_id,
        )
        return []

    content_type = resp.headers.get("content-type", "application/octet-stream").split(";")[0]
    path = urlparse(url).path
    filename = path.rstrip("/").rsplit("/", 1)[-1] or "product.jpg"
    return [(resp.content, filename, content_type)]
