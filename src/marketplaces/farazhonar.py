"""
Faraz Honar adapter - farazhonar.com, a self-hosted WordPress/WooCommerce
site the client administers directly (not a third-party marketplace).

Unlike the other four adapters, this one is NOT based on discovery,
browser inspection, or partial docs - WooCommerce's REST API v3
(/wp-json/wc/v3/) is a public, stable, officially documented API that
has been essentially unchanged for years. Confidence here is high.

Endpoints used:
  GET /wp-json/wc/v3/orders             -> paginated order list, and
                                            CRUCIALLY already includes full
                                            line_items[] per order - unlike
                                            every marketplace adapter in this
                                            project, no separate detail call
                                            is needed just to get items.
  GET /wp-json/wc/v3/orders/{id}         -> single order (used to satisfy
                                            the MarketplaceAdapter interface,
                                            though fetch_new_orders already
                                            has everything it needs)
  GET /wp-json/wc/v3/products/{id}       -> used ONLY to resolve each line
                                            item's product category (WooCommerce
                                            categories[] isn't included on the
                                            order's line_items themselves) - see
                                            _resolve_category(). Results are
                                            cached for the adapter's lifetime
                                            since product categories rarely
                                            change and this avoids one extra
                                            request per repeated product.

Authentication: HTTP Basic Auth using a Consumer Key / Consumer Secret
pair generated from wp-admin (WooCommerce > Settings > Advanced > REST
API, Read permission). Safe over HTTPS, which this site uses.

Pagination: standard WooCommerce `page` + `per_page` query params
(max per_page=100), with `X-WP-TotalPages` response header indicating
how many pages exist - more reliable than trusting a body field.

Dates: WooCommerce returns both `date_created` (site-local time) and
`date_created_gmt` (UTC, no ambiguity) - this adapter uses the _gmt
variant throughout to avoid timezone bugs.

One thing to confirm once real credentials are available: whether this
site actually has WooCommerce installed and active (the URL structure -
/product-category/ - strongly suggests it, but hasn't been confirmed
by inspecting wp-admin directly).
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import httpx

from src.config import FarazHonarConfig, settings
from src.http_utils import default_retry, raise_for_status_with_body
from src.logger import get_logger
from src.marketplaces.base import MarketplaceAdapter, NormalizedOrder, OrderItem

log = get_logger(__name__)


def _to_decimal(value) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        return Decimal("0")


class FarazHonarAdapter(MarketplaceAdapter):
    name = "farazhonar"

    def __init__(self, config: FarazHonarConfig | None = None) -> None:
        self._config = config or settings.farazhonar
        self._client = httpx.Client(
            base_url=self._config.base_url,
            auth=httpx.BasicAuth(self._config.consumer_key, self._config.consumer_secret),
            timeout=30.0,
        )
        self._category_cache: dict[int, str | None] = {}

    @default_retry()
    def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        resp = self._client.get(path, params=params or {})
        raise_for_status_with_body(resp)
        return resp

    def fetch_new_orders(self, since: datetime) -> list[NormalizedOrder]:
        orders: list[NormalizedOrder] = []
        page = 1
        total_pages = 1

        while page <= total_pages:
            resp = self._get(
                "/wp-json/wc/v3/orders",
                params={
                    "after": since.astimezone(timezone.utc).isoformat(),
                    "per_page": 100,
                    "page": page,
                    "orderby": "date",
                    "order": "asc",
                    "status": "any",
                },
            )
            raw_orders = resp.json()
            orders.extend(self._normalize(o) for o in raw_orders)

            total_pages = int(resp.headers.get("X-WP-TotalPages", 1))
            page += 1

        log.info("farazhonar: fetched %d new orders since %s", len(orders), since.isoformat())
        return orders

    def fetch_order_detail(self, source_order_id: str) -> NormalizedOrder:
        resp = self._get(f"/wp-json/wc/v3/orders/{source_order_id}")
        return self._normalize(resp.json())

    def _resolve_category(self, product_id: int) -> str | None:
        """First WooCommerce product category title, or None if the
        product has none / product_id is missing / the lookup fails.
        A failed lookup must not break order sync, so errors here are
        swallowed (logged) rather than raised - the item just falls
        back to Didar's default catch-all category."""
        if not product_id:
            return None
        if product_id in self._category_cache:
            return self._category_cache[product_id]

        category = None
        try:
            resp = self._get(f"/wp-json/wc/v3/products/{product_id}")
            categories = resp.json().get("categories", [])
            if categories:
                category = str(categories[0].get("name") or "") or None
        except httpx.HTTPError as exc:
            log.warning(
                "farazhonar: failed to resolve category for product_id=%s: %s",
                product_id, exc,
            )

        self._category_cache[product_id] = category
        return category

    def _normalize(self, raw: dict) -> NormalizedOrder:
        billing = raw.get("billing", {})
        full_name = " ".join(
            part for part in [billing.get("first_name"), billing.get("last_name")] if part
        ).strip() or None

        items = [
            OrderItem(
                sku=str(item.get("sku") or item.get("product_id", "")),
                title=str(item.get("name", "")),
                quantity=int(item.get("quantity", 1)),
                unit_price=_to_decimal(item.get("price")),
                final_price=_to_decimal(item.get("total")),
                category=self._resolve_category(item.get("product_id")),
            )
            for item in raw.get("line_items", [])
        ]

        return NormalizedOrder(
            source=self.name,
            source_order_id=str(raw.get("id")),
            order_number=str(raw.get("number", raw.get("id"))),
            created_at=_parse_date(raw.get("date_created_gmt")),
            total_price=_to_decimal(raw.get("total")),
            status=str(raw.get("status", "unknown")),
            items=items,
            customer_full_name=full_name,
            customer_mobile=billing.get("phone") or None,
        )


def _parse_date(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        # WooCommerce's _gmt fields are UTC but come without a "Z"/offset suffix.
        return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc)