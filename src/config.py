"""
Central configuration loader.

Everything the rest of the codebase needs comes from here, and here alone
pulls values out of the environment. No module outside this file should
call os.environ / os.getenv directly - that keeps secrets handling in one
place and makes it obvious what the service depends on.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the project root regardless of current working directory.
_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=_ENV_PATH)


def _get(key: str, default: str = "") -> str:
    return os.getenv(key, default)


@dataclass(frozen=True)
class TapsiShopConfig:
    base_url: str = field(default_factory=lambda: _get("TAPSISHOP_BASE_URL"))
    auth_token: str = field(default_factory=lambda: _get("TAPSISHOP_AUTH_TOKEN"))
    webhook_token: str = field(default_factory=lambda: _get("TAPSISHOP_WEBHOOK_TOKEN"))


@dataclass(frozen=True)
class DigikalaConfig:
    base_url: str = field(default_factory=lambda: _get("DIGIKALA_BASE_URL"))
    client_code: str = field(default_factory=lambda: _get("DIGIKALA_CLIENT_CODE"))
    client_secret: str = field(default_factory=lambda: _get("DIGIKALA_CLIENT_SECRET"))
    access_token: str = field(default_factory=lambda: _get("DIGIKALA_ACCESS_TOKEN"))
    refresh_token: str = field(default_factory=lambda: _get("DIGIKALA_REFRESH_TOKEN"))


@dataclass(frozen=True)
class SnappShopConfig:
    base_url: str = field(default_factory=lambda: _get("SNAPPSHOP_BASE_URL"))
    auth_token: str = field(default_factory=lambda: _get("SNAPPSHOP_AUTH_TOKEN"))
    agent_user: str = field(default_factory=lambda: _get("SNAPPSHOP_AGENT_USER"))
    vendor_id: str = field(default_factory=lambda: _get("SNAPPSHOP_VENDOR_ID"))


@dataclass(frozen=True)
class BasalamConfig:
    base_url: str = field(default_factory=lambda: _get("BASALAM_BASE_URL"))
    access_token: str = field(default_factory=lambda: _get("BASALAM_ACCESS_TOKEN"))


@dataclass(frozen=True)
class FarazHonarConfig:
    base_url: str = field(default_factory=lambda: _get("FARAZHONAR_BASE_URL"))
    consumer_key: str = field(default_factory=lambda: _get("FARAZHONAR_CONSUMER_KEY"))
    consumer_secret: str = field(default_factory=lambda: _get("FARAZHONAR_CONSUMER_SECRET"))


@dataclass(frozen=True)
class DidarConfig:
    base_url: str = field(default_factory=lambda: _get("DIDAR_BASE_URL"))
    api_key: str = field(default_factory=lambda: _get("DIDAR_API_KEY"))
    pipeline_id: str = field(default_factory=lambda: _get("DIDAR_PIPELINE_ID"))
    pipeline_stage_id: str = field(default_factory=lambda: _get("DIDAR_PIPELINE_STAGE_ID"))


@dataclass(frozen=True)
class Settings:
    log_level: str = field(default_factory=lambda: _get("LOG_LEVEL", "INFO"))
    db_path: str = field(default_factory=lambda: _get("DB_PATH", "./data/sync.db"))
    poll_interval_seconds: int = field(
        default_factory=lambda: int(_get("POLL_INTERVAL_SECONDS", "120"))
    )
    tapsishop: TapsiShopConfig = field(default_factory=TapsiShopConfig)
    digikala: DigikalaConfig = field(default_factory=DigikalaConfig)
    snappshop: SnappShopConfig = field(default_factory=SnappShopConfig)
    basalam: BasalamConfig = field(default_factory=BasalamConfig)
    farazhonar: FarazHonarConfig = field(default_factory=FarazHonarConfig)
    didar: DidarConfig = field(default_factory=DidarConfig)


settings = Settings()
