"""
Small, pure helpers for the Digikala access/refresh token cache files
(data/digikala_tokens.json, data/digikala2_tokens.json).

Why this exists (2026-09 token-expiry fix): the three Digikala adapters
used to refresh ONLY reactively - i.e. after a request already came back
401 - and only during their own poll cycle (every POLL_INTERVAL_SECONDS).
Access tokens live ~2 hours (every refresh in the production logs
returned an expiry exactly +2h), so for a couple of minutes after each
expiry the shared cache file held an already-expired token. Faraz-Honar,
which reads that same file but is NOT allowed to refresh (the refresh
token is single-owner), saw the expired token and failed every remaining
product instantly (564 failures in <3 s on 2026-09-23 21:04).

The fix lives in the adapters (proactive refresh + adopting a newer pair
from the cache before refreshing); the pure pieces they need are here so
they exist once. This module deliberately holds NO auth flow - each
adapter still keeps its own copy of _refresh_access_token/_get, per the
project's "auth is a copy, not an import" convention (see digikala2.py /
digikala_warehouse.py docstrings). It only decodes a JWT `exp`, reads the
cache, and writes it atomically.
"""
from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path


def jwt_seconds_left(token: object, now: float | None = None) -> float | None:
    """Seconds until the access token's JWT `exp` claim (negative if it is
    already past). None when the token is not a decodable JWT with a
    numeric `exp` - callers must then do nothing clever (reactive 401
    handling still covers it). The signature is NOT verified; this is
    only a local expiry estimate."""
    if not token or not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        exp = payload.get("exp")
    except (ValueError, TypeError, AttributeError):
        return None
    if isinstance(exp, bool) or not isinstance(exp, (int, float)):
        return None
    return float(exp) - (time.time() if now is None else now)


def read_token_cache(path: str | Path) -> tuple[str, str] | None:
    """(access_token, refresh_token) from the cache file, or None if it is
    missing / unreadable / not the expected shape."""
    try:
        data = json.loads(Path(path).read_text())
        return data["access_token"], data["refresh_token"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def write_token_cache_atomic(path: str | Path, access_token: str, refresh_token: str) -> None:
    """Write the cache file so a concurrent reader (Faraz-Honar reads it
    while this service may be mid-refresh) never sees a half-written JSON
    document: write a temp file in the same folder, then os.replace it.
    On Windows os.replace can raise PermissionError for a moment if a
    reader has the target open, so retry briefly and only then fall back
    to the old direct write (same content/format as before)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"access_token": access_token, "refresh_token": refresh_token})
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(payload)
    try:
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                time.sleep(0.05 * (attempt + 1))
        path.write_text(payload)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def prefer_cached_token(
    current_access: str, cached_access: str, *, current_known_bad: bool = False
) -> bool:
    """Should an adapter drop its in-memory pair for the one in the cache?

    - Never if the cached access token is identical to ours.
    - After a 401 (`current_known_bad`): yes for any different cached
      token that is not known to be expired - another adapter/process has
      already refreshed, so refreshing again would just waste a refresh
      (and, if Digikala rotates refresh tokens, could burn the pair).
    - Otherwise (proactive check): only when the cached token is a JWT
      with strictly more life left than ours.
    """
    if cached_access == current_access:
        return False
    cached_left = jwt_seconds_left(cached_access)
    if current_known_bad:
        return cached_left is None or cached_left > 0
    if cached_left is None:
        return False
    current_left = jwt_seconds_left(current_access)
    return current_left is None or cached_left > current_left
