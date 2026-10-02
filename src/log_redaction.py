"""
Secret redaction for log output and exception messages.

WHY THIS EXISTS (2026-10): Didar's API takes its key as a query-string
parameter (`...?apikey=<key>`), and httpx / our own
raise_for_status_with_body() put the full request URL into the exception
message - so every Didar HTTP error wrote the live API key into
order-sync.log (and service-stdout.log, and the sync_failures table).
The Telegram Bot API has the same shape (the bot token is part of the URL
path: https://api.telegram.org/bot<token>/getUpdates).

redact_secrets() is applied in two places:
  1. src/logger.py's formatters - last line of defence, covers every log
     line and every traceback regardless of where the text came from.
  2. src/http_utils.py raise_for_status_with_body() - so the exception
     message itself (which also gets stored in the DB by
     Repository.record_failure()) never contains the secret.
"""
from __future__ import annotations

import re

REDACTED = "<redacted>"

# ?apikey=... / &api_key=... / ?access_token=... etc. (query-string secrets)
_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|"
    r"secret|client[_-]?secret|password)=)[^&\s'\"<>]+"
)

# Telegram bot token embedded in the URL path: /bot<digits>:<token>/method
_TELEGRAM_BOT_RE = re.compile(r"(/bot)\d{6,}:[A-Za-z0-9_-]{20,}")

# "Bearer <token>" style Authorization values
_BEARER_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{16,}")


def redact_secrets(text: str) -> str:
    """Return `text` with API keys / tokens replaced by <redacted>.
    Idempotent and safe on any string (including non-secret text)."""
    if not text:
        return text
    text = _QUERY_SECRET_RE.sub(lambda m: m.group(1) + REDACTED, text)
    text = _TELEGRAM_BOT_RE.sub(lambda m: m.group(1) + REDACTED, text)
    text = _BEARER_RE.sub(lambda m: m.group(1) + REDACTED, text)
    return text
