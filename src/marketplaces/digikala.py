"""
Digikala adapter.

Based on the official Open API (seller.digikala.com/open-api/v1/doc):
  - GET /open-api/v1/orders/history  -> paginated order *item* rows, with
    precise date-range filtering (order_created_at_from / _to)

IMPORTANT: this endpoint returns one row per order line item, not one row
per order - a 3-item order produces 3 rows sharing the same order_id. This
adapter groups rows by order_id before producing NormalizedOrder objects.

Per project decision, customer contact details are not required for this
source and are not requested - customer_full_name / customer_mobile are
always None here (see src/didar/contact_client.py for the synthetic
CustomerCode strategy this implies).

DATE FILTER DOESN'T ACTUALLY FILTER (confirmed live, production log
2026-08-27): order_created_at_from/_to are sent on every request exactly
as the docs describe, but the API returns the account's ENTIRE order
history regardless - a real poll returned orders back to 2024 despite
`since` being "yesterday". SyncEngine._drop_orders_older_than_since()
already guards against this at the application level (see
sync_engine.py's module docstring), so no old order actually reaches
Didar - but without the optimization below, every single poll cycle
would walk the account's ENTIRE order history page by page just to
throw almost all of it away client-side, getting slower forever as the
account accumulates more orders. Fetching newest-first (order=desc,
changed from the original asc) plus an early pagination stop the
moment a page's oldest row predates `since` fixes the wasted work
without weakening the actual safety guarantee, which still lives in
SyncEngine, not here.

TOKEN LIFECYCLE (confirmed via a real /auth/token exchange):
  - access_token: short-lived, ~24 hours
  - refresh_token: long-lived, ~1 year - matches the ~360-day validity
    shown in the seller panel's own "توکن اختصاصی" screen, which
    reflects the client/refresh-token grant, not the short-lived
    access_token used on every request

There are two SEPARATE processes here, easy to conflate:

  1. Getting the INITIAL access_token/refresh_token pair (manual,
     one-time, or roughly once a year when refresh_token expires):
     the seller panel issues an RSA-encrypted authorization_code,
     which is decrypted locally with a private key (openssl pkeyutl),
     then exchanged via POST /auth/token. This requires the private
     key and is NOT something this service does - it's a manual step
     whose *result* (the two tokens) gets seeded into .env once.

  2. Renewing the access_token day-to-day (automatic, done by THIS
     adapter): POST /auth/refresh-token - CONFIRMED via the official
     docs (Authentication section) that despite the name, this call
     requires BOTH the (expired) access_token AND the refresh_token in
     the request body, not refresh_token alone:
         {"access_token": "<expired token>", "refresh_token": "<...>"}
     Omitting access_token gets a 400 with
     errors.access_token = ["این قسمت نباید خالی باشد"] - this was a
     real bug in an earlier version of this adapter (see git history)
     that broke every scheduled refresh. No private key or
     authorization_code is involved in this step, only in step 1 above.

The private key used in step 1 should never be placed on the server -
this service only ever needs the refresh_token for step 2. Roughly
once a year (when refresh_token approaches its own expiry), step 1
needs to be repeated manually and the new tokens re-seeded into .env
(or directly into data/digikala_tokens.json).
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx

from src.config import DigikalaConfig, settings
from src.currency import to_rial
from src.http_utils import default_retry, raise_for_status_with_body
from src.logger import get_logger
from src.marketplaces.base import MarketplaceAdapter, NormalizedOrder, OrderItem

log = get_logger(__name__)


def _to_decimal(value) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        return Decimal("0")


def _first_item_photo_url(raw_items: list[dict]) -> str | None:
    """
    Best-effort product photo for the order - see the UNCONFIRMED note at
    its call site in _normalize_detail. Tries the first item's nested
    product.photo (confirmed shape: {"original", "xs", "sm", "md", "lg"}),
    preferring "original" then falling back to the largest thumbnail.
    """
    for item in raw_items:
        photo = ((item.get("product") or {}).get("photo")) or {}
        url = photo.get("original") or photo.get("lg") or photo.get("md")
        if url:
            return str(url)
    return None


class DigikalaAdapter(MarketplaceAdapter):
    name = "digikala"

    def __init__(self, config: DigikalaConfig | None = None) -> None:
        self._config = config or settings.digikala
        self._token_cache_path = Path(settings.db_path).resolve().parent / "digikala_tokens.json"
        self._access_token, self._refresh_token = self._load_tokens()
        self._client = httpx.Client(
            base_url=self._config.base_url,
            headers={"content-type": "application/json"},
            timeout=30.0,
        )

    def _load_tokens(self) -> tuple[str, str]:
        """Prefer a previously-refreshed pair over the static .env seed,
        since refresh_token rotates and .env is not rewritten at runtime."""
        if self._token_cache_path.exists():
            try:
                cached = json.loads(self._token_cache_path.read_text())
                return cached["access_token"], cached["refresh_token"]
            except (json.JSONDecodeError, KeyError, OSError):
                log.warning(
                    "digikala: failed to read cached tokens at %s, falling back to .env",
                    self._token_cache_path,
                )
        return self._config.access_token, self._config.refresh_token

    def _save_tokens(self) -> None:
        self._token_cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._token_cache_path.write_text(
            json.dumps({"access_token": self._access_token, "refresh_token": self._refresh_token})
        )

    @default_retry()
    def _refresh_access_token(self) -> None:
        # Confirmed via official docs: both access_token and refresh_token
        # are required in the body, even though this call's purpose is to
        # replace the (expired) access_token - see module docstring.
        resp = self._client.post(
            "/open-api/v1/auth/refresh-token",
            json={"access_token": self._access_token, "refresh_token": self._refresh_token},
        )
        raise_for_status_with_body(resp)
        data = resp.json().get("data", {})
        self._access_token = data["access_token"]
        # Digikala may or may not rotate the refresh_token on each use -
        # keep the old one only if a new one wasn't actually returned.
        self._refresh_token = data.get("refresh_token", self._refresh_token)
        self._save_tokens()
        log.info(
            "digikala: access token refreshed, new expiry=%s",
            data.get("access_token_expires_at", {}).get("date"),
        )

    @default_retry()
    def _get(self, path: str, params: dict, _already_refreshed: bool = False) -> dict:
        resp = self._client.get(
            path, params=params, headers={"Authorization": f"Bearer {self._access_token}"}
        )
        if resp.status_code == 401 and not _already_refreshed:
            log.info("digikala: access token expired (401), refreshing")
            self._refresh_access_token()
            return self._get(path, params, _already_refreshed=True)
        raise_for_status_with_body(resp)
        return resp.json()

    def fetch_new_orders(self, since: datetime | None) -> list[NormalizedOrder]:
        # The sync_engine now passes `since = now - 5h` and also drops
        # orders outside the window client-side. We keep passing None for
        # order_created_at_from so the adapter can use full-history mode
        # (its API does not filter server-side) while still letting
        # SyncEngine._sync_source enforce the window below. We DO pass
        # order_type so that cancelled/failed orders can be mapped to
        # status strings and filtered by the central filter in
        # sync_engine.py.
        now = datetime.now(timezone.utc)
        rows = self._fetch_history_rows(
            order_created_at_from=None,  # fetch all orders (API returns all; client-side window drops old)
            order_created_at_to=now,
            order_type=None,  # sync_engine handles order_type filtering via status mapping
        )
        orders = self._group_rows_into_orders(rows, order_type=None)
        log.info("digikala: fetched %d orders for sync window", len(orders))
        return orders

    def fetch_order_detail(self, source_order_id: str) -> NormalizedOrder:
        # The history endpoint has no direct order_id filter, and search_text_all
        # does NOT search by order_id (it matches serial / order_shipment_id /
        # product identifiers). Instead, fetch full history (newest-first) and
        # let _group_rows_into_orders group by order_id, then pick the one we want.
        rows = self._fetch_history_rows(order_type=None)
        orders = self._group_rows_into_orders(rows, order_type=None)
        for order in orders:
            if order.source_order_id == source_order_id:
                return order
        raise ValueError(f"digikala: order {source_order_id} not found in history")

    def fetch_sbs_customer_details(self, shipment_id: str) -> dict:
        """Fetch customer details for a Digikala Ship-by-Seller (SBS) order.

        Calls GET /open-api/v1/ship-by-seller-orders/customer/{shipment_id}
        with Bearer token and extracts customer data (name, phoneNumber,
        state, city, address, postalCode).

        Returns a dict with keys:
            customer_full_name: str | None
            customer_mobile: str | None

        On any error (transport, auth, or malformed response), returns a dict
        with both values as None so the caller can fall back to a synthetic
        contact name without breaking the sync flow.
        """
        path = f"/open-api/v1/ship-by-seller-orders/customer/{shipment_id}"
        try:
            payload = self._get(path, params={})
            data = payload.get("data", {}) or {}
            full_name = data.get("name") or None
            mobile = data.get("phoneNumber") or None
            log.info(
                "digikala: fetched SBS customer details for shipment %s (name=%r, mobile=%r)",
                shipment_id, full_name, mobile,
            )
            return {"customer_full_name": full_name, "customer_mobile": mobile}
        except Exception:
            log.exception(
                "digikala: failed to fetch SBS customer details for shipment %s",
                shipment_id,
            )
            return {"customer_full_name": None, "customer_mobile": None}

    def _fetch_history_rows(
        self,
        order_created_at_from: datetime | None = None,
        order_created_at_to: datetime | None = None,
        order_type: str | None = None,
    ) -> list[dict]:
        rows: list[dict] = []
        page = 1
        size = 50

        while True:
            # order=desc (newest first) - changed from the original asc.
            # See module docstring: order_created_at_from doesn't actually
            # filter server-side, so with asc (oldest first) every poll
            # cycle would walk the account's entire history from the very
            # beginning. desc + the early-stop below fixes that.
            params = {"page": page, "size": size, "sort": "id", "order": "desc"}
            if order_created_at_from:
                params["order_created_at_from"] = self._fmt(order_created_at_from)
            if order_created_at_to:
                params["order_created_at_to"] = self._fmt(order_created_at_to)
            if order_type:
                params["order_type"] = order_type

            payload = self._get("/open-api/v1/orders/history", params=params)
            data = payload.get("data", {})
            items = data.get("items", [])
            rows.extend(items)

            # Early stop (only meaningful in date-filtered mode, i.e. from
            # fetch_new_orders - fetch_order_detail's lookup passes no
            # order_created_at_from and so never takes this branch,
            # matching its existing "fetch full history" behavior
            # unchanged). Rows arrive newest-first, so once a page's
            # OLDEST row already predates what we asked for, every row
            # on every subsequent page is guaranteed even older - this
            # is purely a wasted-work optimization, not a correctness
            # guarantee: SyncEngine._drop_orders_older_than_since()
            # is still the actual safety net regardless of what happens
            # here (see its module docstring).
            # NOTE: search_text_all parameter was removed in a previous fix
            # as it does NOT search by order_id (it matches serial / order_shipment_id /
            # product identifiers).
            if order_created_at_from is not None and items:
                oldest_on_page = _parse_date(items[-1].get("order_created_at"))
                if oldest_on_page < order_created_at_from:
                    break

            pager = data.get("pager", {})
            total_pages = pager.get("total_pages", 0)

            # Don't rely on total_pages alone - if the API ever reports it
            # incorrectly (seen as 0 even with items present in the docs'
            # own example response), a page full of items is itself a sign
            # more may follow. Stopping only requires BOTH signals to agree
            # there's nothing left, so a bad total_pages value can't cause
            # silently-dropped orders.
            got_full_page = len(items) == size
            more_by_pager = page < total_pages
            if not items or not (got_full_page or more_by_pager):
                break
            page += 1

        return rows

    def _group_rows_into_orders(self, rows: list[dict], order_type: str | None = None) -> list[NormalizedOrder]:
        grouped: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            grouped[str(row.get("order_id"))].append(row)

        orders: list[NormalizedOrder] = []
        for order_id, item_rows in grouped.items():
            first = item_rows[0]
            items = [
                OrderItem(
                    sku=str(r.get("product_supplier_code", r.get("product_id", ""))),
                    title=str(r.get("product_variant_title", "")),
                    quantity=int(r.get("quantity", 1)),
                    unit_price=to_rial(_to_decimal(r.get("unit_price")), self._config.price_unit),
                    final_price=to_rial(_to_decimal(r.get("total_price")), self._config.price_unit),
                    # Extract product image URL from the first item's product data
                    # The Digikala API response structure may vary, but we assume
                    # a "product" object with "photo" field containing image URLs
                    product_image_url=self._extract_product_image_url(r),
                )
                for r in item_rows
            ]
            # Map order_type to status for Digikala (since order_status may not reflect cancelled/failed)
            status = first.get("order_status", {})
            if order_type == "canceled":
                status_val = "canceled"
            elif order_type == "returned":
                status_val = "refunded"
            else:
                status_val = str(status.get("title") or status.get("key") or "unknown")
            # NormalizedOrder.product_image_url (order-level, used by
            # DidarSyncService._fetch_product_image to attach a photo to the
            # "ارسال محصول" Activity) was never set here before - only each
            # OrderItem got a product_image_url, which nothing downstream
            # reads for the attachment. Reuse the first item's image as the
            # order-level photo.
            product_image_url = items[0].product_image_url if items else None
            # Extract shipment_id from the first row (all rows in group should have same shipment_id).
            # NOTE: the real /orders/history response (confirmed against docs/api digikala.docx)
            # returns this as a top-level "shipment_id" field. "order_shipment_id" only ever
            # appears as one of the searchable fields for the search_text_all query param, not
            # as a response field - using it here made shipment_id always None, which silently
            # disabled SBS customer enrichment for every order.
            shipment_id = str(first.get("shipment_id")) if first.get("shipment_id") else None
            orders.append(
                NormalizedOrder(
                    source=self.name,
                    source_order_id=order_id,
                    order_number=order_id,
                    created_at=_parse_date(first.get("order_created_at")),
                    total_price=sum((i.final_price for i in items), Decimal("0")),
                    status=status_val,
                    items=items,
                    customer_full_name=None,  # not requested for this project - see module docstring
                    customer_mobile=None,
                    shipment_id=shipment_id,
                    product_image_url=product_image_url,
                )
            )
        return orders

    def _extract_product_image_url(self, row: dict) -> str | None:
        """
        Extract product image URL from a row.

        CORRECTED: the real /orders/history response (confirmed against
        docs/api digikala.docx) returns the image directly as a top-level
        "image_src" string field - there is no nested "product.photo.*"
        object on this endpoint (that shape belongs to a different,
        unused endpoint: GET /open-api/v1/orders). The old lookup here
        always returned None, so no order ever had a photo to attach.
        """
        url = row.get("image_src")
        return str(url) if url else None

    def _fmt(self, dt: datetime) -> str:
        # Digikala's documented format: Y-m-d\TH:i:s.v\Z
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _parse_date(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)