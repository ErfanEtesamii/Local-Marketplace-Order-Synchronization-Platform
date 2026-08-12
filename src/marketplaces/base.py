"""
Shared contract for every marketplace integration.

Each marketplace (Tapsi Shop, Digikala, SnappShop, Basalam) has its own API
shape, auth mechanism, and field names. Every adapter's job is to translate
that into the single NormalizedOrder shape defined here, so the Sync Engine
and the Didar module never need to know which marketplace an order came from.

Adding a fifth marketplace later means writing one new adapter class that
implements MarketplaceAdapter - nothing else in the system changes.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class OrderItem:
    """A single line item within an order."""
    sku: str
    title: str
    quantity: int
    unit_price: Decimal
    final_price: Decimal


@dataclass(frozen=True)
class NormalizedOrder:
    """
    The common shape every marketplace adapter must produce.

    `source` + `source_order_id` together form the unique key used for
    duplicate-prevention in the local database - see db/repository.py.
    """
    source: str                      # e.g. "tapsishop", "digikala", "snappshop", "basalam"
    source_order_id: str             # marketplace's own order identifier, as a string
    order_number: str                # human-readable order number, if different from the id
    created_at: datetime
    total_price: Decimal
    status: str                      # raw status text/code from the marketplace
    items: list[OrderItem] = field(default_factory=list)

    # Customer fields are intentionally optional: some sources (e.g. Digikala
    # in this project) do not provide them at all. Downstream (Didar contact
    # creation) must handle the case where these are empty.
    customer_full_name: str | None = None
    customer_mobile: str | None = None


class MarketplaceAdapter(ABC):
    """
    Abstract base every marketplace adapter implements.

    Keeping this interface intentionally small (two methods) makes each
    adapter easy to test in isolation and easy to swap out.
    """

    #: short machine-readable identifier, must match NormalizedOrder.source
    name: str

    @abstractmethod
    def fetch_new_orders(self, since: datetime) -> list[NormalizedOrder]:
        """
        Return all orders created at or after `since`, already normalized.

        Implementations own their own pagination - callers just get the
        full list back. Must raise on transport/auth errors rather than
        silently returning an empty list, so the Sync Engine's retry logic
        can distinguish "no new orders" from "the request failed".
        """
        raise NotImplementedError

    @abstractmethod
    def fetch_order_detail(self, source_order_id: str) -> NormalizedOrder:
        """
        Return full detail (including line items) for a single order.

        Some marketplaces return line items directly in the list call;
        in that case this can just look the order up from a cached list
        or re-fetch it. Others (Tapsi Shop) require a separate call.
        """
        raise NotImplementedError
