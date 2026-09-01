"""
Combines the two-step Didar flow (Contact upsert, then Deal creation)
into a single call per order - this is what the Sync Engine will call
once it exists. Kept as a thin wrapper so the two clients stay testable
in isolation while still being trivial to use together.
"""
from __future__ import annotations

from urllib.parse import urlparse

import httpx

from src.didar.activity_client import DidarActivityClient
from src.didar.contact_client import DidarContactClient
from src.didar.deal_client import DidarDealClient
from src.logger import get_logger
from src.marketplaces.base import NormalizedOrder

log = get_logger(__name__)


class DidarSyncService:
    def __init__(
        self,
        contact_client: DidarContactClient | None = None,
        deal_client: DidarDealClient | None = None,
        activity_client: DidarActivityClient | None = None,
    ) -> None:
        self._contacts = contact_client or DidarContactClient()
        self._deals = deal_client or DidarDealClient()
        self._activities = activity_client or DidarActivityClient()

    def sync_order(self, order: NormalizedOrder) -> str:
        """
        Upsert the Contact for this order, then create a Deal linked to
        it, then attach the standard post-sale checklist Activities
        (see DidarActivityClient) to that new Deal. Returns the Deal's
        Id (what the Repository stores for duplicate-prevention
        bookkeeping).

        Checks Didar itself for an already-existing Deal for this order
        BEFORE touching Contact/Deal creation at all - see
        DidarDealClient.find_existing_deal_id()'s docstring for exactly
        why the local Repository dedupe check alone isn't sufficient.
        When a match is found, nothing else happens - no Contact
        upsert, no Deal creation, and NO checklist re-creation (it was
        already created the first time this Deal was made) - we just
        hand back the existing Id so the caller (SyncEngine) records it
        as synced same as a normal create.
        """
        existing_deal_id = self._deals.find_existing_deal_id(order)
        if existing_deal_id:
            return existing_deal_id

        customer_code = _customer_code_for(order)
        contact = self._contacts.upsert_contact(
            customer_code=customer_code,
            mobile_phone=order.customer_mobile,
            full_name=order.customer_full_name,
        )
        deal_id = self._deals.create_deal(
            contact_id=contact.id, display_name=contact.display_name, order=order
        )
        log.info(
            "didar: synced %s order %s -> contact=%s deal=%s",
            order.source, order.source_order_id, contact.id, deal_id,
        )

        # Fire-and-forget: a checklist failure must never fail the order
        # sync itself - see DidarActivityClient.create_post_sale_checklist's
        # docstring for how it isolates per-item failures. Due dates are
        # computed from order.ship_time / order.created_at (see
        # src/didar/scheduling.py) - if a given marketplace adapter
        # doesn't expose ship_time yet, create_post_sale_checklist
        # defaults it to order_registered_at + 2 days rather than
        # skipping the checklist (see that method's docstring).
        self._activities.create_post_sale_checklist(
            deal_id=deal_id,
            order_registered_at=order.created_at,
            ship_time=order.ship_time,
            ship_attachments=_fetch_product_images(order),
        )

        return deal_id


def _fetch_product_images(order: NormalizedOrder) -> list[tuple[bytes, str, str]]:
    """
    Downloads a product photo for every line item that has its own
    image URL, so they can all be attached to the "ارسال محصول" Activity
    - see DidarActivityClient.create_post_sale_checklist's
    ship_attachments param.

    BUGFIX (client feedback, 2026-09 - "if a customer ordered more than
    one product, all of them need a photo in the shipping activity, not
    just one"): this previously read only order.product_image_url, a
    single ORDER-level field every adapter populated from items[0]
    alone (see each adapter's module comments - "nothing downstream
    reads per-item images" was true until now). A 2+ item order
    therefore silently lost every photo but the first one. Each
    OrderItem already carries its own product_image_url (see
    marketplaces/base.py) - that per-item field is now the source of
    truth here. order.product_image_url is kept only as a last-resort
    fallback for the rare case an order's items carry no image URLs of
    their own at all.

    Duplicate URLs (e.g. two items pointing at the exact same photo)
    are only downloaded once. A failed/missing download for one item
    never blocks the others - same fire-and-forget philosophy as the
    checklist itself; a partial set of photos is still useful, and a
    photo issue must never fail the order sync.
    """
    urls = [item.product_image_url for item in order.items if item.product_image_url]
    if not urls and order.product_image_url:
        urls = [order.product_image_url]

    attachments: list[tuple[bytes, str, str]] = []
    seen: set[str] = set()
    for url in urls:
        if url in seen:
            continue
        seen.add(url)

        try:
            resp = httpx.get(url, timeout=30.0, follow_redirects=True)
            resp.raise_for_status()
        except Exception:
            log.exception(
                "didar: failed to download product image %r for %s "
                "order %s - continuing without this ship attachment",
                url, order.source, order.source_order_id,
            )
            continue

        content_type = resp.headers.get("content-type", "application/octet-stream").split(";")[0]
        # BUGFIX: filename must come from the URL's PATH only, not the raw
        # string. Digikala's CDN URLs carry image-transform params in the
        # query string using "/" as a separator (e.g.
        # ".../xxx.jpg?x-oss-process=image/resize,m_lfit/quality,q_60"), so
        # naively taking the text after the last "/" in the full URL grabs a
        # piece of that query string ("quality,q_60") instead of the real
        # filename - confirmed live: every single "ارسال محصول" attachment
        # in production logs was named literally "quality,q_60". Parsing out
        # the path component first fixes this regardless of what query
        # string is appended.
        path = urlparse(url).path
        filename = path.rstrip("/").rsplit("/", 1)[-1] or "product.jpg"
        attachments.append((resp.content, filename, content_type))

    return attachments


def _customer_code_for(order: NormalizedOrder) -> str:
    """
    Real mobile number when the source provides one (confirmed available
    for Basalam; not available via REST polling for Tapsi Shop or
    Digikala - see each adapter's module docstring for why). Falls back
    to a synthetic, per-source-per-order code so every order still gets
    its own Contact even without a phone number.
    """
    if order.customer_mobile:
        return order.customer_mobile
    return f"{order.source}-{order.source_order_id}"