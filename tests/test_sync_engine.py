from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from src.db.repository import Repository
from src.marketplaces.base import MarketplaceAdapter, NormalizedOrder, OrderItem
from src.sync_engine import SyncEngine


def _order(
    source: str,
    order_id: str,
    with_items: bool = False,
) -> NormalizedOrder:
    return NormalizedOrder(
        source=source,
        source_order_id=order_id,
        order_number=order_id,
        created_at=datetime.now(timezone.utc),
        total_price=Decimal("100000"),
        status="confirmed",
        items=[OrderItem(sku="s", title="t", quantity=1, unit_price=Decimal("1"),
                          final_price=Decimal("100000"))] if with_items else [],
    )


class FakeAdapter(MarketplaceAdapter):
    """In-memory stand-in for a real marketplace adapter - lets us test
    orchestration logic (dedupe, retries, isolation) without any HTTP."""

    def __init__(self, name: str, list_orders=None, details=None, fail_fetch=False,
                 sbs_customer_details=None, sbs_customer_details_fail=False,
                 shipment_details=None, shipment_details_fail=False):
        self.name = name
        self._list_orders = list_orders or []
        self._details = details or {}
        self._fail_fetch = fail_fetch
        self._sbs_customer_details = sbs_customer_details or {}
        self._sbs_customer_details_fail = sbs_customer_details_fail
        self._shipment_details = shipment_details or {}
        self._shipment_details_fail = shipment_details_fail
        self.fetch_new_orders_calls = 0
        self.fetch_order_detail_calls = 0
        self.fetch_sbs_customer_details_calls: list[str] = []
        self.fetch_shipment_details_calls: list[str] = []
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

    def fetch_sbs_customer_details(self, shipment_id: str) -> dict:
        self.fetch_sbs_customer_details_calls.append(shipment_id)
        if self._sbs_customer_details_fail:
            raise RuntimeError("simulated SBS customer details fetch failure")
        return self._sbs_customer_details.get(shipment_id, {
            "customer_full_name": None,
            "customer_mobile": None,
        })

    def fetch_shipment_details(self, shipment_id: str) -> dict:
        self.fetch_shipment_details_calls.append(shipment_id)
        if self._shipment_details_fail:
            raise RuntimeError("simulated shipment details fetch failure")
        return self._shipment_details.get(shipment_id, {
            "tracking_code": None,
            "shipping_cost": None,
        })


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


@pytest.fixture
def synced_ids_file(tmp_path):
    """Isolated tmp_path for the synced_ids.json tracking file so tests
    don't pollute the real data/ directory or each other's state."""
    return tmp_path / "synced_ids.json"


def test_new_order_gets_synced_and_marked(repo, synced_ids_file):
    adapter = FakeAdapter("fake1", list_orders=[_order("fake1", "1", with_items=True)])
    didar = FakeDidarService()
    engine = SyncEngine(
        adapters=[adapter],
        repository=repo,
        didar_service=didar,
        synced_ids_file_path=str(synced_ids_file),
    )

    engine.run_once()

    assert len(didar.synced_orders) == 1
    assert repo.is_already_synced("fake1", "1")


def test_new_order_marks_its_deal_notified(repo, synced_ids_file):
    """Regression test (2026-09 "any deal" Telegram poller - see
    src/didar/deal_poller.py): a deal SyncEngine creates itself must be
    marked notified up front, so DidarDealPoller's independent sweep of
    Didar never sends a second Telegram message for the same deal."""
    adapter = FakeAdapter("fake1", list_orders=[_order("fake1", "1", with_items=True)])
    didar = FakeDidarService()
    engine = SyncEngine(
        adapters=[adapter], repository=repo, didar_service=didar,
        synced_ids_file_path=str(synced_ids_file),
    )

    engine.run_once()

    assert repo.is_deal_notified("deal-1") is True


