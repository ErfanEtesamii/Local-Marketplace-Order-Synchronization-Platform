"""
Tapsi Shop adapter.

Based on the vendor's "TapsiShop_v_0_2" API document:
  - POST /Web/Hub/vendors/v1/orders       -> paginated order list
  - GET  /Web/Hub/vendors/v1/orders/{id}  -> full order detail (incl. items)

IMPORTANT DISCOVERY (recorded here, not just in chat, so it survives):
Neither the list nor the detail REST endpoint returns customer name or
mobile number - those fields only appear in the push Webhook payload,
which this project intentionally does not use (see architecture.md for
why polling was chosen over webhooks). So despite the original proposal
assuming `CustomerCode = customer mobile number` for this source, that
is not available via polling. Until/unless we revisit the webhook
decision, Tapsi Shop orders use the same synthetic-CustomerCode strategy
as Digikala (see src/didar/contact_client.py).
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception

from src.config import TapsiShopConfig, settings
from src.logger import get_logger
from src.marketplaces.base import MarketplaceAdapter, NormalizedOrder, OrderItem

log = get_logger(__name__)


def _retryable_status(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        # Retry on server errors and rate limiting only - not on 4xx auth/validation errors.
        return exc.response.status_code >= 500 or exc.response.status_code == 429
    return isinstance(exc, httpx.TransportError)


def _to_decimal(value) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        return Decimal("0")


class TapsiShopAdapter(MarketplaceAdapter):
    name = "tapsishop"

    def __init__(self, config: TapsiShopConfig | None = None) -> None:
        self._config = config or settings.tapsishop
        self._client = httpx.Client(
            base_url=self._config.base_url,
            headers={
                "accept": "text/plain",
                "Content-Type": "application/json",
                "TapsiShop.Hub.Authorization": self._config.auth_token,
            },
            timeout=30.0,
        )

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception(_retryable_status),
    )
    def _post(self, path: str, json: dict) -> dict:
        resp = self._client.post(path, json=json)
        resp.raise_for_status()
        return resp.json()

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception(_retryable_status),
    )
    def _get(self, path: str) -> dict:
        resp = self._client.get(path)
        resp.raise_for_status()
        return resp.json()

    def fetch_new_orders(self, since: datetime) -> list[NormalizedOrder]:
        orders: list[NormalizedOrder] = []
        page = 0
        page_size = 50

        while True:
            body = {
                "pageNumber": page,
                "pageSize": page_size,
                "fromDate": since.astimezone(timezone.utc).isoformat(),
                "toDate": datetime.now(timezone.utc).isoformat(),
            }
            payload = self._post("/Web/Hub/vendors/v1/orders", json=body)
            data = payload.get("data", {})
            total_items = data.get("totalItems", 0)
            items = data.get("items", [])

            for raw in items:
                orders.append(self._normalize_list_item(raw))

            # Same defense as the Digikala adapter: don't trust totalItems
            # alone. A full page is itself a signal there may be more,
            # regardless of what totalItems claims.
            got_full_page = len(items) == page_size
            more_by_total = (page + 1) * page_size < total_items
            if not items or not (got_full_page or more_by_total):
                break
            page += 1

        log.info("tapsishop: fetched %d new orders since %s", len(orders), since.isoformat())
        return orders

    def fetch_order_detail(self, source_order_id: str) -> NormalizedOrder:
        payload = self._get(f"/Web/Hub/vendors/v1/orders/{source_order_id}")
        data = payload.get("data", {})
        order = data.get("order", {})
        raw_items = data.get("items", [])

        items = [
            OrderItem(
                sku=str(i.get("sku", "")),
                title=str(i.get("name", "")),
                # The detail response does not expose a per-item quantity field;
                # each entry represents one unit. Revisit if the vendor adds one.
                quantity=1,
                unit_price=_to_decimal(i.get("price")),
                final_price=_to_decimal(i.get("finalPrice")),
            )
            for i in raw_items
        ]

        return NormalizedOrder(
            source=self.name,
            source_order_id=str(source_order_id),
            order_number=str(order.get("orderNumber", source_order_id)),
            created_at=_parse_date(order.get("orderDate")),
            total_price=_to_decimal(order.get("amountAfterDiscount") or order.get("originalAmount")),
            status=str(order.get("status", "unknown")),
            items=items,
            customer_full_name=None,  # not available via REST polling - see module docstring
            customer_mobile=None,
        )

    def _normalize_list_item(self, raw: dict) -> NormalizedOrder:
        return NormalizedOrder(
            source=self.name,
            source_order_id=str(raw.get("id")),
            order_number=str(raw.get("orderNumber", raw.get("id"))),
            created_at=_parse_date(raw.get("createdOn")),
            total_price=_to_decimal(raw.get("finalPrice")),
            status=str(raw.get("stateTitle", raw.get("stateCode", "unknown"))),
            items=[],  # list endpoint doesn't include line items - fetch_order_detail does
            customer_full_name=None,
            customer_mobile=None,
        )


def _parse_date(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
