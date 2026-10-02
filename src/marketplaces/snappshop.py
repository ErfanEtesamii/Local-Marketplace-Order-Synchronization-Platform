"""
SnappShop adapter - vendor webservice.

STATUS (2026-09): SnappShop published its own official vendor API doc
("پیوست فنی یکپارچه سازی فروشندگان اسنپ شاپ", v2.1.2, PDF), superseding
the two vendor-onboarding blog posts this adapter was originally (and
only partially) built from. `_normalize_detail()`, `_normalize_list_item()`,
and the `orders?start_date=/cursor=` pagination in `fetch_new_orders()`
were rewritten against the PDF's confirmed field names (both order
endpoints share one normalization path - see `_normalize_order()`), and
`tests/test_snappshop.py` was rewritten to assert against the PDF's own
worked JSON samples in place of the old guessed shape
(`status`/`total_price`/`unit_price` at the top level) SnappShop never
actually sent. That rewrite was then independently confirmed live - see
the "CONFIRMED 2026-09 against a real order" notes below - which is why
`_SCHEMA_CONFIRMED` (bottom of this docstring) is now `True`.

CONFIRMED (from the official v2.1.2 PDF):
  - Base URL: `https://apix.snappshop.ir/automation/v1` - stated
    explicitly in the doc (`baseUrl = ...`), no longer an inference
    from Snapp's bug-bounty domain scope. See .env.example.
  - Auth: header `Authorization: Bearer {token}` + header
    `Agent-User: {vendor_identifier}` (a unique caller-chosen id, used
    to identify the requester - not a per-endpoint value) on every
    request. Invalid/expired token -> 401 with
    `{"message", "code", "trackId", "status": false, "errors": []}`.
  - GET  /vendors                                    -> list accessible vendors
  - GET  /vendors/{vendor_id}                         -> single vendor detail
  - GET  /vendors/{vendor_id}/products                -> vendor's product
           catalog, page-number pagination (`?page=N`, 20/page) - NOT
           cursor-based, unlike the orders endpoints below.
  - PATCH /vendors/{vendor_id}/products                -> bulk update
           price/stock/capacity/discount for up to 50 products per
           call, keyed by `id` or `sku`. Not currently used by this
           adapter (order sync only, one direction) - documented here
           in case price/stock push-back to SnappShop is ever needed.
  - GET  /vendors/{vendor_id}/orders/events           -> cursor-paginated
           lifecycle events (`NEW_ORDER` / `CANCELLATION` /
           `CHANGE_STATUS`), 50/page, `meta.pagination.{has_more,
           next_cursor,links.next}`. Full item-level field names are
           confirmed (see doc section 2-3-1) - a candidate for a
           lower-latency/more precise change-detection mechanism than
           polling the history endpoint below, not currently used by
           this adapter.
  - GET  /vendors/{vendor_id}/orders/{order_number}   -> full order
           detail: `order_number`, `created_at`, `delivery_type`,
           `order_status`, `pickup_time.{start,end}`,
           `customer.{first_name,last_name,phone,national_id,address}`,
           `items[].{sku,product_number,parent_product_number,
           item_status,quantity,canceled_quantity,discount_amount,
           final_price}`. `customer.address` is only populated when
           the vendor (not SnappShop) handles delivery; `phone`/
           `national_id` are only populated when this vendor account
           has buyer-info visibility enabled - otherwise both are
           `null` (not omitted).
  - GET  /vendors/{vendor_id}/orders?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD
        -> paginated order history, 20/page, cursor-based
           (`meta.pagination.{has_more,next_cursor,links.next}`).
           First call with no filters returns the last **14** days
           (not 10, as the old blog-post-derived docstring said).
           Same per-order/per-item shape as the detail endpoint above,
           plus `vendor_product_info_id` and `original_price` per item.
           To page forward, send only `cursor` (not the date filters
           again) - or just follow `meta.pagination.links.next`
           verbatim. To fetch "since date X, up to now", send only
           `start_date` (no `end_date`). `fetch_new_orders()` now
           follows this exactly: the first request sends `start_date`
           only when `since` is given (nothing at all when it isn't,
           so the API's own 14-day default applies), and every
           follow-up request sends `cursor` alone, driven by
           `has_more`/`next_cursor`.
  - Confirmed: **no order-level total-price field** exists on either
    orders endpoint - only per-item `final_price`/`discount_amount`/
    `original_price`. An order total must be summed from items.
  - CONFIRMED 2026-09 against a real order (client pulled
    `GET /vendors/{vendor_id}/orders/{order_number}` and cross-checked
    against the vendor panel): the order-DETAIL endpoint's items DO
    carry `original_price` directly, same as the history endpoint -
    the PDF's worked example for this endpoint just didn't happen to
    show it. `_normalize_items` already prefers `original_price` when
    present and only falls back to `final_price + discount_amount`
    when it's absent, so no code change was needed here, but the
    module docstring above (which claimed detail-endpoint items have
    "no such field") was wrong and is corrected by this note.
  - CONFIRMED 2026-09 (same real order): item currency unit is
    **Toman**, not Rial - `final_price` (3350000) matched the vendor
    panel's "قیمت کل (تومان)" total (3,350,000) exactly. See
    src/currency.py's module docstring and .env.example -
    `SNAPPSHOP_PRICE_UNIT` now defaults to `toman`.
  - Also observed on that same real order (not yet documented anywhere
    else, and not yet consumed by this adapter): items can carry an
    `inventory_product_id` field alongside `vendor_product_info_id`,
    and `point_of_sales_at` can be populated (not always `null`) once
    a pickup has actually happened.
  - Confirmed: **no item title/name field** exists on either orders
    endpoint - only `product_number`/`parent_product_number`/`sku`/
    `vendor_product_info_id`. A human-readable item title would need a
    separate lookup via `GET /vendors/{vendor_id}/products/{id}`
    (`product_number` -> that endpoint's `id`/`product_number`) - not
    yet implemented; see the follow-up note where item titles are
    normalized.

_SCHEMA_CONFIRMED = True as of 2026-09: `_normalize_order()` /
`_normalize_items()` are rewritten against the PDF's field names above,
`tests/test_snappshop.py` locks in both the PDF's worked samples and
the real order's numbers (Toman unit, `original_price` reconciliation),
and the client independently cross-checked a real order against the
vendor panel (see the "CONFIRMED 2026-09" notes above) - the same bar
Basalam's adapter cleared before its own schema was confirmed. The
one-time startup warning below no longer fires as a result.

SnappShop stays `SNAPPSHOP_ENABLED=false` in `.env.example` regardless
- that flag tracks whether the client has been granted API access, a
credentials gap unrelated to schema confidence. Flip it once real
credentials exist; no further code change is needed to trust the
output at that point.
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

_SCHEMA_CONFIRMED = True  # confirmed 2026-09 - see module docstring; flip back to False if a future SnappShop API change is suspected

_EMPTY_IDS = {"", "0", "none", "null"}
_TITLE_KEYS = ("title", "name", "product_title", "product_name", "title_fa", "name_fa")
_ID_KEYS = ("id", "product_number", "parent_product_number", "vendor_product_info_id", "inventory_product_id", "sku")
_TITLE_CACHE_TTL_S = 600
_MAX_PRODUCT_PAGES = 100


def _clean_id(value) -> str:
    """SnappShop sends a placeholder sku of "0" on real orders (seen 2026-09-29/30:
    every item collapsed onto one Didar product "snappshop item 0"). Treat it as absent."""
    text = "" if value is None else str(value).strip()
    return "" if text.lower() in _EMPTY_IDS else text


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
            # Dormant as of 2026-09 (_SCHEMA_CONFIRMED is True - see module
            # docstring) - left in place so a future revert of the flag
            # (e.g. a suspected SnappShop API change) immediately restores
            # this warning without writing it again.
            log.warning(
                "snappshop: order field schema was rewritten against the official "
                "v2.1.2 vendor-API PDF but has not yet been verified against a real "
                "populated response - see src/marketplaces/snappshop.py module "
                "docstring before trusting normalized output in production."
            )

        vendor_id = self._config.vendor_id
        orders: list[NormalizedOrder] = []
        cursor: str | None = None

        while True:
            # Confirmed (doc section 2-3-3): continuation requests send
            # `cursor` alone - the date filters are a first-request-only
            # concept and must NOT be repeated once paging. When there's
            # no cursor yet, only send `start_date` if the caller gave us
            # a `since` - sending no params at all lets the API apply its
            # own documented default (last 14 days), rather than us
            # re-deriving an equivalent date ourselves.
            if cursor:
                params: dict = {"cursor": cursor}
            elif since is not None:
                params = {"start_date": since.date().isoformat()}
            else:
                params = {}

            payload = self._get(f"/vendors/{vendor_id}/orders", params=params)
            raw_orders = payload.get("data", [])
            orders.extend(self._normalize_list_item(o) for o in raw_orders)

            pagination = payload.get("meta", {}).get("pagination", {})
            cursor = pagination.get("next_cursor")
            # Confirmed (doc section 2-3-3): `has_more`/`next_cursor` alone
            # govern continuation - an empty page is not itself a stop
            # signal (the API could legitimately return zero rows for a
            # given cursor while more still follow). `not cursor` is kept
            # as a defensive-only guard against a malformed response that
            # claims `has_more: true` with no `next_cursor` to follow,
            # which would otherwise loop forever re-sending `cursor=None`.
            if not pagination.get("has_more") or not cursor:
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
        """
        Normalizes one entry from `GET /vendors/{vendor_id}/orders`
        (order history, cursor-paginated). Confirmed (doc section
        2-3-3): this endpoint returns the exact same per-order/per-item
        shape as the order-detail endpoint (`_normalize_detail`) - the
        only addition is `vendor_product_info_id` and `original_price`
        per item, both already handled by `_normalize_items`. So this
        is no longer a thin/partial normalization (the old version left
        `items=[]` and both customer fields `None`, deferring to a
        separate `fetch_order_detail` call) - it shares the full
        `_normalize_order` path with the detail endpoint.
        """
        return self._normalize_order(raw)

    def _normalize_detail(self, raw: dict) -> NormalizedOrder:
        """Normalizes `GET /vendors/{vendor_id}/orders/{order_number}`."""
        return self._normalize_order(raw)

    def _normalize_order(self, raw: dict) -> NormalizedOrder:
        """
        Shared normalization for the order-detail and order-history
        endpoints, which return an identical per-order shape (see the
        two callers' docstrings above and the module docstring).
        """
        items = self._normalize_items(raw.get("items", []))

        customer = raw.get("customer", {}) or {}
        full_name = (
            " ".join(part for part in (customer.get("first_name"), customer.get("last_name")) if part)
            or None
        )
        # Confirmed: `address` is an empty array `[]` whenever SnappShop
        # itself handles delivery - only vendor-fulfilled orders carry a
        # real value, per the doc's note under sections 2-3-2/2-3-3. The
        # doc doesn't show a worked example of the populated case
        # (string vs. a structured object), so only a non-empty string
        # is trusted - anything else (including the documented
        # empty-array case) normalizes to "no address" rather than
        # guessing a shape.
        address = customer.get("address")
        customer_address = address if isinstance(address, str) and address else None

        # `pickup_time.start` is the courier pickup window's start, the
        # closest SnappShop equivalent to the ship_time anchor other
        # adapters provide (see NormalizedOrder.ship_time's docstring in
        # base.py) - used to schedule the post-sale follow-up checklist.
        pickup_time = raw.get("pickup_time") or {}
        pickup_start = pickup_time.get("start")

        return NormalizedOrder(
            source=self.name,
            source_order_id=str(raw.get("order_number", "")),
            order_number=str(raw.get("order_number", "")),
            created_at=_parse_date(raw.get("created_at")),
            # Confirmed: no order-level total-price field exists at all
            # on either endpoint (see module docstring) - summed from
            # the (already net of cancellation) per-item final_price
            # values instead.
            total_price=sum((item.final_price for item in items), start=Decimal("0")),
            status=str(raw.get("order_status", "unknown")),
            items=items,
            # `phone`/`national_id` are `null` (not omitted) unless this
            # vendor account has buyer-info visibility enabled - `.get()`
            # already degrades that to None correctly. `national_id` has
            # no home in NormalizedOrder yet, so it's read but not
            # currently forwarded anywhere - not a data-loss concern
            # since Didar's Contact model doesn't have a matching field
            # either.
            customer_full_name=persianize_name(full_name),
            customer_mobile=customer.get("phone"),
            customer_address=customer_address,
            ship_time=_parse_date(pickup_start) if pickup_start else None,
            # Confirmed on both callers of _normalize_order (order-detail
            # and order-history - see their docstrings above): `delivery_type`
            # is documented only for these two endpoints, NOT for
            # GET /orders/events (unused by this adapter - see
            # fetch_new_orders, which polls /orders, not /orders/events).
            # Nothing is guessed when it's absent - same None-means-
            # "don't touch it" convention as customer_address above.
            shipping_method=raw.get("delivery_type") or None,
        )

    def _load_product_titles(self) -> dict[str, str]:
        """Vendor catalog (GET /vendors/{id}/products?page=N) -> {any known id: title}.
        Best effort: field names for the title are NOT confirmed in this repo, so several
        candidates are tried. Any failure returns what was loaded so far (never raises)."""
        import time
        now = time.monotonic()
        cache = getattr(self, "_title_cache", None)
        if cache is not None and now - self._title_cache_at < _TITLE_CACHE_TTL_S:
            return cache
        titles: dict[str, str] = {}
        seen_first: str | None = None
        try:
            for page in range(1, _MAX_PRODUCT_PAGES + 1):
                payload = self._get(f"/vendors/{self._config.vendor_id}/products", params={"page": page})
                rows = payload.get("data", []) if isinstance(payload, dict) else []
                if not rows:
                    break
                marker = str(rows[0].get("id") or rows[0].get("product_number"))
                if marker == seen_first:
                    break  # API ignored the page param - avoid looping forever
                seen_first = seen_first or marker
                for row in rows:
                    title = next((str(row[k]).strip() for k in _TITLE_KEYS if row.get(k)), "")
                    if not title:
                        continue
                    for k in _ID_KEYS:
                        key = _clean_id(row.get(k))
                        if key:
                            titles.setdefault(key, title)
                if len(rows) < 20:
                    break
        except Exception:
            log.warning("snappshop: could not load product titles for item naming", exc_info=True)
        if not titles:
            log.warning("snappshop: product catalog gave no usable titles - items will fall back to id-based names")
        self._title_cache, self._title_cache_at = titles, now
        return titles

    def _lookup_title(self, item: dict) -> str:
        titles = self._load_product_titles()
        for k in ("product_number", "vendor_product_info_id", "inventory_product_id", "parent_product_number", "sku"):
            key = _clean_id(item.get(k))
            if key and key in titles:
                return titles[key]
        log.warning("snappshop: no title found for item keys=%s product_number=%s", sorted(item), item.get("product_number"))
        return ""

    def _normalize_items(self, raw_items: list[dict]) -> list[OrderItem]:
        """
        Shared item-normalization for the confirmed `items[]` shape used
        by both the order-detail and order-history endpoints (both go
        through `_normalize_order` now, see its docstring).
        """
        items: list[OrderItem] = []
        for item in raw_items:
            item_status = str(item.get("item_status", "")).upper()
            if item_status == "CANCELED":
                # Confirmed: item_status is only ever CANCELED when the
                # entire line was canceled (partial cancellation still
                # reports CONFIRMED - see module docstring). Nothing is
                # left to deliver or bill for, so this line is dropped
                # rather than synced as a zero-value sold item -
                # deliberate, not a silent data-loss bug.
                log.debug(
                    "snappshop: dropping fully-canceled item (product_number=%s)",
                    item.get("product_number"),
                )
                continue

            quantity = int(item.get("quantity") or 1)
            if quantity <= 0:
                quantity = 1

            final_price = to_rial(_to_decimal(item.get("final_price")), self._config.price_unit)

            # Confirmed: the order-HISTORY endpoint's items carry an
            # explicit `original_price` (pre-discount line total) per
            # the doc's section 2-3-3 sample - use it directly when
            # present, it's authoritative. The order-DETAIL endpoint
            # has no such field (see module docstring) - only
            # `discount_amount` - so it falls back to a derivation:
            # pre-discount line total = final_price + discount_amount.
            # UNCONFIRMED either way: the doc's own sample numbers don't
            # reconcile cleanly (e.g. discount_amount exceeding
            # original_price in the section 2-3-3 example), which may
            # just be placeholder data, but flag this for review once a
            # real populated order is inspected.
            raw_original_price = item.get("original_price")
            if raw_original_price is not None:
                original_total = to_rial(_to_decimal(raw_original_price), self._config.price_unit)
            else:
                discount_amount = to_rial(_to_decimal(item.get("discount_amount")), self._config.price_unit)
                original_total = final_price + discount_amount
            unit_price = original_total / quantity if quantity else final_price

            items.append(
                OrderItem(
                    sku=_clean_id(item.get("sku")) or _clean_id(item.get("vendor_product_info_id")) or _clean_id(item.get("product_number")),
                    # Confirmed: neither orders endpoint returns an item
                    # title/name field at all - only product identifiers
                    # (see module docstring). A real title would need a
                    # separate GET /vendors/{vendor_id}/products/{id}
                    # lookup keyed on product_number - not yet
                    # implemented, tracked as a follow-up.
                    title=self._lookup_title(item),
                    quantity=quantity,
                    unit_price=unit_price,
                    final_price=final_price,
                )
            )
        return items


# SnappShop sends created_at as a NAIVE wall-clock string (e.g. "2026-10-01 00:52:50")
# in Iran local time (UTC+3:30, no DST since 2022). Confirmed from production logs:
# order 551790613 was synced at 00:54 Iran time with created_at "00:52:50".
# Treating it as UTC made Telegram (which converts UTC -> Iran) show it 3.5h late.
_SNAPP_NAIVE_TZ = timezone(timedelta(hours=3, minutes=30))


def _parse_date(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        # BUGFIX (2026-09-21): SnappShop doesn't always send an offset/"Z"
        # suffix on created_at - when it doesn't, fromisoformat() returns
        # an offset-naive datetime, which crashes sync_engine.py's window
        # comparison ("can't compare offset-naive and offset-aware
        # datetimes") every single poll for as long as that order stays
        # in the fetch window, silently killing the rest of the cycle for
        # this source each time (see farazhonar.py's _parse_date for the
        # same fix applied there). Assume UTC, same as SnappShop's
        # documented/observed offset-aware timestamps.
        return dt if dt.tzinfo else dt.replace(tzinfo=_SNAPP_NAIVE_TZ).astimezone(timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc)