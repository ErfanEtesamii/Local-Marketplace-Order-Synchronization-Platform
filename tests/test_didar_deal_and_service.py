from datetime import datetime, timezone
from decimal import Decimal

import respx
import httpx

from src.config import DidarConfig
from src.didar.contact_client import DidarContactClient
from src.didar.deal_client import DidarDealClient
from src.didar.service import DidarSyncService, _customer_code_for
from src.marketplaces.base import NormalizedOrder, OrderItem

_CFG = DidarConfig(base_url="https://app.didar.me/api", api_key="test-key",
                    pipeline_id="p1", pipeline_stage_id="stage-1")

_ORDER = NormalizedOrder(
    source="tapsishop",
    source_order_id="999",
    order_number="ORD-999",
    created_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
    total_price=Decimal("250000"),
    status="confirmed",
    items=[OrderItem(sku="SKU-1", title="Product A", quantity=2, unit_price=Decimal("100000"),
                      final_price=Decimal("200000"))],
    customer_mobile=None,
    customer_full_name=None,
)


@respx.mock
def test_create_deal_includes_pipeline_stage_and_contact():
    route = respx.post("https://app.didar.me/api/deal/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Deal": {"Id": "d-1"}}})
    )

    client = DidarDealClient(config=_CFG)
    deal_id = client.create_deal(contact_id="c-1", order=_ORDER)

    assert deal_id == "d-1"
    body = route.calls[0].request.content
    assert b"stage-1" in body
    assert b"c-1" in body
    assert b'"PersonId":"c-1"' in body  # regression test - Didar expects PersonId, not ContactId
    assert b"ORD-999" in body


def test_customer_code_prefers_mobile_then_falls_back_to_synthetic():
    with_mobile = NormalizedOrder(**{**_ORDER.__dict__, "customer_mobile": "0912"})
    assert _customer_code_for(with_mobile) == "0912"
    assert _customer_code_for(_ORDER) == "tapsishop-999"  # no mobile -> synthetic


@respx.mock
def test_sync_service_calls_contact_then_deal_in_order():
    contact_route = respx.post("https://app.didar.me/api/contact/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Contact": {"Id": "c-42"}}})
    )
    deal_route = respx.post("https://app.didar.me/api/deal/save").mock(
        return_value=httpx.Response(200, json={"Response": {"Deal": {"Id": "d-42"}}})
    )

    service = DidarSyncService(
        contact_client=DidarContactClient(config=_CFG),
        deal_client=DidarDealClient(config=_CFG),
    )
    deal_id = service.sync_order(_ORDER)

    assert deal_id == "d-42"
    assert contact_route.called
    assert deal_route.called
    # Deal must be created with the Id that came back from the Contact call.
    assert b"c-42" in deal_route.calls[0].request.content