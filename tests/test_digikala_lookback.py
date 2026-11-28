"""Lookback re-read of recent Digikala shipment ids (late-appearing shipments)."""
import pytest
import respx

from src.db.repository import Repository
from src.marketplaces.digikala import DigikalaAdapter
from tests.test_digikala import (
    _CFG,
    _SBS_URL,
    _mock_no_promotions,
    _sbs_list_response,
    _sbs_row,
)


@pytest.fixture
def repo(tmp_path):
    return Repository(db_path=str(tmp_path / "test.db"))


@respx.mock
def test_late_appearing_lower_shipment_id_is_still_picked_up(repo, monkeypatch):
    """Real miss 2026-11: shipment 401560931 became visible after 401562093
    had moved the watermark past it. With the lookback it is fetched, the
    already-synced one is skipped, and the watermark never goes backwards."""
    monkeypatch.setenv("DIGIKALA_ID_LOOKBACK", "5000")
    repo.set_last_shipment_id("digikala", 401562093)
    repo.mark_synced("digikala", "401562093", "deal-1")
    _mock_no_promotions()
    route = respx.get(_SBS_URL).mock(
        return_value=_sbs_list_response(
            [_sbs_row(shipment_id=401560931), _sbs_row(shipment_id=401562093)]
        )
    )

    orders = DigikalaAdapter(config=_CFG, repository=repo).fetch_new_orders(since=None)

    assert [o.source_order_id for o in orders] == ["401560931"]
    assert route.calls[0].request.url.params["search[min_shipment_id]"] == str(401562094 - 5000)
    assert repo.get_last_shipment_id("digikala") == 401562093


