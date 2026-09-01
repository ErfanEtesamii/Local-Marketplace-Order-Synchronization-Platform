"""
Didar CRM - Product (catalog) client.

Per the project's decision (see the analysis document that drove this
change): order line items must be linked to a real catalog Product via
ProductId, not just written as text. The existing Didar product catalog
uses internal manual codes (1, 10, 100, 1000001...) that have no
relationship to marketplace SKUs, so a match-by-SKU lookup would almost
never succeed. The agreed approach: auto-create a Didar product whenever
no exact match exists, using the marketplace's own product title verbatim.

ENDPOINT CORRECTION (2026-08, after reading Didar's actual API docs -
the earlier module comment guessed wrong): the official docs
("مستندات API دیدار") document exactly three Product endpoints under
the "محصولات" section header text search picks up:

    POST /product/search      - Criteria.Keywords (required) + From/Limit
    POST /product/categories  - list valid ProductCategoryIds
    POST /product/getproductbycodes - {"Code": [...]}, exact Code match

CORRECTION (2026-09, production incident - order 364139925): the code
here previously used POST /product/GetProductsList (assumed to list
the whole catalog, cached client-side for exact Code lookups) instead
of getproductbycodes. Confirmed live that GetProductsList returned 0
products on this account even though the catalog has 3000+ entries,
which meant search_by_code() could never find an existing product via
that path - it silently fell through to /product/search's full-text
ranking, which also missed short/generic codes like "38", so
upsert_product() then tried to CREATE a product that already existed
and Didar correctly rejected it with "duplicate product code",
failing the whole order. getproductbycodes was confirmed live to
return the exact match Didar's docs promise (see _lookup_by_codes()
below), so GetProductsList and its client-side cache were removed
entirely in favor of it.

/product/save IS ALSO documented (found later, under "پارامترهای
خروجی ایجاد/ویرایش محصول" - easy to miss since the docx's text
extraction doesn't surface the request-body code block the same way
as the other endpoints' plain-text JSON examples; confirmed instead
from screenshots of the rendered docs page). The earlier "Product Not
Exist" 400 was NOT evidence the endpoint doesn't support creation -
it was caused by missing required fields. The documented example
request/response confirms a Product needs at minimum:

    Code, Title, TitleForInvoice, Unit, UnitPrice, ProductCategoryId

`upsert_product()` was previously only sending Code/Title/
ProductCategoryId - TitleForInvoice and Unit are now always included
(see below). `DidarId` (an integer) only ever appears in *responses* -
it's Didar's own internal sequential id, auto-assigned, never sent on
create.

FIX: search-first. upsert_product() now calls the documented
POST /product/search for the item's Code before ever calling
/product/save, and uses the existing product's Id directly when a
result's Code matches exactly (see _find_by_code) - this avoids
"duplicate product code" almost entirely, since save is then only ever
attempted for genuinely new codes. If save still fails with "duplicate
product code" (a create/search race: another process/run created the
same Code in between), one automatic retry-via-search recovers the
existing Id rather than failing the whole order for a timing issue.

Code = the marketplace SKU when available, otherwise a fallback derived
from the item title, so at least same-titled items from the same run
resolve consistently within a sync cycle even without a real SKU.

CATEGORY: confirmed both live (400 "product category is empty") and
in Didar's own API docs ("پارامترهای خروجی ایجاد/ویرایش محصول" table)
that /product/save requires ProductCategoryId - it's not optional the
way the module-level comment above originally assumed. The docs also
confirm a real endpoint to list valid categories:

    POST {DIDAR_BASE_URL}/product/categories?apikey={API_KEY}
    -> {"Response": [{"Id": "...", "Title": "..."}, ...]}

Per client feedback: a single catch-all category is wrong when the
Didar catalog already has real per-craft categories (خاتم، میناکاری،
قلم‌زنی، فیروزه، ...) - each product needs to land in ITS OWN category,
not all in one. upsert_product() now takes an optional `category` name
(the marketplace's own category/group label for that product, when the
source provides one - currently only farazhonar/WooCommerce does, see
its adapter) and resolves it to a Didar ProductCategoryId by exact
title match (list_categories() results, cached for this client's
lifetime - categories change rarely enough that a stale cache for the
life of one run is an acceptable tradeoff over re-fetching per item).

Matching is deliberately simple (case-insensitive, whitespace-trimmed
exact match) rather than fuzzy - a silent wrong-category match would
be worse than a clear fallback.

KEYWORD FALLBACK (added per client feedback, 2026-08): an exact
marketplace-category match is only possible for Faraz Honar today -
Digikala/Basalam/Tapsi Shop/SnappShop don't expose a category field at
all (see each adapter). For those, and for any farazhonar category
that doesn't exact-match a Didar category, _category_id_for() now
tries src.didar.category_mapping.keyword_category_title() against the
item's own TITLE before giving up - see that module's docstring for
how the keyword lists were built (draft, unconfirmed against the real
catalog) and its match-order caveats. Only when that also finds
nothing does the product fall back to
DidarConfig.default_product_category_id (DIDAR_DEFAULT_PRODUCT_CATEGORY_ID
in .env) - the genuine catch-all ("متفرقه") for exactly the
unmatched/unknown cases, not everything.

CATALOG-BASED CODE LOOKUP (client feedback, 2026-08-29): most products
already exist in Didar under a short internal name/Code that has no
relationship at all to the marketplace's own SKU or title (see
product_catalog.py's module docstring for the motivating example).
resolve_catalog_code() looks up the item's marketplace title against a
client-maintained Excel export of the Didar catalog (path from
DidarConfig.product_catalog_xlsx) and, on a confident match, returns
the catalog's OWN Code and title - the caller (deal_client.py) then
searches/creates using those instead of the marketplace SKU/title.
Loaded lazily (only touches the filesystem on first real lookup) and
only when DIDAR_PRODUCT_CATALOG_XLSX is actually set, so leaving it
blank is a complete, silent no-op (falls back to the pre-existing
SKU/title behaviour) rather than a startup requirement.
"""
from __future__ import annotations