def test_retried_order_marks_its_deal_notified(repo, synced_ids_file):
    """Same guard as above, but via the retry path (retry_pending_failures())."""
    adapter = FakeAdapter(
        "fake1",
        list_orders=[_order("fake1", "1", with_items=True)],
        details={"1": _order("fake1", "1", with_items=True)},
    )
    didar = FakeDidarService(fail_once_for={"fake1:1"})
    engine = SyncEngine(
        adapters=[adapter], repository=repo, didar_service=didar,
        synced_ids_file_path=str(synced_ids_file),
    )

    # NOTE: deliberately call _sync_source() here, not run_once() - the
    # latter already includes a retry_pending_failures() pass at the end
    # (see SyncEngine.run_once()'s docstring: "every source, then a retry
    # pass"), which would immediately retry and succeed within this same
    # call, defeating the point of testing the two phases separately.
    engine._sync_source(adapter)  # first attempt fails and is recorded

    assert repo.is_deal_notified("deal-1") is False

    engine.retry_pending_failures()

    assert repo.is_deal_notified("deal-1") is True


def test_list_without_items_triggers_detail_fetch(repo, synced_ids_file):
    adapter = FakeAdapter(
        "fake1",
        list_orders=[_order("fake1", "1", with_items=False)],
        details={"1": _order("fake1", "1", with_items=True)},
    )
    didar = FakeDidarService()
    engine = SyncEngine(
        adapters=[adapter],
        repository=repo,
        didar_service=didar,
        synced_ids_file_path=str(synced_ids_file),
    )

    engine.run_once()

    assert adapter.fetch_order_detail_calls == 1
    assert len(didar.synced_orders[0].items) == 1


def test_duplicate_order_is_not_synced_twice(repo, synced_ids_file):
    """ID-based deduplication: an order with the same (platform, source_order_id)
    must never be synced twice, even if returned by the adapter multiple times."""
    adapter = FakeAdapter("fake1", list_orders=[_order("fake1", "1", with_items=True)])
    didar = FakeDidarService()
    engine = SyncEngine(
        adapters=[adapter],
        repository=repo,
        didar_service=didar,
        synced_ids_file_path=str(synced_ids_file),
    )

    engine.run_once()
    engine.run_once()  # same order returned again by the (fake) source

    assert len(didar.synced_orders) == 1  # not synced a second time


def test_one_source_failing_does_not_block_others(repo, synced_ids_file):
    """Each source is isolated in its own try/except so that a fetch failure
    on one adapter doesn't prevent others from syncing."""
    broken = FakeAdapter("broken", fail_fetch=True)
    healthy = FakeAdapter("healthy", list_orders=[_order("healthy", "1", with_items=True)])
    didar = FakeDidarService()
    engine = SyncEngine(
        adapters=[broken, healthy],
        repository=repo,
        didar_service=didar,
        synced_ids_file_path=str(synced_ids_file),
    )

    engine.run_once()

    assert len(didar.synced_orders) == 1
    assert didar.synced_orders[0].source == "healthy"
    # broken source must be cleanly skipped - its failure is logged and
    # the engine continues with other sources
    assert len(didar.synced_orders) == 1


def test_didar_failure_is_recorded_and_retried_successfully(repo, synced_ids_file):
    adapter = FakeAdapter(
        "fake1",
        list_orders=[_order("fake1", "1", with_items=True)],
        details={"1": _order("fake1", "1", with_items=True)},
    )
    didar = FakeDidarService(fail_once_for={"fake1:1"})
    engine = SyncEngine(
        adapters=[adapter],
        repository=repo,
        didar_service=didar,
        synced_ids_file_path=str(synced_ids_file),
    )

    # Call the fetch+sync phase directly (bypassing run_once's own
    # end-of-cycle retry pass) so the two phases can be verified separately.
    engine._sync_source(adapter)
    assert not repo.is_already_synced("fake1", "1")
    assert len(repo.get_pending_failures()) == 1

    engine.retry_pending_failures()  # second attempt succeeds

    assert repo.is_already_synced("fake1", "1")
    assert len(repo.get_pending_failures()) == 0


def test_run_once_self_heals_within_a_single_cycle(repo, synced_ids_file):
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
    engine = SyncEngine(
        adapters=[adapter],
        repository=repo,
        didar_service=didar,
        synced_ids_file_path=str(synced_ids_file),
    )

    engine.run_once()

    assert repo.is_already_synced("fake1", "1")
    assert len(repo.get_pending_failures()) == 0


