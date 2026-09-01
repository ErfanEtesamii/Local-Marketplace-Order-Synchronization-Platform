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

DUE DATE (2026-08 follow-up client feedback - supersedes the original
"everything due at sync time" placeholder): every item now has its own
due date, all derived from the order's ship time (زمان ارسال محصول)
except پیامک 1, which is derived from order registration time instead.
The actual date math lives in src/didar/scheduling.py (kept separate so
it's unit-testable without HTTP mocking) - this module just looks each
title's computed date up and never invents one itself. ship_time
should come from the marketplace's own API (see NormalizedOrder.ship_time)
when available (currently: Basalam only). For every other source, whose
adapter doesn't expose a real ship_time yet, create_post_sale_checklist
falls back to order_registered_at + 2 days (client instruction,
2026-08-29, see _DEFAULT_SHIP_DELAY) rather than skipping the checklist
- a missing ActivityType Id (below) still skips the whole checklist,
but a missing ship_time no longer does.

SHIP ACTIVITY ATTACHMENT (2026-08 client feedback; flow CORRECTED
2026-09 after directly confirming with Didar's own support agent): the
"ارسال محصول" item gets the order's product photo(s) attached via a
two-step flow - (1) create the Activity as normal via /activity/save,
with no attachment fields in the body at all, (2) POST each photo to
the documented /activity/AttachFilesToActivity as multipart/form-data,
with "activityId" (the Id from step 1) and the file itself under
"uploads". See attach_photo_to_activity() and
create_post_sale_checklist() for the exact flow.

MULTIPLE PRODUCTS PER ORDER (BUGFIX, client feedback 2026-09 - "if a
customer ordered more than one product, all of them need a photo in
the ارسال محصول activity, not just one"): create_post_sale_checklist()
takes ship_attachments as a LIST now (previously a single tuple) and
attaches every one of them to the same ship Activity via repeated
attach_photo_to_activity() calls. This was a real bug, not a
theoretical one - src/didar/service.py._fetch_product_images()
downloads one photo per line item, but only the first ever reached
this client before. Each attach is isolated in its own try/except so
one bad/missing photo never blocks the rest of the order's photos.

This REPLACES an earlier implementation (upload_attachment() posting
to a guessed /file/upload path, then passing the returned Key back
into /activity/save's NewAttachments field) that was never
independently verified against Didar and turned out not to match the
documented flow at all - Didar's own agent, asked directly, described
only the create-then-AttachFilesToActivity flow above and made no
mention of NewAttachments or a standalone upload endpoint.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx

from src.config import DidarConfig, settings
from src.didar.contact_client import DidarApiError
from src.didar.scheduling import compute_checklist_due_dates
from src.http_utils import default_retry, raise_for_status_with_body
from src.logger import get_logger

log = get_logger(__name__)

# Title of the checklist item that gets the order's product photo(s)
# attached (see create_post_sale_checklist's ship_attachments param).
SHIP_ACTIVITY_TITLE = "ارسال محصول"

# Fallback anchor (client instruction, 2026-08-29) for marketplaces whose
# adapter doesn't yet expose a real ship_time (currently: everything
# except Basalam - see NormalizedOrder.ship_time). Rather than skip the
# whole checklist for those sources, assume shipping happens 2 days
# after the order was registered. Once a given adapter is wired to a
# real ship_time (the more accurate anchor), this fallback is simply
# never reached for that source - no code change needed here.
_DEFAULT_SHIP_DELAY = timedelta(days=2)

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
        self,
        deal_id: str,
        title: str,
        activity_type_id: str,
        due_date: datetime,
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

        request_body = {"Activity": activity_body, "SetDone": False}

        payload = self._post("/activity/save", json=request_body)
        activity_id = _extract_activity_id(payload)
        log.info("didar: created activity '%s' on deal %s -> Id=%s", title, deal_id, activity_id)
        return activity_id

    def attach_photo_to_activity(
        self, activity_id: str, file_bytes: bytes, filename: str, content_type: str
    ) -> None:
        """
        Attaches a file directly to an already-created Activity, via the
        documented POST /activity/AttachFilesToActivity - confirmed
        2026-09 straight from Didar's own support agent (see module
        docstring). multipart/form-data with two fields: "activityId"
        (plain form field, the Id returned by create_activity()) and
        "uploads" (the file itself - NOT pre-uploaded anywhere first,
        unlike the old, incorrect /file/upload flow this replaces).

        Response includes file metadata (Key/Size/Type/Name per the
        agent's description) but nothing this project needs to chain
        into another call, so it's only logged, not parsed/returned.
        """
        form = {"activityId": activity_id}
        files = {"uploads": (filename, file_bytes, content_type)}
        resp = self._client.post(
            self._config.attach_files_to_activity_path,
            params={"apikey": self._config.api_key},
            data=form,
            files=files,
        )
        raise_for_status_with_body(resp)
        log.info(
            "didar: attached photo '%s' to activity %s", filename, activity_id,
        )

    def create_post_sale_checklist(
        self,
        deal_id: str,
        order_registered_at: datetime,
        ship_time: datetime | None,
        ship_attachments: list[tuple[bytes, str, str]] | None = None,
    ) -> None:
        """
        Creates every item in POST_SALE_CHECKLIST on the given deal, each
        with its own due date computed by
        src.didar.scheduling.compute_checklist_due_dates from
        order_registered_at and ship_time (see that module for the exact
        per-item rules).

        ship_attachments, when given, is a list of (file_bytes, filename,
        content_type) - one per product photo (see module docstring's
        "MULTIPLE PRODUCTS PER ORDER" note: an order with several line
        items gets several photos, not just the first) - after the
        "ارسال محصول" item (see SHIP_ACTIVITY_TITLE) is created, EVERY
        one of them gets attached via its own attach_photo_to_activity()
        call using that item's Activity Id. Each attach is tried
        independently - one failed/missing photo is logged and skipped,
        the rest still get attached, same fire-and-forget philosophy as
        everything else here: a photo attachment must never be the
        reason the whole checklist (or the order sync) fails.

        All-or-nothing on configuration (see module docstring), but NOT
        all-or-nothing on execution: one item failing (a bad type Id,
        a transient API error) is logged and the rest of the checklist
        still gets attempted - this checklist is a sales-team
        convenience, not something that should ever fail the order sync
        itself. Callers should treat this as fire-and-forget.

        A missing ship_time (source adapter doesn't expose one yet) no
        longer skips the checklist - see _DEFAULT_SHIP_DELAY above.
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

        if ship_time is None:
            ship_time = order_registered_at + _DEFAULT_SHIP_DELAY
            log.info(
                "didar: no ship_time available for deal %s (marketplace "
                "adapter doesn't expose it yet - see NormalizedOrder.ship_time) "
                "- defaulting to order_registered_at + %s per client instruction",
                deal_id, _DEFAULT_SHIP_DELAY,
            )

        due_dates = compute_checklist_due_dates(
            order_registered_at=order_registered_at, ship_time=ship_time,
        )

        for title, config_attr in POST_SALE_CHECKLIST:
            try:
                activity_id = self.create_activity(
                    deal_id=deal_id,
                    title=title,
                    activity_type_id=self._activity_type_id(config_attr),
                    due_date=due_dates[title],
                )
            except Exception:
                log.exception(
                    "didar: failed to create checklist activity '%s' on deal %s "
                    "- continuing with the rest of the checklist",
                    title, deal_id,
                )
                continue

            if title == SHIP_ACTIVITY_TITLE and ship_attachments:
                for file_bytes, filename, content_type in ship_attachments:
                    try:
                        self.attach_photo_to_activity(activity_id, file_bytes, filename, content_type)
                    except Exception:
                        log.exception(
                            "didar: failed to attach product photo '%s' to "
                            "activity %s (deal %s) - continuing with the "
                            "rest of this order's photos",
                            filename, activity_id, deal_id,
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