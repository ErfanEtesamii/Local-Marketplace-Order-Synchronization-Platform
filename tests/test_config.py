from src.config import SnappShopConfig


def test_snappshop_disabled_by_default_when_env_var_unset(monkeypatch):
    """Regression test (client request, 2026-08): no SnappShop API access
    yet - the adapter must be OFF unless explicitly turned on, not on by
    default the way every other source is."""
    monkeypatch.delenv("SNAPPSHOP_ENABLED", raising=False)
    assert SnappShopConfig().enabled is False


def test_snappshop_enabled_when_env_var_is_true(monkeypatch):
    monkeypatch.setenv("SNAPPSHOP_ENABLED", "true")
    assert SnappShopConfig().enabled is True


def test_snappshop_enabled_flag_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("SNAPPSHOP_ENABLED", "TRUE")
    assert SnappShopConfig().enabled is True


def test_snappshop_disabled_for_any_value_other_than_true(monkeypatch):
    monkeypatch.setenv("SNAPPSHOP_ENABLED", "yes")
    assert SnappShopConfig().enabled is False
