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


def _get_price_unit(key: str, default: str) -> str:
    """Reads a <SOURCE>_PRICE_UNIT env var - must be "toman" or "rial"
    (case-insensitive). Fails loudly on anything else rather than
    silently treating a typo as "rial" (no conversion) - wrong money
    should be visible immediately, not discovered later in Didar."""
    value = _get(key, default).strip().lower()
    if value not in ("toman", "rial"):
        raise ValueError(
            f"{key}={value!r} is invalid - must be 'toman' or 'rial' "
            f"(see src/currency.py for what each source is currently set to)"
        )
    return value


@dataclass(frozen=True)
class TapsiShopConfig:
    base_url: str = field(default_factory=lambda: _get("TAPSISHOP_BASE_URL"))
    auth_token: str = field(default_factory=lambda: _get("TAPSISHOP_AUTH_TOKEN"))
    webhook_token: str = field(default_factory=lambda: _get("TAPSISHOP_WEBHOOK_TOKEN"))
    # UNCONFIRMED - see src/currency.py's module docstring. Defaults to
    # "rial" (no conversion) until someone checks a real order.
    price_unit: str = field(
        default_factory=lambda: _get_price_unit("TAPSISHOP_PRICE_UNIT", "rial")
    )


@dataclass(frozen=True)
class DigikalaConfig:
    base_url: str = field(default_factory=lambda: _get("DIGIKALA_BASE_URL"))
    client_code: str = field(default_factory=lambda: _get("DIGIKALA_CLIENT_CODE"))
    client_secret: str = field(default_factory=lambda: _get("DIGIKALA_CLIENT_SECRET"))
    access_token: str = field(default_factory=lambda: _get("DIGIKALA_ACCESS_TOKEN"))
    refresh_token: str = field(default_factory=lambda: _get("DIGIKALA_REFRESH_TOKEN"))
    # Digikala's web service is documented as Rial-based - see
    # src/currency.py's module docstring for the source/confidence.
    price_unit: str = field(
        default_factory=lambda: _get_price_unit("DIGIKALA_PRICE_UNIT", "rial")
    )


@dataclass(frozen=True)
class SnappShopConfig:
    # Explicit opt-in switch (client request, 2026-08: "غیرفعالش کن اجرا
    # نشه" - SnappShop API access hasn't been granted yet, keep it out
    # of the poll loop entirely rather than letting it fail every cycle
    # with a config error). Defaults to disabled - set
    # SNAPPSHOP_ENABLED=true in .env once real credentials exist.
    enabled: bool = field(default_factory=lambda: _get("SNAPPSHOP_ENABLED", "false").lower() == "true")
    base_url: str = field(default_factory=lambda: _get("SNAPPSHOP_BASE_URL"))
    auth_token: str = field(default_factory=lambda: _get("SNAPPSHOP_AUTH_TOKEN"))
    agent_user: str = field(default_factory=lambda: _get("SNAPPSHOP_AGENT_USER"))
    vendor_id: str = field(default_factory=lambda: _get("SNAPPSHOP_VENDOR_ID"))
    # UNCONFIRMED - see src/currency.py's module docstring. Defaults to
    # "rial" (no conversion) until someone checks a real order.
    price_unit: str = field(
        default_factory=lambda: _get_price_unit("SNAPPSHOP_PRICE_UNIT", "rial")
    )


@dataclass(frozen=True)
class BasalamConfig:
    base_url: str = field(default_factory=lambda: _get("BASALAM_BASE_URL"))
    access_token: str = field(default_factory=lambda: _get("BASALAM_ACCESS_TOKEN"))
    # Best-guess default, not confirmed against a live order - see
    # src/currency.py's module docstring for the (indirect) evidence.
    price_unit: str = field(
        default_factory=lambda: _get_price_unit("BASALAM_PRICE_UNIT", "toman")
    )


