"""
Combines the two-step Didar flow (Contact upsert, then Deal creation)
into a single call per order - this is what the Sync Engine will call
once it exists. Kept as a thin wrapper so the two clients stay testable
in isolation while still being trivial to use together.
"""
from __future__ import annotations

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
    ) -> None:
        self._contacts = contact_client or DidarContactClient()
        self._deals = deal_client or DidarDealClient()

    def sync_order(self, order: NormalizedOrder) -> str:
        """
        Upsert the Contact for this order, then create a Deal linked to
        it. Returns the new Deal's Id (what the Repository stores for
        duplicate-prevention bookkeeping).
        """
        customer_code = _customer_code_for(order)
        contact_id = self._contacts.upsert_contact(
            customer_code=customer_code,
            mobile_phone=order.customer_mobile,
            full_name=order.customer_full_name,
        )
        deal_id = self._deals.create_deal(contact_id=contact_id, order=order)
        log.info(
            "didar: synced %s order %s -> contact=%s deal=%s",
            order.source, order.source_order_id, contact_id, deal_id,
        )
        return deal_id


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
