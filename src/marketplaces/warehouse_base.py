"""
Shared data shape for "ارسال به انبار دیجی‌کالا" (FBD - Fulfillment by
Digikala) shipment items.

WHY A SEPARATE FILE INSTEAD OF ADDING TO base.py: base.py defines
NormalizedOrder + MarketplaceAdapter, the contract every *customer order*
source (Tapsi Shop, Digikala SBS, SnappShop, Basalam, Faraz Honar)
implements. FBD items are not customer orders - they are items Digikala
itself is buying from the seller to stock its own warehouse, fetched from
a completely different endpoint (GET /open-api/v1/orders, NOT
/ship-by-seller-orders), with no customer field of any kind. Forcing this
into NormalizedOrder/OrderItem/MarketplaceAdapter would mean a pile of
Optional fields that are always None for this source and a fake
implementation of fetch_order_detail() (there is no per-item detail
endpoint to call). Kept fully separate instead, per this project's
per-source isolation convention (see digikala.py's own docstring) -
NormalizedOrder/MarketplaceAdapter in base.py are untouched by this file.

FIELD PROVENANCE (2026-09-18 live probe against seller.digikala.com,
GET /open-api/v1/orders?page=1&size=5 - see scripts/probe_digikala_warehouse.py
and its saved output):

  - order_item_id: CONFIRMED present on every row, and unique per row in
    the sample - used as source_shipment_id. (order_id is NOT unique per
    row: a single order can contain more than one item.)
  - Status: the response has NO textual status enum - only
    `warehouse_status_at`, a timestamp. There is nothing resembling
    "pending" vs "confirmed" to filter on - so no status field is
    modeled here at all; being returned by this endpoint IS "active".
    Every item seen is tracked from the first poll onward (see the
    cold-start docstring in digikala_warehouse.py, stage 2). Each Deal
    DOES carry a PersonId - a dedicated placeholder Contact, configured
    via DIDAR_WAREHOUSE_PLACEHOLDER_PERSON_ID, the same on every FBD
    deal - because Didar's Deal.save_v2 rejects a Deal with neither
    PersonId nor CompanyId; see create_warehouse_shipment_deal() in
    deal_client.py.
  - Dates (order_created_at, warehouse_status_at, commitment_date):
    CONFIRMED Gregorian/ISO 8601 with a numeric UTC offset (e.g.
    "2026-09-11T08:49:01.000000+03:30") - NOT Jalali. Parsed the same way
    digikala.py's _parse_history_date() parses /orders/history dates
    (also ISO), not _parse_jalali_date().
  - Price: CONFIRMED `selling_price` is PER UNIT - the probe's two sample
    rows both have selling_price * quantity == total_price exactly (one
    row: 3,000,000 * 1 == 3,000,000; see probe output). Per this
    project's convention, total_price is therefore NOT stored as its own
    field (it's derived, and storing both invites the two silently
    drifting apart) - unit_price is the one source of truth, and is
    already converted via src/currency.py's to_rial(selling_price,
    config.price_unit) by the adapter before this dataclass is built, the
    same as every other source's OrderItem.unit_price.
  - supplier_code: present but EMPTY STRING on both probed sample rows -
    kept as a plain optional string for display only. Per the spec's
    5-3/collision-prone-SKU warning (2026-09 wrong-product incident,
    order 382920341), this must never be used as a Didar product Code -
    that decision lives in the Didar deal-item builder (stage 4), not
    here.
  - product_image_url: CONFIRMED present.

Every field below is None only to mean "the API did not return this for
this row" - never a stand-in for zero, an empty string, or a guessed
default.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class WarehouseShipmentItem:
    """
    One row from GET /open-api/v1/orders (an item Digikala is buying from
    the seller to stock its own warehouse - "ارسال به انبار دیجی‌کالا").

    `source` + `source_shipment_id` together form the unique key used for
    duplicate-prevention in the local database - see
    db/repository.py's synced_warehouse_shipments table (stage 3) - the
    same pairing convention NormalizedOrder.source/source_order_id uses
    in base.py, kept parallel on purpose even though this dataclass is
    otherwise unrelated to NormalizedOrder.
    """

    source: str                  # "digikala_warehouse" (first Digikala store only - see digikala_warehouse.py)
    source_shipment_id: str      # order_item_id, as a string - CONFIRMED unique per row, see module docstring
    product_title: str           # product_variant_title
    quantity: int
    unit_price: Decimal          # selling_price, already run through to_rial() by the adapter
    created_at: datetime         # order_created_at

    # URL of the item's product photo, attached to the single "ارسال
    # محصول" ship Activity in Didar (stage 5) - None means no photo was
    # returned for this row, not a broken/missing image.
    product_image_url: str | None = None

    # Anchor date for the ship Activity's due date (stage 5). None means
    # this specific row didn't return one - the Activity builder falls
    # back to created_at + a default delay rather than fabricating a date.
    commitment_date: datetime | None = None

    # Digikala's own order id this item belongs to (NOT the dedupe key -
    # see source_shipment_id above). Carried through only for display in
    # the Didar Deal/Activity description, same as order_number is
    # display-only context elsewhere in this project.
    order_id: str | None = None

    # Seller's own short product code, as Digikala returns it. Display
    # (Description text) ONLY - see module docstring's collision-prone-SKU
    # warning. Never fed to Didar's product Code field.
    supplier_code: str | None = None
