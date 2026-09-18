"""
Express-order detection: the single place that answers "is this order an
EXPRESS shipment?" for every source, so the SMS alert feature
(src/modir_payamak.py) never has to branch per marketplace.

WHY ONE SOURCE-AGNOSTIC KEYWORD MATCH INSTEAD OF PER-SOURCE BRANCHES:
each adapter already does its own source-specific parsing and writes the
result into the SAME field, NormalizedOrder.shipping_method (see
base.py):

    - Basalam    -> shipping_method.default.title (fallback .current.title),
                    free Persian text, e.g. "پست اکسپرس"
    - SnappShop  -> delivery_type, only present on GET /orders/ and
                    GET /orders/{order_number} (never on /orders/events,
                    where it stays None - nothing is guessed)
    - Digikala   -> the boolean isDigiExpress, already mapped by
      / digikala2   _digikala_shipping_method() to the sentinel strings
                    "EXPRESS" / "NORMAL" / None
    - Faraz Honar-> WooCommerce shipping_lines[].method_title ("پیشتاز",
                    "تیپاکس", ...) - never express, matches nothing below
    - Tapsi Shop -> no shipping-method field at all -> None -> False

So by the time an order reaches this module the per-source work is
done, and adding a source here is a no-op as long as its adapter fills
shipping_method. That also keeps the sources isolated: this function
reads one field and cannot make one marketplace's payload shape affect
another's.

NEVER GUESS: a missing/empty/None shipping_method returns False, not a
throw and not an optimistic "probably express". The cost of a false
negative is one un-sent warehouse SMS; a false positive would page
three people for a normal order.

MATCHING: keywords are compared as substrings after the project's
existing Persian normalization (src/didar/category_mapping._normalize_fa
- the same helper src/shipping_fees.py uses for its courier keywords),
which folds Arabic ي/ك to Persian ی/ک, turns ZWNJ into a space and
casefolds. Substring rather than equality because the real values are
embedded in longer titles ("ارسال اکسپرس", "پست اکسپرس"). "3198" is the
numeric delivery-type code that also stands for express.

DIGITS: _fold_digits() below additionally maps Persian (۰-۹, U+06F0..F9)
and Arabic-Indic (٠-٩, U+0660..69) digits to ASCII before matching, so a
delivery type reported as "۳۱۹۸" resolves the same as "3198". No source
has been observed to send the Persian form - this is defensive only
(client request, on reviewing stage 3) - but folding digit shapes is a
pure character normalization, not an assumption about a payload: the
code still has to be literally present to match, nothing is inferred
from its absence. Deliberately done HERE rather than inside the shared
_normalize_fa(), which is also used by the Didar category-title
matching that this feature must not touch.
"""
from __future__ import annotations

from src.didar.category_mapping import _normalize_fa
from src.marketplaces.base import NormalizedOrder

# Persian and Arabic-Indic digit shapes -> ASCII. str.translate table,
# built once; see the DIGITS paragraph in the module docstring for why
# this lives here and not in _normalize_fa().
_DIGIT_TRANSLATION = {
    **{0x06F0 + i: str(i) for i in range(10)},  # ۰-۹ Persian (extended Arabic-Indic)
    **{0x0660 + i: str(i) for i in range(10)},  # ٠-٩ Arabic-Indic
}


def _fold_digits(text: str) -> str:
    """Rewrites Persian/Arabic-Indic digits as ASCII so a numeric
    delivery-type code matches whichever digit shape the API sent."""
    return text.translate(_DIGIT_TRANSLATION)


# Normalized once at import time so the comparison below is a plain
# substring check against already-normalized text (same shape as
# shipping_fees.py's _PISHTAZ_KEYWORD / _TIPAX_KEYWORD).
EXPRESS_KEYWORDS: tuple[str, ...] = tuple(
    _normalize_fa(_fold_digits(keyword)) for keyword in ("EXPRESS", "اکسپرس", "3198")
)


def is_express_order(order: NormalizedOrder) -> bool:
    """True only when this order's shipping method positively identifies
    it as an express shipment (see module docstring for the per-source
    field each value comes from). False for every other case, including
    a shipping_method that is None or empty - never guessed, never
    raises.

    Note the Digikala sentinel "NORMAL" correctly returns False here
    while still being distinguishable from "field absent" (None)
    upstream, which is why the adapter maps False to "NORMAL" rather
    than to None.
    """
    raw_method = order.shipping_method
    if raw_method is None:
        return False
    # str() rather than assuming text: SnappShop's delivery_type is a
    # raw JSON value that may well arrive as a number (e.g. 3198).
    method = _normalize_fa(_fold_digits(str(raw_method)))
    if not method:
        return False
    return any(keyword in method for keyword in EXPRESS_KEYWORDS) 