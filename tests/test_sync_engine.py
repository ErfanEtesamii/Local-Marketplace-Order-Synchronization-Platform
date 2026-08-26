from datetime import datetime, timezone, timedelta
from decimal import Decimal

import pytest

from src.db.repository import Repository
from src.marketplaces.base import MarketplaceAdapter, NormalizedOrder, OrderItem
from src.sync_engine import SyncEngine


def _order(
    source: str,
    order_id: str,
    with_items: bool = False,
    created_at: datetime | None = None,
) -> NormalizedOrder:
    return NormalizedOrder(
        source=source,
        source_order_id=order_id,
        order_number=order_id,
        # Fresh by default (not a fixed past date) - _sync_source() now
        # drops any order whose created_at is older than the `since` it
        # asked for (see sync_engine.py's _drop_orders_older_than_since),
        # so tests that aren't specifically exercising that guard need
        # their fixture orders to actually look "new" relative to
        # whatever `since` the engine computes at run time.
        created_at=created_at or datetime.now(timezone.utc),
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
        self.received_since_values = []

    def fetch_new_orders(self, since):
        self.fetch_new_orders_calls += 1
        self.received_since_values.append(since)
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
def repo(tmp_path):
    # tmp_path (pytest's built-in fixture) rather than
    # tempfile.NamedTemporaryFile: the latter keeps its own file handle
    # open, which blocks sqlite3.connect() from opening the same file on
    # Windows (works fine on Linux/macOS, which allow concurrent opens -
    # this was failing only in Windows CI runs).
    db_path = tmp_path / "test_sync.db"
    return Repository(db_path=str(db_path))


def _seed_watermark(repo, source: str, minutes_ago: int = 60) -> None:
    """
    Most of these tests are about orchestration (dedupe, retries, source
    isolation), not specifically about the first-run/no-watermark date
    logic - call this to give `source` an existing watermark comfortably
    in the past, so a normal `_order()` fixture (created_at defaults to
    "now") lands after `since` and isn't dropped by
    _drop_orders_older_than_since(). Tests that ARE about the
    no-watermark / first-run behavior deliberately don't call this.
    """
    repo.set_last_sync_time(source, datetime.now(timezone.utc) - timedelta(minutes=minutes_ago))


def test_new_order_gets_synced_and_marked(repo):
    _seed_watermark(repo, "fake1")
    adapter = FakeAdapter("fake1", list_orders=[_order("fake1", "1", with_items=True)])
    didar = FakeDidarService()
    engine = SyncEngine(adapters=[adapter], repository=repo, didar_service=didar)

    engine.run_once()

    assert len(didar.synced_orders) == 1
    assert repo.is_already_synced("fake1", "1")


def test_list_without_items_triggers_detail_fetch(repo):
    _seed_watermark(repo, "fake1")
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
    _seed_watermark(repo, "fake1")
    adapter = FakeAdapter("fake1", list_orders=[_order("fake1", "1", with_items=True)])
    didar = FakeDidarService()
    engine = SyncEngine(adapters=[adapter], repository=repo, didar_service=didar)

    engine.run_once()
    engine.run_once()  # same order returned again by the (fake) source

    assert len(didar.synced_orders) == 1  # not synced a second time


def test_one_source_failing_does_not_block_others(repo):
    _seed_watermark(repo, "healthy")
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
    _seed_watermark(repo, "fake1")
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
    _seed_watermark(repo, "fake1")
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


def test_first_run_never_backfills_history(repo):
    """
    Regression test for a real production incident: a source with no
    prior watermark used to default to "now - 1 day", which flooded
    Didar with historical (already-completed, already-handled-manually)
    orders on the very first run. The 'since' passed to fetch_new_orders
    on a first run must be ~now, not some lookback window into the past.
    """
    adapter = FakeAdapter("fake1", list_orders=[])
    engine = SyncEngine(adapters=[adapter], repository=repo, didar_service=FakeDidarService())

    before = datetime.now(timezone.utc)
    engine.run_once()
    after = datetime.now(timezone.utc)

    assert adapter.fetch_new_orders_calls == 1
    received_since = adapter.received_since_values[0]
    assert before <= received_since <= after  # no artificial lookback applied

    watermark = repo.get_last_sync_time("fake1")
    assert watermark >= before - timedelta(minutes=11)


def test_orders_older_than_since_are_dropped_even_if_the_adapter_returns_them(repo):
    """
    Defense-in-depth regression test: even when a marketplace adapter's
    own server-side date filter doesn't actually mean "order creation
    date" the way we assume - see marketplaces/tapsishop.py's
    dateFilterTypeCode caveat, explicitly flagged there as unconfirmed -
    and returns an order that's genuinely older than the `since` we
    asked for, the Sync Engine must never hand it to Didar. FakeAdapter
    ignores `since` entirely (like a buggy real filter would), so this
    old order is exactly what such a bug would hand back.
    """
    old_order = _order(
        "fake1", "old-1", with_items=True,
        created_at=datetime.now(timezone.utc) - timedelta(days=30),
    )
    adapter = FakeAdapter("fake1", list_orders=[old_order])
    didar = FakeDidarService()
    engine = SyncEngine(adapters=[adapter], repository=repo, didar_service=didar)

    engine.run_once()

    assert didar.synced_orders == []
    assert not repo.is_already_synced("fake1", "old-1")


def test_orders_at_or_after_since_still_sync_normally(repo):
    """Companion to the test above - the new floor must not accidentally
    swallow legitimately new orders."""
    _seed_watermark(repo, "fake1")
    fresh_order = _order("fake1", "fresh-1", with_items=True)  # created_at defaults to "now"
    adapter = FakeAdapter("fake1", list_orders=[fresh_order])
    didar = FakeDidarService()
    engine = SyncEngine(adapters=[adapter], repository=repo, didar_service=didar)

    engine.run_once()

    assert len(didar.synced_orders) == 1
    assert repo.is_already_synced("fake1", "fresh-1")
