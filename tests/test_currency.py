import pytest

from src.config import (
    BasalamConfig,
    DigikalaConfig,
    FarazHonarConfig,
    SnappShopConfig,
    TapsiShopConfig,
)
from src.currency import to_rial
from decimal import Decimal


def test_to_rial_multiplies_toman_by_ten():
    assert to_rial(Decimal("480000"), "toman") == Decimal("4800000")


def test_to_rial_leaves_rial_unchanged():
    assert to_rial(Decimal("480000"), "rial") == Decimal("480000")


def test_to_rial_is_case_insensitive():
    assert to_rial(Decimal("100"), "TOMAN") == Decimal("1000")
    assert to_rial(Decimal("100"), "Rial") == Decimal("100")


def test_farazhonar_price_unit_defaults_to_toman(monkeypatch):
    # Confirmed by the client checking real order data (2026-08-29).
    monkeypatch.delenv("FARAZHONAR_PRICE_UNIT", raising=False)
    assert FarazHonarConfig().price_unit == "toman"


def test_digikala_price_unit_defaults_to_rial(monkeypatch):
    monkeypatch.delenv("DIGIKALA_PRICE_UNIT", raising=False)
    assert DigikalaConfig().price_unit == "rial"


def test_basalam_price_unit_defaults_to_rial(monkeypatch):
    # CONFIRMED rial (2026-09, client checked real order data) - the
    # earlier "toman" default was only inferred indirectly from the
    # official Basalam SDK's quick-start example and turned out wrong.
    # See src/currency.py's module docstring.
    monkeypatch.delenv("BASALAM_PRICE_UNIT", raising=False)
    assert BasalamConfig().price_unit == "rial"


def test_tapsishop_and_snappshop_price_unit_default_to_rial_when_unconfirmed(monkeypatch):
    # Neither vendor's docs state a currency unit - default to "rial"
    # (no conversion) rather than guessing, until confirmed otherwise.
    monkeypatch.delenv("TAPSISHOP_PRICE_UNIT", raising=False)
    monkeypatch.delenv("SNAPPSHOP_PRICE_UNIT", raising=False)
    assert TapsiShopConfig().price_unit == "rial"
    assert SnappShopConfig().price_unit == "rial"


def test_price_unit_env_var_overrides_default(monkeypatch):
    monkeypatch.setenv("TAPSISHOP_PRICE_UNIT", "toman")
    assert TapsiShopConfig().price_unit == "toman"


def test_price_unit_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("BASALAM_PRICE_UNIT", "RIAL")
    assert BasalamConfig().price_unit == "rial"


def test_invalid_price_unit_raises_immediately(monkeypatch):
    # A typo in the currency unit is a silent-money-corruption risk if
    # it's ever allowed through - fail loudly at config load instead.
    monkeypatch.setenv("BASALAM_PRICE_UNIT", "dollars")
    with pytest.raises(ValueError):
        BasalamConfig()