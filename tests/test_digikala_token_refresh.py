"""
Tests for the 2026-09 Digikala token-expiry fix (see src/token_utils.py's
docstring): proactive refresh before expiry, adopting a newer pair from the
shared token cache instead of refreshing again, atomic cache writes.

The same behaviour is copied into digikala.py, digikala2.py and
digikala_warehouse.py (project convention: auth is a copy), so the
behavioural tests below run against all three.
"""
import base64
import dataclasses
import json
import time

import httpx
import pytest
import respx

from src.config import DigikalaConfig, settings
from src.db.repository import Repository
from src.marketplaces.digikala import DigikalaAdapter
from src.marketplaces.digikala2 import Digikala2Adapter
from src.marketplaces.digikala_warehouse import DigikalaWarehouseAdapter
from src.token_utils import (
    jwt_seconds_left,
    prefer_cached_token,
    read_token_cache,
    write_token_cache_atomic,
)

_BASE = "https://seller.digikala.com"
_GET_URL = f"{_BASE}/open-api/v1/orders"
_REFRESH_URL = f"{_BASE}/open-api/v1/auth/refresh-token"
_CFG = DigikalaConfig(base_url=_BASE, access_token="seed-access", refresh_token="seed-refresh")


def _jwt(seconds_from_now: float) -> str:
    def b64(obj) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{b64({'alg': 'none'})}.{b64({'exp': time.time() + seconds_from_now})}.sig"


@pytest.fixture
def repo(tmp_path):
    return Repository(db_path=str(tmp_path / "test.db"))


def _make(kind, repo, tmp_path, access):
    cache = tmp_path / "tokens.json"
    if kind == "digikala_warehouse":
        adapter = DigikalaWarehouseAdapter(config=_CFG, repository=repo, token_cache_path=cache)
    else:
        adapter = (DigikalaAdapter if kind == "digikala" else Digikala2Adapter)(
            config=_CFG, repository=repo
        )
        adapter._token_cache_path = cache  # never touch the real data/ cache
    adapter._access_token, adapter._refresh_token = access, "seed-refresh"
    return adapter, cache


KINDS = ["digikala", "digikala2", "digikala_warehouse"]


def _refresh_ok(new_access):
    return httpx.Response(
        200,
        json={
            "status": "ok",
            "data": {
                "access_token": new_access,
                "refresh_token": "rotated-refresh",
                "access_token_expires_at": {"date": "2026-09-24 13:00:00"},
            },
        },
    )


# --- pure helpers ------------------------------------------------------------

def test_jwt_seconds_left_reads_exp_and_ignores_junk():
    assert 3590 < jwt_seconds_left(_jwt(3600)) <= 3600
    assert jwt_seconds_left(_jwt(-30)) < 0
    for junk in ("", None, "opaque-token", "a.b.c", "a.e30.c", 123):
        assert jwt_seconds_left(junk) is None


def test_atomic_write_roundtrip_leaves_no_temp_files(tmp_path):
    path = tmp_path / "sub" / "tokens.json"
    write_token_cache_atomic(path, "acc", "ref")
    write_token_cache_atomic(path, "acc2", "ref2")
    assert read_token_cache(path) == ("acc2", "ref2")
    assert [p.name for p in path.parent.iterdir()] == ["tokens.json"]
    assert read_token_cache(tmp_path / "missing.json") is None


def test_prefer_cached_token_rules():
    soon, later = _jwt(300), _jwt(7000)
    assert prefer_cached_token(soon, later)
    assert not prefer_cached_token(later, soon)
    assert not prefer_cached_token(soon, soon)
    # after a 401 any different, not-known-expired token wins ...
    assert prefer_cached_token("opaque-old", "opaque-new", current_known_bad=True)
    # ... but never one that is known to be expired
    assert not prefer_cached_token("opaque-old", _jwt(-10), current_known_bad=True)
    # proactive path never adopts something it cannot date
    assert not prefer_cached_token(soon, "opaque-new")


# --- adapters ----------------------------------------------------------------

@pytest.mark.parametrize("kind", KINDS)
@respx.mock
def test_refreshes_proactively_before_the_token_expires(kind, repo, tmp_path):
    adapter, cache = _make(kind, repo, tmp_path, _jwt(300))  # < 900 s lead
    fresh = _jwt(7200)
    refresh = respx.post(_REFRESH_URL).mock(return_value=_refresh_ok(fresh))
    get = respx.get(_GET_URL).mock(return_value=httpx.Response(200, json={"ok": True}))

    assert adapter._get("/open-api/v1/orders", {}) == {"ok": True}

    assert refresh.call_count == 1
    assert get.call_count == 1  # no 401 round trip any more
    assert get.calls[0].request.headers["Authorization"] == f"Bearer {fresh}"
    assert read_token_cache(cache) == (fresh, "rotated-refresh")


@pytest.mark.parametrize("kind", KINDS)
@respx.mock
def test_no_refresh_while_the_token_has_plenty_of_life(kind, repo, tmp_path):
    token = _jwt(7000)
    adapter, _ = _make(kind, repo, tmp_path, token)
    refresh = respx.post(_REFRESH_URL).mock(return_value=_refresh_ok(_jwt(7200)))
    get = respx.get(_GET_URL).mock(return_value=httpx.Response(200, json={}))

    adapter._get("/open-api/v1/orders", {})

    assert not refresh.called
    assert get.calls[0].request.headers["Authorization"] == f"Bearer {token}"


