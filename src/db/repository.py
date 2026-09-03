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
"""


@dataclass(frozen=True)
class SyncFailure:
    platform: str
    source_order_id: str
    error_message: str
    attempt_count: int
    last_attempt_at: str


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
        """`period` is one of "day"/"week"/"month". Returns the last
        rollover marker src/telegram.py's check_and_send_reports() saw for
        that period (a Jalali date key), or None if never set."""
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
