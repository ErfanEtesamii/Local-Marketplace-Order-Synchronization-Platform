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
    # Second-store opt-in (2026-09, client request: a second Digikala
    # seller account/store, fully independent adapter - see
    # src/marketplaces/digikala2.py). Same pattern as SnappShopConfig.
    # enabled below: this field is only ever meaningful on
    # `settings.digikala2` (main.py checks it before constructing
    # Digikala2Adapter) - the primary store's own `settings.digikala.
    # enabled` is always True and isn't checked anywhere.
    enabled: bool = True


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
    # CONFIRMED 2026-09 - see src/currency.py's module docstring: a
    # real order's item final_price matched the vendor panel's Toman
    # total exactly (no factor-of-10 gap). Defaults to "toman"; still
    # overridable via env in case a different vendor account ever
    # shows otherwise.
    price_unit: str = field(
        default_factory=lambda: _get_price_unit("SNAPPSHOP_PRICE_UNIT", "toman")
    )


@dataclass(frozen=True)
class BasalamConfig:
    base_url: str = field(default_factory=lambda: _get("BASALAM_BASE_URL"))
    access_token: str = field(default_factory=lambda: _get("BASALAM_ACCESS_TOKEN"))
    # CONFIRMED rial (2026-09, by the client checking real order data) -
    # the earlier "toman" default was only a best-guess inferred from the
    # official Basalam SDK's quick-start example, never a confirmed live
    # order payload. See src/currency.py's module docstring.
    price_unit: str = field(
        default_factory=lambda: _get_price_unit("BASALAM_PRICE_UNIT", "rial")
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
    # category-by-title matching in product_client.py. "با سلام" (two
    # words, with a space) for basalam and "سایت فرازهنر" for farazhonar
    # are CONFIRMED (seen directly in a screenshot of this account's Deal
    # Labels, 2026-09 - an earlier version of this comment/default had
    # both wrong: "باسلام" with no space, and "سایت فرامرزی", a
    # different word entirely); the other three are best guesses and
    # MUST be verified against a live GET /Label/GetDealLabels response
    # for this account. Matching is exact after Persian normalization
    # (see _normalize_fa), which only collapses repeated/half-space
    # whitespace - it does NOT merge two genuinely different strings
    # like "باسلام" and "با سلام". A Title here that doesn't match what's
    # actually in Didar just means that source's Deals get created
    # without a label (logged as a warning), never an error.
    deal_label_title_tapsishop: str = field(
        default_factory=lambda: _get("DIDAR_DEAL_LABEL_TITLE_TAPSISHOP", "تپسی")
    )
    deal_label_title_digikala: str = field(
        default_factory=lambda: _get("DIDAR_DEAL_LABEL_TITLE_DIGIKALA", "دیجی کالا")
    )
    # Second Digikala store (src/marketplaces/digikala2.py) - CONFIRMED
    # 2026-09 directly from a live GET /Label/GetDealLabels response for
    # this account (client screenshot/PowerShell output): the label
    # "دیجی کالا سریع" (Id 9255c089-2479-401d-bc4f-dc90b4c1bfd4, Code 1)
    # is the one the client wants used for the second store's Deals -
    # distinct from the first store's own "دیجی کالا" label above.
    deal_label_title_digikala2: str = field(
        default_factory=lambda: _get(
            "DIDAR_DEAL_LABEL_TITLE_DIGIKALA2", "دیجی کالا سریع"
        )
    )
    deal_label_title_basalam: str = field(
        default_factory=lambda: _get("DIDAR_DEAL_LABEL_TITLE_BASALAM", "با سلام")
    )
    deal_label_title_snappshop: str = field(
        default_factory=lambda: _get("DIDAR_DEAL_LABEL_TITLE_SNAPPSHOP", "اسنپ")
    )
    deal_label_title_farazhonar: str = field(
        default_factory=lambda: _get("DIDAR_DEAL_LABEL_TITLE_FARAZHONAR", "سایت فرازهنر")
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
            "digikala2": self.deal_label_title_digikala2,
            "basalam": self.deal_label_title_basalam,
            "snappshop": self.deal_label_title_snappshop,
            "farazhonar": self.deal_label_title_farazhonar,
        }


@dataclass(frozen=True)
class ModirPayamakConfig:
    """Express-order warehouse SMS, via Modir Payamak's IPPanel Edge API
    (see src/modir_payamak.py).

    NOTE: src/modir_payamak.py reads these same five env vars directly
    with os.getenv rather than through this object. That is the existing
    notifier-module exception to this file's "nothing outside here calls
    os.getenv" rule - src/telegram.py already does exactly the same with
    TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID* - and it is kept that way here
    deliberately, because ModirPayamakNotifier is constructed once per
    process and must be able to notice credentials that appeared AFTER
    import time (this module's `settings` is a module-level singleton
    built at import). This dataclass is the declarative record of what
    the service depends on, and gives operators/scripts one place to ask
    "is the SMS alert actually configured?" without importing the
    notifier and its httpx client.

    `recipients` drops blank entries, matching the notifier's own rule
    that only non-empty EXPRESS_ALERT_SMS_RECIPIENT_{1,2,3} values are
    sent to - so one or two configured numbers is valid; zero is not.
    """

    token: str = field(default_factory=lambda: _get("MODIR_PAYAMAK_TOKEN"))
    from_number: str = field(default_factory=lambda: _get("MODIR_PAYAMAK_FROM_NUMBER"))
    recipients: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            value
            for key in (
                "EXPRESS_ALERT_SMS_RECIPIENT_1",
                "EXPRESS_ALERT_SMS_RECIPIENT_2",
                "EXPRESS_ALERT_SMS_RECIPIENT_3",
            )
            if (value := _get(key).strip())
        )
    )

    @property
    def enabled(self) -> bool:
        """Same three-part check as ModirPayamakNotifier.is_configured(),
        minus its caching/client setup - a token, a sender line, and at
        least one recipient. Never raises; a missing value just means
        False (express alerts stay queued rather than sent)."""
        return bool(self.token.strip() and self.from_number.strip() and self.recipients)


