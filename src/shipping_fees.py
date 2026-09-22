"""
Fixed, client-specified shipping-fee DISPLAY amounts - REMOVED
ENTIRELY 2026-09. Both platforms that ever had a flat override here
(Digikala, removed earlier in 2026-09; Faraz Honar, removed later the
same month once the client confirmed they want real per-order figures
everywhere) have had it taken out. shipping_fee_toman()/
shipping_fee_rial() are kept as always-None stubs purely so
src/didar/deal_client.py and src/telegram.py don't need their call
sites rewritten - both already fall back to the real
NormalizedOrder.shipping_cost whenever these return None (see each
call site's own comment), so with these permanently returning None,
every source now shows its real, API-reported shipping figure:
    - Digikala: `shippingCost` via its SBS endpoints (GET
      /open-api/v1/ship-by-seller-orders and
      GET /open-api/v1/ship-by-seller-orders/{shipment_id})
    - Basalam: `shipping_cost` via GET /v3/vendor-parcels/{parcel_id}
    - Tapsi Shop: `shipments[].operationalCost` via
      GET /orders/{orderId} (docs confirm this one field is already
      Rial, unlike this source's other price fields)
    - Faraz Honar: WooCommerce's standard `shipping_total` order field
    - SnappShop: has no shipping-cost field anywhere in its API at
      all (confirmed against its own docs) - order.shipping_cost stays
      None for this source, same as always, so no shipping line shows.
All four sources besides SnappShop already populated
NormalizedOrder.shipping_cost correctly from these real fields before
this change - the flat overrides only ever affected the two DISPLAY
lines this module covers (Didar's DealItem Description, Telegram's
per-order "هزینه ارسال" line + grand total), never the aggregate
report totals, which always used the real figure.

HISTORY (kept for context - neither override applies anymore):
  - DIGIKALA: was a flat 239,000 Toman for every order, corrected once
    already from a client typo of 239 Toman, then removed entirely
    once the client confirmed Digikala's real shipping cost varies per
    shipment (SBS `shippingCost`, e.g. 650,000 Rial in one sample
    order).
  - FARAZ HONAR: was 225,000 Toman for "پیشتاز" (Pishtaz) and 250,000
    Toman for "تیپاکس" (Tipax), read from
    NormalizedOrder.shipping_method (WooCommerce's
    shipping_lines[].method_title) - also corrected once already from
    225/250 Toman. Removed once the client confirmed they want the
    real WooCommerce `shipping_total` shown instead, same as every
    other source.
"""
from __future__ import annotations

from decimal import Decimal

from src.currency import TOMAN, to_rial
from src.marketplaces.base import NormalizedOrder


def shipping_fee_toman(order: NormalizedOrder) -> Decimal | None:
    """Always None now - see module docstring. Kept so call sites keep
    falling back to the real order.shipping_cost without needing to be
    rewritten."""
    return None


def shipping_fee_rial(order: NormalizedOrder) -> Decimal | None:
    """Always None now, same as `shipping_fee_toman()` - see module
    docstring. Kept (rather than deleted) purely so
    src/telegram.py's call site doesn't need rewriting."""
    fee_toman = shipping_fee_toman(order)
    if fee_toman is None:
        return None
    return to_rial(fee_toman, TOMAN)


def format_toman(amount: Decimal) -> str:
    """Format a Toman amount with thousands separators, e.g. 12500 ->
    "12,500". Matches the ASCII-digit/comma convention already used for
    Rial amounts elsewhere in this project (src/telegram.py's
    _format_rial, src/didar/deal_client.py's _format_rial)."""
    return f"{int(round(float(amount))):,}"