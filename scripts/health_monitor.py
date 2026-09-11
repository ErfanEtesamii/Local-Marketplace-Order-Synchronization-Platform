"""
Standalone Telegram-connectivity health monitor.

Runs completely independently of the main OrderSyncPlatform service (see
the "تشخیص قطعی" report, section 7.4 / 8.4 point 4): if the whole NSSM
service, the proxy, or the network path itself is dead, this script must
still be runnable and must still show that to the (non-technical) office
staff. Because of that, it deliberately does NOT import anything from
src.telegram, src.config, or src.main - none of those need to be alive
(or even importable without error) for this script to report a status.

Allowed / used from the rest of the codebase:
- src.proxy_config.get_current_proxy - a pure helper that just reads
  proxy.txt, no service state involved.

Deliberately NOT used: src.logger.get_logger. That helper always points
every logger at the single shared logs/order-sync.log file (that's its
whole design - see its docstring) and also imports src.config at import
time. This script needs its own separate rotating log file
(logs/health_monitor.log, not mixed with the main service's log) and
needs to stay independent of the main config loader, so it builds a
small logger of its own instead (see _setup_logger()).

Usage:
    python scripts/health_monitor.py [--interval SECONDS]

(--interval defaults to the HEALTH_CHECK_INTERVAL_SECONDS env var, or
90 seconds if that isn't set either.)

Every cycle writes:
    status/status.json       - {"ok", "checked_at", "proxy_host", "error"}
    status/status.html       - a big, plain, self-refreshing green/red
                                page meant to be left open in a browser
                                tab by non-technical staff
    logs/health_monitor.log  - one summary line per cycle (rotating,
                                separate from logs/order-sync.log)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv

from src.proxy_config import get_current_proxy

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_PATH = _PROJECT_ROOT / ".env"
_LOG_DIR = _PROJECT_ROOT / "logs"

# Module-level variables (not just local constants) so tests can
# monkeypatch them to point at a tmp_path instead of the real
# project's status/ folder - same pattern src/proxy_config.py uses
# for _PROXY_PATH.
_STATUS_DIR = _PROJECT_ROOT / "status"
_STATUS_JSON_PATH = _STATUS_DIR / "status.json"
_STATUS_HTML_PATH = _STATUS_DIR / "status.html"

_DEFAULT_INTERVAL_SECONDS = 90
_REQUEST_TIMEOUT_SECONDS = 10.0

_TELEGRAM_GET_ME_URL_TEMPLATE = "https://api.telegram.org/bot{token}/getMe"


def _setup_logger() -> logging.Logger:
    """Build a logger writing to its own rotating file
    (logs/health_monitor.log). See the module docstring for why this
    doesn't reuse src.logger.get_logger."""
    logger = logging.getLogger("health_monitor")
    if logger.handlers:
        # Already configured (e.g. module re-imported) - don't add
        # duplicate handlers.
        return logger

    logger.setLevel(logging.INFO)
    _LOG_DIR.mkdir(exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    )

    file_handler = TimedRotatingFileHandler(
        _LOG_DIR / "health_monitor.log",
        when="midnight",
        backupCount=30,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    logger.propagate = False
    return logger


log = _setup_logger()


def _proxy_host(proxy: Optional[str]) -> Optional[str]:
    """Return only the hostname of a proxy URL - never the full URL,
    since proxy.txt may contain embedded user:pass credentials and
    this value ends up on disk in status.json/status.html/the log, all
    of which non-technical staff are told to keep open."""
    if not proxy:
        return None
    try:
        return urlparse(proxy).hostname
    except ValueError:
        return None


def write_status(ok: bool, proxy_host: Optional[str], error: Optional[str]) -> None:
    """Write status/status.json and status/status.html for one check
    result. Kept separate from the network call itself so it can be
    unit tested without performing a real request - the main loop
    below just calls this."""
    _STATUS_DIR.mkdir(parents=True, exist_ok=True)

    checked_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

    payload = {
        "ok": ok,
        "checked_at": checked_at,
        "proxy_host": proxy_host,
        "error": error,
    }
    _STATUS_JSON_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if ok:
        bg_color = "#2ecc71"
        headline = "\u2705 \u067e\u0631\u0648\u06a9\u0633\u06cc \u0645\u062a\u0635\u0644 \u0627\u0633\u062a"
    else:
        bg_color = "#e74c3c"
        headline = (
            "\u274c \u067e\u0631\u0648\u06a9\u0633\u06cc \u0642\u0637\u0639 \u0627\u0633\u062a - "
            "proxy.txt \u0631\u0627 \u0639\u0648\u0636 \u06a9\u0646\u06cc\u062f \u0648 NSSM "
            "\u0633\u0631\u0648\u06cc\u0633 \u0631\u0627 \u0631\u06cc\u200c\u0627\u0633\u062a\u0627\u0631\u062a "
            "\u0646\u06a9\u0646\u06cc\u062f\u060c \u0641\u0642\u0637 \u0686\u0646\u062f \u062b\u0627\u0646\u06cc\u0647 "
            "\u0635\u0628\u0631 \u06a9\u0646\u06cc\u062f \u062a\u0627 \u062e\u0648\u062f\u06a9\u0627\u0631 "
            "\u0648\u0635\u0644 \u0634\u0648\u062f"
        )

    proxy_label = "\u067e\u0631\u0648\u06a9\u0633\u06cc"  # "پروکسی"
    no_proxy_label = "(\u0628\u062f\u0648\u0646 \u067e\u0631\u0648\u06a9\u0633\u06cc)"  # "(بدون پروکسی)"
    checked_at_label = "\u0622\u062e\u0631\u06cc\u0646 \u0628\u0631\u0631\u0633\u06cc"  # "آخرین بررسی"
    error_label = "\u062e\u0637\u0627"  # "خطا"

    proxy_line = f"{proxy_label}: {proxy_host}" if proxy_host else f"{proxy_label}: {no_proxy_label}"
    error_line = (
        f'<p style="font-size:20px;">{error_label}: {error}</p>'
        if (error and not ok)
        else ""
    )

    html = f"""<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="10">
<title>\u0648\u0636\u0639\u06cc\u062a \u0627\u062a\u0635\u0627\u0644 \u062a\u0644\u06af\u0631\u0627\u0645</title>
<style>
  body {{
    margin: 0;
    height: 100vh;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    background-color: {bg_color};
    color: #ffffff;
    font-family: Tahoma, "Segoe UI", Arial, sans-serif;
    text-align: center;
    padding: 20px;
    box-sizing: border-box;
  }}
  h1 {{ font-size: 42px; margin: 0 0 24px 0; line-height: 1.4; max-width: 900px; }}
  p {{ margin: 6px 0; }}
</style>
</head>
<body>
  <h1>{headline}</h1>
  <p style="font-size:22px;">{proxy_line}</p>
  <p style="font-size:18px;">{checked_at_label}: {checked_at}</p>
  {error_line}
</body>
</html>
"""
    _STATUS_HTML_PATH.write_text(html, encoding="utf-8")


def _check_once(token: str) -> None:
    proxy = get_current_proxy()
    host = _proxy_host(proxy)
    error: Optional[str] = None
    ok = False

    try:
        with httpx.Client(timeout=_REQUEST_TIMEOUT_SECONDS, proxy=proxy) as client:
            response = client.get(_TELEGRAM_GET_ME_URL_TEMPLATE.format(token=token))
            response.raise_for_status()
            ok = True
    except Exception as exc:  # noqa: BLE001 - any failure at all means "not ok"
        error = str(exc)

    write_status(ok=ok, proxy_host=host, error=error)

    if ok:
        log.info("health check OK (proxy=%s)", host or "none")
    else:
        log.warning("health check FAILED (proxy=%s): %s", host or "none", error)


def _load_token() -> str:
    # Read TELEGRAM_BOT_TOKEN directly from .env - not via src.config's
    # Settings object, so this script never needs the rest of the
    # marketplace configuration to be valid just to check one token.
    load_dotenv(dotenv_path=_ENV_PATH)
    return os.getenv("TELEGRAM_BOT_TOKEN", "").strip()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Standalone Telegram/proxy connectivity health monitor."
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=int(
            os.getenv("HEALTH_CHECK_INTERVAL_SECONDS", str(_DEFAULT_INTERVAL_SECONDS))
        ),
        help=(
            "Seconds between checks (default: 90, or the "
            "HEALTH_CHECK_INTERVAL_SECONDS env var if set)."
        ),
    )
    args = parser.parse_args()

    token = _load_token()
    if not token:
        log.warning(
            "TELEGRAM_BOT_TOKEN is empty in .env - checks will fail until it's set"
        )

    log.info("health_monitor starting (interval=%ss)", args.interval)

    while True:
        try:
            _check_once(token)
        except Exception:  # noqa: BLE001 - this loop must never die
            log.exception("unexpected error in health-check cycle - continuing")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
