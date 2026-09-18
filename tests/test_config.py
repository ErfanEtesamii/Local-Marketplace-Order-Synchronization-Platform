from src.config import DigikalaConfig, Settings, SnappShopConfig


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


# --- Digikala FBD ("ارسال به انبار دیجی‌کالا") opt-in flag (2026-09) -------
# Same explicit opt-in contract as SNAPPSHOP_ENABLED above: this feature
# writes real Deals into the client's CRM, so it must be OFF unless
# someone deliberately turned it on.


def test_digikala_warehouse_disabled_by_default_when_env_var_unset(monkeypatch):
    monkeypatch.delenv("DIGIKALA_WAREHOUSE_ENABLED", raising=False)
    assert Settings().digikala_warehouse_enabled is False


def test_digikala_warehouse_enabled_when_env_var_is_true(monkeypatch):
    monkeypatch.setenv("DIGIKALA_WAREHOUSE_ENABLED", "TRUE")
    assert Settings().digikala_warehouse_enabled is True


def test_digikala_warehouse_disabled_for_any_value_other_than_true(monkeypatch):
    monkeypatch.setenv("DIGIKALA_WAREHOUSE_ENABLED", "yes")
    assert Settings().digikala_warehouse_enabled is False


def test_the_flag_is_not_inherited_by_the_second_digikala_store(monkeypatch):
    """FBD is first-store-only by client decision (there is no
    digikala2_warehouse.py), which is why the flag lives on Settings and
    not on DigikalaConfig - a per-store field would be silently picked
    up by the second store's own config too."""
    monkeypatch.setenv("DIGIKALA_WAREHOUSE_ENABLED", "true")
    assert not hasattr(DigikalaConfig(), "warehouse_enabled")