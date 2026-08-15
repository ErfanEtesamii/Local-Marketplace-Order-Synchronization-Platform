import tempfile
from datetime import datetime, timezone, timedelta
from decimal import Decimal

import pytest

from src.db.repository import Repository
from src.marketplaces.base import MarketplaceAdapter, NormalizedOrder, OrderItem
from src.sync_engine import SyncEngine


def _order(source: str, order_id: str, with_items: bool = False) -> NormalizedOrder:
    return NormalizedOrder(
        source=source,
        source_order_id=order_id,
        order_number=order_id,
        created_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
        total_price=Decimal("100000"),
        status="confirmed",
        items=[OrderItem(sku="s", title="t", quantity=1, unit_price=Decimal("1"),
                          final_price=Decimal("100000"))] if with_items else [],
    )


class FakeAdapter(MarketplaceAdapter):
    """In-memory stand-in for a real marketplace adapter - lets us test
    orchestration logic (dedupe, retries, isolation) without any HTTP."""

    def __init__(self, name: str, list_orders=None, details=None, fail_fetch=False):
        self.name = name
        self._list_orders = list_orders or []
        self._details = details or {}
        self._fail_fetch = fail_fetch
        self.fetch_new_orders_calls = 0
        self.fetch_order_detail_calls = 0

    def fetch_new_orders(self, since):
        self.fetch_new_orders_calls += 1
        if self._fail_fetch:
            raise RuntimeError(f"{self.name}: simulated fetch failure")
        return self._list_orders

    def fetch_order_detail(self, source_order_id):
        self.fetch_order_detail_calls += 1
        return self._details[source_order_id]


class FakeDidarService:
    """Records every order it was asked to sync; can be told to fail for
    specific order ids on their first call, to test the retry path."""

    def __init__(self, fail_once_for: set[str] | None = None):
        self._fail_once_for = set(fail_once_for or ())
        self.synced_orders: list[NormalizedOrder] = []

    def sync_order(self, order: NormalizedOrder) -> str:
        key = f"{order.source}:{order.source_order_id}"
        if key in self._fail_once_for:
            self._fail_once_for.remove(key)
            raise RuntimeError("simulated Didar failure")
        self.synced_orders.append(order)
        return f"deal-{order.source_order_id}"


@pytest.fixture
def repo():
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        yield Repository(db_path=f.name)


def test_new_order_gets_synced_and_marked(repo):
    adapter = FakeAdapter("fake1", list_orders=[_order("fake1", "1", with_items=True)])
    didar = FakeDidarService()
    engine = SyncEngine(adapters=[adapter], repository=repo, didar_service=didar)

    engine.run_once()

    assert len(didar.synced_orders) == 1
    assert repo.is_already_synced("fake1", "1")


def test_list_without_items_triggers_detail_fetch(repo):
    adapter = FakeAdapter(
        "fake1",
        list_orders=[_order("fake1", "1", with_items=False)],
        details={"1": _order("fake1", "1", with_items=True)},
    )
    didar = FakeDidarService()
    engine = SyncEngine(adapters=[adapter], repository=repo, didar_service=didar)

    engine.run_once()

    assert adapter.fetch_order_detail_calls == 1
    assert len(didar.synced_orders[0].items) == 1


def test_duplicate_order_is_not_synced_twice(repo):
    adapter = FakeAdapter("fake1", list_orders=[_order("fake1", "1", with_items=True)])
    didar = FakeDidarService()
    engine = SyncEngine(adapters=[adapter], repository=repo, didar_service=didar)

    engine.run_once()
    engine.run_once()  # same order returned again by the (fake) source

    assert len(didar.synced_orders) == 1  # not synced a second time


def test_one_source_failing_does_not_block_others(repo):
    broken = FakeAdapter("broken", fail_fetch=True)
    healthy = FakeAdapter("healthy", list_orders=[_order("healthy", "1", with_items=True)])
    didar = FakeDidarService()
    engine = SyncEngine(adapters=[broken, healthy], repository=repo, didar_service=didar)

    engine.run_once()

    assert len(didar.synced_orders) == 1
    assert didar.synced_orders[0].source == "healthy"
    # broken source's watermark must NOT advance, so it's retried in full next time
    assert repo.get_last_sync_time("broken") is None


def test_didar_failure_is_recorded_and_retried_successfully(repo):
    adapter = FakeAdapter(
        "fake1",
        list_orders=[_order("fake1", "1", with_items=True)],
        details={"1": _order("fake1", "1", with_items=True)},
    )
    didar = FakeDidarService(fail_once_for={"fake1:1"})
    engine = SyncEngine(adapters=[adapter], repository=repo, didar_service=didar)

    # Call the fetch+sync phase directly (bypassing run_once's own
    # end-of-cycle retry pass) so the two phases can be verified separately.
    engine._sync_source(adapter)
    assert not repo.is_already_synced("fake1", "1")
    assert len(repo.get_pending_failures()) == 1

    engine.retry_pending_failures()  # second attempt succeeds

    assert repo.is_already_synced("fake1", "1")
    assert len(repo.get_pending_failures()) == 0


def test_run_once_self_heals_within_a_single_cycle(repo):
    """
    run_once() runs a retry pass at the end of every cycle, so a
    transient failure that would succeed on a second attempt is already
    resolved by the time run_once() returns - no separate call needed.
    """
    adapter = FakeAdapter(
        "fake1",
        list_orders=[_order("fake1", "1", with_items=True)],
        details={"1": _order("fake1", "1", with_items=True)},
    )
    didar = FakeDidarService(fail_once_for={"fake1:1"})
    engine = SyncEngine(adapters=[adapter], repository=repo, didar_service=didar)

    engine.run_once()

    assert repo.is_already_synced("fake1", "1")
    assert len(repo.get_pending_failures()) == 0


def test_watermark_advances_with_overlap_margin(repo):
    adapter = FakeAdapter("fake1", list_orders=[])
    engine = SyncEngine(adapters=[adapter], repository=repo, didar_service=FakeDidarService())

    before = datetime.now(timezone.utc)
    engine.run_once()
    after = datetime.now(timezone.utc)

    watermark = repo.get_last_sync_time("fake1")
    assert watermark is not None
    # Watermark should be "now" minus the overlap margin, not exactly "now".
    assert before - timedelta(minutes=11) <= watermark <= after - timedelta(minutes=9)
