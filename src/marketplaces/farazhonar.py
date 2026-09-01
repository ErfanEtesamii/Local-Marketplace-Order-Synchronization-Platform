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
  GET /wp-json/wc/v3/products/{id}       -> used to resolve each line item's
                                            product category AND image in a
                                            SINGLE request (WooCommerce's
                                            order line_items[] includes
                                            neither) - see
                                            _resolve_product_meta(). Only a
                                            successful lookup is cached, for
                                            the adapter's lifetime, since
                                            product categories/images rarely
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

from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation

import httpx

from src.config import FarazHonarConfig, settings
from src.currency import to_rial
from src.finglish import persianize_name
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
        # Category + image url are cached TOGETHER, keyed by product_id,
        # because both come from the exact same /products/{id} response -
        # see _resolve_product_meta(). Only a CONFIRMED (successful) lookup
        # is cached; a failed lookup is never stored here, so a transient
        # error doesn't permanently poison this product_id for the rest of
        # this adapter instance's lifetime (it lives for the whole service
        # uptime - see main.py's build_engine(), which builds each adapter
        # exactly once, not per poll cycle).
        self._product_meta_cache: dict[int, tuple[str | None, str | None]] = {}

    @default_retry()
    def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        resp = self._client.get(path, params=params or {})
        raise_for_status_with_body(resp)
        return resp

    def fetch_new_orders(self, since: datetime | None) -> list[NormalizedOrder]:
        orders: list[NormalizedOrder] = []
        page = 1
        total_pages = 1

        while page <= total_pages:
            resp = self._get(
                "/wp-json/wc/v3/orders",
                params={
                    "after": (since or datetime.now(timezone.utc) - timedelta(hours=5)).astimezone(timezone.utc).isoformat(),
                    # Without this, WooCommerce compares "after" against
                    # the site-LOCAL `date_created` column, not the UTC
                    # `date_created_gmt` one - despite the value above
                    # already being converted to UTC. If this store's
                    # WordPress timezone isn't UTC (common for Iranian
                    # sites, e.g. Asia/Tehran = +03:30), the fetch window
                    # silently shifts by that offset. This flag tells
                    # WooCommerce to compare against date_created_gmt
                    # instead, matching what _parse_date() already does
                    # on the way out (see module docstring).
                    "dates_are_gmt": "true",
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

        log.info("farazhonar: fetched %d orders", len(orders))
        return orders

    def fetch_order_detail(self, source_order_id: str) -> NormalizedOrder:
        resp = self._get(f"/wp-json/wc/v3/orders/{source_order_id}")
        return self._normalize(resp.json())

    def _resolve_product_meta(self, product_id: int) -> tuple[str | None, str | None]:
        """(category, image_url) for a WooCommerce product in ONE request.

        BUGFIX: this used to be two separate methods (_resolve_category /
        _resolve_image_url), each hitting GET /products/{id} on its own -
        doubling the number of requests to the store for every unique
        product on every order, even though a single response already
        contains both `categories` and `images`.

        BUGFIX: a failed lookup is no longer cached. Previously, `None`
        was written to the cache regardless of whether it meant "this
        product genuinely has no category/image" or "the request failed"
        - and since this adapter instance lives for the entire service
        uptime (see main.py), one transient error (a dropped connection,
        a brief 5xx) permanently blanked that product_id's category/image
        for every later order until the service was restarted. A failed
        lookup now returns (None, None) WITHOUT being cached, so the next
        order referencing the same product tries again.

        A failed lookup must still not break order sync, so errors here
        are swallowed (logged) rather than raised - the item falls back
        to Didar's default catch-all category / no product image."""
        if not product_id:
            return None, None
        if product_id in self._product_meta_cache:
            return self._product_meta_cache[product_id]

        try:
            resp = self._get(f"/wp-json/wc/v3/products/{product_id}")
        except httpx.HTTPError as exc:
            log.warning(
                "farazhonar: failed to resolve category/image for product_id=%s: %s",
                product_id, exc,
            )
            return None, None

        data = resp.json()
        categories = data.get("categories", [])
        category = (str(categories[0].get("name") or "") or None) if categories else None
        images = data.get("images", [])
        image_url = (images[0].get("src") or None) if images else None

        self._product_meta_cache[product_id] = (category, image_url)
        return category, image_url

    def _normalize(self, raw: dict) -> NormalizedOrder:
        billing = raw.get("billing", {})
        full_name = " ".join(
            part for part in [billing.get("first_name"), billing.get("last_name")] if part
        ).strip() or None
        # Customers can fill the WooCommerce billing form with an
        # English keyboard layout (e.g. "mohammad ahmadi" instead of
        # "محمد احمدی") - convert that back to Persian before it reaches
        # Didar. See src/finglish.py for how/why this is approximate.
        full_name = persianize_name(full_name)

        items = []
        for item in raw.get("line_items", []):
            category, image_url = self._resolve_product_meta(item.get("product_id"))
            items.append(
                OrderItem(
                    sku=str(item.get("sku") or item.get("product_id", "")),
                    title=str(item.get("name", "")),
                    quantity=int(item.get("quantity", 1)),
                    unit_price=to_rial(_to_decimal(item.get("price")), self._config.price_unit),
                    final_price=to_rial(_to_decimal(item.get("total")), self._config.price_unit),
                    category=category,
                    product_image_url=image_url,
                )
            )

        return NormalizedOrder(
            source=self.name,
            source_order_id=str(raw.get("id")),
            order_number=str(raw.get("number", raw.get("id"))),
            created_at=_parse_date(raw.get("date_created_gmt")),
            total_price=to_rial(_to_decimal(raw.get("total")), self._config.price_unit),
            status=str(raw.get("status", "unknown")),
            items=items,
            customer_full_name=full_name,
            customer_mobile=billing.get("phone") or None,
            # NormalizedOrder.product_image_url (order-level) is now just a
            # last-resort fallback - each OrderItem's OWN image from
            # _resolve_image_url (set above) is what actually gets
            # attached to the "ارسال محصول" Activity, one photo per line
            # item, so a multi-item order gets every product's photo, not
            # just the first (client feedback, 2026-09).
            product_image_url=items[0].product_image_url if items else None,
            # "shipping_total" is a standard, officially-documented
            # WooCommerce REST API v3 order field (order-level shipping
            # cost) - see the module docstring for why this API is high-
            # confidence. Same TOMAN/RIAL unit as every other price on
            # this source (src/currency.py).
            shipping_cost=to_rial(_to_decimal(raw.get("shipping_total")), self._config.price_unit),
            # shipment_id intentionally left None (the dataclass default):
            # core WooCommerce REST API v3 has no tracking/parcel-number
            # field - that's only ever added by a shipment-tracking
            # plugin as custom order meta, which hasn't been confirmed to
            # exist on this site (see module docstring).
        )


def _parse_date(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        # WooCommerce's _gmt fields are UTC but come without a "Z"/offset suffix.
        return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc)