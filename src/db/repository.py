"""
Local persistence layer (SQLite).

Five responsibilities:
  1. Remember which (platform, source_order_id) pairs have already been
     synced to Didar, so we never create a duplicate Deal.
  2. Track failed sync attempts so the SyncEngine can retry them later
     instead of silently dropping orders when Didar is briefly unreachable.
  3. Per-platform sync watermark (sync_state table) - kept for backward
     compatibility with reporting.py's health checks, but NOT used by the
     active sync path anymore (see sync_engine.py for the new ID-based
     dedup algorithm).
  4. Per-order money breakdown (products/shipping/total, on synced_orders)
     plus day/week/month rollover markers (report_progress) - both added
     for the Telegram daily/weekly/monthly reports in src/telegram.py, so
     those reports aggregate from the same dedup table rather than a
     second parallel tracking system.
  5. Digikala's shipment-ID watermark (digikala_shipment_watermark table,
     2026-09 migration - see digikala-sbs-migration-prompt.md). Deliberately
     a SEPARATE table from sync_state above rather than reusing/extending
     it: sync_state's `last_synced_at` is a point in TIME, while this is a
     monotonic ID cursor - conflating the two concepts under one column
     would make the semantics ambiguous for whoever reads this file next.
     See src/marketplaces/digikala.py for why a time-based window can't
     work for this source at all.
  6. The Didar "any deal" Telegram poller (notified_deals +
     deal_poll_state tables, 2026-09 - see src/didar/deal_poller.py):
     notified_deals is the Id-based dedup guard so a Deal - whether
     entered by hand in Didar or created by this program itself - is
     never sent to Telegram twice; deal_poll_state is the single-row
     sliding watermark DidarDealPoller advances every poll cycle.
  7. Permanent skip-list (ignored_orders table, 2026-09): any
     (platform, source_order_id) that sync_engine.py's window-drop
     logic has ever rejected as out-of-window is recorded here once and
     then filtered out of every future fetch_new_orders() result BEFORE
     any other check runs. Without this, a source that keeps re-serving
     the same old orders every poll (as Digikala's list endpoint did
     before the shipment-ID watermark existed - see
     digikala_shipment_watermark above) pays the same "drop" evaluation,
     and logs the same line, forever. This table is deliberately
     separate from sync_failures: a sync_failure is a retry candidate
     (it should eventually succeed), an ignored_order is a permanent
     "never look at this again" - the two must never be conflated.
  8. Telegram notification retry queue (notification_failures table,
     2026-09 - see src/telegram.py's module docstring for the incident
     this fixes): a Telegram send happens strictly AFTER
     mark_synced()/mark_deal_notified() have already run, so unlike a
     Didar-sync failure (sync_failures above), nothing else in the
     system used to ever retry a failed send - it was just logged and
     lost. This table is that missing retry candidate list,
     specifically for the send step, keyed by a stable `ref_id`
     ("order:<platform>:<source_order_id>" or "deal:<deal_id>") so a
     re-queue replaces the same row instead of piling up duplicates for
     the same logical notification.
  9. Permanent history of every Deal ever seen in the "new customer"
     ("مشتری جدید") pipeline stage (new_customer_stage_deals table,
     2026-09 - see the "تفکیک سفارش‌های مرحله «مشتری جدید»" prompt).
     Didar's API only exposes a Deal's CURRENT PipelineStageId, not
     when it entered that stage, so a live query can answer "how many
     deals are in this stage right now" but never "how many entered it
     today/this week/etc". This table is the missing history: a Deal
     Id is recorded here, once, together with the timestamp this
     program first observed it sitting in that stage. Rows are NEVER
     deleted or reset - every report period (day/week/month/
     six-month/year) is just a since/until filter over this one
     ever-growing table, the same pattern get_amount_stats_since()
     already uses over synced_orders above.
 10. Express-order SMS dedup guard (express_alerts_sent table, 2026-09 -
     see src/modir_payamak.py's module docstring). Deliberately NOT
     folded into synced_orders (e.g. as an `sms_sent_at` column): an
     SMS alert is not a property of "this order was synced to Didar",
     it is a separate commitment made later in the same lifecycle, and
     the two must be able to fail independently. It is also
     deliberately separate from notified_deals (item 6): that table is
     keyed by Didar Deal Id, while this one is keyed by
     (platform, source_order_id) - the only identity
     ModirPayamakNotifier.notify_if_express() has at its call sites.
     The row is written BEFORE the send is attempted, because
     sync_engine.py calls notify_if_express() from BOTH
     _sync_one_order() and retry_pending_failures(), and this SMS pages
     three real people: a duplicate send is a worse outcome than a send
     that has to be retried from the queue below. Rows are never
     deleted.
 11. Modir Payamak SMS retry queue (sms_notification_failures table,
     2026-09). Same shape, and the same NotificationFailure dataclass,
     as notification_failures (item 8), for the same reason - a send
     that fails after mark_express_alert_sent() has already run has no
     other retry path in the system - but a SEPARATE table on purpose:
     the two notifiers have different providers, different outage
     windows and their own attempt budgets, so a Telegram outage must
     never queue into, drain from, or burn the attempt counter of the
     SMS queue (or vice versa). Sharing one table would also make the
     two notifiers' retry_pending_notifications() loops pick up each
     other's rows, since neither filters by ref_id prefix.

 12. Digikala "ارسال به انبار دیجی‌کالا" (FBD) dedup guard
     (synced_warehouse_shipments table, 2026-09 - see
     src/marketplaces/digikala_warehouse.py). Deliberately NOT stored in
     synced_orders (item 1): an FBD item is not a customer order, it has
     no NormalizedOrder, and mixing the two would silently change every
     report and health check that aggregates over synced_orders by
     platform (src/reporting.py, src/telegram.py) - none of which this
     source is meant to appear in. Keeping it in its own table is what
     makes "FBD stays out of the customer-order reports" the default
     rather than something each reader has to remember to filter for.
     Columns mirror synced_orders minus the products/shipping split,
     which does not exist on this endpoint (only a per-unit
     selling_price - see WarehouseShipmentItem).

     NO RETRY QUEUE ON PURPOSE: there is deliberately no
     warehouse_sync_failures twin of sync_failures (item 2). The
     existing retry path rebuilds an order via
     adapter.fetch_order_detail(source_order_id), and this endpoint has
     no per-item detail call to rebuild from - a shared queue would
     crash or, worse, retry against the wrong adapter. It also isn't
     needed: an item that fails mid-sync is never written here, and
     GET /open-api/v1/orders keeps returning it for as long as it is
     active, so the very next poll retries it naturally. The cost of a
     failure is a delay of one poll interval, not a lost item.

 13. SnappShop order-type Didar note dedup guard (snappshop_notes_added
     table, 2026-11 - see the "طبقه‌بندی انواع سفارش اسنپ‌شاپ" prompt and
     src/didar/activity_client.py's create_note()). Exactly the same
     shape and reasoning as express_alerts_sent (item 10) - "have we
     already committed this side effect for this
     (platform, source_order_id)?" - copied on purpose rather than
     reused: a snappshop/snappshop2 order gets exactly ONE Didar note
     ("اسنپ اکسپرس : ..." or "ارسال به انبار") regardless of how many
     times _sync_one_order()/retry_pending_failures() see it, and that
     commitment is independent of both the express-SMS commitment above
     and the Didar-sync commitment in synced_orders - each must be able
     to fail without affecting the others. The row is written only
     AFTER DidarActivityClient.create_note() succeeds (unlike
     express_alerts_sent, which is written before the send - see
     src/sync_engine.py's _add_snappshop_warehouse_note()), since a
     failed note has no retry queue of its own and simply gets
     reattempted on the very next poll cycle that sees the same order.
     Rows are never deleted.

Kept deliberately simple - one file, no ORM - matching the scale of a
single-server background service.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS synced_orders (
    platform        TEXT NOT NULL,
    source_order_id TEXT NOT NULL,
    didar_deal_id   TEXT,
    synced_at       TEXT NOT NULL,
    products_amount INTEGER,
    shipping_amount INTEGER,
    total_amount    INTEGER,
    PRIMARY KEY (platform, source_order_id)
);

CREATE TABLE IF NOT EXISTS sync_failures (
    platform        TEXT NOT NULL,
    source_order_id TEXT NOT NULL,
    error_message   TEXT,
    attempt_count   INTEGER NOT NULL DEFAULT 1,
    last_attempt_at TEXT NOT NULL,
    PRIMARY KEY (platform, source_order_id)
);

CREATE TABLE IF NOT EXISTS sync_state (
    source          TEXT PRIMARY KEY,
    last_synced_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS report_progress (
    period          TEXT PRIMARY KEY,
    marker          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS digikala_shipment_watermark (
    platform          TEXT PRIMARY KEY,
    last_shipment_id  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS notified_deals (
    deal_id       TEXT PRIMARY KEY,
    notified_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deal_poll_state (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    last_poll_time  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ignored_orders (
    platform        TEXT NOT NULL,
    source_order_id TEXT NOT NULL,
    reason          TEXT,
    ignored_at      TEXT NOT NULL,
    PRIMARY KEY (platform, source_order_id)
);

CREATE TABLE IF NOT EXISTS notification_failures (
    ref_id          TEXT NOT NULL PRIMARY KEY,
    message_text    TEXT NOT NULL,
    error_message   TEXT,
    attempt_count   INTEGER NOT NULL DEFAULT 1,
    last_attempt_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS express_alerts_sent (
    platform        TEXT NOT NULL,
    source_order_id TEXT NOT NULL,
    sent_at         TEXT NOT NULL,
    PRIMARY KEY (platform, source_order_id)
);

CREATE TABLE IF NOT EXISTS sms_notification_failures (
    ref_id          TEXT NOT NULL PRIMARY KEY,
    message_text    TEXT NOT NULL,
    error_message   TEXT,
    attempt_count   INTEGER NOT NULL DEFAULT 1,
    last_attempt_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS new_customer_stage_deals (
    deal_id     TEXT PRIMARY KEY,
    label_id    TEXT,
    label_title TEXT,
    amount      INTEGER,
    entered_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_new_customer_stage_deals_entered_at
    ON new_customer_stage_deals (entered_at);

CREATE TABLE IF NOT EXISTS synced_warehouse_shipments (
    source             TEXT NOT NULL,
    source_shipment_id TEXT NOT NULL,
    didar_deal_id      TEXT,
    synced_at          TEXT NOT NULL,
    total_amount       INTEGER,
    PRIMARY KEY (source, source_shipment_id)
);

CREATE TABLE IF NOT EXISTS snappshop_notes_added (
    platform        TEXT NOT NULL,
    source_order_id TEXT NOT NULL,
    added_at        TEXT NOT NULL,
    PRIMARY KEY (platform, source_order_id)
);
"""


@dataclass(frozen=True)
class SyncFailure:
    platform: str
    source_order_id: str
    error_message: str
    attempt_count: int
    last_attempt_at: str


@dataclass(frozen=True)
class NotificationFailure:
    ref_id: str
    message_text: str
    error_message: str
    attempt_count: int
    last_attempt_at: str


@dataclass(frozen=True)
class NewStageDeal:
    deal_id: str
    label_id: str | None
    label_title: str | None
    amount: int | None
    entered_at: str


class Repository:
    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path or settings.db_path
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            # Migrate legacy DB: rename old `source` column to `platform`
            # on tables where the column was renamed in code. SQLite <3.25
            # doesn't support RENAME COLUMN, but Python's stdlib ships with
            # a newer libsqlite3 than that threshold on all supported
            # platforms; the try/except is just defensive in case the
            # legacy DB has already been migrated.
            for legacy_table in ("synced_orders", "sync_failures"):
                try:
                    conn.execute(
                        f"ALTER TABLE {legacy_table} RENAME COLUMN source TO platform"
                    )
                except sqlite3.OperationalError:
                    # Column may already be named `platform` or table is fresh
                    pass
            conn.executescript(_SCHEMA)
            # Migrate pre-existing synced_orders tables (created before the
            # Telegram reporting feature) to add the money-breakdown columns.
            # Safe to run unconditionally: a fresh DB already has these
            # columns from _SCHEMA above, so ADD COLUMN just raises
            # "duplicate column" here, which is caught and ignored exactly
            # like the legacy rename above.
            for column in ("products_amount", "shipping_amount", "total_amount"):
                try:
                    conn.execute(f"ALTER TABLE synced_orders ADD COLUMN {column} INTEGER")
                except sqlite3.OperationalError:
                    pass

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self._db_path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # --- duplicate prevention -------------------------------------------------

    def is_already_synced(self, platform: str, source_order_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM synced_orders WHERE platform = ? AND source_order_id = ?",
                (platform, source_order_id),
            ).fetchone()
        return row is not None

    def mark_synced(
        self,
        platform: str,
        source_order_id: str,
        didar_deal_id: str,
        products_amount=None,
        shipping_amount=None,
        total_amount=None,
    ) -> None:
        """Record a successful sync.

        `products_amount` / `shipping_amount` / `total_amount` are the
        order's Rial money breakdown (Decimal, int, or float - anything
        `float()` accepts), used only by the Telegram daily/weekly/monthly
        reports (see src/telegram.py). All three are optional and default
        to NULL so existing callers (and orders synced before this feature
        existed) keep working unchanged; a NULL amount is simply excluded
        from report totals rather than treated as zero.
        """
        def _to_int(value):
            return int(round(float(value))) if value is not None else None

        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO synced_orders
                    (platform, source_order_id, didar_deal_id, synced_at,
                     products_amount, shipping_amount, total_amount)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    platform, source_order_id, didar_deal_id,
                    datetime.now(timezone.utc).isoformat(),
                    _to_int(products_amount), _to_int(shipping_amount), _to_int(total_amount),
                ),
            )
            # Clear any prior failure record now that it succeeded.
            conn.execute(
                "DELETE FROM sync_failures WHERE platform = ? AND source_order_id = ?",
                (platform, source_order_id),
            )

    # --- retry tracking ---------------------------------------------------

    def record_failure(self, platform: str, source_order_id: str, error_message: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sync_failures (platform, source_order_id, error_message, attempt_count, last_attempt_at)
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(platform, source_order_id) DO UPDATE SET
                    error_message   = excluded.error_message,
                    attempt_count   = attempt_count + 1,
                    last_attempt_at = excluded.last_attempt_at
                """,
                (platform, source_order_id, error_message, datetime.now(timezone.utc).isoformat()),
            )

    def get_pending_failures(self, max_attempts: int = 5) -> list[SyncFailure]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT platform, source_order_id, error_message, attempt_count, last_attempt_at "
                "FROM sync_failures WHERE attempt_count < ?",
                (max_attempts,),
            ).fetchall()
        return [SyncFailure(*row) for row in rows]

    def clear_failure(self, platform: str, source_order_id: str) -> None:
        """Remove a row from sync_failures WITHOUT a successful sync ever
        happening. Distinct from mark_synced()'s cleanup of the same
        table: this is for the case where a pending failure's order has
        SINCE been added to the permanent ignore-list (ignored_orders -
        e.g. via scripts/seed_ignored_orders.py, run after the order
        already had a sync_failures row from an earlier poll). Without
        this, retry_pending_failures() would keep retrying an order we've
        explicitly decided to never sync, forever - see its call site in
        sync_engine.py for the full story (2026-09 production bug: 100%
        of the digikala rows in sync_failures at the time turned out to
        already be on the ignore-list, yet kept getting retried and
        re-failing every single poll cycle)."""
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM sync_failures WHERE platform = ? AND source_order_id = ?",
                (platform, source_order_id),
            )

    # --- Telegram notification retry queue (2026-09) ----------------------
    # See notification_failures in the schema docstring above - this is the
    # send-step counterpart to sync_failures/get_pending_failures above,
    # for the one failure mode that had no retry path at all before: a
    # Telegram send failing AFTER the order was already synced+marked
    # notified. Same shape (INSERT ... ON CONFLICT DO UPDATE bumping
    # attempt_count) deliberately mirrored from record_failure() above.

    def record_notification_failure(self, ref_id: str, message_text: str, error_message: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO notification_failures
                    (ref_id, message_text, error_message, attempt_count, last_attempt_at)
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(ref_id) DO UPDATE SET
                    message_text    = excluded.message_text,
                    error_message   = excluded.error_message,
                    attempt_count   = attempt_count + 1,
                    last_attempt_at = excluded.last_attempt_at
                """,
                (ref_id, message_text, error_message, datetime.now(timezone.utc).isoformat()),
            )

    def get_pending_notification_failures(self, max_attempts: int = 5) -> list[NotificationFailure]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT ref_id, message_text, error_message, attempt_count, last_attempt_at "
                "FROM notification_failures WHERE attempt_count < ?",
                (max_attempts,),
            ).fetchall()
        return [NotificationFailure(*row) for row in rows]

    def clear_notification_failure(self, ref_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM notification_failures WHERE ref_id = ?", (ref_id,))

    # --- express-order SMS dedup guard (2026-09) --------------------------
    # See express_alerts_sent in the schema docstring above and
    # src/modir_payamak.py's module docstring. Same is_already_synced() /
    # mark_synced() shape as the Didar dedup at the top of this file, and
    # for the same reason - "have we already committed to this side
    # effect for this (platform, source_order_id)?" - but against its own
    # table, because the SMS commitment and the Didar sync commitment are
    # made at different points in the lifecycle and must be able to fail
    # independently of each other.

    def has_express_alert_been_sent(self, platform: str, source_order_id: str) -> bool:
        """True iff an express-alert SMS has already been committed for
        this order. Called by ModirPayamakNotifier.notify_if_express()
        before every send - which sync_engine.py invokes from BOTH
        _sync_one_order() and retry_pending_failures(), so this method
        is the only thing standing between a re-processed order and a
        second SMS to the warehouse staff.

        Note "committed", not "delivered": a row exists here as soon as
        the send was decided on, even if that particular send then
        failed and is sitting in sms_notification_failures awaiting
        retry. That is intentional - the retry queue owns delivery, this
        table owns the decision - and it mirrors mark_synced() running
        before the Telegram send attempt in the existing flow.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM express_alerts_sent WHERE platform = ? AND source_order_id = ?",
                (platform, source_order_id),
            ).fetchone()
        return row is not None

    def mark_express_alert_sent(self, platform: str, source_order_id: str) -> None:
        """Record that this order's express alert has been committed.

        INSERT OR IGNORE (same pattern as mark_deal_notified() below) so
        a repeated call for the same order - two poll cycles racing, or
        the same order arriving through both sync_engine.py call sites -
        is a harmless no-op that keeps the FIRST sent_at rather than an
        error or an overwrite.
        """
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO express_alerts_sent (platform, source_order_id, sent_at)
                VALUES (?, ?, ?)
                """,
                (platform, source_order_id, datetime.now(timezone.utc).isoformat()),
            )

    # --- SnappShop order-type Didar note dedup guard (2026-11) -------------
    # See snappshop_notes_added in the schema docstring above (item 13)
    # and src/didar/activity_client.py's create_note(). Same
    # has_X_been_sent()/mark_X_sent() shape as
    # has_express_alert_been_sent/mark_express_alert_sent right above,
    # copied against its own table so a snappshop/snappshop2 order's
    # single Didar note is a commitment independent of both the
    # express-SMS guard above and the Didar-sync guard at the top of
    # this file.

    def has_snappshop_note_been_added(self, source: str, source_order_id: str) -> bool:
        """True iff the single SnappShop order-type note ("اسنپ اکسپرس :
        ..." or "ارسال به انبار") has already been added for this order.
        Called by sync_engine.py's _add_snappshop_warehouse_note() before
        every attempt - which, like notify_if_express(), is invoked from
        BOTH _sync_one_order() and retry_pending_failures() - so this is
        the only thing standing between a re-processed order and a
        second note on the same Didar deal.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM snappshop_notes_added WHERE platform = ? AND source_order_id = ?",
                (source, source_order_id),
            ).fetchone()
        return row is not None

    def mark_snappshop_note_added(self, source: str, source_order_id: str) -> None:
        """Record that this order's SnappShop order-type note has been
        added.

        Unlike mark_express_alert_sent() (written BEFORE the send is
        attempted), this is written only AFTER
        DidarActivityClient.create_note() has already succeeded - a
        failed note has no retry queue of its own, so leaving no row
        behind on failure is what lets the very next poll cycle that
        sees the same order try again.

        INSERT OR IGNORE (same pattern as mark_express_alert_sent()) so
        a repeated call for the same order is a harmless no-op that
        keeps the FIRST added_at rather than an error or an overwrite.
        """
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO snappshop_notes_added (platform, source_order_id, added_at)
                VALUES (?, ?, ?)
                """,
                (source, source_order_id, datetime.now(timezone.utc).isoformat()),
            )

    # --- Digikala FBD ("ارسال به انبار") dedup guard (2026-09) ------------
    # See synced_warehouse_shipments in the schema docstring above (item
    # 12) and src/marketplaces/digikala_warehouse.py. Same
    # question/shape as has_express_alert_been_sent/mark_express_alert_sent
    # right above - "have we already committed this side effect for this
    # (source, id)?" - against its own table, because an FBD item is a
    # different kind of thing from a customer order and must never be
    # counted, reported on, or retried as one.

    def is_warehouse_shipment_synced(self, source: str, source_shipment_id: str) -> bool:
        """True iff a Didar Deal has already been committed for this FBD
        item. This is the ONLY "already seen" guard on this source
        besides the adapter's created-at floor: GET /open-api/v1/orders
        keeps returning an item for as long as it is active, so without
        this check every poll cycle would create another Deal for the
        same item.

        Like has_express_alert_been_sent(), "committed" - not
        "delivered": see mark_warehouse_shipment_synced() below for what
        is (and isn't) guaranteed to have happened by the time a row
        exists here.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM synced_warehouse_shipments "
                "WHERE source = ? AND source_shipment_id = ?",
                (source, source_shipment_id),
            ).fetchone()
        return row is not None

    def mark_warehouse_shipment_synced(
        self,
        source: str,
        source_shipment_id: str,
        didar_deal_id: str,
        total_amount=None,
    ) -> None:
        """Record that this FBD item now has a Didar Deal.

        Written only AFTER the Deal itself was created successfully - the
        ship Activity and its photo upload are fire-and-forget and may
        still fail afterwards (see stage 5), so a row here means "the
        Deal exists", not "everything downstream of it succeeded". That
        is the right trade-off in this direction: re-running the Deal
        creation would produce a duplicate deal, while a missing
        Activity is visible and fixable by hand in Didar.

        INSERT OR IGNORE (same pattern as mark_express_alert_sent()
        above) so a repeated call for the same item - two poll cycles
        racing - is a harmless no-op that keeps the FIRST synced_at and
        deal id rather than overwriting it with a second, later one.

        `total_amount` is this item's Rial line total (unit_price *
        quantity) purely for local traceability; nothing reads it yet -
        this source is deliberately kept out of the daily/weekly reports
        (see item 12 of the module docstring).
        """
        def _to_int(value):
            return int(round(float(value))) if value is not None else None

        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO synced_warehouse_shipments
                    (source, source_shipment_id, didar_deal_id, synced_at, total_amount)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    source, source_shipment_id, didar_deal_id,
                    datetime.now(timezone.utc).isoformat(),
                    _to_int(total_amount),
                ),
            )

    # --- Modir Payamak SMS retry queue (2026-09) --------------------------
    # The SMS-side twin of record_notification_failure() /
    # get_pending_notification_failures() / clear_notification_failure()
    # above: identical shape, identical NotificationFailure rows, but a
    # separate table so the Telegram and Modir Payamak queues can never
    # drain or exhaust each other (see item 11 of the schema docstring).
    # Reusing NotificationFailure rather than declaring an
    # SmsNotificationFailure dataclass is deliberate: the columns are the
    # same five, and src/modir_payamak.py's retry loop reads exactly the
    # fields (ref_id, message_text) TelegramNotifier's does.

    def record_sms_failure(self, ref_id: str, message_text: str, error_message: str) -> None:
        """Queue (or re-queue) a failed express-alert SMS under a stable
        `ref_id` - "express_sms:<platform>:<source_order_id>", built by
        ModirPayamakNotifier. ON CONFLICT DO UPDATE (not INSERT OR
        IGNORE) so a repeated failure for the same logical message bumps
        attempt_count on the one row instead of piling up duplicates,
        which is what lets get_pending_sms_failures()'s max_attempts
        eventually give up.
        """
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sms_notification_failures
                    (ref_id, message_text, error_message, attempt_count, last_attempt_at)
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(ref_id) DO UPDATE SET
                    message_text    = excluded.message_text,
                    error_message   = excluded.error_message,
                    attempt_count   = attempt_count + 1,
                    last_attempt_at = excluded.last_attempt_at
                """,
                (ref_id, message_text, error_message, datetime.now(timezone.utc).isoformat()),
            )

    def get_pending_sms_failures(self, max_attempts: int = 5) -> list[NotificationFailure]:
        """Every queued SMS still under its attempt budget. Rows at or
        over `max_attempts` are left in the table (not deleted) so they
        remain inspectable after the retry loop has given up on them -
        same policy as get_pending_notification_failures().
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT ref_id, message_text, error_message, attempt_count, last_attempt_at "
                "FROM sms_notification_failures WHERE attempt_count < ?",
                (max_attempts,),
            ).fetchall()
        return [NotificationFailure(*row) for row in rows]

    def clear_sms_failure(self, ref_id: str) -> None:
        """Drop a queued SMS after it finally sent. Note there is no
        corresponding un-mark on express_alerts_sent: the alert was
        committed once and has now been delivered once, which is exactly
        the intended end state.
        """
        with self._connect() as conn:
            conn.execute("DELETE FROM sms_notification_failures WHERE ref_id = ?", (ref_id,))

    # --- permanent skip-list (out-of-window / never-again orders) ---------

    def get_ignored_ids(self, platform: str) -> set[str]:
        """All source_order_ids permanently skipped for this platform.
        Called once per _sync_source() pass and used as an in-memory set
        filter - see ignored_orders in the schema docstring above."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT source_order_id FROM ignored_orders WHERE platform = ?",
                (platform,),
            ).fetchall()
        return {row[0] for row in rows}

    def add_ignored_ids(self, platform: str, source_order_ids: list[str], reason: str) -> None:
        """Permanently mark these ids as ignored for this platform. Uses
        INSERT OR IGNORE so re-adding an id already in the table (e.g. a
        race between two poll cycles) is a harmless no-op rather than an
        error."""
        if not source_order_ids:
            return
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT OR IGNORE INTO ignored_orders (platform, source_order_id, reason, ignored_at)
                VALUES (?, ?, ?, ?)
                """,
                [(platform, source_order_id, reason, now) for source_order_id in source_order_ids],
            )

    # --- per-source sync watermark ------------------------------------

    def get_last_sync_time(self, platform: str) -> datetime | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT last_synced_at FROM sync_state WHERE source = ?", (platform,)
            ).fetchone()
        if row is None:
            return None
        return datetime.fromisoformat(row[0])

    def set_last_sync_time(self, platform: str, when: datetime) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sync_state (source, last_synced_at)
                VALUES (?, ?)
                ON CONFLICT(source) DO UPDATE SET last_synced_at = excluded.last_synced_at
                """,
                (platform, when.isoformat()),
            )

    # --- Digikala shipment-ID watermark (2026-09 SBS migration) --------
    # See digikala-sbs-migration-prompt.md and src/marketplaces/digikala.py:
    # unlike the time-based sync_state above, this is a monotonic cursor
    # over shipmentId, updated after every fetched PAGE (not just at the
    # end of a whole poll) so a mid-pagination crash can't lose progress.

    def get_last_shipment_id(self, platform: str) -> int | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT last_shipment_id FROM digikala_shipment_watermark WHERE platform = ?",
                (platform,),
            ).fetchone()
        return row[0] if row is not None else None

    def set_last_shipment_id(self, platform: str, shipment_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO digikala_shipment_watermark (platform, last_shipment_id)
                VALUES (?, ?)
                ON CONFLICT(platform) DO UPDATE SET last_shipment_id = excluded.last_shipment_id
                """,
                (platform, shipment_id),
            )

    # --- Didar "any deal" Telegram poller (2026-09) --------------------
    # See src/didar/deal_poller.py's module docstring for the full
    # design. notified_deals is the Id-based dedup guard (once a Deal
    # Id is in here it is never re-notified - this is what lets
    # SyncEngine's own per-order flow and DidarDealPoller's generic
    # sweep safely overlap without double-messaging Telegram);
    # deal_poll_state is the single-row sliding watermark the poller
    # advances every cycle.

    def is_deal_notified(self, deal_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM notified_deals WHERE deal_id = ?", (deal_id,)
            ).fetchone()
        return row is not None

    def mark_deal_notified(self, deal_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO notified_deals (deal_id, notified_at) VALUES (?, ?)",
                (deal_id, datetime.now(timezone.utc).isoformat()),
            )

    def get_deal_poll_watermark(self) -> datetime | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT last_poll_time FROM deal_poll_state WHERE id = 1"
            ).fetchone()
        return datetime.fromisoformat(row[0]) if row else None

    def set_deal_poll_watermark(self, when: datetime) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO deal_poll_state (id, last_poll_time) VALUES (1, ?)
                ON CONFLICT(id) DO UPDATE SET last_poll_time = excluded.last_poll_time
                """,
                (when.isoformat(),),
            )

    # --- reporting / health check -----------------------------------

    def count_synced_since(self, platform: str, since: datetime) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM synced_orders WHERE platform = ? AND synced_at >= ?",
                (platform, since.isoformat()),
            ).fetchone()
        return row[0] if row else 0

    def count_pending_failures(self, platform: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM sync_failures WHERE platform = ?", (platform,)
            ).fetchone()
        return row[0] if row else 0

    def get_amount_stats_since(
        self, platform: str, since: datetime, until: datetime | None = None
    ) -> tuple[int, int, int, int]:
        """Aggregate money/count for one platform's synced_orders rows in
        [since, until). `until=None` means "no upper bound". Returns
        (products_sum, shipping_sum, total_sum, order_count) - used by the
        Telegram daily/weekly/monthly reports (see src/telegram.py), which
        sum this across every platform. Orders synced before the money
        columns existed have NULL amounts, which SUM() already excludes
        (COALESCE only guards against an all-NULL/empty result set)."""
        query = (
            "SELECT COALESCE(SUM(products_amount), 0), "
            "       COALESCE(SUM(shipping_amount), 0), "
            "       COALESCE(SUM(total_amount), 0), "
            "       COUNT(*) "
            "FROM synced_orders WHERE platform = ? AND synced_at >= ?"
        )
        params: list = [platform, since.isoformat()]
        if until is not None:
            query += " AND synced_at < ?"
            params.append(until.isoformat())

        with self._connect() as conn:
            row = conn.execute(query, params).fetchone()
        return (row[0], row[1], row[2], row[3]) if row else (0, 0, 0, 0)

    # --- Telegram report rollover markers -------------------------------

    def get_report_marker(self, period: str) -> str | None:
        """`period` is one of "day"/"week"/"month"/"year" for
        src/telegram.py's check_and_send_reports() rollover markers, or
        the dedicated key "telegram_update_offset" for poll_updates()'s
        Telegram getUpdates offset - this table is a generic
        string-keyed marker store, reused there rather than adding a
        second table just for one integer. Returns the stored value, or
        None if never set."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT marker FROM report_progress WHERE period = ?", (period,)
            ).fetchone()
        return row[0] if row else None

    def set_report_marker(self, period: str, marker: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO report_progress (period, marker)
                VALUES (?, ?)
                ON CONFLICT(period) DO UPDATE SET marker = excluded.marker
                """,
                (period, marker),
            )

    # --- "مشتری جدید" pipeline-stage history (2026-09) -------------------
    # See new_customer_stage_deals in the schema docstring above. This is
    # a permanent, append-only log of every Deal Id ever observed in that
    # stage - idempotent on deal_id (INSERT OR IGNORE, same pattern as
    # mark_deal_notified above) so a Deal seen across many poll cycles is
    # recorded exactly once, at its FIRST observed entered_at. There is
    # deliberately no update/reset/delete method: every report period
    # reads this one table with a since/until window instead.

    def record_new_stage_deal(
        self,
        deal_id: str,
        label_id: str | None,
        label_title: str | None,
        amount,
        entered_at: datetime,
    ) -> None:
        """Record a Deal's first-observed entry into the "new customer"
        stage. `amount` accepts Decimal/int/float/None like mark_synced()'s
        money columns; INSERT OR IGNORE means repeated calls for the same
        deal_id (e.g. seen again on the next poll cycle) are a no-op, so
        entered_at is never overwritten once set."""
        def _to_int(value):
            return int(round(float(value))) if value is not None else None

        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO new_customer_stage_deals
                    (deal_id, label_id, label_title, amount, entered_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (deal_id, label_id, label_title, _to_int(amount), entered_at.isoformat()),
            )

    def get_new_stage_deals(
        self, since: datetime, until: datetime | None = None
    ) -> list[NewStageDeal]:
        """Rows whose entered_at falls in [since, until). `until=None`
        means no upper bound - same half-open-interval convention as
        get_amount_stats_since() above."""
        query = (
            "SELECT deal_id, label_id, label_title, amount, entered_at "
            "FROM new_customer_stage_deals WHERE entered_at >= ?"
        )
        params: list = [since.isoformat()]
        if until is not None:
            query += " AND entered_at < ?"
            params.append(until.isoformat())

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [NewStageDeal(*row) for row in rows]