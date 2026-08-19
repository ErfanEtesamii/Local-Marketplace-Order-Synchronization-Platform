"""
Daily summary report + lightweight health check.

Per the original proposal's Stage 6 (Logging, Reporting, and
Operational Visibility): gives a human a quick daily answer to "is
everything working?" without reading raw log files, and flags sources
that have gone quiet or are stuck retrying failures.

Deliberately NOT an HTTP endpoint - this service is local-only and
intentionally not exposed to the network (see the proposal's
"local-only deployment, no cloud dependency" principle); listening on
a port would work against that. Instead:
  - generate_daily_report() writes a plain-text summary to
    reports/YYYY-MM-DD.txt, run once a day by the scheduler in main.py
  - check_health() runs after every poll cycle and logs a WARNING for
    any source that looks stuck, so problems surface in the normal
    log file rather than needing a separate monitoring step
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.db.repository import Repository
from src.logger import get_logger

log = get_logger(__name__)

REPORTS_DIR = Path(__file__).resolve().parent.parent / "reports"

# A source that hasn't completed a successful poll cycle within this
# window is considered stale - flagged in both the health check and
# the daily report. Comfortably above the default 2-minute poll
# interval so a couple of missed cycles don't trigger a false alarm.
STALE_AFTER = timedelta(hours=2)


@dataclass(frozen=True)
class SourceHealth:
    source: str
    last_synced_at: datetime | None
    is_stale: bool
    pending_failures: int


def check_health(repository: Repository, source_names: list[str]) -> list[SourceHealth]:
    """
    Called after every poll cycle (see main.py). Cheap - a handful of
    indexed SQLite lookups - so safe to run every cycle rather than on
    a separate schedule.
    """
    now = datetime.now(timezone.utc)
    results = []

    for source in source_names:
        last_synced = repository.get_last_sync_time(source)
        is_stale = last_synced is None or (now - last_synced) > STALE_AFTER
        pending = repository.count_pending_failures(source)
        results.append(SourceHealth(source, last_synced, is_stale, pending))

        if is_stale:
            log.warning(
                "health_check: %s hasn't completed a poll cycle since %s (stale)",
                source, last_synced.isoformat() if last_synced else "never",
            )
        if pending > 0:
            log.warning("health_check: %s has %d order(s) stuck in retry", source, pending)

    return results


def generate_daily_report(repository: Repository, source_names: list[str]) -> Path:
    now = datetime.now(timezone.utc)
    since_24h = now - timedelta(days=1)
    health = check_health(repository, source_names)

    lines = [
        "Order Sync Platform - Daily Report",
        f"Generated: {now.isoformat()}",
        "",
    ]
    for h in health:
        synced_today = repository.count_synced_since(h.source, since_24h)
        status = "STALE" if h.is_stale else "OK"
        lines.append(f"[{status}] {h.source}")
        lines.append(f"    orders synced in last 24h : {synced_today}")
        lines.append(
            f"    last successful poll      : "
            f"{h.last_synced_at.isoformat() if h.last_synced_at else 'never'}"
        )
        lines.append(f"    pending failures          : {h.pending_failures}")
        lines.append("")

    report_text = "\n".join(lines)

    REPORTS_DIR.mkdir(exist_ok=True)
    report_path = REPORTS_DIR / f"{now.date().isoformat()}.txt"
    report_path.write_text(report_text, encoding="utf-8")
    log.info("daily report written to %s", report_path)
    return report_path
