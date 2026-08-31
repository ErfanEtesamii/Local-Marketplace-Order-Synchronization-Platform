"""
Structured logging setup.

One rotating log file for the whole service, plus console output (useful
when running interactively during development / debugging on the server).
"""
from __future__ import annotations

import io
import logging
import sys
import time
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from src.config import settings

_LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)

_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


class _EncodingSafeStream(io.TextIOBase):
    """
    Wraps a stream (typically sys.stdout) so that writes never raise
    UnicodeEncodeError. Any character the underlying stream can't encode
    is replaced with U+FFFD rather than crashing the log call - the
    alternative (letting an exception bubble up) would break the sync
    itself every time a Persian title or message got logged, and worse,
    it would do so silently in the sense that the file handler (which
    DOES support utf-8) would still record everything correctly - only
    the console output would be affected.

    This matters specifically on Windows: the default console codepage
    (cp1252 / cp437) has no Persian glyphs, so logging a Persian string
    through the raw stdout stream raises UnicodeEncodeError. On Linux
    and macOS the terminal is usually utf-8 and this wrapper is a
    no-op, so it costs nothing there.
    """

    def __init__(self, stream) -> None:
        self._stream = stream

    def write(self, data: str) -> int:
        try:
            return self._stream.write(data)
        except UnicodeEncodeError:
            # Replace the unencodable characters with the replacement
            # character and retry - never fail the log call.
            encoding = getattr(self._stream, "encoding", None) or "ascii"
            safe = data.encode(encoding, errors="replace").decode(encoding)
            return self._stream.write(safe)

    def flush(self) -> None:
        try:
            self._stream.flush()
        except (ValueError, OSError):
            # Stream may already be closed (e.g. during interpreter
            # shutdown) - swallowing here is intentional, matching how
            # logging.StreamHandler itself ignores closed streams.
            pass

    def writable(self) -> bool:
        return True


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

    # Use encoding-safe wrapper for console output to handle non-ASCII characters
    # (e.g. Persian activity titles, product catalog titles) on Windows consoles
    # that have encoding like cp1252 and can't natively handle Unicode.
    safe_stdout = _EncodingSafeStream(sys.stdout)
    console_handler = logging.StreamHandler(safe_stdout)
    console_handler.setFormatter(formatter)

    file_handler = _WindowsSafeTimedRotatingFileHandler(
        _LOG_DIR / "order-sync.log", when="midnight", backupCount=30, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    logger.propagate = False
    return logger