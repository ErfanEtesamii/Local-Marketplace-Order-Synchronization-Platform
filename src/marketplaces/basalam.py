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

IMPORTANT: a parcel's own `created_at` can predate the underlying
order's `paid_at` by a long margin (confirmed: over a day) - see
_PARCEL_CREATION_LOOKBACK_HOURS below for the full story and the fix.

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

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import httpx
from src.http_utils import default_retry, raise_for_status_with_body

from src.config import BasalamConfig, settings
from src.currency import to_rial
from src.finglish import persianize_name
from src.logger import get_logger
from src.marketplaces.base import MarketplaceAdapter, NormalizedOrder, OrderItem

log = get_logger(__name__)

# NEW_ORDER status code, per the documented `statuses` enum - available for
# callers that want to scope polling to freshly placed orders only.
STATUS_NEW_ORDER = 3739

# BUGFIX (2026-09, order 80027578): a Basalam parcel's own `created_at`
# can be set LONG before the underlying order is actually paid - e.g.
# this order's parcel had created_at=2026-09-04T21:17 while
# order.paid_at was 2026-09-06T04:52, ~31.5 hours later. sync_engine.py
# uses NormalizedOrder.created_at both (a) to build the `created_at[gte]`
# query param sent to /v3/vendor-parcels, and (b) for its own
# client-side window-drop check - so as long as this field carried the
# stale parcel date, an order like this was outside the 5-hour window
# (FETCH_WINDOW_HOURS in sync_engine.py) from the very first poll and
# could NEVER be seen, permanently. This wasn't a token/scope/Didar
# issue - the parcel was simply never inside the window basalam.py or
# sync_engine.py ever asked for.
#
# Fix, in two parts (see _order_created_at and fetch_new_orders below):
#   1. NormalizedOrder.created_at now reflects order.paid_at (falling
#      back to the parcel's created_at only if paid_at is missing), so
#      sync_engine's window-drop judges freshness by when the customer
#      actually paid, not by whatever moment Basalam happened to create
#      the parcel record.
#   2. The server-side `created_at[gte]` query param - the ONLY time
#      filter /v3/vendor-parcels accepts (paid_at[gte] is documented
#      only on the different /v1/customer-orders endpoint, which needs
#      a buyer-side `customer.order.read` scope this project doesn't
#      have - see basalam_api_full.md) - now always looks back at LEAST
#      _PARCEL_CREATION_LOOKBACK_HOURS, regardless of the `since` the
#      engine passed in. Otherwise part 1 alone would fix the window
#      check but the parcel would still never be RETURNED by the API in
#      the first place.
# This mirrors the spirit of Digikala's uses_id_based_watermark fix
# (fetch a wider net server-side, then let a more reliable freshness
# signal + the synced_orders ID dedup in sync_engine.py sort out what's
# actually new) without needing a full ID-watermark migration - unlike
# Digikala's shipmentId, Basalam's parcel id ordering relative to
# payment time isn't confirmed, so an ID-based cursor isn't a safe
# substitute for a date filter here.
_PARCEL_CREATION_LOOKBACK_HOURS = 72


def _to_decimal(value) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        return Decimal("0")



def _item_photo_url(item: dict) -> str | None:
    """
    Best-effort product photo for ONE order item. CONFIRMED against
    docs/document.json (Basalam's own OpenAPI spec, ProductSummaryResponse
    -> FileResponse): each item's nested product carries a "photos" ARRAY
    (not a singular "photo" object), and each entry there is {"id",
    "original", "format", "resized": {size_key: url}} (not
    "xs"/"sm"/"md"/"lg" keys directly on the photo). Prefers "original",
    falling back to any one "resized" variant.
    """
    photos = ((item.get("product") or {}).get("photos")) or []
    for photo in photos:
        url = photo.get("original")
        if not url:
            resized = photo.get("resized") or {}
            url = next(iter(resized.values()), None)
        if url:
            return str(url)
    return None