@dataclass(frozen=True)
class FarazHonarConfig:
    base_url: str = field(default_factory=lambda: _get("FARAZHONAR_BASE_URL"))
    consumer_key: str = field(default_factory=lambda: _get("FARAZHONAR_CONSUMER_KEY"))
    consumer_secret: str = field(default_factory=lambda: _get("FARAZHONAR_CONSUMER_SECRET"))
    # Confirmed by the client checking real order data (2026-08-29).
    price_unit: str = field(
        default_factory=lambda: _get_price_unit("FARAZHONAR_PRICE_UNIT", "toman")
    )


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
    # Path to the client-maintained Excel export of the existing Didar
    # product catalog (columns: عنوان محصول / کد محصول) - see
    # src/didar/product_catalog.py for how it's used to recover a
    # product's real Didar Code from its marketplace title. Blank
    # (default) disables catalog-based Code lookup entirely - every
    # item then falls back to the marketplace SKU/title, same as before
    # this feature existed.
    product_catalog_xlsx: str = field(
        default_factory=lambda: _get("DIDAR_PRODUCT_CATALOG_XLSX")
    )
    # Deal Labels (a distinct Didar concept from Tags - confirmed 2026-09
    # directly from Didar's own support agent, after the earlier LabelId
    # implementation below turned out to be based on a wrong assumption
    # (treating this as a Tag, matched via GET /Tag/GetTagList, with a
    # manually hardcoded GUID per source and a singular "LabelId" field).
    #
    # The confirmed, correct flow: GET /Label/GetDealLabels returns every
    # Deal Label as {Id, Title, Code, Type} - the caller matches by
    # Title and sends the resulting Id(s) as a LIST in Deal.save's
    # "LabelIds" (not the old singular "LabelId") - see
    # DidarDealClient._label_id_for_source().
    #
    # One Title per marketplace source rather than a hardcoded GUID, so
    # this survives the label being deleted/recreated in Didar (a new
    # GUID) without a code or .env change - same tradeoff as the
    # category-by-title matching in product_client.py. "سایت فرامرزی"
    # for farazhonar is CONFIRMED (seen directly in a screenshot of this
    # account's Deal Labels, 2026-09); the other four are best guesses
    # and MUST be verified against a live GET /Label/GetDealLabels
    # response for this account. Matching is exact after Persian
    # normalization (see _normalize_fa) - a Title here that doesn't
    # match what's actually in Didar just means that source's Deals get
    # created without a label (logged as a warning), never an error.
    deal_label_title_tapsishop: str = field(
        default_factory=lambda: _get("DIDAR_DEAL_LABEL_TITLE_TAPSISHOP", "تپسی")
    )
    deal_label_title_digikala: str = field(
        default_factory=lambda: _get("DIDAR_DEAL_LABEL_TITLE_DIGIKALA", "دیجی کالا")
    )
    deal_label_title_basalam: str = field(
        default_factory=lambda: _get("DIDAR_DEAL_LABEL_TITLE_BASALAM", "باسلام")
    )
    deal_label_title_snappshop: str = field(
        default_factory=lambda: _get("DIDAR_DEAL_LABEL_TITLE_SNAPPSHOP", "اسنپ")
    )
    deal_label_title_farazhonar: str = field(
        default_factory=lambda: _get("DIDAR_DEAL_LABEL_TITLE_FARAZHONAR", "سایت فرامرزی")
    )
    # Confirmed 2026-09 from Didar's own support agent. Relative to
    # base_url (same convention as every other path in this project).
    get_deal_labels_path: str = field(
        default_factory=lambda: _get("DIDAR_GET_DEAL_LABELS_PATH", "/Label/GetDealLabels")
    )
    # Optional - Activity.OwnerId is always present in the docs' own
    # /activity/save example, but NOT confirmed required (create_deal()
    # already works fine without ever setting Deal's OwnerId - see
    # deal_client.py). Left blank, OwnerId is simply omitted from the
    # request, same as LabelId below when a source has none configured.
    default_owner_id: str = field(default_factory=lambda: _get("DIDAR_DEFAULT_OWNER_ID"))
    # Post-sale checklist Activity types (src/didar/activity_client.py) -
    # confirmed live for this account via POST /activity/GetActivityType
    # (2026-08): this account already has ONE dedicated ActivityType per
    # checklist item (not just generic call/sms/task buckets), so each
    # item gets its own exact Id rather than sharing one per category.
    # Any left blank means the whole checklist is skipped (logged), not
    # partially created - see .env.example.
    activity_type_new_call_id: str = field(
        default_factory=lambda: _get("DIDAR_ACTIVITY_TYPE_NEW_CALL_ID")
    )
    activity_type_sms1_id: str = field(default_factory=lambda: _get("DIDAR_ACTIVITY_TYPE_SMS1_ID"))
    activity_type_sms2_id: str = field(default_factory=lambda: _get("DIDAR_ACTIVITY_TYPE_SMS2_ID"))
    activity_type_sms3_id: str = field(default_factory=lambda: _get("DIDAR_ACTIVITY_TYPE_SMS3_ID"))
    activity_type_ship_id: str = field(default_factory=lambda: _get("DIDAR_ACTIVITY_TYPE_SHIP_ID"))
    activity_type_satisfaction_call_id: str = field(
        default_factory=lambda: _get("DIDAR_ACTIVITY_TYPE_SATISFACTION_CALL_ID")
    )
    # CONFIRMED (2026-09) directly from Didar's own support agent,
    # citing "Attach Files To Activity" in their docs - this SUPERSEDES
    # an earlier guess (POST /file/upload + Activity.save's NewAttachments)
    # that was never independently verified and turned out not to match
    # the documented flow at all. The real flow is two calls: (1) create
    # the Activity via /activity/save as normal (no attachment fields),
    # (2) POST here as multipart/form-data with an "activityId" field
    # (the Id from step 1) and the file itself under "uploads" - see
    # DidarActivityClient.attach_photo_to_activity().
    # Relative to base_url, which already includes "/api" (same
    # convention as every other path in this file, e.g. "/activity/save").
    attach_files_to_activity_path: str = field(
        default_factory=lambda: _get(
            "DIDAR_ATTACH_FILES_TO_ACTIVITY_PATH", "/activity/AttachFilesToActivity"
        )
    )
    # CONFIRMED (client-supplied Didar documentation, 2026-09): this is
    # the one endpoint in this project that does NOT take apikey as a
    # query-string param and has no request body - see
    # DidarContactClient.list_locations()/_post_no_apikey(). Returns
    # {"Response": {"Countries": [...], "Provinces": [...], "Cities":
    # [...]}}, used to resolve Contact.ProvinceId/CityId from a
    # marketplace's raw province/city name.
    #
    # PATH NOTE: the client's docs write this as
    # "{{baseURL}}/api/shared/GetLocations", but THIS project's
    # DIDAR_BASE_URL already ends in "/api" (see .env.example -
    # "https://app.didar.me/api"), same as every other path constant
    # here (/contact/save, /product/categories, /Label/GetDealLabels -
    # none of them repeat "/api"). So relative to THIS project's
    # base_url the correct path is "/shared/GetLocations", NOT
    # "/api/shared/GetLocations" (which would double up to
    # ".../api/api/shared/GetLocations" and 404). This inference
    # follows the same convention as every other path in this file, but
    # hasn't itself been hit with a live call - if it 404s, that
    # doubled "/api" is the first thing to check.
    get_locations_path: str = field(
        default_factory=lambda: _get("DIDAR_GET_LOCATIONS_PATH", "/shared/GetLocations")
    )

    @property
    def deal_label_title_by_source(self) -> dict[str, str]:
        return {
            "tapsishop": self.deal_label_title_tapsishop,
            "digikala": self.deal_label_title_digikala,
            "basalam": self.deal_label_title_basalam,
            "snappshop": self.deal_label_title_snappshop,
            "farazhonar": self.deal_label_title_farazhonar,
        }