def test_fetch_new_orders_called_with_since_5_hours_ago(repo, synced_ids_file):
    """Verify that run_once passes `since=now-5h` to adapters - the SyncEngine
    enforces the window, not the adapters. This was the root cause of a bug
    where Digikala's entire order history was synced because `since` was None."""
    adapter = FakeAdapter("fake1", list_orders=[])
    didar = FakeDidarService()
    engine = SyncEngine(
        adapters=[adapter],
        repository=repo,
        didar_service=didar,
        synced_ids_file_path=str(synced_ids_file),
    )

    before = datetime.now(timezone.utc)
    engine.run_once()
    after = datetime.now(timezone.utc)

    assert adapter.fetch_new_orders_calls == 1
    received_since = adapter.received_since_values[0]
    # SyncEngine passes since=now-5h; adapters that don't respect it
    # are guarded by SyncEngine's client-side window drop
    from src.sync_engine import FETCH_WINDOW_HOURS
    assert received_since is not None
    assert received_since < before
    # Allow a small skew for test execution: since should be no earlier than
    # (now - FETCH_WINDOW_HOURS) minus a few seconds of test execution time.
    assert received_since >= before - timedelta(hours=FETCH_WINDOW_HOURS) - timedelta(seconds=5)


def test_sliding_window_enforced_client_side_by_sync_engine(repo, synced_ids_file):
    """
    Verify the 5-hour sliding window: SyncEngine computes `since=now-5h`,
    passes it to the adapter, AND drops any returned order whose created_at
    predates the window client-side. This is the two-layer guard that prevents
    old orders from reaching Didar even when the adapter doesn't filter
    server-side (e.g. Digikala).
    """
    adapter = FakeAdapter("fake1", list_orders=[])
    didar = FakeDidarService()
    engine = SyncEngine(
        adapters=[adapter],
        repository=repo,
        didar_service=didar,
        synced_ids_file_path=str(synced_ids_file),
    )

    engine.run_once()
    engine.run_once()
    engine.run_once()

    assert adapter.fetch_new_orders_calls == 3
    # Every call receives a valid since=now-5h (not None)
    for received in adapter.received_since_values:
        assert received is not None


def test_already_synced_orders_are_skipped_from_file(repo, synced_ids_file):
    """Orders already in the file tracking set are not synced again.
    File-based dedup persists across runs.

    Pre-seeds the synced_ids.json file (via the injected path) with an ID,
    then verifies the engine skips that order."""
    import json as _json

    # Pre-seed the file with an ID that should be skipped
    synced_ids_file.write_text(_json.dumps(["fake1-pre-existing-1"]), encoding='utf-8')

    adapter = FakeAdapter("fake1", list_orders=[_order("fake1", "pre-existing-1", with_items=True)])
    didar = FakeDidarService()
    engine = SyncEngine(
        adapters=[adapter],
        repository=repo,
        didar_service=didar,
        synced_ids_file_path=str(synced_ids_file),
    )

    engine.run_once()

    # Not synced again - skipped by file-based dedup
    assert len(didar.synced_orders) == 0


def test_new_order_from_different_platform_syncs_normally(repo, synced_ids_file):
    """Orders from different platforms with the same source_order_id are
    treated as distinct (unique key is platform + source_order_id)."""
    # Same ID but different platforms
    order_fake1 = _order("platform-a", "order-123", with_items=True)
    order_fake2 = _order("platform-b", "order-123", with_items=True)

    adapter_a = FakeAdapter("platform-a", list_orders=[order_fake1])
    adapter_b = FakeAdapter("platform-b", list_orders=[order_fake2])
    didar = FakeDidarService()
    engine = SyncEngine(
        adapters=[adapter_a, adapter_b],
        repository=repo,
        didar_service=didar,
        synced_ids_file_path=str(synced_ids_file),
    )

    engine.run_once()

    # Both orders should sync (different platforms = different unique IDs)
    assert len(didar.synced_orders) == 2
    sync_ids = {o.source_order_id for o in didar.synced_orders}
    assert sync_ids == {"order-123"}
    # But they're from different platforms
    sources = {o.source for o in didar.synced_orders}
    assert sources == {"platform-a", "platform-b"}


