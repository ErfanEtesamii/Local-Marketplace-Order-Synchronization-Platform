"""
Basalam adapter - Official Order Processing Service.

Base URL:  https://order-processing.basalam.com
Docs:      developers.basalam.com -> Order Processing Service -> VendorOrder

This replaces the earlier draft of this file, which was based on an
undocumented internal endpoint discovered via browser network inspection.
Basalam turned out to have an official developer platform ("Salam API")
with OAuth2 authentication and a documented VendorOrder API - that is what
this adapter uses instead. See docs/architecture.md for the history.

Key concept: Basalam's unit is the "parcel", not the "order" directly.
A single platform order can be split into multiple parcels (e.g. across
vendors in the same cart); since this API is vendor-scoped, each parcel
already represents exactly this vendor's share of an order, which is the
natural sync unit for this project. NormalizedOrder.source_order_id maps
to the parcel id; NormalizedOrder.order_number carries the underlying
platform order id (order.id) for cross-reference.

Endpoints used:
  GET /v3/vendor-parcels                 -> paginated list (cursor-based)
  GET /v3/vendor-parcels/{parcel_id}      -> full detail incl. items

Required OAuth scope: vendor.parcel.read

Authentication: standard OAuth2 Bearer token (or Personal Access Token
from the developer panel, which is what this project uses - a single
vendor's own service does not need the full authorization-code redirect
flow). No confirmed refresh-token endpoint URL for this project yet;
until that is added, a 401 raises BasalamAuthError with instructions to
issue a fresh token from the developer panel.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import httpx
from src.http_utils import default_retry, raise_for_status_with_body

from src.config import BasalamConfig, settings
from src.logger import get_logger
from src.marketplaces.base import MarketplaceAdapter, NormalizedOrder, OrderItem

log = get_logger(__name__)

# NEW_ORDER status code, per the documented `statuses` enum - available for
# callers that want to scope polling to freshly placed orders only.
STATUS_NEW_ORDER = 3739


def _to_decimal(value) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        return Decimal("0")



class BasalamAuthError(RuntimeError):
    """Raised when the access token is rejected (expired/revoked).

    Recovery: issue a fresh Personal Access Token from
    developers.basalam.com/panel and update BASALAM_ACCESS_TOKEN in .env.
    """


class BasalamAdapter(MarketplaceAdapter):
    name = "basalam"

    def __init__(self, config: BasalamConfig | None = None) -> None:
        self._config = config or settings.basalam
        self._client = httpx.Client(
            base_url=self._config.base_url or "https://order-processing.basalam.com",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._config.access_token}",
            },
            timeout=30.0,
        )

    @default_retry()
    def _get(self, path: str, params: dict | None = None) -> dict:
        resp = self._client.get(path, params=params or {})
        if resp.status_code == 401:
            raise BasalamAuthError(
                "Basalam access token rejected (401). Issue a fresh Personal Access "
                "Token from developers.basalam.com/panel and update .env."
            )
        raise_for_status_with_body(resp)
        return resp.json()

    def fetch_new_orders(self, since: datetime) -> list[NormalizedOrder]:
        orders: list[NormalizedOrder] = []
        cursor = None

        while True:
            params = {
                "created_at[gte]": since.astimezone(timezone.utc).isoformat(),
                # Confirmed via live 422s ("مرتب سازی معتبر نمی باشد"):
                # created_at:desc and id:desc are both rejected. Only
                # estimate_send_at:desc (the documented default) works -
                # order discovery still relies on created_at[gte] below,
                # which the API does accept as a filter, just not as a
                # sort key.
                "sort": "estimate_send_at:desc",
                # Confirmed via a live 422 ("Input should be less than or
                # equal to 30"): 50 (an assumption from earlier adapters)
                # is rejected. 30 is the confirmed maximum.
                "per_page": 30,
            }
            if cursor:
                params["cursor"] = cursor

            payload = self._get("/v3/vendor-parcels", params=params)
            raw_parcels = payload.get("data", [])
            orders.extend(self._normalize_list_item(p) for p in raw_parcels)

            cursor = payload.get("next_cursor")
            if not cursor or not raw_parcels:
                break

        log.info("basalam: fetched %d new orders since %s", len(orders), since.isoformat())
        return orders

    def fetch_order_detail(self, source_order_id: str) -> NormalizedOrder:
        payload = self._get(f"/v3/vendor-parcels/{source_order_id}")
        return self._normalize_detail(payload)

    def _normalize_list_item(self, raw: dict) -> NormalizedOrder:
        order = raw.get("order", {})
        status = raw.get("status") or {}
        return NormalizedOrder(
            source=self.name,
            source_order_id=str(raw.get("id")),
            order_number=str(order.get("id", raw.get("id"))),
            created_at=_parse_date(raw.get("created_at")),
            total_price=_to_decimal(raw.get("total_items_price")),
            status=str(status.get("title", "unknown")),
            items=[],  # list endpoint gives summary items only - fetch_order_detail has full items
            customer_full_name=None,
            customer_mobile=None,
        )

    def _normalize_detail(self, raw: dict) -> NormalizedOrder:
        order = raw.get("order", {})
        status = raw.get("status") or {}
        customer = order.get("customer") or {}

        items = [
            OrderItem(
                # Product SKU field name is not confirmed in the documented
                # schema snippet - falling back through the identifiers we
                # do know about (product id) rather than guessing a name.
                sku=str((item.get("product") or {}).get("id", item.get("id", ""))),
                title=str(item.get("title", "")),
                quantity=int(item.get("quantity", 1)),
                unit_price=_to_decimal(item.get("price")),
                final_price=_to_decimal(item.get("price", 0)) * int(item.get("quantity", 1)),
            )
            for item in raw.get("items", [])
        ]

        return NormalizedOrder(
            source=self.name,
            source_order_id=str(raw.get("id")),
            order_number=str(order.get("id", raw.get("id"))),
            created_at=_parse_date(raw.get("created_at")),
            total_price=_to_decimal(raw.get("total_items_price")),
            status=str(status.get("title", "unknown")),
            items=items,
            # Confirmed via live testing: customer name/mobile came back
            # empty on real orders (order.customer exists in the schema
            # but wasn't populated in practice). Falls back to a
            # synthetic CustomerCode (basalam-{parcel_id}) via
            # src/didar/service.py, same as Tapsi Shop and Digikala.
            customer_full_name=customer.get("name") or customer.get("title"),
            customer_mobile=customer.get("mobile") or customer.get("phone_number"),
        )


def _parse_date(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)