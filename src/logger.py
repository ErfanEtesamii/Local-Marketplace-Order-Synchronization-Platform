"""
Structured logging setup.

One rotating log file for the whole service, plus console output (useful
when running interactively during development / debugging on the server).
"""
from __future__ import annotations

import io
import logging
import os
import sys
import time
import traceback as _traceback_mod
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from src.config import settings

_LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)

_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-28s | %(message)s"

# Any frame whose filename contains one of these path fragments is "our
# code" for the purposes of the condensed console traceback below -
# everything else (venv site-packages: httpx, httpcore, tenacity,
# urllib3, stdlib socket/ssl plumbing, etc.) is library internals that
# almost never has anything to do with *why* a call failed.
_OWN_CODE_MARKERS = (f"{os.sep}src{os.sep}", "OrderSyncPlatform")


class _ReadableFormatter(logging.Formatter):
    """
    Same one-line-per-record format as before, but a raised exception's
    traceback is fenced with blank lines and a "-- traceback --" /
    "-- end traceback --" marker pair. A bare traceback dumped in the
    middle of a stream of one-line records is the single biggest reason
    this log is hard to scan (see _RepeatSuppressFilter's docstring for
    the other reason) - there's nothing visually separating "one line
    per event" from "30 lines of stack frames", so the eye can't jump
    past it while skimming. The fence makes that jump possible without
    changing what's actually recorded (still the full traceback, still
    on disk).

    This is the FILE formatter - it keeps the complete, unabridged
    traceback (every library frame included) because when something
    really does need root-causing, the frame you trim away is sometimes
    the one that mattered. See _CondensedConsoleFormatter below for the
    version meant for a human skimming the live console.
    """

    def formatException(self, ei) -> str:  # noqa: N802 (stdlib name)
        original = super().formatException(ei)
        return f"\n---- traceback ----\n{original}\n---- end traceback ----"


class _Ansi:
    """Minimal ANSI SGR codes, used only when the destination stream is
    a real terminal that's expected to support them (see _supports_color
    below) - never written into the log file, and never relied on for
    meaning (level name text is always present too)."""

    RESET = "\x1b[0m"
    DIM = "\x1b[2m"
    BOLD = "\x1b[1m"
    RED = "\x1b[31m"
    YELLOW = "\x1b[33m"
    CYAN = "\x1b[36m"
    GREY = "\x1b[90m"

    LEVEL_COLORS = {
        "DEBUG": GREY,
        "INFO": CYAN,
        "WARNING": YELLOW,
        "ERROR": RED,
        "CRITICAL": BOLD + RED,
    }


def _supports_color(stream) -> bool:
    """True only when writing color escape codes to `stream` is likely
    to render as color rather than as literal garbage. NO_COLOR is a
    de-facto standard (https://no-color.org) for opting out. On Windows,
    `python -m src.main` in a modern (Windows 10 1511+) terminal does
    interpret ANSI, but the old-style console some deployments still use
    doesn't unless something has already enabled VT processing - since
    this service is commonly run non-interactively via NSSM (no console
    at all - isatty() is False there), the isatty() check below already
    excludes that case."""
    if os.getenv("NO_COLOR") is not None:
        return False
    try:
        return stream.isatty()
    except Exception:
        return False


