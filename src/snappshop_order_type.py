"""
Classifies a SnappShop (first vendor account, Tehran) order as one of the
two "send to Snapp's warehouse" types, from fields confirmed on real
orders (2026-10-11, GET /vendors/{id}/orders/{order_number}):

    ارسال فوری به انبار اسنپ  -> delivery_type "FBM"   (pickup window 00:00-10:00)
    ارسال به انبار اسنپ       -> delivery_type "NORMAL" with an EMPTY customer.address
    اکسپرس                    -> delivery_type "EXPRESS"   (not handled here)
    ارسال توسط فروشنده         -> NORMAL with a populated customer.address (not handled here)

Per the vendor API doc, customer.address is populated ONLY for
vendor-delivered orders, which is what separates a NORMAL warehouse order
from a vendor-delivered one. Snapp itself decides "urgent" (Tehran
customers) - the API has no buyer city field - so FBM is taken as that
signal rather than guessing a city.

Only the first account ("snappshop") is classified: the second account
(Isfahan) has no warehouse modes, only express and normal. Anything not
positively identified returns None, and callers keep their old behaviour.
"""
from __future__ import annotations

from src.marketplaces.base import NormalizedOrder

URGENT_WAREHOUSE = "urgent_warehouse"
WAREHOUSE = "warehouse"

URGENT_WAREHOUSE_NOTE_TEXT = "ارسال فوری به انبار اسنپ"
WAREHOUSE_NOTE_TEXT = "ارسال به انبار اسنپ"


def snappshop_warehouse_kind(order: NormalizedOrder) -> str | None:
    if order.source != "snappshop":
        return None
    method = (order.shipping_method or "").strip().upper()
    if method == "FBM":
        return URGENT_WAREHOUSE
    if method == "NORMAL" and order.has_delivery_address is False:
        return WAREHOUSE
    return None
