"""
Local persistence layer (SQLite).

Three responsibilities:
  1. Remember which (platform, source_order_id) pairs have already been
     synced to Didar, so we never create a duplicate Deal.
  2. Track failed sync attempts so the SyncEngine can retry them later
     instead of silently dropping orders when Didar is briefly unreachable.
  3. Per-platform sync watermark (sync_state table) - kept for backward
     compatibility with reporting.py's health checks, but NOT used by the
     active sync path anymore (see sync_engine.py for the new ID-based
     dedup algorithm).

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

    def mark_synced(self, platform: str, source_order_id: str, didar_deal_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO synced_orders
                    (platform, source_order_id, didar_deal_id, synced_at)
                VALUES (?, ?, ?, ?)
                """,
                (platform, source_order_id, didar_deal_id, datetime.now(timezone.utc).isoformat()),
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
