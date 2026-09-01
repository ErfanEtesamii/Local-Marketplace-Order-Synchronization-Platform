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

TWO MORE CONSTRAINTS CONFIRMED VIA LIVE TESTING (undocumented in the PDF):
  1. dateFilterTypeCode is required whenever fromDate/toDate are sent -
     the PDF's example value of 0 was just a placeholder, not a valid
     value. 1 is confirmed to work (meaning unconfirmed, but presumably
     "filter by order creation date").
  2. The [fromDate, toDate] window is capped at 7 days - a longer span
     is rejected outright with a 400. fetch_new_orders therefore chunks
     any longer requested range into <=7-day windows and fans out one
     request per window, aggregating the results.
  3. Rate limit: the vendor gateway allows only one call per 5 seconds
     (confirmed via a live 429). _throttle() enforces a minimum gap
     between every request this adapter makes, proactively rather than
     relying on retry-after-a-429, since the chunking above means a
     large backfill can trigger dozens of sequential calls.

ORDER STATUS FILTER (client requirement, 2026-08): only orders whose
status is 4 (تایید سفارش - confirmed/still active) are fetched.
Excludes 6 (لغو سفارش - cancelled) and 9 (تحویل کامل - fully
delivered), per the confirmed order-status enum in the vendor docs.
Already-delivered orders were previously entered into Didar manually
and must not be re-created by this sync; only orders still awaiting
fulfillment are the sales team's concern here.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import httpx

from src.config import TapsiShopConfig, settings
from src.currency import to_rial
from src.http_utils import default_retry, raise_for_status_with_body
from src.logger import get_logger
from src.marketplaces.base import MarketplaceAdapter, NormalizedOrder, OrderItem

log = get_logger(__name__)

_MAX_WINDOW = timedelta(days=7) - timedelta(minutes=1)  # small safety margin under the confirmed 7-day cap
_MIN_REQUEST_INTERVAL_SECONDS = 5.5  # confirmed limit is 5s flat - small margin added
_ACTIVE_ORDER_STATUS_IDS = [4]  # تایید سفارش only - excludes cancelled(6)/delivered(9), see module docstring


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
        self._last_request_at: float | None = None

    def _throttle(self) -> None:
        now = time.monotonic()
        if self._last_request_at is not None:
            elapsed = now - self._last_request_at
            remaining = _MIN_REQUEST_INTERVAL_SECONDS - elapsed
            if remaining > 0:
                time.sleep(remaining)
        self._last_request_at = time.monotonic()

    @default_retry()
    def _post(self, path: str, json: dict) -> dict:
        self._throttle()
        resp = self._client.post(path, json=json)
        raise_for_status_with_body(resp)
        return resp.json()

    @default_retry()
    def _get(self, path: str) -> dict:
        self._throttle()
        resp = self._client.get(path)
        raise_for_status_with_body(resp)
        return resp.json()

    def fetch_new_orders(self, since: datetime | None) -> list[NormalizedOrder]:
        orders: list[NormalizedOrder] = []
        now = datetime.now(timezone.utc)
        # When since is None, default to the FETCH_WINDOW_HOURS lookback
        # so that orders are always fetched from a recent window.
        # The sync engine relies on ID-based dedup to prevent duplicates
        # across repeated runs.
        window_start = (since or now - timedelta(hours=5)).astimezone(timezone.utc)

        while window_start < now:
            window_end = min(window_start + _MAX_WINDOW, now)
            orders.extend(self._fetch_orders_in_window(window_start, window_end))
            window_start = window_end

        log.info("tapsishop: fetched %d orders", len(orders))
        return orders

    def _fetch_orders_in_window(
        self, window_start: datetime, window_end: datetime
    ) -> list[NormalizedOrder]:
        orders: list[NormalizedOrder] = []
        page = 0
        page_size = 50

        while True:
            body = {
                "pageNumber": page,
                "pageSize": page_size,
                "dateFilterTypeCode": 1,
                "fromDate": window_start.isoformat(),
                "toDate": window_end.isoformat(),
                "orderStatusId": _ACTIVE_ORDER_STATUS_IDS,
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
                unit_price=to_rial(_to_decimal(i.get("price")), self._config.price_unit),
                final_price=to_rial(_to_decimal(i.get("finalPrice")), self._config.price_unit),
                # CONFIRMED field per docs/TapsiShop.v.0.2.pdf (order-detail
                # response, items[].picture) - was never read before, so no
                # Tapsi Shop order ever had a photo to attach.
                product_image_url=str(i["picture"]) if i.get("picture") else None,
            )
            for i in raw_items
        ]

        return NormalizedOrder(
            source=self.name,
            source_order_id=str(source_order_id),
            order_number=str(order.get("orderNumber", source_order_id)),
            created_at=_parse_date(order.get("orderDate")),
            total_price=to_rial(
                _to_decimal(order.get("amountAfterDiscount") or order.get("originalAmount")),
                self._config.price_unit,
            ),
            status=str(order.get("status", "unknown")),
            items=items,
            customer_full_name=None,  # not available via REST polling - see module docstring
            customer_mobile=None,
            # NormalizedOrder.product_image_url (order-level) is now just a
            # last-resort fallback - each OrderItem's OWN image (set
            # above) is what DidarSyncService actually attaches to the
            # "ارسال محصول" Activity, one photo per line item, so a
            # multi-item order gets every product's photo, not just the
            # first (client feedback, 2026-09).
            product_image_url=items[0].product_image_url if items else None,
        )

    def _normalize_list_item(self, raw: dict) -> NormalizedOrder:
        return NormalizedOrder(
            source=self.name,
            source_order_id=str(raw.get("id")),
            order_number=str(raw.get("orderNumber", raw.get("id"))),
            created_at=_parse_date(raw.get("createdOn")),
            total_price=to_rial(_to_decimal(raw.get("finalPrice")), self._config.price_unit),
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