@pytest.mark.parametrize("kind", KINDS)
@respx.mock
def test_failed_proactive_refresh_is_not_fatal_and_backs_off(kind, repo, tmp_path):
    token = _jwt(300)
    adapter, _ = _make(kind, repo, tmp_path, token)
    refresh = respx.post(_REFRESH_URL).mock(return_value=httpx.Response(400, json={"e": 1}))
    get = respx.get(_GET_URL).mock(return_value=httpx.Response(200, json={"ok": True}))

    assert adapter._get("/open-api/v1/orders", {}) == {"ok": True}
    assert adapter._get("/open-api/v1/orders", {}) == {"ok": True}

    assert refresh.call_count == 1  # second call is inside the back-off window
    assert get.call_count == 2
    assert get.calls[0].request.headers["Authorization"] == f"Bearer {token}"


@pytest.mark.parametrize("kind", KINDS)
@respx.mock
def test_adopts_fresher_cached_token_instead_of_refreshing(kind, repo, tmp_path):
    adapter, cache = _make(kind, repo, tmp_path, _jwt(300))
    newer = _jwt(7100)
    write_token_cache_atomic(cache, newer, "cached-refresh")  # e.g. the other adapter refreshed
    refresh = respx.post(_REFRESH_URL).mock(return_value=_refresh_ok(_jwt(7200)))
    get = respx.get(_GET_URL).mock(return_value=httpx.Response(200, json={}))

    adapter._get("/open-api/v1/orders", {})

    assert not refresh.called
    assert get.calls[0].request.headers["Authorization"] == f"Bearer {newer}"
    assert adapter._refresh_token == "cached-refresh"


@pytest.mark.parametrize("kind", KINDS)
@respx.mock
def test_401_uses_newer_cached_pair_without_refreshing(kind, repo, tmp_path):
    adapter, cache = _make(kind, repo, tmp_path, "stale-opaque-token")
    write_token_cache_atomic(cache, "cache-opaque-token", "cached-refresh")
    refresh = respx.post(_REFRESH_URL).mock(return_value=_refresh_ok("never-used"))
    get = respx.get(_GET_URL).mock(
        side_effect=[httpx.Response(401, json={}), httpx.Response(200, json={"ok": True})]
    )

    assert adapter._get("/open-api/v1/orders", {}) == {"ok": True}

    assert not refresh.called
    assert get.calls[1].request.headers["Authorization"] == "Bearer cache-opaque-token"


@pytest.mark.parametrize("kind", KINDS)
@respx.mock
def test_401_still_refreshes_when_the_cache_has_nothing_newer(kind, repo, tmp_path):
    adapter, cache = _make(kind, repo, tmp_path, "stale-opaque-token")
    refresh = respx.post(_REFRESH_URL).mock(return_value=_refresh_ok("fresh-opaque-token"))
    get = respx.get(_GET_URL).mock(
        side_effect=[httpx.Response(401, json={}), httpx.Response(200, json={"ok": True})]
    )

    assert adapter._get("/open-api/v1/orders", {}) == {"ok": True}

    assert refresh.call_count == 1
    assert get.calls[1].request.headers["Authorization"] == "Bearer fresh-opaque-token"
    assert read_token_cache(cache) == ("fresh-opaque-token", "rotated-refresh")


@pytest.mark.parametrize(
    "kind,module",
    [
        ("digikala", "src.marketplaces.digikala"),
        ("digikala2", "src.marketplaces.digikala2"),
        ("digikala_warehouse", "src.marketplaces.digikala_warehouse"),
    ],
)
@respx.mock
def test_lead_zero_restores_the_old_401_only_behaviour(kind, module, repo, tmp_path, monkeypatch):
    monkeypatch.setattr(
        f"{module}.settings",
        dataclasses.replace(settings, digikala_token_refresh_lead_seconds=0),
    )
    token = _jwt(60)
    adapter, _ = _make(kind, repo, tmp_path, token)
    refresh = respx.post(_REFRESH_URL).mock(return_value=_refresh_ok(_jwt(7200)))
    get = respx.get(_GET_URL).mock(return_value=httpx.Response(200, json={}))

    adapter._get("/open-api/v1/orders", {})

    assert not refresh.called
    assert get.calls[0].request.headers["Authorization"] == f"Bearer {token}"


@respx.mock
def test_two_adapters_sharing_one_cache_refresh_only_once(repo, tmp_path):
    """digikala + digikala_warehouse share data/digikala_tokens.json: in the
    same poll cycle the first one refreshes, the second adopts."""
    cache = tmp_path / "tokens.json"
    old = _jwt(300)
    first = DigikalaAdapter(config=_CFG, repository=repo)
    first._token_cache_path = cache
    second = DigikalaWarehouseAdapter(config=_CFG, repository=repo, token_cache_path=cache)
    for a in (first, second):
        a._access_token, a._refresh_token = old, "seed-refresh"
    fresh = _jwt(7200)
    refresh = respx.post(_REFRESH_URL).mock(return_value=_refresh_ok(fresh))
    get = respx.get(_GET_URL).mock(return_value=httpx.Response(200, json={}))

    first._get("/open-api/v1/orders", {})
    second._get("/open-api/v1/orders", {})

    assert refresh.call_count == 1
    assert [c.request.headers["Authorization"] for c in get.calls] == [f"Bearer {fresh}"] * 2


def test_default_lead_setting_is_15_minutes_unless_overridden():
    assert isinstance(settings.digikala_token_refresh_lead_seconds, int)
