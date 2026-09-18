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
from src.didar.deal_client import DidarDealClient
from src.didar.deal_poller import DidarDealPoller
from src.didar.service import DidarSyncService
from src.logger import get_logger
from src.marketplaces.basalam import BasalamAdapter
from src.marketplaces.digikala import DigikalaAdapter
from src.marketplaces.digikala2 import Digikala2Adapter
from src.marketplaces.farazhonar import FarazHonarAdapter
from src.marketplaces.snappshop import SnappShopAdapter
from src.marketplaces.tapsishop import TapsiShopAdapter
from src.modir_payamak import ModirPayamakNotifier
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

    if settings.digikala2.enabled:
        adapters.append(Digikala2Adapter())
    else:
        # DIGIKALA2_ENABLED=false (the default) - same opt-in pattern as
        # SnappShopConfig.enabled above. Left out of the poll loop
        # entirely until the second store's own config is confirmed -
        # see DigikalaConfig.enabled / _build_digikala2_config in
        # config.py. Set DIGIKALA2_ENABLED=true in .env once ready; no
        # code change needed.
        log.info("digikala2: disabled (DIGIKALA2_ENABLED is not 'true') - skipping")

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
    sms_notifier: ModirPayamakNotifier,
    deal_poller: DidarDealPoller | None,
    stage_snapshot_client: DidarDealClient,
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
    # Retry any Telegram notification that failed to send on a previous
    # cycle (2026-09 - see telegram.py's module docstring for the
    # "orders sync but Telegram never fires" incident this closes).
    # Best-effort like every other telegram.* call here.
    telegram.retry_pending_notifications(repository)
    # The SMS-side counterpart of the line above: re-attempt every
    # express-order alert that failed to send on a previous cycle
    # (2026-09 - see src/modir_payamak.py's module docstring). Its own
    # queue and its own table, so a Telegram outage and a Modir Payamak
    # outage never drain each other. Best-effort and no-ops entirely
    # when MODIR_PAYAMAK_* isn't configured.
    sms_notifier.retry_pending_notifications(repository)
    # "Any deal" Telegram notification (client request, 2026-09): every
    # Deal registered in Didar, manual or automatic, not just the ones
    # this program itself creates from a marketplace order - see
    # src/didar/deal_poller.py's module docstring. None when
    # DIDAR_DEAL_POLL_ENABLED=false.
    if deal_poller is not None:
        _poll_new_deals(deal_poller, repository, telegram)
    # "مشتری جدید" pipeline-stage history (client request, 2026-09 -
    # see the "تفکیک سفارش‌های مرحله «مشتری جدید»" prompt / src/db/
    # repository.py's new_customer_stage_deals docstring). Every poll
    # cycle, not on a separate schedule - see
    # DidarDealClient.record_current_stage_snapshot()'s docstring for
    # why this must be a live, re-taken-every-cycle snapshot rather
    # than a one-shot/backfill job. Isolated in its own try/except,
    # same "one Didar feature outage must never break the rest of the
    # poll cycle" philosophy as _poll_new_deals() below - unlike that
    # one, there's no DIDAR_DEAL_POLL_ENABLED-style toggle here:
    # record_current_stage_snapshot() -> list_current_deals_in_stage()
    # already no-ops (with its own warning) whenever
    # DIDAR_PIPELINE_ID/DIDAR_PIPELINE_STAGE_ID aren't configured, so a
    # second enable flag would be redundant.
    _record_new_stage_deals(stage_snapshot_client, repository)


def _record_new_stage_deals(deal_client: DidarDealClient, repository: Repository) -> None:
    """One step of the "مشتری جدید" stage-history snapshot, isolated
    in its own try/except so a Didar outage here can never break the
    marketplace poll cycle it's called from - same shape as
    _poll_new_deals() below."""
    try:
        deal_client.record_current_stage_snapshot(repository)
    except Exception:
        log.exception(
            "didar: \"مشتری جدید\" stage snapshot failed - will retry next cycle"
        )


def _poll_new_deals(
    deal_poller: DidarDealPoller, repository: Repository, telegram: TelegramNotifier
) -> None:
    """One step of the "any deal" poller, isolated in its own try/except
    (same "each source isolated" philosophy as _sync_source() in
    sync_engine.py) so a Didar outage here can never break the
    marketplace poll cycle it's called from."""
    try:
        for deal in deal_poller.poll_new_deals(repository):
            telegram.notify_new_deal(deal, repository)
    except Exception:
        log.exception("didar: deal poller cycle failed - will retry next cycle")


def run_forever() -> None:
    engine, repository = build_engine()
    scheduler = BlockingScheduler(timezone="UTC")
    telegram = TelegramNotifier()
    # Separate instance from the one SyncEngine builds for itself -
    # same tradeoff (and same reason) as `telegram` above: the object
    # holds nothing but an httpx.Client, all real state lives in the
    # repository, and this one is only ever used for the per-cycle
    # retry drain below.
    sms_notifier = ModirPayamakNotifier()
    # Separate instance from DidarSyncService's own internal deal
    # client, same tradeoff as deal_poller below - this one is only
    # ever used for the read-only stage-snapshot call.
    stage_snapshot_client = DidarDealClient()

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
        args=[
            engine, repository, telegram, sms_notifier, deal_poller,
            stage_snapshot_client,
        ],
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
    # Interactive /report command (custom Jalali date-range picker -
    # see src/telegram.py's poll_updates()). A separate, much shorter
    # interval than the main poll cycle above: that one defaults to
    # 120s (POLL_INTERVAL_SECONDS), which would make every calendar
    # button press feel unresponsive. APScheduler won't overlap two
    # runs of the same job (default max_instances=1), so if a
    # getUpdates call ever takes a while this just runs back-to-back
    # rather than piling up.
    scheduler.add_job(
        telegram.poll_updates,
        "interval",
        seconds=settings.telegram_report_picker_poll_seconds,
        args=[repository, engine.adapter_names],
    )

    log.info(
        "order-sync-platform starting - polling every %d seconds",
        settings.poll_interval_seconds,
    )

    # Run once immediately on startup rather than waiting a full interval.
    try:
        _poll_cycle(
            engine, repository, telegram, sms_notifier, deal_poller,
            stage_snapshot_client,
        )
    except Exception:
        log.exception("sync_engine: initial run_once failed - will retry on schedule")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("order-sync-platform shutting down")


if __name__ == "__main__":
    run_forever()