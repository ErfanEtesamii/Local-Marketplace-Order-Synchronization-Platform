"""
Didar CRM - Deal client.

Rewritten per the project's structured-data decision: pricing and
product data go into their own proper Didar fields, not a text blob.
Specifically:

  - Amount            -> DealItems[].UnitPrice (not text)
  - Customer name      -> already handled via Contact/PersonId
  - Product name        -> DealItems[].ProductId, linked to a real
                           catalog Product (auto-created if no match -
                           see product_client.py)
  - Order source (site) -> Deal.LabelIds (a Deal Label - see
                           _label_id_for_source() below; NOT a Tag,
                           corrected 2026-09 after direct confirmation
                           from Didar's own support agent) AND, per a
                           later client request, also written as
                           readable text in Description alongside a
                           link back to the order on its origin
                           platform - see below
  - Title                -> "معامله {display_name}", matching Didar's
                           own default naming convention for manually
                           created deals - NOT "{order_number} - {source}"
                           as originally implemented

Endpoint: POST {DIDAR_BASE_URL}/deal/save?apikey={API_KEY}
Confirmed via live testing: Deal.save expects PersonId (not ContactId).

DESCRIPTION - source label + order link (client request, 2026-08):
Only Faraz Honar (the client's own WooCommerce site) gets a confirmed,
real per-order deep link (standard wp-admin edit-order URL - this
pattern is well established, not a guess). The four marketplaces
(Tapsi Shop, Digikala, Basalam, SnappShop) do NOT have a confirmed
per-order URL pattern for their vendor panels - inventing one risks a
broken/misleading link, so Description instead links to that vendor's
panel *home page* (confirmed URLs, from the original project proposal)
plus the order number as text, to be searched manually. Revisit with a
real per-order URL once one is confirmed from any of those panels.

NOT YET CONFIRMED (pending a live test of the DealItems rewrite):
  - Whether DealItems is a top-level sibling key alongside "Deal" in
    the request body (assumed here) or nested inside the Deal object.
  - The exact DealItems field names beyond ProductId/Quantity/UnitPrice/
    Discount, which are confirmed from the API docs.
"""
from __future__ import annotations

from decimal import Decimal

import httpx

from src.config import DidarConfig, settings
from src.didar.category_mapping import _normalize_fa
from src.didar.contact_client import DidarApiError
from src.didar.product_client import DidarProductClient
from src.http_utils import default_retry, raise_for_status_with_body
from src.logger import get_logger
from src.marketplaces.base import NormalizedOrder

log = get_logger(__name__)

_SOURCE_DISPLAY_NAMES = {
    "tapsishop": "تپسی‌شاپ",
    "digikala": "دیجی‌کالا",
    "basalam": "باسلام",
    "snappshop": "اسنپ‌شاپ",
    "farazhonar": "فرازهنر",
}

# Vendor panel home page URLs (confirmed - these are the same links
# listed in the original project proposal). NOT per-order deep links -
# see module docstring.
_PANEL_URLS = {
    "tapsishop": "https://vendor.tapsi.shop/dashboard",
    "digikala": "https://seller.digikala.com/pwa",
    "basalam": "https://vendor.basalam.com",
    "snappshop": "https://seller.snappshop.ir/dashboard",
}


def _order_link(order: NormalizedOrder) -> str:
    if order.source == "farazhonar":
        # Confirmed real WooCommerce admin URL pattern - opens this
        # exact order directly.
        return (
            f"{settings.farazhonar.base_url}/wp-admin/post.php"
            f"?post={order.source_order_id}&action=edit"
        )
    return _PANEL_URLS.get(order.source, "")


def _order_reference(order: NormalizedOrder) -> str:
    """
    Stable, globally-unique string identifying one order, independent of
    order_number (which is only "human-readable", per NormalizedOrder's
    docstring, and isn't guaranteed unique on its own the way
    source+source_order_id is). This exact string is what
    find_existing_deal_id() searches for and matches against - see that
    method's docstring for why an exact anchor is needed instead of a
    raw keyword hit.
    """
    return f"{order.source}:{order.source_order_id}"


