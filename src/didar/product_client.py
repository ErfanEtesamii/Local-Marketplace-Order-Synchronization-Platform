"""
Didar CRM - Product (catalog) client.

Per the project's decision (see the analysis document that drove this
change): order line items must be linked to a real catalog Product via
ProductId, not just written as text. The existing Didar product catalog
uses internal manual codes (1, 10, 100, 1000001...) that have no
relationship to marketplace SKUs, so a match-by-SKU lookup would almost
never succeed. The agreed approach: auto-create a Didar product whenever
no exact match exists, using the marketplace's own product title verbatim.

NOT YET CONFIRMED: a dedicated product-search endpoint. Rather than
guess one, this client mirrors the pattern already proven to work for
Contact (upsert via POST /product/save, keyed on a Code field) - Didar's
API consistently upserts-by-code elsewhere (Contact.CustomerCode), so
the same behavior is assumed here pending live confirmation. If
product/save turns out NOT to upsert-by-Code in practice (i.e. it
always creates a new product even when Code repeats), duplicate
products will accumulate on re-sync of the same SKU - flagged here so
it's the first thing to check if the Didar catalog looks cluttered
after go-live.

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
"""
from __future__ import annotations

import httpx

from src.config import DidarConfig, settings
from src.didar.category_mapping import _normalize_fa, keyword_category_title
from src.didar.contact_client import DidarApiError
from src.http_utils import default_retry, raise_for_status_with_body
from src.logger import get_logger

log = get_logger(__name__)


class DidarProductClient:
    def __init__(self, config: DidarConfig | None = None) -> None:
        self._config = config or settings.didar
        self._client = httpx.Client(base_url=self._config.base_url, timeout=30.0)
        self._category_by_title: dict[str, str] | None = None  # populated lazily

    @default_retry()
    def _post(self, path: str, json: dict | None = None) -> dict:
        resp = self._client.post(path, params={"apikey": self._config.api_key}, json=json or {})
        raise_for_status_with_body(resp)
        return resp.json()

    def list_categories(self) -> list[dict]:
        """GET the {Id, Title} list of valid product categories - confirmed
        via Didar's own docs (POST /product/categories)."""
        payload = self._post("/product/categories")
        return payload.get("Response", [])

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

    def upsert_product(self, code: str, title: str, category: str | None = None) -> str:
        category_id = self._category_id_for(category, title)
        body = {
            "Product": {
                "Code": code,
                "Title": title,
                "ProductCategoryId": category_id,
            }
        }
        payload = self._post("/product/save", json=body)
        product_id = _extract_product_id(payload)
        log.info(
            "didar: upserted product Code=%s Title=%s CategoryId=%s -> Id=%s",
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