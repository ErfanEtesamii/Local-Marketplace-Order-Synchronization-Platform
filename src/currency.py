"""
Toman -> Rial conversion.

WHY THIS EXISTS: Didar expects order/deal amounts in Rial, but not
every marketplace's API returns prices in Rial - some return Toman
(1 Toman = 10 Rial), so those numbers need multiplying by 10 before
they reach src/didar/deal_client.py, or every amount synced from that
source is off by a factor of 10.

PER-SOURCE UNIT (confirmed by the client, 2026-08-29):
  - Faraz Honar (WooCommerce): TOMAN
  - Basalam: TOMAN
  - SnappShop: TOMAN
  - Digikala: RIAL
  - Tapsi Shop: RIAL

Each source's unit is a config value (see src/config.py's
`price_unit` fields, one per marketplace, each reading a
`<SOURCE>_PRICE_UNIT` env var), NOT hardcoded here - so if any of the
above ever turns out wrong (e.g. a marketplace changes its API), it's
a one-line .env change, not a code change.
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
