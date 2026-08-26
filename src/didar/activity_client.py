"""
Didar CRM - Activity client.

Creates the standard post-sale follow-up checklist as *planned* (not
done) Activities on a newly-created Deal - the exact set the sales team
already builds by hand for every order (client feedback, 2026-08, from
a screenshot of a manually-run deal's "فعالیت‌های برنامه‌ریزی شده"
timeline): تماس جدید (new call), پیامک ۲ / پیامک ۱ / پیامک ۳ (SMS x3),
ارسال محصول (ship), تماس رضایت (satisfaction call) - in that exact
order, copied directly from the client's own checklist rather than
re-sorted by us. This ONLY creates the todo items - IsDone is always
false. Checking them off as things actually happen stays a manual,
human step, per the client's own framing: "هر اتفاقی افتاد خودشون تیک
میزنن به صورت دستی".

ENDPOINT: POST /activity/save - confirmed from the docs' own example
("ایجاد فعالیت با پارامترهای کامل"): request body is
{"Activity": {...}, "SetDone": bool}, response is {"Response": {"Id": ...}}.

ACTIVITY TYPES: NOT hardcoded - fetched and confirmed live for this
account (2026-08, via POST /activity/GetActivityType). Unlike the
generic demo-account list shown in Didar's own docs (تماس پیگیری، چت،
جلسه...), THIS account already has one dedicated ActivityType per
checklist item (e.g. an actual "پیامک 1" type, distinct from "پیامک 2"
and "پیامک 3" - not one shared "sms" bucket), so each item is mapped to
its own exact type Id via a dedicated DidarConfig field rather than a
shared category. See .env.example for the real Ids and how to re-fetch
them if the account's type list ever changes. Any one left blank skips
the ENTIRE checklist for that sync (logged as a warning) rather than
partially creating it - a missing config value must never block the
order sync itself, but a half-built checklist would be more confusing
than none.

OWNER: OwnerId appears in the docs' own example but is NOT confirmed
required - Deal.save's docs example also always includes it, and this
project's existing create_deal() already works fine without ever
setting one (see deal_client.py). Optional here too
(DidarConfig.default_owner_id) - omitted entirely from the request
when unset, same as LabelId's "blank = omit the key" handling in
create_deal().

DUE DATE (assumption - revisit if the client wants different pacing):
every item is due at the moment the order syncs ("actionable from day
one"). The client's own framing is about MANUAL progression ("هر
اتفاقی افتاد خودشون تیک میزنن"), not an automated schedule, so there's
no confirmed timing rule to encode here - this is the simplest default
that doesn't invent one. If a real cadence (e.g. satisfaction call N
days after shipping) is wanted later, this is the one place to add it.
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx

from src.config import DidarConfig, settings
from src.didar.contact_client import DidarApiError
from src.http_utils import default_retry, raise_for_status_with_body
from src.logger import get_logger

log = get_logger(__name__)

# Exact checklist + order, copied from the client's screenshot of a
# manually-built deal's "فعالیت‌های برنامه‌ریزی شده" timeline (2026-08
# feedback) - do not reorder or reword without new client input, this
# mirrors what the sales team already does by hand for every order.
# Second element is the DidarConfig attribute holding that item's own
# confirmed ActivityType Id for this account (see module docstring).
POST_SALE_CHECKLIST: list[tuple[str, str]] = [
    ("تماس جدید", "activity_type_new_call_id"),
    ("پیامک 2", "activity_type_sms2_id"),
    ("پیامک 1", "activity_type_sms1_id"),
    ("ارسال محصول", "activity_type_ship_id"),
    ("پیامک 3", "activity_type_sms3_id"),
    ("تماس رضایت", "activity_type_satisfaction_call_id"),
]


class DidarActivityClient:
    def __init__(self, config: DidarConfig | None = None) -> None:
        self._config = config or settings.didar
        self._client = httpx.Client(base_url=self._config.base_url, timeout=30.0)

    @default_retry()
    def _post(self, path: str, json: dict) -> dict:
        resp = self._client.post(path, params={"apikey": self._config.api_key}, json=json)
        raise_for_status_with_body(resp)
        return resp.json()

    def _activity_type_id(self, config_attr: str) -> str:
        return getattr(self._config, config_attr)

    def create_activity(
        self, deal_id: str, title: str, activity_type_id: str, due_date: datetime,
    ) -> str:
        activity_body = {
            "ActivityTypeId": activity_type_id,
            "Title": title,
            "DealId": deal_id,
            "IsDone": False,
            "DueDate": _fmt(due_date),
        }
        if self._config.default_owner_id:
            activity_body["OwnerId"] = self._config.default_owner_id

        payload = self._post("/activity/save", json={"Activity": activity_body, "SetDone": False})
        activity_id = _extract_activity_id(payload)
        log.info("didar: created activity '%s' on deal %s -> Id=%s", title, deal_id, activity_id)
        return activity_id

    def create_post_sale_checklist(self, deal_id: str, due_date: datetime) -> None:
        """
        Creates every item in POST_SALE_CHECKLIST on the given deal.

        All-or-nothing on configuration (see module docstring), but NOT
        all-or-nothing on execution: one item failing (a bad type Id,
        a transient API error) is logged and the rest of the checklist
        still gets attempted - this checklist is a sales-team
        convenience, not something that should ever fail the order sync
        itself. Callers should treat this as fire-and-forget.
        """
        missing_types = sorted({
            config_attr for _, config_attr in POST_SALE_CHECKLIST
            if not self._activity_type_id(config_attr)
        })
        if missing_types:
            log.warning(
                "didar: skipping post-sale checklist for deal %s - "
                "activity type Id(s) not configured for: %s "
                "(see DidarConfig.activity_type_*_id / .env.example)",
                deal_id, ", ".join(missing_types),
            )
            return

        for title, config_attr in POST_SALE_CHECKLIST:
            try:
                self.create_activity(
                    deal_id=deal_id,
                    title=title,
                    activity_type_id=self._activity_type_id(config_attr),
                    due_date=due_date,
                )
            except Exception:
                log.exception(
                    "didar: failed to create checklist activity '%s' on deal %s "
                    "- continuing with the rest of the checklist",
                    title, deal_id,
                )


def _fmt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _extract_activity_id(payload: dict) -> str:
    candidates = [
        lambda p: p.get("Response", {}).get("Id"),
        lambda p: p.get("Id"),
    ]
    for get in candidates:
        try:
            value = get(payload)
        except AttributeError:
            continue
        if value:
            return str(value)
    raise DidarApiError(
        f"didar: could not find Activity Id in response - shape is unconfirmed, "
        f"update _extract_activity_id() once a real payload has been inspected. "
        f"Raw response: {payload!r}"
    )
