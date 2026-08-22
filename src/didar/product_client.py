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
"""
from __future__ import annotations

import httpx

from src.config import DidarConfig, settings
from src.didar.contact_client import DidarApiError
from src.http_utils import default_retry, raise_for_status_with_body
from src.logger import get_logger

log = get_logger(__name__)


class DidarProductClient:
    def __init__(self, config: DidarConfig | None = None) -> None:
        self._config = config or settings.didar
        self._client = httpx.Client(base_url=self._config.base_url, timeout=30.0)

    @default_retry()
    def _post(self, path: str, json: dict) -> dict:
        resp = self._client.post(path, params={"apikey": self._config.api_key}, json=json)
        raise_for_status_with_body(resp)
        return resp.json()

    def upsert_product(self, code: str, title: str) -> str:
        body = {"Product": {"Code": code, "Title": title}}
        payload = self._post("/product/save", json=body)
        product_id = _extract_product_id(payload)
        log.info("didar: upserted product Code=%s Title=%s -> Id=%s", code, title, product_id)
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
