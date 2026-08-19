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
from src.reporting import check_health, generate_daily_report
from src.sync_engine import SyncEngine

log = get_logger(__name__)


def build_engine() -> tuple[SyncEngine, Repository]:
    repository = Repository()
    adapters = [
        TapsiShopAdapter(),
        DigikalaAdapter(),
        BasalamAdapter(),
        SnappShopAdapter(),
        FarazHonarAdapter(),
    ]
    engine = SyncEngine(
        adapters=adapters,
        repository=repository,
        didar_service=DidarSyncService(),
    )
    return engine, repository


def _poll_cycle(engine: SyncEngine, repository: Repository) -> None:
    engine.run_once()
    # Cheap SQLite lookups - safe to run every cycle rather than on a
    # separate schedule. Logs a WARNING for anything that looks stuck;
    # see src/reporting.py for what "stale" means.
    check_health(repository, engine.adapter_names)


def run_forever() -> None:
    engine, repository = build_engine()
    scheduler = BlockingScheduler(timezone="UTC")

    scheduler.add_job(
        _poll_cycle,
        "interval",
        seconds=settings.poll_interval_seconds,
        args=[engine, repository],
        next_run_time=None,  # first run fires immediately, see below
    )
    scheduler.add_job(
        generate_daily_report,
        "cron",
        hour=0,
        minute=5,
        args=[repository, engine.adapter_names],
    )

    log.info(
        "order-sync-platform starting - polling every %d seconds",
        settings.poll_interval_seconds,
    )

    # Run once immediately on startup rather than waiting a full interval.
    try:
        _poll_cycle(engine, repository)
    except Exception:
        log.exception("sync_engine: initial run_once failed - will retry on schedule")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("order-sync-platform shutting down")


if __name__ == "__main__":
    run_forever()
