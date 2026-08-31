"""
SnappShop adapter - vendor webservice.

Source: two vendor-onboarding blog posts (not a Swagger/Postman doc), so
we have solid *behavioral* documentation but no worked JSON example for
two of the three endpoints. Documenting exactly what is and isn't
confirmed, the same way basalam.py and digikala.py do:

CONFIRMED:
  - Auth: header `Authorization: Bearer {token}` + header
    `Agent-User: {vendor_identifier}` on every request. 401 if invalid.
  - GET  /vendors                                    -> list accessible vendors
  - GET  /vendors/{vendor_id}                         -> single vendor detail
  - GET  /vendors/{vendor_id}/orders?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD
        -> paginated order history, 20/page, cursor-based (`next_cursor`).
           Defaults to the last 10 days if no dates are given.
  - GET  /vendors/{vendor_id}/orders/{order_number}   -> full order detail;
           customer contact info only included when the vendor (not
           SnappShop) handles delivery.
  - GET  /vendors/{vendor_id}/orders/events           -> cursor-paginated
           lifecycle events (NEW_ORDER / CANCELLATION / CHANGE_STATUS),
           each with `event_type`, `order_number`, `event_at`. This is
           SnappShop's inventory-sync feature, not used by this adapter,
           but is a candidate for a lower-latency detection mechanism
           later if the history endpoint proves too coarse.

NOT CONFIRMED (no JSON example was provided for these two endpoints):
  - The exact field names inside each order's JSON object for the
    history list and the order-detail endpoints (only prose
    descriptions like "مبالغ، نوع ارسال" were given, no schema).
  - The exact response envelope shape (assumed to follow the same
    `{"data": [...], "meta": {"pagination": {"has_more", "next_cursor"}}}`
    shape shown for the Order Events endpoint, since these come from the
    same vendor webservice - reasonable but NOT verified).

_SCHEMA_CONFIRMED = False until a real token lets us inspect an actual
populated response; _normalize_* functions use defensive .get() with
fallbacks and log a loud warning on every call in the meantime, exactly
like the Basalam adapter did before its schema was confirmed.

Base URL: apix.snappshop.ir is *inferred* from Snapp's public bug-bounty
domain scope, not stated in the docs we have - see .env.example.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import httpx

from src.config import SnappShopConfig, settings
from src.currency import to_rial
from src.finglish import persianize_name
from src.http_utils import default_retry, raise_for_status_with_body
from src.logger import get_logger
from src.marketplaces.base import MarketplaceAdapter, NormalizedOrder, OrderItem

log = get_logger(__name__)

_SCHEMA_CONFIRMED = False  # flip to True once _normalize_* has been verified against real data


def _to_decimal(value) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        return Decimal("0")


class SnappShopAdapter(MarketplaceAdapter):
    name = "snappshop"

    def __init__(self, config: SnappShopConfig | None = None) -> None:
        self._config = config or settings.snappshop
        self._client = httpx.Client(
            base_url=self._config.base_url,
            headers={
                "Authorization": f"Bearer {self._config.auth_token}",
                "Agent-User": self._config.agent_user,
            },
            timeout=30.0,
        )

    @default_retry()
    def _get(self, path: str, params: dict | None = None) -> dict:
        resp = self._client.get(path, params=params or {})
        raise_for_status_with_body(resp)
        return resp.json()

    def fetch_new_orders(self, since: datetime | None) -> list[NormalizedOrder]:
        if not _SCHEMA_CONFIRMED:
            log.warning(
                "snappshop: order field schema is UNCONFIRMED (no populated response "
                "has been inspected yet - only prose docs, no JSON example). Normalized "
                "output may be missing fields until this is verified - see "
                "src/marketplaces/snappshop.py module docstring."
            )

        vendor_id = self._config.vendor_id
        orders: list[NormalizedOrder] = []
        cursor = None

        while True:
            params = {
                "start_date": (since or datetime.now(timezone.utc) - timedelta(hours=5)).date().isoformat(),
                "end_date": datetime.now(timezone.utc).date().isoformat(),
            }
            if cursor:
                params["cursor"] = cursor

            payload = self._get(f"/vendors/{vendor_id}/orders", params=params)
            raw_orders = payload.get("data", [])
            orders.extend(self._normalize_list_item(o) for o in raw_orders)

            pagination = payload.get("meta", {}).get("pagination", {})
            cursor = pagination.get("next_cursor")
            if not pagination.get("has_more") or not raw_orders:
                break

        log.info("snappshop: fetched %d orders", len(orders))
        return orders

    def fetch_order_detail(self, source_order_id: str) -> NormalizedOrder:
        vendor_id = self._config.vendor_id
        payload = self._get(f"/vendors/{vendor_id}/orders/{source_order_id}")
        return self._normalize_detail(payload.get("data", payload))

    def discover_vendor_id(self) -> str:
        """
        One-time helper: call GET /vendors and return the first vendor's
        id, for populating SNAPPSHOP_VENDOR_ID in .env. Not called
        automatically - vendor_id should be a fixed config value once
        known, not re-discovered on every run.
        """
        payload = self._get("/vendors")
        vendors = payload.get("data", [])
        if not vendors:
            raise ValueError("snappshop: GET /vendors returned no vendors")
        return str(vendors[0].get("id") or vendors[0].get("vendor_id"))

    def _normalize_list_item(self, raw: dict) -> NormalizedOrder:
        return NormalizedOrder(
            source=self.name,
            source_order_id=str(raw.get("order_number", raw.get("id", ""))),
            order_number=str(raw.get("order_number", raw.get("id", ""))),
            created_at=_parse_date(raw.get("created_at", raw.get("event_at"))),
            total_price=to_rial(_to_decimal(raw.get("total_price", raw.get("amount"))), self._config.price_unit),
            status=str(raw.get("status", "unknown")),
            items=[],  # list endpoint - fetch_order_detail has full items
            customer_full_name=None,
            customer_mobile=None,
        )

    def _normalize_detail(self, raw: dict) -> NormalizedOrder:
        items = [
            OrderItem(
                sku=str(item.get("sku", item.get("vendor_product_info_id", ""))),
                title=str(item.get("title", item.get("product_title", ""))),
                quantity=int(item.get("quantity", item.get("deliverable_quantity", 1))),
                unit_price=to_rial(
                    _to_decimal(item.get("unit_price", item.get("price"))), self._config.price_unit
                ),
                final_price=to_rial(
                    _to_decimal(item.get("final_price", item.get("total_price"))),
                    self._config.price_unit,
                ),
            )
            for item in raw.get("items", raw.get("products", []))
        ]

        customer = raw.get("customer", {})

        return NormalizedOrder(
            source=self.name,
            source_order_id=str(raw.get("order_number", "")),
            order_number=str(raw.get("order_number", "")),
            created_at=_parse_date(raw.get("created_at")),
            total_price=to_rial(_to_decimal(raw.get("total_price", raw.get("amount"))), self._config.price_unit),
            status=str(raw.get("status", "unknown")),
            items=items,
            # Only populated when the vendor (not SnappShop) handles
            # delivery, per the confirmed doc note - None otherwise.
            # When it is populated and was typed with an English
            # keyboard (e.g. "mohammad ahmadi"), convert it back to
            # Persian script - see src/finglish.py for how/why this is
            # approximate.
            customer_full_name=persianize_name(customer.get("name") or customer.get("full_name")),
            customer_mobile=customer.get("mobile") or customer.get("phone"),
        )


def _parse_date(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
