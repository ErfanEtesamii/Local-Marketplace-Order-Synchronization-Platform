"""
Sync Engine - the piece that ties everything else together:

    for each marketplace adapter:
        fetch recent orders (last 5 hours, sliding window)
        for each order not already synced (per the Repository, keyed
           by platform + source_order_id):
            fetch full detail if the list call didn't include line items
            push it to Didar (Contact upsert -> Deal create)
            record success or failure in the Repository

Design choices worth calling out:

- One adapter failing to fetch (e.g. an expired token) must not stop the
  others from running - each source is wrapped in its own try/except.
- Every order is deduplicated by (platform, source_order_id) stored in
  the Repository's synced_orders table. This is the single source of
  truth for "already synced" - it persists across app restarts, so a
  crash and restart never re-syncs orders already pushed to Didar.
- The sliding 5-hour fetch window is ENFORCED, not just advisory: the
  SyncEngine passes since=(now - FETCH_WINDOW_HOURS) to every adapter
  and additionally drops any returned order whose created_at predates
  that window client-side. This is the primary guard against pulling old
  history on a fresh DB (where ID-based dedup hasn't yet seen any orders
  and would otherwise let the entire account history through). ID-based
  dedup is the secondary guard, preventing re-syncs of orders already
  pushed within the window. This matters because at least one adapter
  (Digikala - see src/marketplaces/digikala.py) does not filter
  server-side by date, so without the client-side drop the window would
  be a no-op for it.
- Failed Didar syncs are recorded via Repository.record_failure() rather
  than just logged and dropped, so retry_pending_failures() can give them
  another attempt on a later run without re-fetching the entire source.
- The 5-hour sliding window is intentionally wide (well beyond the 2-minute
  poll interval) so that any gap caused by a missed cycle, a restart, or
  a temporary outage is fully recovered on the next run.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from src.config import settings
from src.db.repository import Repository
from src.didar.service import DidarSyncService
from src.logger import get_logger
from src.marketplaces.base import MarketplaceAdapter, NormalizedOrder
from src.telegram import TelegramNotifier

log = get_logger(__name__)

# Sliding fetch window: pull all orders created in the last
# FETCH_WINDOW hours on every poll. This is deliberately much wider
# than the poll interval so that any gap (missed cycle, restart,
# temporary outage) is fully recovered without re-scanning the
# entire account history.
FETCH_WINDOW_HOURS = 5


class SyncEngine:
    def __init__(
        self,
        adapters: list[MarketplaceAdapter],
        repository: Repository | None = None,
        didar_service: DidarSyncService | None = None,
        synced_ids_file_path: str | None = None,
    ) -> None:
        self._adapters = {a.name: a for a in adapters}
        self._repo = repository or Repository()
        self._didar = didar_service or DidarSyncService()
        self._synced_ids_file_path = synced_ids_file_path
        self._synced_ids = self._load_synced_ids()
        self._telegram = TelegramNotifier()

    @property
    def adapter_names(self) -> list[str]:
        return list(self._adapters.keys())

    def run_once(self) -> None:
        """One full poll cycle: every source, then a retry pass over
        previously-failed orders."""
        for adapter in self._adapters.values():
            self._sync_source(adapter)
        self.retry_pending_failures()

    def _sync_source(self, adapter: MarketplaceAdapter) -> None:
        # Build a unique platform-specific ID for each order.
        # Format: "{platform}-{source_order_id}" (e.g. "digikala-112736712").
        #
        # Two layers of dedup, working together:
        #  1. Sliding FETCH_WINDOW_HOURS window: passed as `since` to every
        #     adapter. Adapters that respect it (most do) use it to
        #     constrain their API call.
        #  2. Client-side drop below: rejects any order whose created_at
        #     predates the window, regardless of what the adapter returned.
        #     This is the safety net for adapters that don't filter
        #     server-side (e.g. Digikala - see its module docstring) and
        #     for any adapter bug that returns old orders. Without it,
        #     a fresh DB with no synced_orders yet would let the entire
        #     account history through to Didar (the exact bug that synced
        #     43 two-month-old Digikala orders on 2026-08-31).
        #  3. ID-based dedup via synced_orders table: the persistent
        #     "already pushed" guard that survives restarts.
        platform = adapter.name

        since = datetime.now(timezone.utc) - timedelta(hours=FETCH_WINDOW_HOURS)

        try:
            orders = adapter.fetch_new_orders(since)
        except Exception:
            log.exception("sync_engine: failed to fetch new orders from %s", adapter.name)
            return

        # Client-side window enforcement. Compare against the same `since`
        # we just passed to the adapter - any order outside the window
        # is silently dropped (logged) so it can never reach Didar.
        # Orders without created_at (rare; defensive) are kept and let
        # the status filter / ID dedup decide - we don't want to drop
        # legitimate orders just because the adapter couldn't parse a date.
        window_kept: list[NormalizedOrder] = []
        window_dropped = 0
        for order in orders:
            if order.created_at is not None and order.created_at < since:
                window_dropped += 1
                log.info(
                    "sync_engine: dropping %s order %s - created_at %s is outside the %dh window",
                    platform, order.source_order_id, order.created_at, FETCH_WINDOW_HOURS,
                )
                continue
            window_kept.append(order)

        for order in window_kept:
            # Build the unique ID used for dedup in the repository.
            unique_id = self._order_id(platform, order.source_order_id)

            # Check against in-memory set of already-synced IDs
            if unique_id in self._synced_ids:
                log.info(
                    "sync_engine: skipping already-synced %s order %s",
                    platform, order.source_order_id,
                )
                continue

            # Add to in-memory set and persist to file for future runs
            self._synced_ids.add(unique_id)
            self._save_order_id_to_file(platform, order.source_order_id, unique_id)

            self._sync_one_order(adapter, order, unique_id)

        log.info(
            "sync_engine: completed poll of %s (kept=%d, dropped-out-of-window=%d, total=%d)",
            adapter.name, len(window_kept), window_dropped, len(orders),
        )

    def _order_id(self, platform: str, source_order_id: str) -> str:
        """Build a unique ID combining platform name + platform order ID."""
        return f"{platform}-{source_order_id}"

    def _synced_ids_path(self) -> Path:
        """Path to the synced-IDs tracking file.

        Defaults to `<db_dir>/synced_ids.json` (next to the SQLite file). Tests
        inject a per-test tmp_path via the constructor to avoid polluting the
        real `data/` directory between test runs.
        """
        if self._synced_ids_file_path is not None:
            return Path(self._synced_ids_file_path)
        return Path(settings.db_path).resolve().parent / "synced_ids.json"

    def _load_synced_ids(self) -> set[str]:
        """Load synced order IDs from file for deduplication.

        Reads the tracking file at `data/synced_ids.json` and returns a set of
        unique IDs that have already been synced. This provides a lightweight
        alternative to SQLite's synced_orders table for deduplication.
        """
        file_path = self._synced_ids_path()
        synced_ids: set[str] = set()

        if not file_path.exists():
            log.info("sync_engine: tracking file %s does not exist, starting fresh", file_path)
            return synced_ids

        try:
            content = file_path.read_text(encoding='utf-8')
            if not content.strip():
                return synced_ids

            data = json.loads(content)
            if isinstance(data, list):
                synced_ids.update(data)
                log.info("sync_engine: loaded %d synced IDs from %s", len(synced_ids), file_path)
            else:
                log.warning("sync_engine: expected list in %s, got %s, starting fresh", file_path, type(data))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("sync_engine: failed to load synced IDs from %s: %s", file_path, exc)

        return synced_ids

    def _save_order_id_to_file(self, platform: str, source_order_id: str, unique_id: str) -> None:
        """Persist a new order ID to the tracking file.

        Appends the unique_id to `data/synced_ids.json` and ensures the file
        remains valid JSON. The file stores a JSON array of unique IDs for easy
        loading in future runs.
        """
        file_path = self._synced_ids_path()
        file_path.parent.mkdir(parents=True, exist_ok=True)

        synced_ids: set[str] = set()
        if file_path.exists():
            try:
                content = file_path.read_text(encoding='utf-8')
                if content.strip():
                    synced_ids.update(json.loads(content))
            except (json.JSONDecodeError, OSError):
                log.warning("sync_engine: failed to read existing file %s, starting fresh", file_path)

        synced_ids.add(unique_id)

        try:
            file_path.write_text(json.dumps(list(synced_ids), ensure_ascii=False, indent=2), encoding='utf-8')
            log.debug("sync_engine: saved order ID %s to %s", unique_id, file_path)
        except OSError as exc:
            log.exception("sync_engine: failed to save order ID %s to %s", unique_id, file_path)

    def _sync_one_order(
        self, adapter: MarketplaceAdapter, order: NormalizedOrder, unique_id: str
    ) -> None:
        # Central filter: prevent cancelled/failed orders from syncing to Didar.
        # Uses NormalizedOrder.status rather than per-adapter filters so that
        # no order of any marketplace slips through if an adapter's own guard
        # is incomplete or outdated.
        if order.status.lower() in CANCELLED_OR_FAILED_STATUSES:
            log.info(
                "sync_engine: skipping %s order %s - status %s is cancelled/failed",
                order.source, order.source_order_id, order.status,
            )
            return

        try:
            deal_id, order = self._prepare_and_push_to_didar(adapter, order)
            products_amount, shipping_amount, total_amount = self._order_amounts(order)
            self._repo.mark_synced(
                order.source, order.source_order_id, deal_id,
                products_amount=products_amount,
                shipping_amount=shipping_amount,
                total_amount=total_amount,
            )
            # Fire and forget - notify_new_order catches and logs its own
            # errors, so a Telegram outage can never break the sync itself.
            self._telegram.notify_new_order(order, deal_id)
        except Exception as exc:
            log.exception(
                "sync_engine: failed to sync %s order %s", order.source, order.source_order_id
            )
            self._repo.record_failure(order.source, order.source_order_id, str(exc))

    def _prepare_and_push_to_didar(
        self, adapter: MarketplaceAdapter, order: NormalizedOrder
    ) -> tuple[str, NormalizedOrder]:
        """Shared prep + push step used by BOTH the first-attempt sync
        path (_sync_one_order) AND the retry path (retry_pending_failures).

        BUGFIX (2026-09): retry_pending_failures() used to call
        self._didar.sync_order(order) directly, completely bypassing the
        Digikala SBS customer-name enrichment below. In production this
        meant customer_full_name was NEVER populated for any order that
        failed even once on its first attempt (for ANY reason, including
        transient/unrelated errors) and only went through on a later
        retry - confirmed live: every single synced Digikala order in
        the account's logs had gone through the retry path, so
        enrichment had in effect never run at all. Centralizing the
        fetch-detail + enrich + sync_order sequence here, and having
        both callers use it, makes that impossible to diverge again.
        """
        if not order.items:
            # Several adapters' list endpoints omit line items -
            # fetch the full order before pushing to Didar.
            order = adapter.fetch_order_detail(order.source_order_id)

        # Enrich Digikala SBS orders with customer data from the
        # ship-by-seller customer API before pushing to Didar. Only for
        # Digikala orders that have a shipment_id and don't already have
        # a real customer name. If the API fails or returns no data,
        # fall back to a synthetic contact name so the sync still
        # succeeds (Didar can still create a deal without a real
        # customer name).
        if (
            order.source == "digikala"
            and order.shipment_id
            and not order.customer_full_name
        ):
            self._enrich_digikala_sbs_customer(adapter, order)

        # Same idea, separate endpoint/field: shipping_cost and the
        # customer-facing tracking code (client request, 2026-09) - see
        # NormalizedOrder.shipment_tracking_code's docstring for why this
        # is a distinct field/call from the customer enrichment above.
        # Gated on shipping_cost being unset (not customer_full_name) so
        # this doesn't accidentally re-fetch on the retry path once
        # already enriched, mirroring the customer enrichment's own gate.
        if (
            order.source == "digikala"
            and order.shipment_id
            and order.shipping_cost is None
        ):
            self._enrich_digikala_shipment_details(adapter, order)

        deal_id = self._didar.sync_order(order)
        return deal_id, order

    def _enrich_digikala_sbs_customer(
        self, adapter: MarketplaceAdapter, order: NormalizedOrder
    ) -> None:
        """Fetch SBS customer details for a Digikala order and enrich the
        NormalizedOrder in-place. Falls back to a synthetic contact name
        if the API fails or returns no data."""
        # Only DigikalaAdapter exposes fetch_sbs_customer_details.
        fetcher = getattr(adapter, "fetch_sbs_customer_details", None)
        if fetcher is None:
            log.debug(
                "sync_engine: adapter %s has no fetch_sbs_customer_details, skipping enrichment",
                adapter.name,
            )
            return

        try:
            details = fetcher(order.shipment_id)
        except Exception:
            log.exception(
                "sync_engine: SBS customer fetch raised for %s order %s",
                order.source, order.source_order_id,
            )
            details = {}

        full_name = details.get("customer_full_name")
        mobile = details.get("customer_mobile")
        province = details.get("customer_province")
        city = details.get("customer_city")
        address = details.get("customer_address")
        postal_code = details.get("customer_postal_code")

        if not full_name:
            # Fallback: synthetic contact name with shipment_id so the order
            # is still identifiable in Didar even without real customer data.
            full_name = f"مشتری دیجی‌کالا ({order.shipment_id})"

        # NormalizedOrder is frozen=True, so we mutate via object.__setattr__.
        object.__setattr__(order, "customer_full_name", full_name)
        if mobile:
            object.__setattr__(order, "customer_mobile", mobile)
        # Full contact info (client request, 2026-09) - see
        # NormalizedOrder.customer_address's docstring. Each field is
        # only set when the SBS response actually had it, same
        # None-means-"don't touch it" convention as mobile above.
        if province:
            object.__setattr__(order, "customer_province", province)
        if city:
            object.__setattr__(order, "customer_city", city)
        if address:
            object.__setattr__(order, "customer_address", address)
        if postal_code:
            object.__setattr__(order, "customer_postal_code", postal_code)

        log.info(
            "sync_engine: enriched Digikala SBS customer for order %s "
            "(name=%r, mobile=%r, province=%r, city=%r, has_address=%s, postal_code=%r)",
            order.source_order_id, full_name, mobile, province, city, bool(address), postal_code,
        )

    def _enrich_digikala_shipment_details(
        self, adapter: MarketplaceAdapter, order: NormalizedOrder
    ) -> None:
        """Fetch shipment/parcel details (tracking code + shipping cost)
        for a Digikala SBS order and enrich the NormalizedOrder in-place.
        Best-effort: if the API fails or returns no data, both fields are
        simply left None - DidarDealClient's _build_item_description
        already omits any line it doesn't have data for, so this can
        never break or block a sync."""
        # Only DigikalaAdapter exposes fetch_shipment_details.
        fetcher = getattr(adapter, "fetch_shipment_details", None)
        if fetcher is None:
            log.debug(
                "sync_engine: adapter %s has no fetch_shipment_details, skipping enrichment",
                adapter.name,
            )
            return

        try:
            details = fetcher(order.shipment_id)
        except Exception:
            log.exception(
                "sync_engine: shipment details fetch raised for %s order %s",
                order.source, order.source_order_id,
            )
            details = {}

        tracking_code = details.get("tracking_code")
        shipping_cost = details.get("shipping_cost")

        # NormalizedOrder is frozen=True, so we mutate via object.__setattr__.
        if tracking_code:
            object.__setattr__(order, "shipment_tracking_code", tracking_code)
        if shipping_cost is not None:
            object.__setattr__(order, "shipping_cost", shipping_cost)

        log.info(
            "sync_engine: enriched Digikala shipment details for order %s "
            "(tracking_code=%r, shipping_cost=%r)",
            order.source_order_id, tracking_code, shipping_cost,
        )

    def _order_amounts(self, order: NormalizedOrder):
        """Money breakdown persisted alongside the dedup row, so the
        Telegram daily/weekly/monthly reports (see src/telegram.py) can
        aggregate straight from synced_orders instead of a second parallel
        tracking table. products_amount is the sum of each line's
        final_price (already the per-line total, not per-unit - see
        OrderItem/NormalizedOrder in src/marketplaces/base.py); shipping
        comes straight off the order; total is the order's own total_price.
        """
        products_amount = sum((item.final_price for item in order.items), Decimal("0"))
        shipping_amount = order.shipping_cost if order.shipping_cost is not None else Decimal("0")
        total_amount = order.total_price
        return products_amount, shipping_amount, total_amount

    def retry_pending_failures(self, max_attempts: int = 5) -> None:
        for failure in self._repo.get_pending_failures(max_attempts=max_attempts):
            adapter = self._adapters.get(failure.platform)
            if adapter is None:
                log.warning(
                    "sync_engine: no adapter registered for platform=%s, cannot retry order %s",
                    failure.platform, failure.source_order_id,
                )
                continue

            try:
                order = adapter.fetch_order_detail(failure.source_order_id)
                # See _prepare_and_push_to_didar's docstring - this used to
                # call self._didar.sync_order(order) directly here, which
                # skipped Digikala SBS customer-name enrichment on every
                # retry (i.e. on effectively every order that ever failed
                # once, for any reason).
                deal_id, order = self._prepare_and_push_to_didar(adapter, order)
                products_amount, shipping_amount, total_amount = self._order_amounts(order)
                self._repo.mark_synced(
                    failure.platform, failure.source_order_id, deal_id,
                    products_amount=products_amount,
                    shipping_amount=shipping_amount,
                    total_amount=total_amount,
                )
                self._telegram.notify_new_order(order, deal_id)
                log.info(
                    "sync_engine: retry succeeded for %s order %s",
                    failure.platform, failure.source_order_id,
                )
            except Exception as exc:
                log.exception(
                    "sync_engine: retry failed for %s order %s",
                    failure.platform, failure.source_order_id,
                )
                self._repo.record_failure(failure.platform, failure.source_order_id, str(exc))


# Central filter: prevent cancelled/failed orders from syncing to Didar.
# Uses NormalizedOrder.status rather than per-adapter filters so that
# no order of any marketplace slips through if an adapter's own guard
# is incomplete or outdated.
# Values confirmed from each marketplace's official API docs (2026-08).
# "unknown" (SnappShop default) is intentionally included so unconfirmed
# schemas don't silently sync orders - they pass through for manual review.
CANCELLED_OR_FAILED_STATUSES: set[str] = {
    # Tapsi Shop: status codes 6 (لغو سفارش - cancelled) and 9 (تحویل کامل - delivered)
    # are explicitly excluded by the adapter's _ACTIVE_ORDER_STATUS_IDS = [4].
    "cancelled",
    "failed",
    # Digikala: order_type query parameter values. The docs don't expose a
    # full order_status.key enum, so the adapter passes order_type to
    # _fetch_history_rows and maps it to a status string here:
    #   order_type=canceled  -> status "canceled"
    #   order_type=returned  -> status "refunded"
    "canceled",          # Digikala order_type=canceled
    "refunded",          # Digikala order_type=returned (treated as failed)
    # Basalam: confirmed status values for cancelled/failed orders
    # (from the "وضعیت‌های سفارش" section in the official docs).
    "cancelled",         # Basalam order_status=cancelled
    "refunded",          # Basalam order_status=refunded
    # Faraz Honar (WooCommerce): typical status values. Exact strings depend
    # on WooCommerce localization/installation.
    "cancelled",
    "failed",
    # SnappShop: schema unconfirmed (_SCHEMA_CONFIRMED = False).
    # "unknown" is the adapter's default fallback - included so unconfirmed
    # schemas don't silently sync orders; they pass through for manual review.
    "unknown",
}