def _build_description(order: NormalizedOrder) -> str:
    source_label = _SOURCE_DISPLAY_NAMES.get(order.source, order.source)
    link = _order_link(order)
    lines = [
        f"فروشگاه: {source_label}",
        f"شماره سفارش: {order.order_number}",
        f"شناسه یکتای هماهنگ‌سازی: {_order_reference(order)}",
    ]
    if link:
        lines.append(f"مشاهده سفارش: {link}")
    return "\n".join(lines)


class DidarDealClient:
    def __init__(
        self,
        config: DidarConfig | None = None,
        product_client: DidarProductClient | None = None,
    ) -> None:
        self._config = config or settings.didar
        self._products = product_client or DidarProductClient(config=self._config)
        self._client = httpx.Client(base_url=self._config.base_url, timeout=30.0)
        self._label_id_by_title: dict[str, str] | None = None  # populated lazily

    @default_retry()
    def _post(self, path: str, json: dict) -> dict:
        resp = self._client.post(path, params={"apikey": self._config.api_key}, json=json)
        raise_for_status_with_body(resp)
        return resp.json()

    @default_retry()
    def _get(self, path: str) -> dict:
        resp = self._client.get(path, params={"apikey": self._config.api_key})
        raise_for_status_with_body(resp)
        return resp.json()

    def _label_id_by_title_map(self) -> dict[str, str]:
        """Title -> Id map for every Deal Label, built once per client
        lifetime from the documented GET /Label/GetDealLabels (confirmed
        2026-09 from Didar's own support agent - see config.py's
        deal_label_title_by_source docstring for the full history of why
        this replaced an earlier, wrong Tag-based implementation).

        Only entries with Type == "Deal" are kept - Didar's Label list
        can include other label Types too (e.g. for Contacts), and a
        same-named non-Deal label must never be matched here.

        Fetched lazily on first use and cached for this client's
        lifetime (one instance per process - see DidarSyncService),
        same tradeoff as product_client.py's category cache. NOT cached
        on failure (unlike product_client's catalog load) - a transient
        network error here is worth retrying on the next order, since
        the cost of one extra GET is negligible next to silently
        disabling labels for the rest of the process's life.
        """
        if self._label_id_by_title is None:
            payload = self._get(self._config.get_deal_labels_path)
            self._label_id_by_title = {
                _normalize_fa(str(item.get("Title", ""))): str(item["Id"])
                for item in payload.get("Response", [])
                if isinstance(item, dict)
                and item.get("Id")
                and item.get("Title")
                and item.get("Type") == "Deal"
            }
        return self._label_id_by_title

    def _label_id_for_source(self, source: str) -> str | None:
        """Resolves one marketplace source to its Deal Label Id, via the
        Title configured in DidarConfig.deal_label_title_by_source (see
        that property's docstring). Returns None - never raises - on
        any failure (no Title configured for this source, the API call
        itself failing, or no live Deal Label matching that Title): a
        missing/wrong label must never be the reason an order's whole
        sync fails, same fire-and-forget philosophy as the post-sale
        checklist.
        """
        title = self._config.deal_label_title_by_source.get(source)
        if not title:
            return None

        try:
            label_id = self._label_id_by_title_map().get(_normalize_fa(title))
        except Exception:
            log.exception(
                "didar: failed to fetch Deal Labels via %s - creating "
                "deal for %s order without a label",
                self._config.get_deal_labels_path, source,
            )
            return None

        if not label_id:
            log.warning(
                "didar: no Deal Label titled %r found via %s for source "
                "%r - creating this deal without a label. Verify the "
                "Title in DidarConfig.deal_label_title_by_source matches "
                "exactly what Didar returns for this account.",
                title, self._config.get_deal_labels_path, source,
            )
        return label_id

    def find_existing_deal_id(self, order: NormalizedOrder) -> str | None:
        """
        Duplicate-prevention safety net that checks Didar itself, not just
        our own local sqlite state.

        WHY THIS IS NEEDED ON TOP OF Repository.is_already_synced():
        that local check only catches a duplicate if mark_synced() ran
        for the earlier attempt. It doesn't run in at least two real
        scenarios:
          1. /deal/save succeeds on Didar's side, but the response never
             reaches us (timeout, connection drop) - http_utils'
             default_retry then automatically retries the SAME POST
             (TransportError is retryable), which creates a SECOND real
             Deal for the same order. sync_one_order() never reaches
             mark_synced() for the first attempt because it never saw a
             response, so nothing local ever recorded it.
          2. record_failure() is written after such a lost-response
             timeout; retry_pending_failures() later calls create_deal()
             again on a source_order_id that was, in fact, already
             synced.
        Both are exactly the "duplicate order already added to Didar"
        symptom - the local DB has no way to know, only Didar does.

        ENDPOINT: uses the documented global search endpoint
        POST /search/search (Keyword + Types) - there is no dedicated
        "/deal/search" in the official docs, only "/Case/search", and a
        "Case" is a distinct entity in Didar (a kanban card / activity,
        linked to a Deal via a DealId field - see the docs' "نمونه
        پارامترهای زمان ویرایش" example) - not the Deal itself, so it
        cannot be used for this.

        MATCHING: deliberately an exact-line check of the unique
        `_order_reference()` string against each result's Description -
        matching the exact line _build_description() writes it as, NOT
        a raw substring containment check. /search/search does fuzzy
        full-text matching (same as /product/search's Keywords), so a
        raw hit could easily be a different order that merely shares
        digits with this one (e.g. reference "tapsishop:999" is a plain
        substring of "tapsishop:9999" - a real different order - so
        `in` alone is NOT safe here). A false match means silently
        skipping an order that was never actually synced, which is
        worse than the duplicate this is meant to prevent, so "not
        found" is the safe default whenever we can't be sure.

        NOT YET CONFIRMED: the docs' one example of this endpoint shows
        only Keyword+Types in the request body, no From/Limit pagination
        params (unlike /product/search, which documents both) - so none
        are sent here. If Didar caps or paginates /search/search results
        under the hood, an existing deal could theoretically fall
        outside what's returned; revisit if that's ever confirmed live.
        """
        reference = _order_reference(order)
        reference_line = f"شناسه یکتای هماهنگ‌سازی: {reference}"
        payload = self._post("/search/search", json={"Keyword": reference, "Types": ["deal"]})
        results = payload.get("Response", {}).get("List", [])
        for item in results:
            if not isinstance(item, dict) or item.get("_tp") != "deal":
                continue
            description_lines = (item.get("Description") or "").splitlines()
            if reference_line in description_lines:
                deal_id = item.get("Id")
                if deal_id:
                    log.info(
                        "didar: found existing deal for %s order %s -> Id=%s "
                        "(skipping create - already in Didar)",
                        order.source, order.source_order_id, deal_id,
                    )
                    return str(deal_id)
        return None

    def create_deal(self, contact_id: str, display_name: str, order: NormalizedOrder) -> str:
        deal_body = {
            "Title": f"معامله {display_name}".strip(),
            "BizdomainId": self._config.bizdomain_id,
            "PersonId": contact_id,
            "PipelineStageId": self._config.pipeline_stage_id,
            "Description": _build_description(order),
        }
        label_id = self._label_id_for_source(order.source)
        if label_id:
            deal_body["LabelIds"] = [label_id]

        body = {
            "Deal": deal_body,
            "DealItems": [self._build_deal_item(item, order) for item in order.items],
        }
        payload = self._post("/deal/save", json=body)
        deal_id = _extract_deal_id(payload)
        log.info(
            "didar: created deal for %s order %s -> Id=%s",
            order.source, order.source_order_id, deal_id,
        )
        return deal_id

    def _build_deal_item(self, item, order: NormalizedOrder) -> dict:
        # Prefer the client's Excel catalog: most products already exist
        # in Didar under a short internal Code/title that has no
        # relationship to the marketplace's own SKU or title (see
        # product_catalog.py's module docstring). A confident match
        # there means we search/create using the REAL catalog Code and
        # title instead of the marketplace's.
        catalog_match = self._products.resolve_catalog_code(item.title)
        if catalog_match:
            code, title = catalog_match
        else:
            # SKU is the natural upsert key; falls back to the item
            # title for the (rare) case a source provides no SKU, so at
            # least same-titled items resolve to the same product
            # within a run.
            code, title = item.sku or item.title, item.title

        product_id = self._products.upsert_product(
            code=code,
            title=title,
            category=item.category,
            unit_price=item.unit_price,
            final_price=item.final_price,
        )
        # Per-unit discount, from the gap between the source's original
        # per-unit price and what the line actually settled for.
        #
        # WHY THIS MATTERS (found comparing an auto-created deal against
        # a manually-entered one, client feedback 2026-08): Discount was
        # previously hardcoded to 0 for every item, unconditionally -
        # losing real discount data whenever a source actually applies
        # one. unit_price/final_price genuinely diverge for Digikala
        # (unit_price vs total_price), Tapsi Shop (price vs finalPrice,
        # both confirmed distinct fields per the vendor's own API docs),
        # Faraz Honar/WooCommerce (price vs the line's "total", which WC
        # itself defines as post-discount, distinct from "subtotal") and
        # SnappShop (unit_price vs final_price). Only Basalam's
        # final_price is a pure unit_price*quantity restatement with no
        # separate discount concept in its API - this naturally computes
        # to a 0 discount there, same as before.
        #
        # Didar's DealItems Discount is a per-unit CURRENCY AMOUNT, not
        # a percentage - confirmed from the docs' own example
        # (UnitPrice=80000, Discount=4800).
        quantity = item.quantity or 1
        per_unit_discount = item.unit_price - (item.final_price / quantity)
        if per_unit_discount < 0:
            # Never negative - a negative gap means final_price is
            # HIGHER than unit_price*quantity (tax/fees added on top by
            # the source, not a discount), which Discount must not
            # represent.
            per_unit_discount = Decimal("0")

        return {
            "ProductId": product_id,
            "Quantity": item.quantity,
            "UnitPrice": int(item.unit_price),
            "Discount": int(per_unit_discount),
            # Order-traceability text, matching the convention seen on
            # manually-entered deals (client feedback 2026-08) - those
            # have "شماره سفارش: X/شماره مرسوله: Y" typed into each
            # item's توضیحات; we only have the order number reliably
            # across all 5 sources (no NormalizedOrder field carries a
            # shipment/parcel number yet), so that's what's written here.
            # Note: Didar's DealItems schema has no dedicated tax/duty field.
            # Tax/fees are represented as Discount = 0 when final_price >
            # unit_price*quantity (see per-unit discount logic above at lines
            # 270-273 where it's clamped to >= 0, meaning a higher final_price
            # is not treated as a negative discount but as zero discount).
            "Description": f"شماره سفارش: {order.order_number}",
        }


def _extract_deal_id(payload: dict) -> str:
    candidates = [
        lambda p: p.get("Response", {}).get("Deal", {}).get("Id"),
        lambda p: p.get("Response", {}).get("Id"),
        lambda p: p.get("Deal", {}).get("Id"),
        lambda p: p.get("Id"),
    ]
    for get in candidates:
        try:
            value = get(payload)
        except AttributeError:
            continue
        if value:
            return str(value)

    raise DidarApiError(
        f"didar: could not find Deal Id in response - shape is unconfirmed, "
        f"update _extract_deal_id() once a real payload has been inspected. "
        f"Raw response: {payload!r}"
    )