def _get_or(key: str, fallback: str) -> str:
    """Like _get, but also falls back when the env var is PRESENT but
    blank (e.g. `DIGIKALA2_BASE_URL=` in .env.example) - plain _get()'s
    os.getenv(key, default) only applies `default` when the key is
    entirely absent, so a blank-but-defined value would otherwise
    override a real fallback with an empty string."""
    value = _get(key)
    return value if value else fallback


def _build_digikala2_config() -> DigikalaConfig:
    """Second Digikala store - see DigikalaConfig.source_name/enabled's
    docstrings for the full rationale. Reads DIGIKALA2_* env vars;
    BASE_URL and PRICE_UNIT fall back to the first store's own values
    (same underlying Digikala API/currency) when left blank or unset,
    since there's no reason to expect those to differ between two
    stores on the same seller platform - CLIENT_CODE/SECRET/
    ACCESS_TOKEN/REFRESH_TOKEN never fall back, each store's
    credentials are its own.

    PRICE_UNIT still gets _get_price_unit's normal "must be toman or
    rial" validation (fails loudly on a typo, same as every other
    source) - just resolved via _get_or first so a blank-but-present
    `DIGIKALA2_PRICE_UNIT=` (e.g. straight out of .env.example) falls
    back to the first store's value instead of being validated as an
    invalid empty string.
    """
    fallback_price_unit = _get_or("DIGIKALA_PRICE_UNIT", "rial")
    resolved_price_unit = _get_or("DIGIKALA2_PRICE_UNIT", fallback_price_unit)
    if resolved_price_unit.strip().lower() not in ("toman", "rial"):
        raise ValueError(
            f"DIGIKALA2_PRICE_UNIT={resolved_price_unit!r} is invalid - must be "
            f"'toman' or 'rial' (see src/currency.py for what each source is "
            f"currently set to)"
        )
    return DigikalaConfig(
        enabled=_get("DIGIKALA2_ENABLED", "false").lower() == "true",
        base_url=_get_or("DIGIKALA2_BASE_URL", _get("DIGIKALA_BASE_URL")),
        client_code=_get("DIGIKALA2_CLIENT_CODE"),
        client_secret=_get("DIGIKALA2_CLIENT_SECRET"),
        access_token=_get("DIGIKALA2_ACCESS_TOKEN"),
        refresh_token=_get("DIGIKALA2_REFRESH_TOKEN"),
        price_unit=resolved_price_unit.strip().lower(),
    )


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
    # Interactive /report date-range picker (src/telegram.py's
    # poll_updates()) - runs on its own short interval, separate from
    # POLL_INTERVAL_SECONDS, because that one defaults to 120s and a
    # calendar button press feeling broken for two minutes is bad UX.
    # Short polling (not Telegram's long-poll), so this is purely how
    # often we ask "anything new?" - a few seconds is plenty responsive
    # for a human tapping buttons and costs nothing at this traffic
    # level.
    telegram_report_picker_poll_seconds: int = field(
        default_factory=lambda: int(_get("TELEGRAM_REPORT_PICKER_POLL_SECONDS", "3"))
    )
    # Digikala FBD - "ارسال به انبار دیجی‌کالا" (2026-09). Opt-in, same
    # pattern as SNAPPSHOP_ENABLED / DIGIKALA2_ENABLED: when false (the
    # default) main.py never constructs the adapter, so the feature
    # costs nothing and cannot touch Didar.
    #
    # A single top-level flag rather than a field on DigikalaConfig
    # because this feature is FIRST-STORE-ONLY by client decision - a
    # per-store field would be silently inherited by
    # _build_digikala2_config()'s own DigikalaConfig and imply a second
    # store's FBD sync that has no adapter (digikala2_warehouse.py does
    # not exist). Everything else this feature needs - pipeline, stage,
    # deal label, ship activity type - deliberately reuses the existing
    # DIDAR_* values (see deal_client.py's _WAREHOUSE_DEAL_LABEL_SOURCE),
    # so this flag is the only new setting.
    digikala_warehouse_enabled: bool = field(
        default_factory=lambda: _get("DIGIKALA_WAREHOUSE_ENABLED", "false").lower() == "true"
    )
    tapsishop: TapsiShopConfig = field(default_factory=TapsiShopConfig)
    digikala: DigikalaConfig = field(default_factory=DigikalaConfig)
    # Second Digikala store - see _build_digikala2_config's and
    # DigikalaConfig.source_name/enabled's docstrings for the full
    # rationale.
    digikala2: DigikalaConfig = field(default_factory=_build_digikala2_config)
    snappshop: SnappShopConfig = field(default_factory=SnappShopConfig)
    basalam: BasalamConfig = field(default_factory=BasalamConfig)
    farazhonar: FarazHonarConfig = field(default_factory=FarazHonarConfig)
    didar: DidarConfig = field(default_factory=DidarConfig)
    # Express-order warehouse SMS (2026-09) - see ModirPayamakConfig's
    # docstring for why src/modir_payamak.py still reads these env vars
    # itself instead of going through this field.
    modir_payamak: ModirPayamakConfig = field(default_factory=ModirPayamakConfig)


settings = Settings()