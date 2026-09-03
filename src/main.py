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
from src.didar.deal_poller import DidarDealPoller
from src.didar.service import DidarSyncService
from src.logger import get_logger
from src.marketplaces.basalam import BasalamAdapter
from src.marketplaces.digikala import DigikalaAdapter
from src.marketplaces.farazhonar import FarazHonarAdapter
from src.marketplaces.snappshop import SnappShopAdapter
from src.marketplaces.tapsishop import TapsiShopAdapter
from src.reporting import check_health, generate_daily_report
from src.sync_engine import SyncEngine
from src.telegram import TelegramNotifier

log = get_logger(__name__)


def build_engine() -> tuple[SyncEngine, Repository]:
    repository = Repository()
    adapters = [
        TapsiShopAdapter(),
        DigikalaAdapter(),
        BasalamAdapter(),
        FarazHonarAdapter(),
    ]
    if settings.snappshop.enabled:
        adapters.append(SnappShopAdapter())
    else:
        # SNAPPSHOP_ENABLED=false (the default) - client request 2026-08,
        # no SnappShop API access yet. Left out of the poll loop entirely
        # rather than left in to fail every single cycle - see
        # SnappShopConfig.enabled in config.py. Set SNAPPSHOP_ENABLED=true
        # in .env once real credentials exist; no code change needed.
        log.info("snappshop: disabled (SNAPPSHOP_ENABLED is not 'true') - skipping")

    engine = SyncEngine(
        adapters=adapters,
        repository=repository,
        didar_service=DidarSyncService(),
    )
    return engine, repository


def _poll_cycle(
    engine: SyncEngine,
    repository: Repository,
    telegram: TelegramNotifier,
    deal_poller: DidarDealPoller | None,
) -> None:
    engine.run_once()
    # Cheap SQLite lookups - safe to run every cycle rather than on a
    # separate schedule. Logs a WARNING for anything that looks stuck;
    # see src/reporting.py for what "stale" means.
    check_health(repository, engine.adapter_names)
    # Telegram daily/weekly/monthly reports: a per-cycle rollover check
    # rather than a cron job - see src/telegram.py's module docstring for
    # why (Gregorian cron triggers don't line up with Jalali month
    # boundaries). Best-effort - logs and swallows its own errors, so a
    # Telegram outage can never break the poll cycle itself.
    telegram.check_and_send_reports(repository, engine.adapter_names)
    # "Any deal" Telegram notification (client request, 2026-09): every
    # Deal registered in Didar, manual or automatic, not just the ones
    # this program itself creates from a marketplace order - see
    # src/didar/deal_poller.py's module docstring. None when
    # DIDAR_DEAL_POLL_ENABLED=false.
    if deal_poller is not None:
        _poll_new_deals(deal_poller, repository, telegram)


def _poll_new_deals(
    deal_poller: DidarDealPoller, repository: Repository, telegram: TelegramNotifier
) -> None:
    """One step of the "any deal" poller, isolated in its own try/except
    (same "each source isolated" philosophy as _sync_source() in
    sync_engine.py) so a Didar outage here can never break the
    marketplace poll cycle it's called from."""
    try:
        for deal in deal_poller.poll_new_deals(repository):
            telegram.notify_new_deal(deal)
    except Exception:
        log.exception("didar: deal poller cycle failed - will retry next cycle")


def run_forever() -> None:
    engine, repository = build_engine()
    scheduler = BlockingScheduler(timezone="UTC")
    telegram = TelegramNotifier()

    deal_poller: DidarDealPoller | None = None
    if settings.didar_deal_poll_enabled:
        deal_poller = DidarDealPoller()
    else:
        log.info(
            "didar deal poller: disabled (DIDAR_DEAL_POLL_ENABLED is not 'true') "
            "- only this program's own marketplace-driven deals will notify Telegram"
        )

    scheduler.add_job(
        _poll_cycle,
        "interval",
        seconds=settings.poll_interval_seconds,
        args=[engine, repository, telegram, deal_poller],
        # NOTE: do NOT pass next_run_time=None here - in APScheduler that
        # means "add this job paused", not "run immediately". It was
        # silently preventing the interval job from ever firing after the
        # one manual _poll_cycle() call below. The manual call already
        # covers "run once immediately on startup"; letting add_job use
        # its normal default next_run_time lets the trigger schedule the
        # next automatic run correctly.
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
        _poll_cycle(engine, repository, telegram, deal_poller)
    except Exception:
        log.exception("sync_engine: initial run_once failed - will retry on schedule")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("order-sync-platform shutting down")


if __name__ == "__main__":
    run_forever()