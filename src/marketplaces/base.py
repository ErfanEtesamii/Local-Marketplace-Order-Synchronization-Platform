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
    # Marketplace's own product category/group name, when the source API
    # exposes one (currently only Faraz Honar/WooCommerce does - see its
    # adapter). Used to pick a matching Didar product category instead of
    # always filing new products under one catch-all category. None means
    # "unknown" - falls back to DIDAR_DEFAULT_PRODUCT_CATEGORY_ID.
    category: str | None = None
    # URL of the product's first image, used to attach to the
    # "ارسال محصول" (ship) Activity in Didar - see
    # NormalizedOrder.product_image_url docs. None means no attachment.
    product_image_url: str | None = None


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

    # Extended customer/contact fields (client request, 2026-09: a new
    # Contact created in Didar was only ever getting CustomerCode /
    # FirstName / LastName / MobilePhone, even on sources whose API
    # already exposes more - see src/didar/contact_client.py's
    # upsert_contact()). Every field here is intentionally optional and
    # is populated ONLY when a given marketplace adapter's API actually
    # returns it for that order - never guessed or defaulted by an
    # adapter. Downstream (DidarContactClient.upsert_contact) already
    # tolerates all of these being None, same as customer_full_name/
    # customer_mobile above.
    customer_email: str | None = None
    # Landline/work phone, distinct from customer_mobile - maps to
    # Didar Contact's "Phone" field (MobilePhone is the mobile one).
    customer_work_phone: str | None = None
    # Free-text full postal address (street/building/unit), NOT
    # including province/city (those are the two fields below, kept
    # separate because Didar's ProvinceId/CityId are its own Location
    # Ids, not the raw text - see DidarContactClient's location
    # resolution).
    customer_address: str | None = None
    customer_postal_code: str | None = None
    # Raw province/city NAMES exactly as the marketplace provides them
    # (e.g. "تهران" / "کرج") - DidarContactClient resolves these to
    # Didar's own ProvinceId/CityId before sending anything to
    # /contact/save; NormalizedOrder never carries a Didar Location Id.
    customer_province: str | None = None
    customer_city: str | None = None

    # Shipment ID from the marketplace's SBS API, used to fetch customer
    # details via the /ship-by-seller-orders/customer/{shipment_id} endpoint.
    # None if the source doesn't expose this field or isn't an SBS order.
    shipment_id: str | None = None

    # When the order was actually shipped (or the marketplace's committed
    # "must ship by" deadline - whichever a given adapter's API exposes).
    # This is the single anchor every post-sale checklist due-date (new
    # call, SMS x3, satisfaction call - see src/didar/scheduling.py) is
    # computed from, per client's 2026-08 timing rules. None means the
    # adapter doesn't yet expose this field for its marketplace - the
    # checklist is skipped for that order rather than guessing a
    # fabricated time (see DidarActivityClient.create_post_sale_checklist).
    ship_time: datetime | None = None

    # URL of the order/product photo as shown on the marketplace, if the
    # source API exposes one directly. Used to attach that photo to the
    # "ارسال محصول" (ship) Activity in Didar - see
    # DidarActivityClient.create_post_sale_checklist. None means no
    # attachment is uploaded for that order (not an error).
    product_image_url: str | None = None

    # Shipping cost for the order. This field is needed by the Telegram
    # notification to display shipping information. Some adapters may not
    # expose this field, in which case it will be None.
    shipping_cost: Decimal | None = None

    # Customer/courier-facing tracking number (شماره مرسوله), for display
    # in Didar (client request, 2026-09) - see DidarDealClient's
    # _build_item_description(). Deliberately a SEPARATE field from
    # shipment_id above: for Digikala, shipment_id is Digikala's own
    # internal identifier used as an API call parameter (fetching SBS
    # customer/shipment details), while the actual postal tracking code a
    # customer would use is a different value only available from a
    # separate call (DigikalaAdapter.fetch_shipment_details) - conflating
    # the two would either break those API calls or show the wrong
    # number to the client. For sources where shipment_id already IS the
    # customer-facing parcel number (Basalam's post_receipt.tracking_code,
    # Tapsi Shop's shipments[].number), adapters set both fields to the
    # same value; DidarDealClient falls back to shipment_id when this is
    # None.
    shipment_tracking_code: str | None = None

    # Courier/shipping method name exactly as the marketplace reports it
    # (e.g. WooCommerce's shipping_lines[].method_title - "پیشتاز" /
    # "تیپاکس" for Faraz Honar). Used by src/shipping_fees.py to pick the
    # right flat shipping-fee amount to display for that order (client
    # request, 2026-09: fixed per-courier amounts, distinct from
    # shipping_cost above which is the real API-reported figure). None
    # means the adapter doesn't expose a shipping method for this order.
    shipping_method: str | None = None


class MarketplaceAdapter(ABC):
    """
    Abstract base every marketplace adapter implements.

    Keeping this interface intentionally small (two methods) makes each
    adapter easy to test in isolation and easy to swap out.
    """

    #: short machine-readable identifier, must match NormalizedOrder.source
    name: str

    @abstractmethod
    def fetch_new_orders(self, since: datetime | None) -> list[NormalizedOrder]:
        """
        Return all orders created at or after `since`, already normalized.

        If `since` is None, the adapter should fetch orders from a reasonable
        recent window (typically the last 5 hours) - the Sync Engine handles
        sliding window logic and deduplication via the repository.

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