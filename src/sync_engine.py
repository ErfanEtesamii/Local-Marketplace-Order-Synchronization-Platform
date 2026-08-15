"""
Sync Engine - the piece that ties everything else together:

    for each marketplace adapter:
        fetch new orders since the last successful check for that source
        for each order not already synced (per the Repository):
            fetch full detail if the list call didn't include line items
            push it to Didar (Contact upsert -> Deal create)
            record success or failure in the Repository

Design choices worth calling out:

- One adapter failing to fetch (e.g. an expired token) must not stop the
  others from running - each source is wrapped in its own try/except.
- The "since" watermark per source is read from and written back to the
  Repository (sync_state table), so a restart resumes from where it left
  off rather than re-scanning everything or missing a gap. A small
  overlap margin is subtracted when advancing the watermark, to guard
  against clock skew / API latency right at the boundary - any order
  that gets fetched twice because of this is caught for free by the
  Repository's is_already_synced() dedupe check, so the overlap costs
  nothing but redundant API calls.
- Failed Didar syncs are recorded via Repository.record_failure() rather
  than just logged and dropped, so retry_pending_failures() can give them
  another attempt on a later run without re-fetching the entire source.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.db.repository import Repository
from src.didar.service import DidarSyncService
from src.logger import get_logger
from src.marketplaces.base import MarketplaceAdapter, NormalizedOrder

log = get_logger(__name__)

DEFAULT_LOOKBACK = timedelta(days=1)     # used only when a source has no prior sync_state
WATERMARK_OVERLAP = timedelta(minutes=10)  # safety margin - see module docstring


class SyncEngine:
    def __init__(
        self,
        adapters: list[MarketplaceAdapter],
        repository: Repository | None = None,
        didar_service: DidarSyncService | None = None,
    ) -> None:
        self._adapters = {a.name: a for a in adapters}
        self._repo = repository or Repository()
        self._didar = didar_service or DidarSyncService()

    def run_once(self) -> None:
        """One full poll cycle: every source, then a retry pass over
        previously-failed orders."""
        for adapter in self._adapters.values():
            self._sync_source(adapter)
        self.retry_pending_failures()

    def _sync_source(self, adapter: MarketplaceAdapter) -> None:
        since = self._repo.get_last_sync_time(adapter.name) or (
            datetime.now(timezone.utc) - DEFAULT_LOOKBACK
        )
        cycle_started_at = datetime.now(timezone.utc)

        try:
            orders = adapter.fetch_new_orders(since)
        except Exception:
            log.exception("sync_engine: failed to fetch new orders from %s", adapter.name)
            return  # don't advance the watermark - retry this same window next cycle

        for order in orders:
            self._sync_one_order(adapter, order)

        # Advance the watermark with a safety overlap - see module docstring.
        self._repo.set_last_sync_time(adapter.name, cycle_started_at - WATERMARK_OVERLAP)
        log.info("sync_engine: completed poll of %s (%d orders seen)", adapter.name, len(orders))

    def _sync_one_order(self, adapter: MarketplaceAdapter, order: NormalizedOrder) -> None:
        if self._repo.is_already_synced(order.source, order.source_order_id):
            return

        try:
            if not order.items:
                # Several adapters' list endpoints omit line items -
                # fetch the full order before pushing to Didar.
                order = adapter.fetch_order_detail(order.source_order_id)

            deal_id = self._didar.sync_order(order)
            self._repo.mark_synced(order.source, order.source_order_id, deal_id)
        except Exception as exc:
            log.exception(
                "sync_engine: failed to sync %s order %s", order.source, order.source_order_id
            )
            self._repo.record_failure(order.source, order.source_order_id, str(exc))

    def retry_pending_failures(self, max_attempts: int = 5) -> None:
        for failure in self._repo.get_pending_failures(max_attempts=max_attempts):
            adapter = self._adapters.get(failure.source)
            if adapter is None:
                log.warning(
                    "sync_engine: no adapter registered for source=%s, cannot retry order %s",
                    failure.source, failure.source_order_id,
                )
                continue

            try:
                order = adapter.fetch_order_detail(failure.source_order_id)
                deal_id = self._didar.sync_order(order)
                self._repo.mark_synced(order.source, order.source_order_id, deal_id)
                log.info(
                    "sync_engine: retry succeeded for %s order %s",
                    failure.source, failure.source_order_id,
                )
            except Exception as exc:
                log.exception(
                    "sync_engine: retry failed for %s order %s",
                    failure.source, failure.source_order_id,
                )
                self._repo.record_failure(failure.source, failure.source_order_id, str(exc))