def _order_created_at(raw: dict) -> datetime:
    """The date NormalizedOrder.created_at should carry for a parcel -
    see _PARCEL_CREATION_LOOKBACK_HOURS above for why this must be
    order.paid_at rather than the parcel's own created_at whenever
    paid_at is present. Falls back to the parcel's created_at only for
    the rare/defensive case of a missing paid_at (a parcel that
    genuinely doesn't have one yet is more likely a data anomaly than a
    real customer order sync_engine should treat as fresh)."""
    order = raw.get("order") or {}
    paid_at = order.get("paid_at")
    if paid_at:
        return _parse_date(paid_at)
    return _parse_date(raw.get("created_at"))


def _first_item_photo_url(raw_items: list[dict]) -> str | None:
    """Best-effort product photo for the ORDER as a whole (order-level
    NormalizedOrder.product_image_url) - kept only as a last-resort
    fallback now that every OrderItem carries its own product_image_url
    (see _item_photo_url above and its use in _normalize_detail);
    src.didar.service._fetch_product_images() prefers the per-item URLs
    and only falls back to this order-level one when an order's items
    have none of their own."""
    for item in raw_items:
        url = _item_photo_url(item)
        if url:
            return url
    return None


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

    def fetch_new_orders(self, since: datetime | None) -> list[NormalizedOrder]:
        orders: list[NormalizedOrder] = []
        cursor = None

        # See _PARCEL_CREATION_LOOKBACK_HOURS's docstring: the parcel's own
        # created_at (the only thing this endpoint can filter by) can lag
        # WAY behind order.paid_at, so the server-side query must look back
        # further than the `since` sync_engine asked for, or a
        # payment-delayed parcel is never returned at all. Freshness is
        # judged downstream by order.paid_at (_order_created_at) plus the
        # synced_orders ID dedup in sync_engine.py, not by this query alone.
        requested_since = (since or datetime.now(timezone.utc) - timedelta(hours=5)).astimezone(timezone.utc)
        lookback_floor = datetime.now(timezone.utc) - timedelta(hours=_PARCEL_CREATION_LOOKBACK_HOURS)
        server_since = min(requested_since, lookback_floor)

        while True:
            params = {
                "created_at[gte]": server_since.isoformat(),
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

        log.info("basalam: fetched %d orders", len(orders))
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
            created_at=_order_created_at(raw),
            total_price=to_rial(_to_decimal(raw.get("total_items_price")), self._config.price_unit),
            status=str(status.get("title", "unknown")),
            items=[],  # list endpoint gives summary items only - fetch_order_detail has full items
            customer_full_name=None,
            customer_mobile=None,
            # CONFIRMED field name - estimate_send_at is the only accepted
            # sort key above (live 422s ruled out created_at/id), and it's
            # exactly the "parcel_estimate_send_at" / "estimate_send_at"
            # filter documented for orders/parcels respectively. This is
            # the ship-time anchor for src/didar/scheduling.py.
            ship_time=_parse_date_or_none(raw.get("estimate_send_at")),
        )

    def _normalize_detail(self, raw: dict) -> NormalizedOrder:
        order = raw.get("order", {})
        status = raw.get("status") or {}
        customer = order.get("customer") or {}
        raw_items = raw.get("items", [])

        items = [
            OrderItem(
                # Product SKU field name is not confirmed in the documented
                # schema snippet - falling back through the identifiers we
                # do know about (product id) rather than guessing a name.
                sku=str((item.get("product") or {}).get("id", item.get("id", ""))),
                title=str(item.get("title", "")),
                quantity=int(item.get("quantity", 1)),
                unit_price=to_rial(_to_decimal(item.get("price")), self._config.price_unit),
                final_price=to_rial(
                    _to_decimal(item.get("price", 0)) * int(item.get("quantity", 1)),
                    self._config.price_unit,
                ),
                # BUGFIX (client feedback, 2026-09): every item now gets
                # ITS OWN photo, not just the order's first item - see
                # _item_photo_url above and src.didar.service.
                # _fetch_product_images, which attaches one photo per
                # line item to the "ارسال محصول" Activity. Previously
                # only NormalizedOrder.product_image_url (order-level,
                # from _first_item_photo_url) was ever set here, so a
                # multi-item Basalam order silently lost every photo but
                # the first.
                product_image_url=_item_photo_url(item),
            )
            for item in raw_items
        ]

        # BUGFIX (2026-09, confirmed against Basalam's own OpenAPI Gateway
        # doc - developers.basalam.com/docs/api/gateway, Order section):
        # name/mobile/postal_code/postal_address are NOT direct children of
        # "customer" - they live one level deeper, under "customer.recipient".
        # The previous customer.get("name")/customer.get("mobile") always
        # read a key that doesn't exist in the real response, which is why
        # every Basalam order silently fell back to a synthetic contact
        # name - it was never actually an API/data-availability limitation.
        recipient = customer.get("recipient") or {}
        city = customer.get("city") or {}

        return NormalizedOrder(
            source=self.name,
            source_order_id=str(raw.get("id")),
            order_number=str(order.get("id", raw.get("id"))),
            created_at=_order_created_at(raw),
            total_price=to_rial(_to_decimal(raw.get("total_items_price")), self._config.price_unit),
            status=str(status.get("title", "unknown")),
            items=items,
            # A customer typing their name with an English keyboard
            # (e.g. "mohammad ahmadi") is converted back to Persian
            # script here - see src/finglish.py for how/why this is
            # approximate.
            customer_full_name=persianize_name(recipient.get("name") or None),
            customer_mobile=recipient.get("mobile") or None,
            # Confirmed on the same recipient object as name/mobile above -
            # full delivery address text and postal code, same
            # None-means-"don't touch it" convention as every other source
            # (see NormalizedOrder.customer_address's docstring).
            customer_address=recipient.get("postal_address") or None,
            customer_postal_code=recipient.get("postal_code") or None,
            # Only the city name is confirmed here - customer.city.parent
            # (which would presumably be the province) is documented only
            # as an untyped/empty object in the gateway spec, with no
            # confirmed field name for the province's own title. Leaving
            # customer_province unset rather than guessing a key, same
            # caution as Faraz Honar's billing.state note above.
            customer_city=city.get("title") or None,
            # Same confirmed field as _normalize_list_item - see its comment.
            ship_time=_parse_date_or_none(raw.get("estimate_send_at")),
            # CONFIRMED via docs/document.json (Basalam's own OpenAPI spec):
            # each vendor-parcel item embeds a nested "product" object with
            # a "photos" array - see _first_item_photo_url's docstring for
            # the exact shape. No separate GET on the product id is needed.
            product_image_url=_first_item_photo_url(raw_items),
            # CONFIRMED via docs/document.json: ParcelResponse.shipping_cost
            # is a required top-level integer field on this same detail
            # response - no separate call needed.
            shipping_cost=to_rial(_to_decimal(raw.get("shipping_cost")), self._config.price_unit),
            # CONFIRMED via docs/document.json: the tracking/parcel number
            # lives at post_receipt.tracking_code (PostReceiptResponse).
            # post_receipt itself is OPTIONAL on ParcelResponse - it's only
            # populated once the seller has actually filed a post receipt
            # for the parcel, so None here for a brand-new order is
            # expected, not a bug.
            shipment_id=str((raw.get("post_receipt") or {}).get("tracking_code") or "") or None,
        )


