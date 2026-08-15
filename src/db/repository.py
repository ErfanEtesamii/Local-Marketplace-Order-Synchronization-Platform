"""
Local persistence layer (SQLite).

Two responsibilities only:
  1. Remember which (source, source_order_id) pairs have already been
     synced to Didar, so we never create a duplicate Deal.
  2. Track failed sync attempts so the Sync Engine can retry them later
     instead of silently dropping orders when Didar is briefly unreachable.

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
    source          TEXT NOT NULL,
    source_order_id TEXT NOT NULL,
    didar_deal_id   TEXT,
    synced_at       TEXT NOT NULL,
    PRIMARY KEY (source, source_order_id)
);

CREATE TABLE IF NOT EXISTS sync_failures (
    source          TEXT NOT NULL,
    source_order_id TEXT NOT NULL,
    error_message   TEXT,
    attempt_count   INTEGER NOT NULL DEFAULT 1,
    last_attempt_at TEXT NOT NULL,
    PRIMARY KEY (source, source_order_id)
);

CREATE TABLE IF NOT EXISTS sync_state (
    source          TEXT PRIMARY KEY,
    last_synced_at  TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class SyncFailure:
    source: str
    source_order_id: str
    error_message: str
    attempt_count: int
    last_attempt_at: str


class Repository:
    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path or settings.db_path
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
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

    def is_already_synced(self, source: str, source_order_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM synced_orders WHERE source = ? AND source_order_id = ?",
                (source, source_order_id),
            ).fetchone()
        return row is not None

    def mark_synced(self, source: str, source_order_id: str, didar_deal_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO synced_orders
                    (source, source_order_id, didar_deal_id, synced_at)
                VALUES (?, ?, ?, ?)
                """,
                (source, source_order_id, didar_deal_id, datetime.now(timezone.utc).isoformat()),
            )
            # Clear any prior failure record now that it succeeded.
            conn.execute(
                "DELETE FROM sync_failures WHERE source = ? AND source_order_id = ?",
                (source, source_order_id),
            )

    # --- retry tracking ---------------------------------------------------

    def record_failure(self, source: str, source_order_id: str, error_message: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sync_failures (source, source_order_id, error_message, attempt_count, last_attempt_at)
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(source, source_order_id) DO UPDATE SET
                    error_message   = excluded.error_message,
                    attempt_count   = attempt_count + 1,
                    last_attempt_at = excluded.last_attempt_at
                """,
                (source, source_order_id, error_message, datetime.now(timezone.utc).isoformat()),
            )

    def get_pending_failures(self, max_attempts: int = 5) -> list[SyncFailure]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT source, source_order_id, error_message, attempt_count, last_attempt_at "
                "FROM sync_failures WHERE attempt_count < ?",
                (max_attempts,),
            ).fetchall()
        return [SyncFailure(*row) for row in rows]

    # --- per-source sync watermark ------------------------------------

    def get_last_sync_time(self, source: str) -> datetime | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT last_synced_at FROM sync_state WHERE source = ?", (source,)
            ).fetchone()
        if row is None:
            return None
        return datetime.fromisoformat(row[0])

    def set_last_sync_time(self, source: str, when: datetime) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sync_state (source, last_synced_at)
                VALUES (?, ?)
                ON CONFLICT(source) DO UPDATE SET last_synced_at = excluded.last_synced_at
                """,
                (source, when.isoformat()),
            )
