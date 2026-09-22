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
  pushed within the window.
  EXCEPTION: adapters that set `uses_id_based_watermark = True` (Digikala,
  since its 2026-09 migration to a shipmentId watermark - see
  src/marketplaces/digikala.py and digikala-sbs-migration-prompt.md) skip
  this window entirely; their own fetch_new_orders() already guarantees
  "new" via a persistent, monotonic ID cursor, which the old time-window
  approach could never do safely for this adapter in the first place
  (Digikala's history endpoint doesn't filter server-side by date at all,
  and even a correctly-filtering endpoint's window is inherently a
  double-edged tradeoff - too wide risks re-syncing old orders, too
  narrow silently drops orders Digikala's backend was slow to record).
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
from src.didar.activity_client import DidarActivityClient
from src.didar.service import DidarSyncService
from src.didar.warehouse_service import DidarWarehouseSyncService
from src.express_alert import is_express_order
from src.logger import get_logger
from src.marketplaces.base import MarketplaceAdapter, NormalizedOrder
from src.modir_payamak import ModirPayamakNotifier
from src.telegram import TelegramNotifier

log = get_logger(__name__)

# SnappShop order-type Didar note text (see "طبقه‌بندی انواع سفارش اسنپ‌شاپ"
# prompt / _add_snappshop_warehouse_note below). Express text is per
# vendor account (snappshop=Tehran, snappshop2=Isfahan); every other
# SnappShop order - i.e. everything is_express_order() doesn't positively
# identify as express - gets the single warehouse text below, with no
# further condition (see the prompt's "زمینه‌ی تصمیم" section for why the
# destination-based split is not implemented yet).
_SNAPPSHOP_EXPRESS_NOTE_TEXT: dict[str, str] = {
    "snappshop": "اسنپ اکسپرس : تهران",
    "snappshop2": "اسنپ اکسپرس : اصفهان",
}
_SNAPPSHOP_WAREHOUSE_NOTE_TEXT = "ارسال به انبار"

# Sliding fetch window: pull all orders created in the last
# FETCH_WINDOW hours on every poll. This is deliberately much wider
# than the poll interval so that any gap (missed cycle, restart,
# temporary outage) is fully recovered without re-scanning the
# entire account history.
FETCH_WINDOW_HOURS = 5

# Adapters that set `fetches_by_modified_time = True` (currently Faraz Honar)
# ask their API for orders MODIFIED inside the sliding window, not CREATED
# inside it - so an order that was created long ago but only just changed
# status (e.g. WooCommerce "pending" -> "processing" once the customer pays)
# is still returned. For these adapters the created_at window check below is
# skipped (it would wrongly drop - and permanently ignore - exactly those
# orders), and this age cap is used instead purely as a safety net against
# old history flooding Didar (e.g. after a lost synced_ids.json). Kept
# generous on purpose: a pre-invoice ("پیش‌فاکتور") order can sit unpaid for
# weeks and must still sync the moment it becomes "processing".
MODIFIED_WINDOW_MAX_ORDER_AGE_DAYS = 90


class SyncEngine:
    def __init__(
        self,
        adapters: list[MarketplaceAdapter],
        repository: Repository | None = None,
        didar_service: DidarSyncService | None = None,
        synced_ids_file_path: str | None = None,
        sms_notifier: ModirPayamakNotifier | None = None,
        warehouse_adapters: list | None = None,
        warehouse_service: DidarWarehouseSyncService | None = None,
        didar_activity_client: DidarActivityClient | None = None,
    ) -> None:
        self._adapters = {a.name: a for a in adapters}
        self._repo = repository or Repository()
        self._didar = didar_service or DidarSyncService()
        self._synced_ids_file_path = synced_ids_file_path
        self._synced_ids = self._load_synced_ids()
        # (unique_id, reason) pairs already logged as "skipped, will be
        # re-checked" - keeps the log to one line per order+status instead
        # of one line per 2-minute poll. In-memory only; a restart may log
        # each currently-pending order once more, which is harmless.
        self._recheck_skip_logged: set[tuple[str, str]] = set()
        self._telegram = TelegramNotifier()
        # Express-order SMS alert (2026-09 - see src/modir_payamak.py).
        # Injectable, unlike self._telegram above, purely so tests can
        # pass a fake without touching env vars or the network; the
        # default is the same "construct our own, it's cheap and
        # config-gated" pattern the Telegram notifier uses.
        self._sms_notifier = sms_notifier or ModirPayamakNotifier()
        # SnappShop order-type Didar note (2026-11 - see
        # _add_snappshop_warehouse_note below). Injectable for the same
        # reason as self._sms_notifier: tests can pass a fake without
        # touching the network; default-constructing DidarActivityClient()
        # is cheap (no network call happens until a request is actually
        # made, same as self._didar/DidarSyncService's own default).
        self._didar_activity_client = didar_activity_client or DidarActivityClient()
        # Digikala FBD ("ارسال به انبار دیجی‌کالا", 2026-09) - kept in its
        # own dict, NEVER merged into self._adapters: an FBD item is not a
        # NormalizedOrder (see marketplaces/warehouse_base.py), it is
        # driven through its own loop/method below, and its name must stay
        # out of adapter_names (main.py relies on that - see its comment
        # at the warehouse_adapters wiring site - to keep check_health()
        # and the Telegram reports untouched by this source).
        self._warehouse_adapters = {a.name: a for a in (warehouse_adapters or [])}
        self._warehouse_service = warehouse_service or (
            DidarWarehouseSyncService() if self._warehouse_adapters else None
        )

    @property
    def adapter_names(self) -> list[str]:
        return list(self._adapters.keys())

    @property
    def warehouse_adapter_names(self) -> list[str]:
        return list(self._warehouse_adapters.keys())

    def run_once(self) -> None:
        """One full poll cycle: every customer-order source, every FBD
        warehouse source, then a retry pass over previously-failed
        customer orders. (The warehouse source has no retry queue of its
        own - see _sync_warehouse_source's docstring.)"""
        for adapter in self._adapters.values():
            self._sync_source(adapter)
        for warehouse_adapter in self._warehouse_adapters.values():
            self._sync_warehouse_source(warehouse_adapter)
        self.retry_pending_failures()

    def _sync_source(self, adapter: MarketplaceAdapter) -> None:
        # Build a unique platform-specific ID for each order.
        # Format: "{platform}-{source_order_id}" (e.g. "digikala-112736712").
        #
        # Three layers of dedup, working together:
        #  0. Permanent skip-list (ignored_orders table): any id this
        #     source has EVER had window-dropped before is filtered out
        #     immediately, before any other check. Added 2026-09 because
        #     some sources (pre-watermark Digikala being the motivating
        #     case - see digikala_shipment_watermark's docstring in
        #     repository.py) keep re-serving the exact same old orders on
        #     every single poll, so without this the window-drop below
        #     would re-evaluate (and re-log) the same ids forever.
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
        #     43 two-month-old Digikala orders on 2026-08-31). Anything
        #     dropped here is fed into the skip-list above so it's never
        #     evaluated again.
        #  3. ID-based dedup via synced_orders table: the persistent
        #     "already pushed" guard that survives restarts.
        platform = adapter.name

        since = datetime.now(timezone.utc) - timedelta(hours=FETCH_WINDOW_HOURS)

        try:
            orders = adapter.fetch_new_orders(since)
        except Exception:
            log.exception("sync_engine: failed to fetch new orders from %s", adapter.name)
            return

        # Layer 0: drop anything already on the permanent skip-list before
        # it reaches the window check (or any log line) below.
        ignored_ids = self._repo.get_ignored_ids(platform)
        if ignored_ids:
            orders = [order for order in orders if order.source_order_id not in ignored_ids]

        # Client-side window enforcement. Compare against the same `since`
        # we just passed to the adapter - any order outside the window
        # is silently dropped (logged) so it can never reach Didar.
        # Orders without created_at (rare; defensive) are kept and let
        # the status filter / ID dedup decide - we don't want to drop
        # legitimate orders just because the adapter couldn't parse a date.
        #
        # BYPASS for adapters with an ID-based watermark (currently only
        # Digikala - see its module docstring and
        # digikala-sbs-migration-prompt.md): their fetch_new_orders()
        # already guarantees "new" via a monotonic ID cursor, so a
        # created_at comparison here is redundant at best. It would
        # actively be WRONG for Digikala specifically, since its
        # orderDate is a Jalali date with no time component (see
        # _parse_jalali_date in digikala.py) - a poor sub-hour freshness
        # signal that could wrongly drop a legitimately new shipment
        # whose local calendar date happens to fall just outside the
        # window's clock boundary.
        if getattr(adapter, "uses_id_based_watermark", False):
            window_kept: list[NormalizedOrder] = list(orders)
            window_dropped = 0
        elif getattr(adapter, "fetches_by_modified_time", False):
            # The adapter already filtered server-side by MODIFIED time, so
            # created_at says nothing about freshness here (see
            # MODIFIED_WINDOW_MAX_ORDER_AGE_DAYS). Too-old orders are just
            # skipped this poll - NOT added to the permanent ignore list.
            max_age_cutoff = datetime.now(timezone.utc) - timedelta(
                days=MODIFIED_WINDOW_MAX_ORDER_AGE_DAYS
            )
            window_kept = []
            window_dropped = 0
            for order in orders:
                if order.created_at is not None and order.created_at < max_age_cutoff:
                    window_dropped += 1
                    self._log_recheck_skip_once(
                        self._order_id(platform, order.source_order_id),
                        "too-old",
                        "sync_engine: skipping %s order %s - created_at %s is older than %d days",
                        platform, order.source_order_id, order.created_at,
                        MODIFIED_WINDOW_MAX_ORDER_AGE_DAYS,
                    )
                    continue
                window_kept.append(order)
        else:
            window_kept = []
            window_dropped = 0
            newly_ignored_ids: list[str] = []
            for order in orders:
                # BUGFIX (2026-09-21): this comparison used to run
                # unguarded. A single order with a bad created_at (e.g.
                # the offset-naive-vs-offset-aware TypeError from
                # SnappShop's _parse_date - see that module's docstring)
                # raised out of this loop entirely, silently aborting the
                # rest of the poll cycle for this source: no exception
                # logged (only the first run after a restart had a
                # try/except around it), no orders synced, and the
                # source's set_last_sync_time() below never reached - for
                # 19+ hours, every 2 minutes, until the bad order aged
                # out of the fetch window and stopped being returned at
                # all. One bad order must never take the rest of this
                # source's batch down with it.
                try:
                    is_outside_window = (
                        order.created_at is not None and order.created_at < since
                    )
                except Exception:
                    log.exception(
                        "sync_engine: could not evaluate window for %s order %s "
                        "(created_at=%r) - keeping it so status/ID dedup can decide",
                        platform, order.source_order_id, order.created_at,
                    )
                    window_kept.append(order)
                    continue
                if is_outside_window:
                    window_dropped += 1
                    newly_ignored_ids.append(order.source_order_id)
                    log.info(
                        "sync_engine: dropping %s order %s - created_at %s is outside the %dh window",
                        platform, order.source_order_id, order.created_at, FETCH_WINDOW_HOURS,
                    )
                    continue
                window_kept.append(order)

            # Feed this pass's drops into the permanent skip-list so the
            # next poll never re-fetches/re-evaluates/re-logs them again.
            if newly_ignored_ids:
                self._repo.add_ignored_ids(platform, newly_ignored_ids, reason="outside_window")
                log.info(
                    "sync_engine: permanently ignoring %d out-of-window %s order(s) going forward",
                    len(newly_ignored_ids), platform,
                )

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

            # BUGFIX (2026-09): for allow-list sources (Faraz Honar) a status
            # rejection is NOT final - a WooCommerce order arrives as
            # "pending"/"on-hold" and becomes "processing" once paid. The id
            # used to be added to _synced_ids BEFORE the status check inside
            # _sync_one_order, so a "pending" order was marked synced on
            # first sight and skipped as "already-synced" forever, even
            # after it became "processing" (e.g. Faraz Honar #43870).
            # Now such an order is left un-marked and simply re-evaluated on
            # the next poll.
            if platform in ALLOWED_STATUSES:
                rejection = self._status_rejection(order)
                if rejection is not None:
                    self._log_recheck_skip_once(
                        unique_id, order.status.lower(),
                        "sync_engine: skipping %s order %s - %s (will re-check on next poll)",
                        platform, order.source_order_id, rejection,
                    )
                    continue

            # Add to in-memory set and persist to file for future runs
            self._synced_ids.add(unique_id)
            self._save_order_id_to_file(platform, order.source_order_id, unique_id)

            self._sync_one_order(adapter, order, unique_id)

        # Record that this poll cycle completed successfully, regardless
        # of whether any orders were found - this is what src/reporting.py
        # (check_health / the daily report) reads via get_last_sync_time()
        # to decide whether a source looks stuck. This used to be the
        # dedup watermark itself, but sync_engine.py moved to the
        # ID-based dedup above (_synced_ids / synced_orders) some time
        # ago and NOTHING has called set_last_sync_time() since - so
        # every source has looked permanently "stale" in the health check
        # ever since that migration, even while syncing correctly. This
        # table is now used PURELY as a "last completed poll" timestamp
        # for reporting, decoupled from dedup.
        self._repo.set_last_sync_time(platform, datetime.now(timezone.utc))

        log.info(
            "sync_engine: completed poll of %s (kept=%d, dropped-out-of-window=%d, total=%d)",
            adapter.name, len(window_kept), window_dropped, len(orders),
        )

    def _sync_warehouse_source(self, adapter) -> None:
        """One poll of a Digikala FBD ("ارسال به انبار دیجی‌کالا") source.

        Deliberately separate from _sync_source above, not a branch
        inside it - see this class's __init__ comment on
        self._warehouse_adapters:

        - No FETCH_WINDOW_HOURS sliding window and no
          set_last_sync_time() call here. For this source,
          get_last_sync_time()/set_last_sync_time() ARE the adapter's own
          created-at floor (see digikala_warehouse.py's COLD START
          section) - the adapter reads and advances it itself inside
          fetch_new_warehouse_shipments(). If this method also called
          set_last_sync_time(platform, now()) the way _sync_source does
          for reporting purposes, it would silently advance that same
          floor and start dropping items the adapter had not yet had a
          chance to sync.
        - Dedup is the single synced_warehouse_shipments check below, not
          the in-memory/synced_ids.json set _sync_source uses - a
          completely separate table (see repository.py) because an FBD
          item is not a customer order and must never be counted,
          reported on, or retried as one.
        - No retry queue: a Deal-creation failure here is simply never
          marked synced, so the item is seen again (and retried) on the
          very next poll, since the adapter keeps returning it for as
          long as it stays on Digikala's active list. There is nothing
          for retry_pending_failures() to do for this source.
        """
        try:
            items = adapter.fetch_new_warehouse_shipments()
        except Exception:
            log.exception(
                "sync_engine: failed to fetch new FBD shipments from %s", adapter.name
            )
            return

        for item in items:
            if self._repo.is_warehouse_shipment_synced(item.source, item.source_shipment_id):
                log.info(
                    "sync_engine: skipping already-synced %s FBD item %s",
                    item.source, item.source_shipment_id,
                )
                continue

            try:
                deal_id = self._warehouse_service.sync_shipment(item)
            except Exception:
                log.exception(
                    "sync_engine: failed to sync %s FBD item %s to Didar - will retry "
                    "on the next poll (Digikala keeps serving active items)",
                    item.source, item.source_shipment_id,
                )
                continue

            # Marked notified BEFORE DidarDealPoller can ever see this same
            # Deal.Id - same reasoning as _sync_one_order() above. Without
            # this, the FBD deal (still in the customer-order pipeline, per
            # create_warehouse_shipment_deal()'s docstring) is invisible to
            # DidarDealPoller's dedup, so it gets treated as an
            # unrecognized/manual entry and sent through
            # notify_new_deal()'s "ثبت دستی در دیدار" template instead of
            # being silently skipped - confirmed in production (deal #6108,
            # 2026-09).
            self._repo.mark_deal_notified(deal_id)

            self._repo.mark_warehouse_shipment_synced(
                item.source,
                item.source_shipment_id,
                deal_id,
                total_amount=item.unit_price * item.quantity,
            )

        log.info(
            "sync_engine: completed FBD poll of %s (%d item(s) seen)",
            adapter.name, len(items),
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

    @staticmethod
    def _status_rejection(order: NormalizedOrder) -> str | None:
        """Why this order's status keeps it out of Didar, or None if OK.

        Single source of truth for the ALLOWED_STATUSES allow-list and the
        CANCELLED_OR_FAILED_STATUSES blacklist, used both by _sync_source
        (to decide whether to mark an id as synced) and _sync_one_order.
        """
        status = order.status.lower()
        allowed = ALLOWED_STATUSES.get(order.source)
        if allowed is not None:
            if status not in allowed:
                return f"status {order.status} is not in the allowed set {sorted(allowed)}"
            return None
        if status in CANCELLED_OR_FAILED_STATUSES.get(order.source, set()):
            return f"status {order.status} is cancelled/failed"
        return None

    def _log_recheck_skip_once(self, unique_id: str, reason_key: str, msg: str, *args) -> None:
        key = (unique_id, reason_key)
        if key in self._recheck_skip_logged:
            return
        self._recheck_skip_logged.add(key)
        log.info(msg, *args)

    def _sync_one_order(
        self, adapter: MarketplaceAdapter, order: NormalizedOrder, unique_id: str
    ) -> None:
        # Status allow-list / blacklist (see _status_rejection and
        # ALLOWED_STATUSES's own docstring for why Faraz Honar is an
        # allow-list). Uses NormalizedOrder.status rather than per-adapter
        # filters so that no order of any marketplace slips through if an
        # adapter's own guard is incomplete or outdated.
        rejection = self._status_rejection(order)
        if rejection is not None:
            log.info(
                "sync_engine: skipping %s order %s - %s",
                order.source, order.source_order_id, rejection,
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
            # Marked notified BEFORE the DidarDealPoller ever gets a
            # chance to see this same Deal.Id (see
            # src/didar/deal_poller.py's module docstring) - this deal
            # is about to get the rich per-order Telegram message right
            # below via notify_new_order(); without this, the generic
            # "any deal" poller would discover the same Id a few
            # seconds/minutes later and send a second, less detailed
            # notification for it.
            self._repo.mark_deal_notified(deal_id)
            # Fire and forget - notify_new_order catches and logs its own
            # errors (queuing a failed send for retry via `self._repo`
            # rather than losing it - see telegram.py's module
            # docstring), so a Telegram outage can never break the sync
            # itself.
            self._telegram.notify_new_order(order, deal_id, self._repo)
            # Same fire-and-forget contract (notify_if_express catches,
            # logs and queues its own failures - see
            # src/modir_payamak.py's module docstring), and a no-op for
            # any order that isn't express or has already been alerted
            # about. Placed after the Telegram send, not before it, so
            # an SMS-provider problem can never delay the message every
            # order gets.
            self._sms_notifier.notify_if_express(order, self._repo)
            # Same fire-and-forget contract, immediately after the SMS
            # alert - see _add_snappshop_warehouse_note's docstring.
            # No-op for every source other than snappshop/snappshop2.
            self._add_snappshop_warehouse_note(order, deal_id)
        except Exception as exc:
            log.exception(
                "sync_engine: failed to sync %s order %s", order.source, order.source_order_id
            )
            self._repo.record_failure(order.source, order.source_order_id, str(exc))

    def _add_snappshop_warehouse_note(self, order: NormalizedOrder, deal_id: str) -> None:
        """Adds exactly one Didar note classifying a SnappShop order as
        express or warehouse-bound (see the "طبقه‌بندی انواع سفارش اسنپ‌شاپ +
        ثبت یادداشت خودکار در دیدار" prompt). A no-op for every other
        marketplace.

        Fire-and-forget, same contract as notify_if_express() right
        above it at both call sites (_sync_one_order and
        retry_pending_failures): this method itself never raises, so an
        order-type note issue can never fail the order sync or the SMS
        alert.

        Guarded by Repository.has_snappshop_note_been_added() so a
        re-processed order - this is called from BOTH _sync_one_order()
        and retry_pending_failures(), same as notify_if_express() - never
        gets a second note on the same Didar deal.

        Destination-based splitting ("ارسال فوری به انبار" vs "ارسال به
        انبار") is deliberately NOT implemented yet - see the prompt's
        "زمینه‌ی تصمیم" section: as of API v2.1.2, neither SnappShop order
        endpoint exposes a structured buyer-destination field, and
        NormalizedOrder.customer_city/customer_province are intentionally
        left unset for these two adapters rather than guessed. This
        if/else is written so that adding that split later is one more
        branch here, not a rewrite of this method.
        """
        if order.source not in ("snappshop", "snappshop2"):
            return
        if self._repo.has_snappshop_note_been_added(order.source, order.source_order_id):
            return

        if is_express_order(order):
            text = _SNAPPSHOP_EXPRESS_NOTE_TEXT.get(order.source)
            if text is None:
                log.warning(
                    "sync_engine: no express note text configured for SnappShop "
                    "source '%s' - skipping order-type note for order %s",
                    order.source, order.source_order_id,
                )
                return
        else:
            text = _SNAPPSHOP_WAREHOUSE_NOTE_TEXT

        if settings.dry_run:
            # DRY_RUN (see config.Settings.dry_run docstring): deal_id at
            # this point is DidarSyncService.sync_order()'s fake DRY_RUN-*
            # id (no real deal exists), so a real create_note() call would
            # just fail against it anyway - skip it cleanly instead of
            # making (and logging an exception for) a doomed API call.
            log.info(
                "sync_engine: [DRY_RUN] would add SnappShop order-type note ('%s') to "
                "deal %s (order %s %s) (no real Didar write made)",
                text, deal_id, order.source, order.source_order_id,
            )
            return

        try:
            self._didar_activity_client.create_note(deal_id, text)
        except Exception:
            log.exception(
                "sync_engine: failed to add SnappShop order-type note ('%s') to "
                "deal %s (order %s %s) - will retry on the next poll/retry cycle",
                text, deal_id, order.source, order.source_order_id,
            )
            return

        self._repo.mark_snappshop_note_added(order.source, order.source_order_id)

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
            order.source in ("digikala", "digikala2")
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
            order.source in ("digikala", "digikala2")
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
        if the API fails or returns no data.

        REVIEWED for the 2026-09 SBS shipment-watermark migration (see
        digikala-sbs-migration-prompt.md): DigikalaAdapter._normalize_sbs_row
        now populates customer_full_name directly from the same
        /ship-by-seller-orders row fetch_new_orders() already made, so the
        gate below (`not order.customer_full_name`) means this extra
        endpoint call is only actually reached as a genuine FALLBACK - when
        that row's own customer_name field was null - not on every order
        as before this migration."""
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
        never break or block a sync.

        REVIEWED for the 2026-09 SBS shipment-watermark migration (see
        digikala-sbs-migration-prompt.md): DigikalaAdapter._normalize_sbs_row
        now populates shipping_cost/shipment_tracking_code directly from
        the same row fetch_new_orders() already fetched, so the gate below
        (`order.shipping_cost is None`) means this extra endpoint call is
        only actually reached as a genuine FALLBACK - not on every order
        as before this migration."""
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
        # Per-platform cache of the permanent skip-list, so a batch of
        # failures for the same platform doesn't re-query it once per row.
        ignored_ids_by_platform: dict[str, set[str]] = {}

        for failure in self._repo.get_pending_failures(max_attempts=max_attempts):
            # Skip (and permanently drop) anything that's SINCE been added
            # to the ignored_orders skip-list. This closes the same gap
            # _sync_source()'s Layer-0 check exists for, but on the retry
            # path: get_pending_failures() reads sync_failures directly and
            # was never cross-checked against ignored_orders, so an order
            # already sitting in sync_failures before it got backfilled
            # into ignored_orders (e.g. via scripts/seed_ignored_orders.py)
            # would keep getting retried - hitting the source's API and
            # failing - forever, even though it's explicitly on the
            # "never sync this" list. Confirmed in production (2026-09):
            # every single Digikala row in sync_failures at the time was
            # already on the ignore-list.
            ignored_ids = ignored_ids_by_platform.setdefault(
                failure.platform, self._repo.get_ignored_ids(failure.platform)
            )
            if failure.source_order_id in ignored_ids:
                self._repo.clear_failure(failure.platform, failure.source_order_id)
                log.info(
                    "sync_engine: retry_pending_failures: dropping now-ignored %s "
                    "order %s instead of retrying (on permanent skip-list)",
                    failure.platform, failure.source_order_id,
                )
                continue

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
                # See the matching call/comment in _sync_one_order() -
                # same reasoning applies to the retry path.
                self._repo.mark_deal_notified(deal_id)
                self._telegram.notify_new_order(order, deal_id, self._repo)
                # Deliberately called on the retry path too: an order
                # whose FIRST sync attempt failed is exactly the one
                # most likely to be running late, so it must not lose
                # its express alert. Safe to repeat - the
                # (source, source_order_id) guard in
                # Repository.has_express_alert_been_sent() is what stops
                # an order that reaches this method twice from paging
                # the warehouse twice.
                self._sms_notifier.notify_if_express(order, self._repo)
                # See the matching call/comment in _sync_one_order() -
                # same reasoning applies to the retry path.
                self._add_snappshop_warehouse_note(order, deal_id)
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
# Values confirmed from each marketplace's official API docs (2026-08,
# Digikala values updated 2026-09 per the SBS shipment-watermark
# migration - see digikala-sbs-migration-prompt.md, Decision 3).
# "unknown" (SnappShop's fallback when a response is missing
# `order_status` entirely) is intentionally included so a malformed
# response doesn't silently sync as a real order - it passes through
# for manual review instead.
#
# BUGFIX (2026-09): this used to be a single flat set shared across every
# source, with no source check at all - so a status string that means
# "cancelled" for one marketplace but "still active" for another (e.g.
# Digikala's "pending", which IS a normal in-progress shipment - see
# digikala.py's _normalize_sbs_row comment "pending/processing/processed/
# edited are all active and sync normally") could never be added for just
# the one source that needs it without silently breaking every other
# source's orders in that status. Now keyed per source, so each
# marketplace's excluded statuses are independent.
CANCELLED_OR_FAILED_STATUSES: dict[str, set[str]] = {
    # Tapsi Shop: status codes 6 (لغو سفارش - cancelled) and 9 (تحویل کامل -
    # delivered) are already excluded by the adapter's own
    # _ACTIVE_ORDER_STATUS_IDS = [4] filter, so nothing further to exclude here.
    "tapsishop": set(),
    # Digikala (2026-09 migration to /ship-by-seller-orders - see
    # digikala.py's _normalize_sbs_row and the migration prompt's
    # Decision 3): the old order_type query parameter
    # ("canceled"/"returned") is no longer sent at all, so those two
    # status strings the adapter used to produce are gone. The SBS
    # schema instead exposes isCancelled (boolean) and status.text
    # (including "rejected"), which the adapter maps directly to:
    "digikala": {
        "cancelled",  # Digikala isCancelled=true
        "rejected",   # Digikala status.text == "rejected"
    },
    # Second Digikala store (src/marketplaces/digikala2.py) - same
    # Open API, same status values, so the same blacklist applies
    # unchanged. Kept as its own dict entry (not a shared reference)
    # since digikala2.py is intentionally maintained as an independent
    # copy of digikala.py's logic - see that module's docstring.
    "digikala2": {
        "cancelled",
        "rejected",
    },
    # Basalam: confirmed status values for cancelled/failed orders
    # (from the "وضعیت‌های سفارش" section in the official docs).
    "basalam": {
        "cancelled",  # Basalam order_status=cancelled
        "refunded",   # Basalam order_status=refunded
    },
    # Faraz Honar is intentionally absent here - see ALLOWED_STATUSES below,
    # which replaced its blacklist entry (2026-09 bugfix, see that dict's
    # docstring).
    # SnappShop: schema confirmed (_SCHEMA_CONFIRMED = True, 2026-09 -
    # see snappshop.py's module docstring) against the official v2.1.2
    # vendor-API PDF and a real order. `order_status` is confirmed to
    # be "CANCELED" for a cancelled order (also seen as the events
    # endpoint's CHANGE_STATUS new_status - doc section 2-3-1/2-3-2).
    # "unknown" is kept as the adapter's fallback for a response
    # missing `order_status` entirely - included so that case passes
    # through for manual review rather than silently syncing.
    "snappshop": {"canceled", "unknown"},
    # Second SnappShop vendor account (src/marketplaces/snappshop2.py) -
    # same vendor API, same status values, so the same blacklist
    # applies unchanged. Kept as its own dict entry (not a shared
    # reference), same reasoning as "digikala2" above.
    "snappshop2": {"canceled", "unknown"},
}

# Allow-list: for a source listed here, ONLY these statuses may reach Didar -
# everything else is dropped, unlike CANCELLED_OR_FAILED_STATUSES above
# (a blacklist, which drops only the named statuses and lets everything
# else - including ones nobody has thought of yet - through).
#
# BUGFIX (2026-09): Faraz Honar used to be a CANCELLED_OR_FAILED_STATUSES
# blacklist entry ({"cancelled", "failed", "pending"}). The adapter fetches
# every WooCommerce order via status="any" (see farazhonar.py's
# fetch_new_orders), and WooCommerce has several other non-final statuses
# a blacklist has to individually name to catch - e.g. "on-hold" (many
# Iranian WooCommerce storefronts show this as a "پیش فاکتور"/pre-invoice
# order awaiting bank-transfer confirmation, distinct from "pending"),
# plus "completed", "refunded", "checkout-draft", "trash". Any of those
# slipped straight through the old blacklist and into Didar as a real
# Deal - which is what the client kept seeing (2026-09: "پیش فاکتور و
# لغو شده رو هم میاره", i.e. proforma AND cancelled orders were both still
# being registered). The client's actual requirement is an allow-list, not
# a blacklist: "ما فقط سفارش های در حال انجام رو میخوایم داخل دیدار ثبت
# بشن" - only WooCommerce's "processing" status (a paid, confirmed,
# in-progress order) should ever become a Deal. Every other status,
# named here or not, is now rejected by construction.
ALLOWED_STATUSES: dict[str, set[str]] = {
    "farazhonar": {"processing"},
}