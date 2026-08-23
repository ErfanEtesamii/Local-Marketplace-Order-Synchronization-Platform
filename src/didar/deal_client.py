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
  - Order source (site) -> Deal.LabelId (a Tag) AND, per a later client
                           request, also written as readable text in
                           Description alongside a link back to the
                           order on its origin platform - see below
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

import httpx

from src.config import DidarConfig, settings
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


def _build_description(order: NormalizedOrder) -> str:
    source_label = _SOURCE_DISPLAY_NAMES.get(order.source, order.source)
    link = _order_link(order)
    lines = [f"فروشگاه: {source_label}", f"شماره سفارش: {order.order_number}"]
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

    @default_retry()
    def _post(self, path: str, json: dict) -> dict:
        resp = self._client.post(path, params={"apikey": self._config.api_key}, json=json)
        raise_for_status_with_body(resp)
        return resp.json()

    def create_deal(self, contact_id: str, display_name: str, order: NormalizedOrder) -> str:
        deal_body = {
            "Title": f"معامله {display_name}".strip(),
            "BizdomainId": self._config.bizdomain_id,
            "PersonId": contact_id,
            "PipelineStageId": self._config.pipeline_stage_id,
            "Description": _build_description(order),
        }
        label_id = self._config.label_by_source.get(order.source)
        if label_id:
            deal_body["LabelId"] = label_id

        body = {
            "Deal": deal_body,
            "DealItems": [self._build_deal_item(item) for item in order.items],
        }
        payload = self._post("/deal/save", json=body)
        deal_id = _extract_deal_id(payload)
        log.info(
            "didar: created deal for %s order %s -> Id=%s",
            order.source, order.source_order_id, deal_id,
        )
        return deal_id

    def _build_deal_item(self, item) -> dict:
        # SKU is the natural upsert key; falls back to the item title
        # for the (rare) case a source provides no SKU, so at least
        # same-titled items resolve to the same product within a run.
        product_id = self._products.upsert_product(code=item.sku or item.title, title=item.title)
        return {
            "ProductId": product_id,
            "Quantity": item.quantity,
            "UnitPrice": int(item.unit_price),
            "Discount": 0,
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
