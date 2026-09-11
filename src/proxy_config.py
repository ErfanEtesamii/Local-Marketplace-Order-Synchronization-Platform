"""
Dynamic proxy-address reader.

Reads the outbound proxy URL to use (for the Telegram bot's HTTP
client) from ``proxy.txt`` in the project root, so an operator can
swap or clear the proxy without restarting the service - just edit
the file and save it.

No module outside this file should read proxy.txt directly - keeping
"what counts as a valid proxy value" and "how/when it's cached" in one
place matches the reasoning src/config.py gives for centralizing all
os.environ access in a single module.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional, Tuple

from src.logger import get_logger

log = get_logger(__name__)

# Resolve relative to the project root, the same way src/config.py's
# _ENV_PATH does (this file lives in src/, so parent.parent is root).
# Exposed as a module-level variable (rather than only a local
# constant) so it can be monkeypatched in tests instead of touching
# the real project's proxy.txt.
_PROXY_PATH = Path(__file__).resolve().parent.parent / "proxy.txt"

_VALID_PREFIXES = ("http://", "https://", "socks5://")

# path -> (mtime_at_last_read, cached_value). Keyed by path (not just
# a single slot) so tests using their own tmp_path files don't share
# a cache entry with the real project path.
_cache: Dict[Path, Tuple[float, Optional[str]]] = {}


def get_current_proxy(path: Optional[Path] = None) -> Optional[str]:
    """Return the proxy URL currently configured in proxy.txt, or
    None if there isn't one to use.

    "None" covers: the file doesn't exist, it's empty, or its content
    doesn't look like a proxy URL at all (in which case a warning is
    logged so a bad edit doesn't fail silently - it just means "no
    proxy" instead of crashing whatever tried to connect).

    The result is cached by the file's mtime: as long as proxy.txt
    hasn't changed on disk, repeated calls don't re-read it. The
    moment its mtime changes (edited and saved, replaced, touched),
    the very next call reads the new value immediately - no TTL or
    delay, since the entire point of this module is that swapping
    proxy.txt takes effect right away for the rest of the code.

    Parameters
    ----------
    path:
        Optional override of which file to read - used by tests so
        they can point at a tmp_path fixture instead of the real
        project's proxy.txt. Defaults to the module-level _PROXY_PATH.
    """
    proxy_path = path if path is not None else _PROXY_PATH

    try:
        mtime = proxy_path.stat().st_mtime
    except FileNotFoundError:
        _cache.pop(proxy_path, None)
        return None
    except OSError as exc:
        # e.g. a transient filesystem lock (seen in practice on
        # Windows). Log it and fail safe to "no proxy" - never crash
        # the caller over a proxy-file read.
        log.warning(
            "proxy_config: could not check %s (%s) - proceeding without a proxy",
            proxy_path,
            exc,
        )
        return None

    cached = _cache.get(proxy_path)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    try:
        raw = proxy_path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning(
            "proxy_config: could not read %s (%s) - proceeding without a proxy",
            proxy_path,
            exc,
        )
        return None

    value = raw.strip()

    if not value:
        _cache[proxy_path] = (mtime, None)
        return None

    if not value.startswith(_VALID_PREFIXES):
        log.warning(
            "proxy_config: %s does not start with http://, https:// or "
            "socks5:// - ignoring it and proceeding without a proxy",
            proxy_path,
        )
        _cache[proxy_path] = (mtime, None)
        return None

    _cache[proxy_path] = (mtime, value)
    return value
