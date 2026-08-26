"""
Structured logging setup.

One rotating log file for the whole service, plus console output (useful
when running interactively during development / debugging on the server).
"""
from __future__ import annotations

import logging
import sys
import time
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from src.config import settings

_LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)

_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


class _WindowsSafeTimedRotatingFileHandler(TimedRotatingFileHandler):
    """
    TimedRotatingFileHandler.doRollover() renames the log file at
    midnight, which Windows refuses (PermissionError) if any process
    still has it open - including a second instance of this same
    service (e.g. the NSSM-installed service running in the background
    while also testing with `python -m src.main` in a terminal, which
    is exactly what happened in a real run - confirmed via the
    "used by another process" error every single log call thereafter).

    The stdlib handler doesn't advance `rolloverAt` when doRollover()
    raises, so a single failed rotation makes EVERY subsequent log
    call retry the same failing rename - a permanent noise storm (and,
    worse, no new lines ever get written to the file again) until the
    process restarts. This override catches that specific failure,
    logs one clear one-line warning instead of a full traceback, and
    manually advances rolloverAt so it only retries at the *next*
    scheduled rollover rather than on every single log call.

    This masks the symptom, not the root cause - if this fires, check
    for a second running instance (`nssm status OrderSyncPlatform`,
    `Get-Process python*`) before assuming it's a transient lock.
    """

    def doRollover(self) -> None:
        try:
            super().doRollover()
        except PermissionError:
            current_time = int(time.time())
            new_rollover_at = self.computeRollover(current_time)
            while new_rollover_at <= current_time:
                new_rollover_at += self.interval
            self.rolloverAt = new_rollover_at
            sys.stderr.write(
                "order-sync-platform: log rotation skipped this cycle - the log "
                "file is locked by another process (possibly a second running "
                "instance - check `nssm status OrderSyncPlatform`). Will retry "
                "at the next scheduled rollover.\n"
            )


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        # Already configured (e.g. re-imported) - don't add duplicate handlers.
        return logger

    logger.setLevel(settings.log_level)

    formatter = logging.Formatter(_FORMAT)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    file_handler = _WindowsSafeTimedRotatingFileHandler(
        _LOG_DIR / "order-sync.log", when="midnight", backupCount=30, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    logger.propagate = False
    return logger