def _parse_date(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        # Handle both formats: with "T" (ISO 8601) and without "T" (common API format)
        iso_value = str(value).replace("Z", "+00:00")
        if " " in iso_value and "T" not in iso_value:
            # Insert "T" between date and time parts
            date_part, time_part = iso_value.split(" ", 1)
            iso_value = f"{date_part}T{time_part}"
        parsed = datetime.fromisoformat(iso_value)
        # Ensure timezone awareness
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return datetime.now(timezone.utc)


def _parse_date_or_none(value: str | None) -> datetime | None:
    # Unlike _parse_date (used for created_at, where "now" is a reasonable
    # fallback for a value that should always be present), a missing or
    # unparseable estimate_send_at must stay None rather than fabricate a
    # ship_time - see NormalizedOrder.ship_time and
    # DidarActivityClient.create_post_sale_checklist, which deliberately
    # skip the whole checklist rather than schedule it off a made-up date.
    if not value:
        return None
    try:
        # Handle both formats: with "T" (ISO 8601) and without "T" (common API format)
        iso_value = str(value).replace("Z", "+00:00")
        if " " in iso_value and "T" not in iso_value:
            # Insert "T" between date and time parts
            date_part, time_part = iso_value.split(" ", 1)
            iso_value = f"{date_part}T{time_part}"
        parsed = datetime.fromisoformat(iso_value)
        # Ensure timezone awareness
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None