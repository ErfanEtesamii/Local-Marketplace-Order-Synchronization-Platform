"""
Fixed, client-specified shipping-fee DISPLAY amounts (client request,
2026-09) for the two platforms whose product Description / Telegram
notification should show a shipping line: Digikala (flat) and Faraz
Honar (depends on which courier the order was shipped by).

THIS IS DELIBERATELY SEPARATE FROM NormalizedOrder.shipping_cost:
that field already holds each source's own real, API-reported shipping
figure (Digikala via its SBS shipment-details endpoint, Faraz Honar via
WooCommerce's "shipping_total") and keeps feeding whatever already
consumes it unchanged (e.g. Telegram's aggregate daily/weekly/monthly/
yearly report totals). The amounts here are flat numbers the client
gave directly and are used ONLY for the two specific display lines this
feature covers (the Didar DealItem Description and the Telegram
per-order "هزینه ارسال" line) - see src/didar/deal_client.py and
src/telegram.py.

UNIT: TOMAN, not Rial - the client stated these two numbers directly in
Toman ("239 تومنه"، "225 ... 250 تومنه") and asked for them to be shown
"با واحد قیمتی خودشون" (in their own price unit). src/currency.py's
Toman->Rial conversion is for amounts that flow into Didar's numeric
money fields (UnitPrice, etc.) - these are plain display text only (see
deal_client.py's _build_item_description docstring), so no conversion
is applied here.

DIGIKALA: flat 239 Toman for every order (client-stated flat rate,
independent of whatever real shipping_cost that source's own API
happens to report for a given order).

FARAZ HONAR: depends on which courier the order was shipped by
(NormalizedOrder.shipping_method, populated by the adapter from
WooCommerce's shipping_lines[].method_title):
    - "پیشتاز" (Pishtaz)  -> 225 Toman
    - "تیپاکس" (Tipax)    -> 250 Toman
    - anything else (a different courier, or no shipping method at all)
      -> None, i.e. no shipping line for that order. Per the client's
      own instruction, an unrecognized method must never be guessed as
      one of these two.

EVERY OTHER SOURCE (Tapsi Shop, Basalam, SnappShop): always None - per
the client, those platforms have no shipping cost to show at all.
"""
from __future__ import annotations

from decimal import Decimal

from src.didar.category_mapping import _normalize_fa
from src.marketplaces.base import NormalizedOrder

DIGIKALA_SHIPPING_FEE_TOMAN = Decimal("239")
FARAZHONAR_PISHTAZ_FEE_TOMAN = Decimal("225")
FARAZHONAR_TIPAX_FEE_TOMAN = Decimal("250")

_PISHTAZ_KEYWORD = "پیشتاز"
_TIPAX_KEYWORD = "تیپاکس"


def shipping_fee_toman(order: NormalizedOrder) -> Decimal | None:
    """The fixed display shipping fee (Toman) for this order's product
    Description / Telegram "هزینه ارسال" line, or None when this
    source/method has no such fixed fee - every source besides Digikala
    and Faraz Honar, or a Faraz Honar order whose shipping method is
    neither Pishtaz nor Tipax. A None return means the caller should
    fall back to its own existing behaviour (see call sites) rather
    than show nothing outright, since only Digikala/Faraz Honar are
    affected by this feature at all."""
    if order.source == "digikala":
        return DIGIKALA_SHIPPING_FEE_TOMAN

    if order.source == "farazhonar":
        method = _normalize_fa(order.shipping_method or "")
        if _PISHTAZ_KEYWORD in method:
            return FARAZHONAR_PISHTAZ_FEE_TOMAN
        if _TIPAX_KEYWORD in method:
            return FARAZHONAR_TIPAX_FEE_TOMAN
        return None

    return None


def format_toman(amount: Decimal) -> str:
    """Format a Toman amount with thousands separators, e.g. 12500 ->
    "12,500". Matches the ASCII-digit/comma convention already used for
    Rial amounts elsewhere in this project (src/telegram.py's
    _format_rial, src/didar/deal_client.py's _format_rial)."""
    return f"{int(round(float(amount))):,}"
