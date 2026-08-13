"""
Didar CRM - Deal client.

Endpoint confirmed the same way as the Contact client:

    POST {DIDAR_BASE_URL}/deal/save?apikey={API_KEY}
    body: {"Deal": {"Title": ..., "ContactId": ..., "PipelineStageId": ...,
                     "Status": ..., "Description": ..., "InvoiceId": ...}}

NOT YET CONFIRMED - line item mapping:
The Deal object takes an InvoiceId, not an embedded Items[] array as this
project's proposal originally assumed. Didar appears to model priced line
items through a separate "Product" entity (POST /product/save, confirmed
to exist with UnitPrice/Quantity/FinalPrice fields) that presumably then
gets linked into an Invoice - but the exact Invoice-creation endpoint and
how it ties Products to a Deal has not been confirmed yet.

Until that's confirmed (needs a real DIDAR_API_KEY + a look at the
Postman docs' Invoice/Product sections), this client takes the safe,
guaranteed-to-work path: it writes a clear, itemized summary of the
order into the Deal's Description field, so no financial detail is lost
even though it isn't yet structured data on the Didar side. total_price
uses whatever numeric field the Deal API turns out to expect once that's
confirmed - currently omitted rather than guessed into the wrong field.

TODO(didar-invoice): replace the Description-based summary with proper
Product + Invoice creation once the endpoints are confirmed.
"""
from __future__ import annotations

import httpx

from src.config import DidarConfig, settings
from src.didar.contact_client import DidarApiError
from src.http_utils import default_retry
from src.logger import get_logger
from src.marketplaces.base import NormalizedOrder

log = get_logger(__name__)


class DidarDealClient:
    def __init__(self, config: DidarConfig | None = None) -> None:
        self._config = config or settings.didar
        self._client = httpx.Client(base_url=self._config.base_url, timeout=30.0)

    @default_retry()
    def _post(self, path: str, json: dict) -> dict:
        resp = self._client.post(path, params={"apikey": self._config.api_key}, json=json)
        resp.raise_for_status()
        return resp.json()

    def create_deal(self, contact_id: str, order: NormalizedOrder) -> str:
        body = {
            "Deal": {
                "Title": f"{order.order_number} - {order.source}",
                "ContactId": contact_id,
                "PipelineStageId": self._config.pipeline_stage_id,
                "Description": _build_description(order),
            }
        }
        payload = self._post("/deal/save", json=body)
        deal_id = _extract_deal_id(payload)
        log.info(
            "didar: created deal for %s order %s -> Id=%s",
            order.source, order.source_order_id, deal_id,
        )
        return deal_id


def _build_description(order: NormalizedOrder) -> str:
    lines = [
        f"Source: {order.source}",
        f"Marketplace order id: {order.source_order_id}",
        f"Status: {order.status}",
        f"Total: {order.total_price}",
        "Items:",
    ]
    for item in order.items:
        lines.append(f"  - {item.title} (sku={item.sku}) x{item.quantity} = {item.final_price}")
    if not order.items:
        lines.append("  (no line items available)")
    return "\n".join(lines)


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