def test_orders_older_than_5_hours_are_dropped_client_side(repo, synced_ids_file):
    """Client-side window enforcement: orders older than 5 hours are dropped
    even if the adapter returns them. This is the safety net for adapters like
    Digikala that don't filter server-side by date."""
    # Create an order with a created_at 10 hours ago (outside the 5h window)
    old_order = NormalizedOrder(
        source="fake1",
        source_order_id="old-order-1",
        order_number="old-order-1",
        created_at=datetime.now(timezone.utc) - timedelta(hours=10),
        total_price=Decimal("100000"),
        status="confirmed",
        items=[OrderItem(sku="s", title="t", quantity=1, unit_price=Decimal("1"),
                          final_price=Decimal("100000"))],
    )

    # Create a newer order (within the 5h window)
    new_order = NormalizedOrder(
        source="fake1",
        source_order_id="new-order-1",
        order_number="new-order-1",
        created_at=datetime.now(timezone.utc) - timedelta(hours=1),
        total_price=Decimal("100000"),
        status="confirmed",
        items=[OrderItem(sku="s", title="t", quantity=1, unit_price=Decimal("1"),
                          final_price=Decimal("100000"))],
    )

    adapter = FakeAdapter("fake1", list_orders=[old_order, new_order])
    didar = FakeDidarService()
    engine = SyncEngine(
        adapters=[adapter],
        repository=repo,
        didar_service=didar,
        synced_ids_file_path=str(synced_ids_file),
    )

    engine.run_once()

    # Only the new order should be synced; the old one dropped client-side
    assert len(didar.synced_orders) == 1
    assert didar.synced_orders[0].source_order_id == "new-order-1"

    # Verify the old order's unique_id was NOT saved to the file
    import json as _json
    content = synced_ids_file.read_text(encoding='utf-8')
    ids = set(_json.loads(content)) if content.strip() else set()
    assert "fake1-old-order-1" not in ids


def test_id_based_watermark_adapter_bypasses_the_created_at_window(repo, synced_ids_file):
    """An adapter that sets uses_id_based_watermark = True (Digikala, since
    the 2026-09 SBS migration - see digikala-sbs-migration-prompt.md) must
    have its orders kept regardless of created_at, since its own
    fetch_new_orders() already guarantees "new" via a persisted ID
    watermark rather than a time window."""
    very_old_order = NormalizedOrder(
        source="fake1",
        source_order_id="ancient-1",
        order_number="ancient-1",
        created_at=datetime.now(timezone.utc) - timedelta(days=60),
        total_price=Decimal("100000"),
        status="confirmed",
        items=[OrderItem(sku="s", title="t", quantity=1, unit_price=Decimal("1"),
                          final_price=Decimal("100000"))],
    )

    adapter = FakeAdapter("fake1", list_orders=[very_old_order])
    adapter.uses_id_based_watermark = True
    didar = FakeDidarService()
    engine = SyncEngine(
        adapters=[adapter],
        repository=repo,
        didar_service=didar,
        synced_ids_file_path=str(synced_ids_file),
    )

    engine.run_once()

    assert len(didar.synced_orders) == 1
    assert didar.synced_orders[0].source_order_id == "ancient-1"


def test_digikala_sbs_enrichment_adds_customer_name_and_mobile(repo, synced_ids_file):
    """Digikala SBS orders with shipment_id get enriched with customer data
    from fetch_sbs_customer_details before syncing to Didar."""
    # Create a Digikala order with shipment_id but no customer name
    digikala_order = NormalizedOrder(
        source="digikala",
        source_order_id="order-123",
        order_number="order-123",
        created_at=datetime.now(timezone.utc) - timedelta(hours=1),
        total_price=Decimal("100000"),
        status="confirmed",
        items=[OrderItem(sku="s", title="t", quantity=1, unit_price=Decimal("1"),
                          final_price=Decimal("100000"))],
        shipment_id="SHIP-123",
        customer_full_name=None,
        customer_mobile=None,
    )

    adapter = FakeAdapter(
        "digikala",
        list_orders=[digikala_order],
        sbs_customer_details={
            "SHIP-123": {
                "customer_full_name": "علی محمدی",
                "customer_mobile": "09123456789",
            }
        },
    )
    didar = FakeDidarService()
    engine = SyncEngine(
        adapters=[adapter],
        repository=repo,
        didar_service=didar,
        synced_ids_file_path=str(synced_ids_file),
    )

    engine.run_once()

    assert len(didar.synced_orders) == 1
    synced = didar.synced_orders[0]
    assert synced.customer_full_name == "علی محمدی"
    assert synced.customer_mobile == "09123456789"
    assert adapter.fetch_sbs_customer_details_calls == ["SHIP-123"]


