"""Secret redaction (2026-10) - Didar ?apikey= and Telegram bot token must
never reach logs or exception messages."""
import logging

import httpx

from src.http_utils import raise_for_status_with_body
from src.log_redaction import REDACTED, redact_secrets
from src.logger import _CondensedConsoleFormatter, _ReadableFormatter

KEY = "abc123SECRETkey456"
BOT = "8913712345:AAE-abcdefghijklmnopqrstuvwxyz0123456"


def test_didar_apikey_redacted():
    out = redact_secrets(f"400 Bad Request for url 'https://app.didar.me/api/product/save?apikey={KEY}': x")
    assert KEY not in out
    assert f"?apikey={REDACTED}'" in out


def test_apikey_in_middle_of_query():
    out = redact_secrets(f"https://x/y?a=1&api_key={KEY}&b=2")
    assert KEY not in out and "a=1" in out and "&b=2" in out


def test_telegram_bot_token_redacted():
    out = redact_secrets(f"502 for url 'https://api.telegram.org/bot{BOT}/getUpdates'")
    assert BOT not in out
    assert f"/bot{REDACTED}/getUpdates" in out


def test_bearer_redacted():
    out = redact_secrets("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig")
    assert "eyJhbGci" not in out


def test_plain_text_untouched_and_idempotent():
    s = "didar: created deal for snappshop order 551790613 -> Id=2036f182"
    assert redact_secrets(s) == s
    once = redact_secrets(f"?apikey={KEY}")
    assert redact_secrets(once) == once
    assert redact_secrets("") == ""


def test_raise_for_status_message_is_redacted():
    req = httpx.Request("POST", f"https://app.didar.me/api/product/save?apikey={KEY}")
    resp = httpx.Response(400, request=req, text='{"Error":"duplicate product code."}')
    try:
        raise_for_status_with_body(resp)
    except httpx.HTTPStatusError as exc:
        assert KEY not in str(exc)
        assert "duplicate product code" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected HTTPStatusError")


def _record_with_exc():
    try:
        raise RuntimeError(f"boom https://app.didar.me/x?apikey={KEY}")
    except RuntimeError:
        import sys
        return logging.LogRecord("t", logging.ERROR, __file__, 1, f"failed ?apikey={KEY}", None, sys.exc_info())


def test_file_formatter_redacts_message_and_traceback():
    text = _ReadableFormatter("%(message)s").format(_record_with_exc())
    assert KEY not in text
    assert REDACTED in text


def test_console_formatter_redacts_message_and_traceback():
    text = _CondensedConsoleFormatter("%(message)s").format(_record_with_exc())
    assert KEY not in text