class _CondensedConsoleFormatter(_ReadableFormatter):
    """
    Console-only formatter. Two changes from the plain one-line format:

    1. The level name gets a color (when the console supports it) so
       ERROR/CRITICAL jump out while skimming, without changing the
       text itself - anyone reading a captured/piped copy of the same
       output still sees the plain level name.

    2. A traceback is condensed rather than dumped in full: library
       frames (venv/site-packages - httpx, httpcore, tenacity, stdlib
       socket/ssl plumbing, ...) are collapsed to a one-line count per
       contiguous run, and only frames from our own code (src/...) are
       shown in full. The exception chain ("The above exception was the
       direct cause of...") is walked and summarized as one line per
       exception instead of repeating the whole stack for each link.
       The full, uncollapsed traceback is still written to the log
       file by _ReadableFormatter - this is purely a live-console
       convenience for spotting "which of MY functions was on the
       stack" without scrolling past 20 lines of transport internals.
    """

    def format(self, record: logging.LogRecord) -> str:
        # logging.Formatter.format() calls self.formatException() itself
        # (and caches the result on record.exc_text) when record.exc_info
        # is set, so overriding formatException below is enough to make
        # the base format() call produce the condensed traceback - no
        # need to build the line up manually here.
        self._use_color = _supports_color(sys.stdout)
        s = super().format(record)
        if self._use_color:
            color = _Ansi.LEVEL_COLORS.get(record.levelname, "")
            if color:
                s = s.replace(record.levelname, f"{color}{record.levelname}{_Ansi.RESET}", 1)
        return s

    def formatException(self, ei) -> str:  # noqa: N802 (stdlib name)
        return self._condensed_exception(ei, getattr(self, "_use_color", False))

    def _condensed_exception(self, ei, use_color: bool) -> str:
        exc_type, exc_value, tb = ei
        chain = []
        seen = set()
        current = exc_value
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            chain.append(current)
            current = current.__cause__ or (
                current.__context__ if not current.__suppress_context__ else None
            )
        chain.reverse()  # root cause first, matches reading order top-to-bottom

        head_color = _Ansi.RED if use_color else ""
        dim = _Ansi.DIM if use_color else ""
        reset = _Ansi.RESET if use_color else ""

        lines = [f"{dim}---- traceback (condensed - full version in the log file) ----{reset}"]
        for i, exc in enumerate(chain):
            label = "Root cause" if i == 0 else "Which caused"
            lines.append(f"{head_color}{label}: {type(exc).__name__}: {exc}{reset}")
            frames = _traceback_mod.extract_tb(exc.__traceback__)
            own_frames = [f for f in frames if any(m in f.filename for m in _OWN_CODE_MARKERS)]
            skipped = len(frames) - len(own_frames)
            if own_frames:
                for f in own_frames:
                    short_file = f.filename.split("src" + os.sep, 1)[-1]
                    lines.append(f"    src/{short_file}:{f.lineno} in {f.name}()")
            if skipped:
                lines.append(f"    {dim}... {skipped} library frame(s) skipped ...{reset}")
            elif not own_frames and frames:
                # Nothing recognized as our own code at all - show just
                # the innermost frame so there's still *something* to
                # look at instead of an empty stack.
                last = frames[-1]
                lines.append(f"    {os.path.basename(last.filename)}:{last.lineno} in {last.name}()")
        lines.append(f"{dim}---- end traceback ----{reset}")
        return "\n".join(lines)


class _RepeatSuppressFilter(logging.Filter):
    """
    Collapses a log statement that fires with the same message template
    over and over in a short span - e.g. "skipping already-synced %s
    order %s" for every order still inside the fetch window on every
    2-minute poll, or the same exception firing on every retry of a
    still-broken call - into a handful of examples plus a single
    "further repeats suppressed" marker, instead of one line (or one
    full traceback) per occurrence.

    Grouped by (logger name, level, message template) - NOT by the
    formatted/interpolated message - so e.g. "skipping already-synced
    digikala order 123" and "...order 456" are treated as the same
    recurring event even though the order id differs; what matters for
    "is this noise" is the shape of the message, not which order it
    happened to be about this time.

    First MAX_REPEATS occurrences in a WINDOW_SECONDS window pass
    through untouched. The next one is allowed through with a note
    appended so it's visible in-line that suppression just started.
    Everything after that in the same window is dropped. A new window
    starts (and the cycle repeats) once WINDOW_SECONDS has passed since
    the window began, so a genuinely still-recurring problem resurfaces
    periodically rather than going silent forever.
    """

    def __init__(self, window_seconds: int = 300, max_repeats: int = 3) -> None:
        super().__init__()
        self._window_seconds = window_seconds
        self._max_repeats = max_repeats
        self._state: dict[tuple, dict] = {}

    def filter(self, record: logging.LogRecord) -> bool:
        key = (record.name, record.levelno, record.msg)
        now = record.created
        state = self._state.get(key)

        if state is None or (now - state["window_start"]) > self._window_seconds:
            self._state[key] = {"window_start": now, "count": 1}
            return True

        state["count"] += 1
        if state["count"] <= self._max_repeats:
            return True
        if state["count"] == self._max_repeats + 1:
            record.msg = (
                f"{record.msg}  [repeats of this exact message will be "
                f"suppressed for the next {self._window_seconds}s]"
            )
            return True
        return False


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

    file_formatter = _ReadableFormatter(_FORMAT)
    console_formatter = _CondensedConsoleFormatter(_FORMAT)

    # Added to the LOGGER, not to each handler: Logger.filter() runs once
    # per record before it's dispatched to any handler, so the console
    # and file both see the same suppression decision. Adding it to each
    # handler instead would call filter() twice per record (double-
    # counting every repeat) since both handlers share one logger.
    logger.addFilter(_RepeatSuppressFilter())

    # Use encoding-safe wrapper for console output to handle non-ASCII characters
    # (e.g. Persian activity titles, product catalog titles) on Windows consoles
    # that have encoding like cp1252 and can't natively handle Unicode.
    safe_stdout = _EncodingSafeStream(sys.stdout)
    console_handler = logging.StreamHandler(safe_stdout)
    console_handler.setFormatter(console_formatter)

    file_handler = _WindowsSafeTimedRotatingFileHandler(
        _LOG_DIR / "order-sync.log", when="midnight", backupCount=30, encoding="utf-8"
    )
    file_handler.setFormatter(file_formatter)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    logger.propagate = False
    return logger