"""
Combines the two-step Didar flow (Contact upsert, then Deal creation)
into a single call per order - this is what the Sync Engine will call
once it exists. Kept as a thin wrapper so the two clients stay testable
in isolation while still being trivial to use together.
"""
from __future__ import annotations

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
        # doesn't expose ship_time yet, the checklist is skipped for
        # that order rather than guessing a schedule (see that method's
        # docstring).
        self._activities.create_post_sale_checklist(
            deal_id=deal_id,
            order_registered_at=order.created_at,
            ship_time=order.ship_time,
            ship_attachment=_fetch_product_image(order),
        )

        return deal_id


def _fetch_product_image(order: NormalizedOrder) -> tuple[bytes, str, str] | None:
    """
    Downloads order.product_image_url (when the source adapter provides
    one) so it can be attached to the "ارسال محصول" Activity - see
    DidarActivityClient.create_post_sale_checklist's ship_attachment
    param. Returns None (not an error) when the order has no image URL,
    or when the download itself fails - a missing/broken product photo
    must never fail the order sync, same fire-and-forget philosophy as
    the checklist itself. The filename is derived from the URL's own
    path segment, falling back to a generic name if that's empty.
    """
    if not order.product_image_url:
        return None
    try:
        resp = httpx.get(order.product_image_url, timeout=30.0, follow_redirects=True)
        resp.raise_for_status()
    except Exception:
        log.exception(
            "didar: failed to download product image for %s order %s - "
            "continuing without a ship attachment",
            order.source, order.source_order_id,
        )
        return None

    content_type = resp.headers.get("content-type", "application/octet-stream").split(";")[0]
    filename = order.product_image_url.rstrip("/").rsplit("/", 1)[-1] or "product.jpg"
    return resp.content, filename, content_type


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
