"""
Service entrypoint. This is what NSSM will run continuously on the
Windows server (see deploy/ once Stage 5/10 is built) - a long-running
process that polls every marketplace on a fixed interval.

Run directly for local testing:  python -m src.main
"""
from __future__ import annotations

from apscheduler.schedulers.blocking import BlockingScheduler

from src.config import settings
from src.db.repository import Repository
from src.didar.service import DidarSyncService
from src.logger import get_logger
from src.marketplaces.basalam import BasalamAdapter
from src.marketplaces.digikala import DigikalaAdapter
from src.marketplaces.farazhonar import FarazHonarAdapter
from src.marketplaces.snappshop import SnappShopAdapter
from src.marketplaces.tapsishop import TapsiShopAdapter
from src.sync_engine import SyncEngine

log = get_logger(__name__)


def build_engine() -> SyncEngine:
    adapters = [
        TapsiShopAdapter(),
        DigikalaAdapter(),
        BasalamAdapter(),
        SnappShopAdapter(),
        FarazHonarAdapter(),
    ]
    return SyncEngine(
        adapters=adapters,
        repository=Repository(),
        didar_service=DidarSyncService(),
    )


def run_forever() -> None:
    engine = build_engine()
    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        engine.run_once,
        "interval",
        seconds=settings.poll_interval_seconds,
        next_run_time=None,  # first run fires immediately, see below
    )

    log.info(
        "order-sync-platform starting - polling every %d seconds",
        settings.poll_interval_seconds,
    )

    # Run once immediately on startup rather than waiting a full interval.
    try:
        engine.run_once()
    except Exception:
        log.exception("sync_engine: initial run_once failed - will retry on schedule")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("order-sync-platform shutting down")


if __name__ == "__main__":
    run_forever()