def test_digikala_shipment_details_enrichment_adds_tracking_code_and_shipping_cost(
    repo, synced_ids_file
):
    """Digikala orders with shipment_id get enriched with tracking code +
    shipping cost from fetch_shipment_details before syncing to Didar
    (client request, 2026-09) - independent of the customer-name
    enrichment above (a different endpoint, gated on shipping_cost being
    unset rather than customer_full_name)."""
    digikala_order = NormalizedOrder(
        source="digikala",
        source_order_id="order-123",
        order_number="order-123",
        created_at=datetime.now(timezone.utc) - timedelta(hours=1),
        total_price=Decimal("100000"),
        status="confirmed",
        items=[OrderItem(sku="s", title="t", quantity=1, unit_price=Decimal("1"),
                          final_price=Decimal("100000"))],
        shipment_id="SHIP-123",
        # Real customer name already known, so the SBS customer
        # enrichment is skipped - shipment details must still run.
        customer_full_name="علی محمدی",
        customer_mobile="09123456789",
    )

    adapter = FakeAdapter(
        "digikala",
        list_orders=[digikala_order],
        shipment_details={
            "SHIP-123": {
                "tracking_code": "11234",
                "shipping_cost": Decimal("650000"),
            }
        },
    )
    didar = FakeDidarService()
    engine = SyncEngine(
        adapters=[adapter],
        repository=repo,
        didar_service=didar,
        synced_ids_file_path=str(synced_ids_file),
    )

    engine.run_once()

    assert len(didar.synced_orders) == 1
    synced = didar.synced_orders[0]
    assert synced.shipment_tracking_code == "11234"
    assert synced.shipping_cost == Decimal("650000")
    assert adapter.fetch_shipment_details_calls == ["SHIP-123"]
    # Customer name was already set, so that enrichment must NOT re-run.
    assert adapter.fetch_sbs_customer_details_calls == []


def test_digikala_sbs_enrichment_also_applies_on_retry(repo, synced_ids_file):
    """Regression test for a real production bug: retry_pending_failures()
    used to call self._didar.sync_order(order) directly, completely
    bypassing SBS enrichment - so ANY order that failed even once on its
    first attempt (for any reason, including one unrelated to Digikala/
    enrichment at all) would sync on retry with a synthetic name forever,
    never a real one. Confirmed live: every single Digikala order in
    production logs had gone through the retry path at least once, so
    enrichment had in effect never actually run for a real order.

    Uses two SEPARATE NormalizedOrder objects (one for the initial
    list_orders pass, one for the retry's own fetch_order_detail call) -
    both start with customer_full_name=None. This matters: retry always
    re-fetches the order fresh by id, so it must perform its OWN
    enrichment rather than relying on a mutation the first (failed)
    attempt happened to make on a shared object - the old bug would
    otherwise be masked by that coincidence instead of actually caught.
    """
    list_order = NormalizedOrder(
        source="digikala",
        source_order_id="order-123",
        order_number="order-123",
        created_at=datetime.now(timezone.utc) - timedelta(hours=1),
        total_price=Decimal("100000"),
        status="confirmed",
        items=[OrderItem(sku="s", title="t", quantity=1, unit_price=Decimal("1"),
                          final_price=Decimal("100000"))],
        shipment_id="SHIP-123",
        customer_full_name=None,
        customer_mobile=None,
    )
    # A distinct object (not the same instance as list_order) - simulates
    # a real retry, which always re-fetches the order by id rather than
    # reusing whatever object the first attempt happened to hold.
    retry_fetched_order = NormalizedOrder(**list_order.__dict__)

    adapter = FakeAdapter(
        "digikala",
        list_orders=[list_order],
        details={"order-123": retry_fetched_order},
        sbs_customer_details={
            "SHIP-123": {
                "customer_full_name": "علی محمدی",
                "customer_mobile": "09123456789",
            }
        },
    )
    # First attempt fails at the Didar push step itself (simulating any
    # transient/unrelated error there) - AFTER enrichment already ran
    # once on list_order. The order only ever actually reaches Didar via
    # the retry pass, using the separate retry_fetched_order object.
    didar = FakeDidarService(fail_once_for={"digikala:order-123"})
    engine = SyncEngine(
        adapters=[adapter],
        repository=repo,
        didar_service=didar,
        synced_ids_file_path=str(synced_ids_file),
    )

    engine.run_once()  # first attempt fails, then run_once's own retry pass fixes it

    assert repo.is_already_synced("digikala", "order-123")
    assert len(didar.synced_orders) == 1
    synced = didar.synced_orders[0]
    assert synced.customer_full_name == "علی محمدی"
    assert synced.customer_mobile == "09123456789"
    # Called once by the failing first attempt (on list_order) and once
    # more by the retry (on retry_fetched_order, a separate object that
    # still had customer_full_name=None) - proving the retry path does
    # its own enrichment rather than piggybacking on the first attempt.
    assert adapter.fetch_sbs_customer_details_calls == ["SHIP-123", "SHIP-123"]