@dataclass(frozen=True)
class Settings:
    log_level: str = field(default_factory=lambda: _get("LOG_LEVEL", "INFO"))
    db_path: str = field(default_factory=lambda: _get("DB_PATH", "./data/sync.db"))
    poll_interval_seconds: int = field(
        default_factory=lambda: int(_get("POLL_INTERVAL_SECONDS", "120"))
    )
    # "Any deal" Telegram poller (client request, 2026-09 - see
    # src/didar/deal_poller.py): every Deal registered in Didar, manual
    # or automatic, gets a Telegram notification. Runs on the same
    # POLL_INTERVAL_SECONDS cadence as the marketplace polling from
    # main.py's single scheduled job - no separate interval setting,
    # since there's no reason for these two polls to run at different
    # rates. Defaults to enabled; set DIDAR_DEAL_POLL_ENABLED=false to
    # turn it off (e.g. while DIDAR_API_KEY isn't set up yet), same
    # explicit opt-out pattern as SnappShopConfig.enabled above.
    didar_deal_poll_enabled: bool = field(
        default_factory=lambda: _get("DIDAR_DEAL_POLL_ENABLED", "true").lower() == "true"
    )
    tapsishop: TapsiShopConfig = field(default_factory=TapsiShopConfig)
    digikala: DigikalaConfig = field(default_factory=DigikalaConfig)
    snappshop: SnappShopConfig = field(default_factory=SnappShopConfig)
    basalam: BasalamConfig = field(default_factory=BasalamConfig)
    farazhonar: FarazHonarConfig = field(default_factory=FarazHonarConfig)
    didar: DidarConfig = field(default_factory=DidarConfig)


settings = Settings()