import httpx

from src.config import DidarConfig, settings
from src.didar.category_mapping import _normalize_fa, keyword_category_title
from src.didar.contact_client import DidarApiError
from src.didar.product_catalog import ProductCatalog
from src.http_utils import default_retry, raise_for_status_with_body
from src.logger import get_logger

log = get_logger(__name__)


def _normalize_code(code: str) -> str:
    """Whitespace-trimmed comparison for Code matching - Digikala's own
    codes have been observed with stray leading/trailing spaces (see
    "10502013 " in the API docs' own example), so an exact `==` would
    silently miss real matches."""
    return code.strip()


class DidarProductClient:
    def __init__(self, config: DidarConfig | None = None) -> None:
        self._config = config or settings.didar
        self._client = httpx.Client(base_url=self._config.base_url, timeout=30.0)
        self._category_by_title: dict[str, str] | None = None  # populated lazily
        self._catalog: ProductCatalog | None = None  # populated lazily
        self._catalog_load_attempted = False

    @default_retry()
    def _post(self, path: str, json: dict | None = None) -> dict:
        resp = self._client.post(path, params={"apikey": self._config.api_key}, json=json or {})
        raise_for_status_with_body(resp)
        return resp.json()

    def resolve_catalog_code(self, marketplace_title: str) -> tuple[str, str] | None:
        """
        Look up a marketplace item's title in the client's Excel product
        catalog (see product_catalog.py) - returns (code, catalog_title)
        on a confident full-word-set match, else None. Callers should
        fall back to their own default Code (marketplace SKU/title) when
        this returns None - see deal_client.py's _build_deal_item.

        The catalog file is loaded at most once per client instance, and
        only if DIDAR_PRODUCT_CATALOG_XLSX is actually set - leaving it
        blank means this always returns None without ever touching the
        filesystem.

        A bad path or a malformed file (ProductCatalog raises
        FileNotFoundError/ValueError for both - see its own tests) is
        caught HERE rather than left to propagate: this method runs
        inside create_deal(), which has no fire-and-forget wrapper
        (unlike the post-sale checklist), so an uncaught exception here
        would fail that order's ENTIRE sync, not just disable catalog
        matching for it. And because loading is attempted only ONCE per
        long-lived instance (see _catalog_load_attempted above, and
        DidarProductClient's own lifetime - one instance for the whole
        process, not per order - via DidarDealClient/DidarSyncService),
        an uncaught failure here would fail exactly the first order to
        need this and then go permanently, silently quiet for every
        order after that - the worst kind of bug to notice in
        production. log.exception (ERROR + full traceback) rather than
        a plain warning is deliberate here, since this fires exactly
        once for the whole process's lifetime (the load is attempted
        only once - see _catalog_load_attempted) and needs to be visible
        enough that it doesn't get lost among the routine per-order info
        logs around it.
        """
        if not self._catalog_load_attempted:
            self._catalog_load_attempted = True
            path = self._config.product_catalog_xlsx
            if path:
                try:
                    self._catalog = ProductCatalog(path)
                except Exception:
                    log.exception(
                        "didar: failed to load product catalog from %r "
                        "(DIDAR_PRODUCT_CATALOG_XLSX) - catalog-based Code "
                        "lookup is DISABLED for the rest of this run, every "
                        "item falls back to marketplace SKU/title. Fix the "
                        "path/file and restart the service to re-enable it.",
                        path,
                    )

        if self._catalog is None:
            return None

        match = self._catalog.match(marketplace_title)
        if match is None:
            return None
        log.info(
            "didar: catalog match for title=%r -> Code=%s (catalog title=%r)",
            marketplace_title, match.code, match.title,
        )
        return match.code, match.title

    def list_categories(self) -> list[dict]:
        """GET the {Id, Title} list of valid product categories - confirmed
        via Didar's own docs (POST /product/categories)."""
        payload = self._post("/product/categories")
        return payload.get("Response", [])

    def _lookup_by_codes(self, codes: list[str]) -> dict[str, str]:
        """Exact Code -> Id lookup via the documented
        POST /product/getproductbycodes. Unlike /product/search (a
        full-text, relevance-ranked search that can silently miss
        short/generic codes like "38" once the catalog has thousands
        of entries - see the 2026-09 production incident, order
        364139925) and unlike the old GetProductsList-based full-catalog
        cache it replaced (which returned 0 products on this account -
        same incident), this is a targeted exact-match lookup, confirmed
        live to correctly find Code=38:

            POST /product/getproductbycodes  {"Code": ["38"]}
            -> {"Response": {"Total": 1, "Products": [{"Id": "...",
                "Code": "38", ...}]}}

        Note Response is an OBJECT with a "Products" list inside, not a
        bare array like /product/search and /product/categories return.
        """
        body = {"Code": codes}
        payload = self._post("/product/getproductbycodes", json=body)
        products = payload.get("Response", {}).get("Products", [])
        return {
            _normalize_code(str(p.get("Code", ""))): str(p["Id"])
            for p in products
            if isinstance(p, dict) and p.get("Code") and p.get("Id")
        }

    def search_by_code(self, code: str, limit: int = 20) -> str | None:
        """Look up an existing product by Code - first via the exact,
        documented POST /product/getproductbycodes, then, only if that
        call itself fails (network error etc.), via /product/search as
        a fallback (Criteria.Keywords is a full-text search, not an
        exact-Code filter, so results are filtered down to an exact
        normalized Code match here - a partial/fuzzy hit on the wrong
        product would be worse than not finding one at all)."""
        target = _normalize_code(code)
        try:
            exact = self._lookup_by_codes([code])
            if target in exact:
                return exact[target]
            return None
        except Exception:
            log.exception(
                "didar: /product/getproductbycodes lookup failed for "
                "Code=%s - falling back to /product/search",
                code,
            )

        body = {"Criteria": {"Keywords": code}, "From": 0, "Limit": limit}
        payload = self._post("/product/search", json=body)
        results = payload.get("Response", [])
        for product in results:
            if not isinstance(product, dict):
                continue
            if _normalize_code(str(product.get("Code", ""))) == target:
                product_id = product.get("Id")
                if product_id:
                    return str(product_id)
        return None

    def _category_by_title_map(self) -> dict[str, str]:
        if self._category_by_title is None:
            self._category_by_title = {
                _normalize_fa(str(c.get("Title", ""))): str(c["Id"])
                for c in self.list_categories()
                if c.get("Id") and c.get("Title")
            }
        return self._category_by_title

    def _category_id_for(self, category_name: str | None, item_title: str) -> str:
        """Resolve a Didar ProductCategoryId for one order item, in order:

        1. Exact (normalized) match of the marketplace's own category name,
           when the source provides one (currently only farazhonar) - see
           module docstring.
        2. A keyword guess from the item's TITLE - see category_mapping.py.
           This is the only signal available at all for the four sources
           that don't report a category (Digikala/Basalam/Tapsi Shop/
           SnappShop), and also covers farazhonar items whose WooCommerce
           category has no same-named Didar category yet.
        3. DidarConfig.default_product_category_id (متفرقه) - the genuine
           catch-all, only reached when both of the above found nothing.
        """
        by_title = self._category_by_title_map()

        if category_name:
            match = by_title.get(_normalize_fa(category_name))
            if match:
                return match
            log.warning(
                "didar: no category named %r in the Didar catalog - "
                "trying a keyword guess from the item title instead",
                category_name,
            )

        guessed_title = keyword_category_title(item_title)
        if guessed_title:
            match = by_title.get(_normalize_fa(guessed_title))
            if match:
                return match
            # KEYWORD_RULES referenced a category title that no longer
            # exists in Didar (renamed/deleted there) - not silently
            # ignorable, since it means category_mapping.py is out of
            # sync with the live catalog.
            log.warning(
                "didar: keyword match picked category %r for title %r, "
                "but no such category exists in the Didar catalog - "
                "falling back to the default category",
                guessed_title, item_title,
            )
        else:
            log.info(
                "didar: no keyword in category_mapping.py matched item "
                "title %r - falling back to the default category",
                item_title,
            )

        if not self._config.default_product_category_id:
            raise DidarApiError(
                "didar: DIDAR_DEFAULT_PRODUCT_CATEGORY_ID is not set in .env - "
                "needed as a fallback whenever an item's category is unknown, "
                "has no matching Didar category, and no keyword in "
                "category_mapping.py matches its title either. Call "
                "DidarProductClient.list_categories() once (or POST "
                "/product/categories directly) to pick a real Id, then set "
                "it in .env - see .env.example."
            )
        return self._config.default_product_category_id

    def upsert_product(
        self,
        code: str,
        title: str,
        category: str | None = None,
        unit_price: object = 0,
        final_price: object = 0,
    ) -> str:
        # Search first (documented endpoint) - if the product already
        # exists, use its Id directly and never touch the undocumented
        # /product/save at all. This is what eliminates "duplicate
        # product code" almost entirely - see module docstring.
        existing_id = self.search_by_code(code)
        if existing_id:
            log.info(
                "didar: found existing product Code=%s -> Id=%s (skipping create)",
                code, existing_id,
            )
            return existing_id

        category_id = self._category_id_for(category, title)
        body = {
            "Product": {
                "Code": code,
                "Title": title,
                # Confirmed required via the client's screenshots of
                # Didar's own docs for /product/save ("پارامترهای
                # خروجی ایجاد/ویرایش محصول"): TitleForInvoice and Unit
                # are populated on every real product, and omitting
                # them was the actual cause of the earlier
                # "Product Not Exist" 400 - not a missing/invalid
                # ProductCategoryId as first suspected. "عدد" (piece)
                # is a safe generic Unit label - none of our sources
                # expose a real unit of measure.
                # Original price (before discount) is stored in TitleForInvoice
                # so it's visible in the Didar product catalog.
                "TitleForInvoice": f"{title} - {unit_price}",
                "Unit": "عدد",
                # UnitPrice sent to Didar is the discounted final price,
                # not the original unit price - see deal_client.py
                # _build_deal_item() which passes final_price here.
                "UnitPrice": int(final_price or 0),
                "ProductCategoryId": category_id,
            }
        }
        try:
            payload = self._post("/product/save", json=body)
        except httpx.HTTPStatusError as exc:
            # Race: search found nothing, but the product was created by
            # something else (another sync run, a concurrent item with
            # the same Code, a manual entry) between our search and this
            # save call. Recover by searching once more rather than
            # failing the whole order for a timing issue.
            if exc.response is not None and "duplicate product code" in exc.response.text.lower():
                # Didar itself just confirmed this Code exists - recover
                # by looking it up directly rather than failing the
                # whole order for a create/search race.
                recovered_id = self.search_by_code(code)
                if recovered_id:
                    log.info(
                        "didar: create raced with another writer for Code=%s - "
                        "recovered existing Id=%s via search",
                        code, recovered_id,
                    )
                    return recovered_id
            raise

        product_id = _extract_product_id(payload)
        log.info(
            "didar: created product Code=%s Title=%s CategoryId=%s -> Id=%s",
            code, title, category_id, product_id,
        )
        return product_id


def _extract_product_id(payload: dict) -> str:
    candidates = [
        lambda p: p.get("Response", {}).get("Product", {}),
        lambda p: p.get("Response", {}),
        lambda p: p.get("Product", {}),
        lambda p: p,
    ]
    for get in candidates:
        try:
            product = get(payload)
        except AttributeError:
            continue
        product_id = product.get("Id") if isinstance(product, dict) else None
        if product_id:
            return str(product_id)

    raise DidarApiError(
        f"didar: could not find Product Id in response - shape is unconfirmed, "
        f"update _extract_product_id() once a real payload has been inspected. "
        f"Raw response: {payload!r}"
    )