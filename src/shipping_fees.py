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

UNIT: TOMAN, not Rial - the client stated these numbers directly in
Toman and asked for them to be shown "با واحد قیمتی خودشون" (in their
own price unit) wherever the ORIGINAL Toman display is used (currently
only Didar's DealItem Description - see deal_client.py). Telegram's
"هزینه ارسال" line is a separate case (client request, 2026-09): it
must show the RIAL equivalent instead (`shipping_fee_rial()` below,
i.e. this Toman amount * 10 via src/currency.py's `to_rial()`), and
that Rial figure also replaces order.total_price when building the
Telegram message's "مبلغ کل" (grand total = products total + this
shipping fee) - see src/telegram.py's _format_new_order_message.
src/currency.py's Toman->Rial conversion is otherwise only for amounts
that flow into Didar's own numeric money fields (UnitPrice, etc.); the
Didar Description text stays plain Toman display text, unaffected by
that.

DIGIKALA: flat 239,000 Toman for every order (client-stated flat rate,
independent of whatever real shipping_cost that source's own API
happens to report for a given order). CORRECTED 2026-09: the original
figure taken from the client was 239 Toman - implausibly small for an
actual shipping fee (~2,390 Rial) - and was itself a client-side typo;
the client confirmed the real intended amount is 239,000 Toman
(2,390,000 Rial).

FARAZ HONAR: depends on which courier the order was shipped by
(NormalizedOrder.shipping_method, populated by the adapter from
WooCommerce's shipping_lines[].method_title):
    - "پیشتاز" (Pishtaz)  -> 225,000 Toman (2,250,000 Rial)
    - "تیپاکس" (Tipax)    -> 250,000 Toman (2,500,000 Rial)
    - anything else (a different courier, or no shipping method at all)
      -> None, i.e. no shipping line for that order. Per the client's
      own instruction, an unrecognized method must never be guessed as
      one of these two.
Same 2026-09 correction as Digikala above: these were originally 225
and 250 Toman (also implausibly small) before the client confirmed the
real amounts are 1,000x that.

EVERY OTHER SOURCE (Tapsi Shop, Basalam, SnappShop): always None - per
the client, those platforms have no shipping cost to show at all.
"""
from __future__ import annotations

from decimal import Decimal

from src.currency import TOMAN, to_rial
from src.didar.category_mapping import _normalize_fa
from src.marketplaces.base import NormalizedOrder

DIGIKALA_SHIPPING_FEE_TOMAN = Decimal("239000")
FARAZHONAR_PISHTAZ_FEE_TOMAN = Decimal("225000")
FARAZHONAR_TIPAX_FEE_TOMAN = Decimal("250000")

_PISHTAZ_KEYWORD = "پیشتاز"
_TIPAX_KEYWORD = "تیپاکس"


def shipping_fee_toman(order: NormalizedOrder) -> Decimal | None:
    """The fixed display shipping fee (Toman) for this order's Didar
    DealItem Description line, or None when this source/method has no
    such fixed fee - every source besides Digikala and Faraz Honar, or a
    Faraz Honar order whose shipping method is neither Pishtaz nor
    Tipax. A None return means the caller should fall back to its own
    existing behaviour (see call sites) rather than show nothing
    outright, since only Digikala/Faraz Honar are affected by this
    feature at all."""
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


def shipping_fee_rial(order: NormalizedOrder) -> Decimal | None:
    """Same fixed fee as `shipping_fee_toman()`, converted to Rial (via
    src/currency.py's `to_rial()`, i.e. * 10) for Telegram's "هزینه
    ارسال" line and grand-total (client request, 2026-09: Telegram shows
    Rial while Didar's Description text stays Toman - see this module's
    docstring). None under the same conditions as `shipping_fee_toman()`.
    """
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
