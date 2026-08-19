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
     adapter): POST /auth/refresh-token using only the refresh_token -
     no private key, no manual step, no authorization_code involved.
     This is what keeps the service running for the ~1 year the
     refresh_token stays valid.

The private key used in step 1 should never be placed on the server -
this service only ever needs the refresh_token for step 2. Roughly
once a year (when refresh_token approaches its own expiry), step 1
needs to be repeated manually and the new tokens re-seeded into .env
(or directly into data/digikala_tokens.json).

Since this service runs continuously, it cannot rely on a static
access_token from .env - it will expire within about a day. Instead,
this adapter refreshes reactively (on a 401) via
POST /open-api/v1/auth/refresh-token, and persists the new
access_token/refresh_token pair to a local JSON file
(data/digikala_tokens.json) so a service restart doesn't need a fresh
manual authorization - only the *initial* tokens in .env are ever used
as a one-time seed.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx

from src.config import DigikalaConfig, settings
from src.http_utils import default_retry, raise_for_status_with_body
from src.logger import get_logger
from src.marketplaces.base import MarketplaceAdapter, NormalizedOrder, OrderItem

log = get_logger(__name__)


def _to_decimal(value) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        return Decimal("0")


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
        resp = self._client.post(
            "/open-api/v1/auth/refresh-token", json={"refresh_token": self._refresh_token}
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

    def fetch_new_orders(self, since: datetime) -> list[NormalizedOrder]:
        rows = self._fetch_history_rows(
            order_created_at_from=since,
            order_created_at_to=datetime.now(timezone.utc),
        )
        orders = self._group_rows_into_orders(rows)
        log.info("digikala: fetched %d new orders since %s", len(orders), since.isoformat())
        return orders

    def fetch_order_detail(self, source_order_id: str) -> NormalizedOrder:
        # The history endpoint has no direct order_id filter, but search_text_all
        # matches serial / order_shipment_id / product identifiers - order_id
        # search is supported by the vendor's own UI via this same param.
        rows = self._fetch_history_rows(search_text_all=source_order_id)
        rows = [r for r in rows if str(r.get("order_id")) == str(source_order_id)]
        orders = self._group_rows_into_orders(rows)
        if not orders:
            raise ValueError(f"digikala: order {source_order_id} not found in history")
        return orders[0]

    def _fetch_history_rows(
        self,
        order_created_at_from: datetime | None = None,
        order_created_at_to: datetime | None = None,
        search_text_all: str | None = None,
    ) -> list[dict]:
        rows: list[dict] = []
        page = 1
        size = 50

        while True:
            params = {"page": page, "size": size, "sort": "id", "order": "asc"}
            if order_created_at_from:
                params["order_created_at_from"] = _fmt(order_created_at_from)
            if order_created_at_to:
                params["order_created_at_to"] = _fmt(order_created_at_to)
            if search_text_all:
                params["search_text_all"] = search_text_all

            payload = self._get("/open-api/v1/orders/history", params=params)
            data = payload.get("data", {})
            items = data.get("items", [])
            rows.extend(items)

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

    def _group_rows_into_orders(self, rows: list[dict]) -> list[NormalizedOrder]:
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
                    unit_price=_to_decimal(r.get("unit_price")),
                    final_price=_to_decimal(r.get("total_price")),
                )
                for r in item_rows
            ]
            status = first.get("order_status", {})
            orders.append(
                NormalizedOrder(
                    source=self.name,
                    source_order_id=order_id,
                    order_number=order_id,
                    created_at=_parse_date(first.get("order_created_at")),
                    total_price=sum((i.final_price for i in items), Decimal("0")),
                    status=str(status.get("title") or status.get("key") or "unknown"),
                    items=items,
                    customer_full_name=None,  # not requested for this project - see module docstring
                    customer_mobile=None,
                )
            )
        return orders


def _fmt(dt: datetime) -> str:
    # Digikala's documented format: Y-m-d\TH:i:s.v\Z
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _parse_date(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)