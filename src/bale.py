"""Bale delivery channel for the order-sync bot.

Bale's Bot API follows Telegram's update/message/inline-keyboard shape.
Keeping the business and report UI in :class:`TelegramNotifier` therefore
lets both messengers expose exactly the same features without maintaining two
copies of the Persian report picker.
"""
from __future__ import annotations

import os

import httpx

from src.logger import get_logger
from src.telegram import TelegramError, TelegramNotifier

log = get_logger(__name__)

_BALE_API_BASE = "https://tapi.bale.ai"

# Rollover markers are stored in the shared ``report_progress`` table.
# Telegram uses the bare names ("day", "week", ...); Bale gets its own
# prefixed copies so that whichever notifier runs first in a poll cycle
# cannot advance the marker and make the other one skip its report.
_BALE_MARKER_PREFIX = "bale_"
_ROLLOVER_PERIODS = frozenset({"day", "week", "month", "year"})


class _BaleMarkerRepository:
    """Thin proxy over ``Repository`` that namespaces rollover markers.

    Everything except get/set_report_marker for the four rollover periods
    is delegated untouched, so report queries behave exactly as for Telegram.
    """

    def __init__(self, repository) -> None:
        self._repository = repository

    @staticmethod
    def _key(period: str) -> str:
        if period in _ROLLOVER_PERIODS:
            return _BALE_MARKER_PREFIX + period
        return period

    def get_report_marker(self, period: str):
        return self._repository.get_report_marker(self._key(period))

    def set_report_marker(self, period: str, marker: str) -> None:
        self._repository.set_report_marker(self._key(period), marker)

    def __getattr__(self, name):
        return getattr(self._repository, name)


class BaleNotifier(TelegramNotifier):
    """Bale counterpart of ``TelegramNotifier``.

    Configure ``BALE_BOT_TOKEN`` plus one or more ``BALE_CHAT_ID[_N]``
    values. Numeric private/group IDs and ``@channel_username`` recipients
    are both accepted by Bale. All order alerts, warehouse alerts, Didar
    deal alerts, scheduled reports, custom-range picker and outage alerts
    are inherited from the shared implementation.
    """

    _OFFSET_MARKER = "bale_update_offset"

    def is_configured(self) -> bool:
        if self._configured:
            return True
        token = os.getenv("BALE_BOT_TOKEN", "").strip()
        raw_ids = self._collect_raw_chat_ids()
        if not token or not raw_ids:
            log.debug("bale: credentials not set, notifications disabled")
            return False

        chat_ids = []
        for raw in raw_ids:
            if raw.startswith("@"):
                chat_ids.append(raw)
            else:
                try:
                    chat_ids.append(int(raw))
                except ValueError:
                    log.warning("bale: invalid chat id %r - skipping", raw)
        if not chat_ids:
            return False

        client = httpx.Client(base_url=f"{_BALE_API_BASE}/bot{token}", timeout=15.0)
        try:
            self._request(client, "getMe")
        except TelegramError as exc:
            log.warning("bale: getMe failed (%s) - notifications disabled", exc)
            client.close()
            return False
        except Exception:
            log.exception("bale: unexpected error during getMe")
            client.close()
            return False

        self._client = client
        self._chat_ids = chat_ids
        self._chat_names = self._collect_raw_chat_names()
        self._configured = True
        log.info("bale: bot configured (%d recipient(s): %r)", len(chat_ids), chat_ids)
        return True

    def _has_credentials(self) -> bool:
        return bool(os.getenv("BALE_BOT_TOKEN", "").strip() and self._collect_raw_chat_ids())

    @staticmethod
    def _collect_raw_chat_ids() -> list[str]:
        values = [os.getenv("BALE_CHAT_ID", "").strip()]
        values.extend(os.getenv(f"BALE_CHAT_ID_{i}", "").strip() for i in range(1, 11))
        return list(dict.fromkeys(value for value in values if value))

    @staticmethod
    def _collect_raw_chat_names() -> dict[str, str]:
        names: dict[str, str] = {}
        chat_id = os.getenv("BALE_CHAT_ID", "").strip()
        name = os.getenv("BALE_CHAT_NAME", "").strip()
        if chat_id and name:
            names[chat_id] = name
        for i in range(1, 11):
            chat_id = os.getenv(f"BALE_CHAT_ID_{i}", "").strip()
            name = os.getenv(f"BALE_CHAT_NAME_{i}", "").strip()
            if chat_id and name:
                names[chat_id] = name
        return names

    def _failure_ref(self, ref_id: str) -> str:
        return f"bale:{ref_id}"

    def _owns_failure(self, ref_id: str) -> bool:
        return ref_id.startswith("bale:")

    def check_and_send_reports(self, repository, source_names: list[str]) -> None:
        """Scheduled daily/weekly/monthly/yearly reports with Bale-only markers."""
        super().check_and_send_reports(_BaleMarkerRepository(repository), source_names)

    def poll_updates(self, repository, source_names: list[str]) -> None:
        """Run the same picker against Bale, with an independent offset."""
        if not self.is_configured():
            return
        try:
            marker = repository.get_report_marker(self._OFFSET_MARKER)
            if marker is None:
                payload = self._request(self._client, "getUpdates", offset=-1, timeout=0)
                results = payload.get("result", [])
                repository.set_report_marker(
                    self._OFFSET_MARKER, str(results[-1]["update_id"] if results else 0)
                )
                return
            payload = self._request(
                self._client, "getUpdates", offset=int(marker) + 1, timeout=0,
                allowed_updates=["message", "callback_query"],
            )
        except TelegramError as exc:
            log.warning("bale: getUpdates failed: %s", exc)
            return
        except Exception:
            log.exception("bale: unexpected error polling updates")
            return

        maximum = None
        for update in payload.get("result", []):
            maximum = update.get("update_id")
            try:
                self._handle_report_update(update, repository, source_names)
            except Exception:
                log.exception("bale: failed to handle update %s", maximum)
        if maximum is not None:
            repository.set_report_marker(self._OFFSET_MARKER, str(maximum))