def test_digikala_sbs_enrichment_falls_back_to_synthetic_name_on_failure(repo, synced_ids_file):
    """When SBS customer fetch fails, fallback to synthetic contact name."""
    digikala_order = NormalizedOrder(
        source="digikala",
        source_order_id="order-456",
        order_number="order-456",
        created_at=datetime.now(timezone.utc) - timedelta(hours=1),
        total_price=Decimal("100000"),
        status="confirmed",
        items=[OrderItem(sku="s", title="t", quantity=1, unit_price=Decimal("1"),
                          final_price=Decimal("100000"))],
        shipment_id="SHIP-456",
        customer_full_name=None,
        customer_mobile=None,
    )

    adapter = FakeAdapter(
        "digikala",
        list_orders=[digikala_order],
        sbs_customer_details_fail=True,  # Simulate API failure
    )
    didar = FakeDidarService()
    engine = SyncEngine(
        adapters=[adapter],
        repository=repo,
        didar_service=didar,
        synced_ids_file_path=str(synced_ids_file),
    )

    engine.run_once()

    assert len(didar.synced_orders) == 1
    synced = didar.synced_orders[0]
    assert synced.customer_full_name == "مشتری دیجی‌کالا (SHIP-456)"
    assert synced.customer_mobile is None
    assert adapter.fetch_sbs_customer_details_calls == ["SHIP-456"]


def test_digikala_sbs_enrichment_skipped_when_customer_name_already_exists(repo, synced_ids_file):
    """Enrichment should not run if customer_full_name is already populated
    (e.g. from a previous sync or re-fetch)."""
    digikala_order = NormalizedOrder(
        source="digikala",
        source_order_id="order-789",
        order_number="order-789",
        created_at=datetime.now(timezone.utc) - timedelta(hours=1),
        total_price=Decimal("100000"),
        status="confirmed",
        items=[OrderItem(sku="s", title="t", quantity=1, unit_price=Decimal("1"),
                          final_price=Decimal("100000"))],
        shipment_id="SHIP-789",
        customer_full_name="موجود از قبل",  # Already has a name
        customer_mobile="09999999999",
    )

    adapter = FakeAdapter(
        "digikala",
        list_orders=[digikala_order],
        sbs_customer_details={
            "SHIP-789": {
                "customer_full_name": "از API",
                "customer_mobile": "09888888888",
            }
        },
    )
    didar = FakeDidarService()
    engine = SyncEngine(
        adapters=[adapter],
        repository=repo,
        didar_service=didar,
        synced_ids_file_path=str(synced_ids_file),
    )

    engine.run_once()

    assert len(didar.synced_orders) == 1
    synced = didar.synced_orders[0]
    # Original name preserved, not overwritten by API
    assert synced.customer_full_name == "موجود از قبل"
    assert synced.customer_mobile == "09999999999"
    # fetch_sbs_customer_details should NOT have been called
    assert adapter.fetch_sbs_customer_details_calls == []


