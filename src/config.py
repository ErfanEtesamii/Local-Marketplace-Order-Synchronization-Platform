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
    api_id: str = field(default_factory=lambda: _get("DIDAR_API_ID"))  # not confirmed to be needed in requests - kept for reference/future use
    bizdomain_id: str = field(default_factory=lambda: _get("DIDAR_BIZDOMAIN_ID"))
    pipeline_id: str = field(default_factory=lambda: _get("DIDAR_PIPELINE_ID"))
    pipeline_stage_id: str = field(default_factory=lambda: _get("DIDAR_PIPELINE_STAGE_ID"))
    # Required by POST /product/save whenever a new product is auto-created
    # (see src/didar/product_client.py) - Didar rejects the call with
    # "product category is empty" if ProductCategoryId is missing.
    # Confirmed via Didar's own docs: fetch valid Ids from
    # POST /product/categories?apikey=... - see .env.example.
    default_product_category_id: str = field(
        default_factory=lambda: _get("DIDAR_DEFAULT_PRODUCT_CATEGORY_ID")
    )
    # Label (Tag) GUIDs, one per marketplace source - see docs/architecture.md
    # for how to fetch these via GET /Tag/GetTagList. Any left blank simply
    # means that source's Deals won't carry a LabelId (not an error).
    label_tapsishop: str = field(default_factory=lambda: _get("DIDAR_LABEL_TAPSISHOP"))
    label_digikala: str = field(default_factory=lambda: _get("DIDAR_LABEL_DIGIKALA"))
    label_basalam: str = field(default_factory=lambda: _get("DIDAR_LABEL_BASALAM"))
    label_snappshop: str = field(default_factory=lambda: _get("DIDAR_LABEL_SNAPPSHOP"))
    label_farazhonar: str = field(default_factory=lambda: _get("DIDAR_LABEL_FARAZHONAR"))

    @property
    def label_by_source(self) -> dict[str, str]:
        return {
            "tapsishop": self.label_tapsishop,
            "digikala": self.label_digikala,
            "basalam": self.label_basalam,
            "snappshop": self.label_snappshop,
            "farazhonar": self.label_farazhonar,
        }


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