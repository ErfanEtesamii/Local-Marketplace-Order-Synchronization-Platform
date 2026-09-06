"""
Toman -> Rial conversion.

WHY THIS EXISTS: Didar expects order/deal amounts in Rial, but not
every marketplace's API returns prices in Rial - some return Toman
(1 Toman = 10 Rial), so those numbers need multiplying by 10 before
they reach src/didar/deal_client.py, or every amount synced from that
source is off by a factor of 10.

PER-SOURCE UNIT, AND HOW CONFIDENT EACH ONE IS (2026-08-29):
  - Faraz Honar (WooCommerce): TOMAN - confirmed directly by the client
    checking real order data.
  - Digikala: RIAL already - Digikala's own web service is documented
    (by a third-party WooCommerce-integration vendor, not Digikala's
    own docs directly, but describing Digikala's API specifically) as
    Rial-based. Not from a Didar-side live test, so treat as
    reasonably-but-not-100%-confirmed.
  - Basalam: RIAL - CONFIRMED (2026-09, client checked real order data).
    The earlier "toman" default here was only inferred indirectly from
    the official Basalam SDK's quick-start example printing a price
    with "تومان" next to it - never a confirmed live order payload, and
    it turned out to be wrong.
  - SnappShop, Tapsi Shop: UNCONFIRMED - neither vendor's available
    documentation states a currency unit anywhere. Defaulted to "rial"
    (no conversion applied) purely to avoid silently guessing on money;
    this is a placeholder, not a claim that it's correct.

Each source's unit is a config value (see src/config.py's
`price_unit` fields, one per marketplace, each reading a
`<SOURCE>_PRICE_UNIT` env var), NOT hardcoded here - so if a default
above turns out wrong, or SnappShop/Tapsi Shop's real unit gets
confirmed later, it's a one-line .env change, not a code change.
Whoever confirms a unit for real should also update the comment above
and in .env.example so this docstring doesn't go stale.
"""
from __future__ import annotations

from decimal import Decimal

RIAL = "rial"
TOMAN = "toman"


def to_rial(amount: Decimal, unit: str) -> Decimal:
    """Convert amount to Rial given the unit it's currently in.
    Unknown/blank units are treated as already-Rial (no-op) rather than
    raising, so a typo'd env var degrades to "no conversion" instead of
    crashing the sync - see PriceUnit validation in src/config.py for
    where a typo would actually get caught."""
    if unit.strip().lower() == TOMAN:
        return amount * 10
    return amount