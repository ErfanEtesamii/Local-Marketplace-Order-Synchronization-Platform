"""
Outage alerts for marketplace sources (2026-10).

WHY THIS EXISTS: on 2026-10-11 ~17:05 (Iran time) Digikala started
answering every request with 401 "Invalid User-Agent!". All three Digikala
pollers failed every cycle for ~17 hours and nobody knew, because a failed
fetch was only logged. Orders were not lost (the watermark never advanced)
but they reached Didar/Telegram many hours late.

DESIGN:
  - SourceStatusTracker: SyncEngine records, per source, every fetch
    success / failure. A source is "failing" from its first failure until
    its next success; the timestamp of that first failure and the last
    error message are kept. Works for every source, including the Digikala
    FBD warehouse source (whose get_last_sync_time is a data floor, not a
    heartbeat, so check_health() can't be used for it).
  - SourceOutageAlerter: called once per poll cycle. When a source has
    been failing continuously for >= STALE_ALERT_MINUTES it sends ONE
    Telegram alert (with the last error), then a reminder every
    SOURCE_ALERT_REMINDER_MINUTES while it is still down, and a
    "recovered" message when it comes back. A message is only counted as
    sent if the send succeeded, so a Telegram outage is retried next
    cycle instead of silently swallowing the alert.

State is in-memory only: after a restart the clock starts again from the
first failure. Best-effort by design - never raises into the poll cycle.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from src.logger import get_logger

log = get_logger(__name__)

DEFAULT_ALERT_AFTER_MINUTES = 30
DEFAULT_REMINDER_MINUTES = 120
_MAX_ERROR_CHARS = 300


def _env_minutes(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


@dataclass
class SourceState:
    first_failure_at: datetime | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    # alerter bookkeeping
    alerted: bool = False
    last_alert_at: datetime | None = None


class SourceStatusTracker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._states: dict[str, SourceState] = {}

    def record_success(self, source: str) -> None:
        with self._lock:
            state = self._states.setdefault(source, SourceState())
            state.first_failure_at = None
            state.last_error = None
            state.consecutive_failures = 0

    def record_failure(
        self, source: str, exc: BaseException, now: datetime | None = None
    ) -> None:
        now = now or datetime.now(timezone.utc)
        message = f"{type(exc).__name__}: {exc}"
        if len(message) > _MAX_ERROR_CHARS:
            message = message[:_MAX_ERROR_CHARS] + "..."
        with self._lock:
            state = self._states.setdefault(source, SourceState())
            if state.first_failure_at is None:
                state.first_failure_at = now
            state.consecutive_failures += 1
            state.last_error = message

    def snapshot(self) -> dict[str, SourceState]:
        with self._lock:
            return dict(self._states)

    def state(self, source: str) -> SourceState:
        with self._lock:
            return self._states.setdefault(source, SourceState())


def _fmt_duration(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f"{hours} ساعت و {mins} دقیقه"
    if hours:
        return f"{hours} ساعت"
    return f"{mins} دقیقه"


class SourceOutageAlerter:
    def __init__(
        self,
        tracker: SourceStatusTracker,
        send: Callable[[str], bool],
        alert_after: timedelta | None = None,
        reminder_every: timedelta | None = None,
    ) -> None:
        self._tracker = tracker
        self._send = send
        self._alert_after = alert_after or timedelta(
            minutes=_env_minutes("STALE_ALERT_MINUTES", DEFAULT_ALERT_AFTER_MINUTES)
        )
        self._reminder_every = reminder_every or timedelta(
            minutes=_env_minutes("SOURCE_ALERT_REMINDER_MINUTES", DEFAULT_REMINDER_MINUTES)
        )

    def check(self, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        try:
            for source, state in self._tracker.snapshot().items():
                self._check_one(source, state, now)
        except Exception:  # never break the poll cycle
            log.exception("source_alerts: check failed")

    def _check_one(self, source: str, state: SourceState, now: datetime) -> None:
        failing = state.first_failure_at is not None
        if failing:
            down_for = now - state.first_failure_at
            if down_for < self._alert_after:
                return
            if not state.alerted:
                text = (
                    f"🚨 هشدار قطعی سینک: «{source}»\n"
                    f"{_fmt_duration(down_for)} است که دریافت سفارش از این منبع خطا می‌دهد "
                    f"({state.consecutive_failures} بار تلاش ناموفق).\n"
                    f"آخرین خطا: {state.last_error}\n\n"
                    "سفارش‌های جدید این منبع تا رفع مشکل وارد دیدار و تلگرام نمی‌شوند؛ "
                    "لاگ سرویس را بررسی کنید."
                )
                if self._send(text):
                    state.alerted = True
                    state.last_alert_at = now
                    log.warning("source_alerts: outage alert sent for %s", source)
            elif state.last_alert_at is None or now - state.last_alert_at >= self._reminder_every:
                text = (
                    f"⏰ یادآوری: «{source}» هنوز {_fmt_duration(down_for)} است که خطا می‌دهد.\n"
                    f"آخرین خطا: {state.last_error}"
                )
                if self._send(text):
                    state.last_alert_at = now
        elif state.alerted:
            text = f"✅ «{source}» دوباره به‌درستی کار می‌کند و سفارش‌ها دریافت می‌شوند."
            if self._send(text):
                state.alerted = False
                state.last_alert_at = None
                log.info("source_alerts: recovery notice sent for %s", source)