def test_non_digikala_orders_not_enriched(repo, synced_ids_file):
    """Only Digikala orders with shipment_id trigger SBS enrichment.
    Other platforms (basalam, tapsishop, etc.) are not enriched."""
    # Basalam order with shipment_id - should NOT be enriched
    basalam_order = NormalizedOrder(
        source="basalam",
        source_order_id="basalam-1",
        order_number="basalam-1",
        created_at=datetime.now(timezone.utc) - timedelta(hours=1),
        total_price=Decimal("100000"),
        status="confirmed",
        items=[OrderItem(sku="s", title="t", quantity=1, unit_price=Decimal("1"),
                          final_price=Decimal("100000"))],
        shipment_id="SHIP-BASALAM",
        customer_full_name=None,
        customer_mobile=None,
    )

    # SnappShop order with shipment_id - should NOT be enriched
    snappshop_order = NormalizedOrder(
        source="snappshop",
        source_order_id="snappshop-1",
        order_number="snappshop-1",
        created_at=datetime.now(timezone.utc) - timedelta(hours=1),
        total_price=Decimal("100000"),
        status="confirmed",
        items=[OrderItem(sku="s", title="t", quantity=1, unit_price=Decimal("1"),
                          final_price=Decimal("100000"))],
        shipment_id="SHIP-SNAPPSHOP",
        customer_full_name=None,
        customer_mobile=None,
    )

    adapter_basalam = FakeAdapter(
        "basalam",
        list_orders=[basalam_order],
        sbs_customer_details={
            "SHIP-BASALAM": {
                "customer_full_name": "از API بالسام",
                "customer_mobile": "09111111111",
            }
        },
    )
    adapter_snappshop = FakeAdapter(
        "snappshop",
        list_orders=[snappshop_order],
        sbs_customer_details={
            "SHIP-SNAPPSHOP": {
                "customer_full_name": "از API اسنپ‌شاپ",
                "customer_mobile": "09222222222",
            }
        },
    )
    didar = FakeDidarService()
    engine = SyncEngine(
        adapters=[adapter_basalam, adapter_snappshop],
        repository=repo,
        didar_service=didar,
        synced_ids_file_path=str(synced_ids_file),
    )

    engine.run_once()

    assert len(didar.synced_orders) == 2
    # Both orders should sync with synthetic names (since enrichment is skipped)
    sources = {o.source for o in didar.synced_orders}
    assert sources == {"basalam", "snappshop"}

    for order in didar.synced_orders:
        # Non-Digikala orders keep None if no customer data was provided
        assert order.customer_full_name is None
        assert order.customer_mobile is None

    # fetch_sbs_customer_details should NOT have been called for either
    assert adapter_basalam.fetch_sbs_customer_details_calls == []
    assert adapter_snappshop.fetch_sbs_customer_details_calls == []


def test_digikala_without_shipment_id_not_enriched(repo, synced_ids_file):
    """Digikala orders without shipment_id should not trigger SBS enrichment."""
    digikala_order = NormalizedOrder(
        source="digikala",
        source_order_id="order-no-shipment",
        order_number="order-no-shipment",
        created_at=datetime.now(timezone.utc) - timedelta(hours=1),
        total_price=Decimal("100000"),
        status="confirmed",
        items=[OrderItem(sku="s", title="t", quantity=1, unit_price=Decimal("1"),
                          final_price=Decimal("100000"))],
        shipment_id=None,  # No shipment_id
        customer_full_name=None,
        customer_mobile=None,
    )

    adapter = FakeAdapter(
        "digikala",
        list_orders=[digikala_order],
        sbs_customer_details={
            "SHIP-X": {
                "customer_full_name": "از API",
                "customer_mobile": "09888888888",
            }
        },
    )
    didar = FakeDidarService()
    engine = SyncEngine(
        adapters=[adapter],
        repository=repo,
        didar_service=didar,
        synced_ids_file_path=str(synced_ids_file),
    )

    engine.run_once()

    assert len(didar.synced_orders) == 1
    synced = didar.synced_orders[0]
    assert synced.customer_full_name is None
    assert synced.customer_mobile is None
    assert adapter.fetch_sbs_customer_details_calls == []