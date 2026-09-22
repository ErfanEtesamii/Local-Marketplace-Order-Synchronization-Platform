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

import time
from datetime import datetime, timedelta, timezone

import httpx

from src.config import DidarConfig, settings
from src.didar.contact_client import DidarApiError
from src.didar.scheduling import compute_checklist_due_dates
from src.http_utils import default_retry, raise_for_status_with_body
from src.logger import get_logger

log = get_logger(__name__)

# Indirection so tests can stub out the waiting between attach retries.
_sleep = time.sleep

# Waits (seconds) between attempts when a single photo upload is rejected
# with a transient-looking status - see DidarActivityClient._attach_with_retry.
_ATTACH_RETRY_DELAYS = (2.0, 5.0)

# Title of the checklist item that gets the order's product photo(s)
# attached (see create_post_sale_checklist's ship_attachments param).
SHIP_ACTIVITY_TITLE = "ارسال محصول"

# ActivityTypeId for a plain Note (as opposed to a planned to-do item),
# per Didar's own /activity/save documentation - always this all-zero
# GUID, never one of the per-account Ids configured on DidarConfig for
# POST_SALE_CHECKLIST above. Named as a module constant (same pattern as
# SHIP_ACTIVITY_TITLE) rather than hardcoded inline inside create_note()
# so this one confirmed value has exactly one place to live.
NOTE_ACTIVITY_TYPE_ID = "00000000-0000-0000-0000-000000000000"

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

    def create_note(self, deal_id: str, text: str) -> str:
        """Adds a plain, already-done Note (`ResultNote`) to `deal_id` -
        NOT a planned to-do item, so this is kept as its own method
        rather than folded into create_activity(): the request body
        shape genuinely differs (IsDone is always True, ResultNote
        replaces Title, there is no DueDate/OwnerId, and ActivityTypeId
        is always the fixed NOTE_ACTIVITY_TYPE_ID above rather than one
        of DidarConfig's per-checklist-item Ids).

        Used by sync_engine.py's SnappShop order-type classification
        (see the "طبقه‌بندی انواع سفارش اسنپ‌شاپ" prompt) to leave exactly
        one note per order - "اسنپ اکسپرس : تهران/اصفهان" or
        "ارسال به انبار".

        Same contract as create_activity(): this call itself can raise
        (a transport error, a non-2xx response, an unrecognized
        response shape from _extract_activity_id()). It does NOT catch
        or log its own errors and does NOT decide fire-and-forget - that
        is the caller's responsibility, exactly like the existing
        relationship between create_activity() and _sync_one_order().
        """
        activity_body = {
            "ActivityTypeId": NOTE_ACTIVITY_TYPE_ID,
            "ResultNote": text,
            "IsDone": True,
            "DealId": deal_id,
        }
        request_body = {"Activity": activity_body}

        payload = self._post("/activity/save", json=request_body)
        activity_id = _extract_activity_id(payload)
        log.info("didar: added note to deal %s -> Id=%s", deal_id, activity_id)
        return activity_id

    def _post_attachments(
        self, activity_id: str, attachments: list[tuple[bytes, str, str]]
    ) -> None:
        """One POST to /activity/AttachFilesToActivity carrying every file in
        `attachments` as a repeated "uploads" multipart part."""
        form = {"activityId": activity_id}
        files = [
            ("uploads", (filename, file_bytes, content_type))
            for file_bytes, filename, content_type in attachments
        ]
        resp = self._client.post(
            self._config.attach_files_to_activity_path,
            params={"apikey": self._config.api_key},
            data=form,
            files=files,
        )
        raise_for_status_with_body(resp)

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
        self._post_attachments(activity_id, [(file_bytes, filename, content_type)])
        log.info(
            "didar: attached photo '%s' to activity %s", filename, activity_id,
        )

    def _attach_with_retry(
        self, activity_id: str, attachment: tuple[bytes, str, str]
    ) -> None:
        """attach_photo_to_activity() for one photo, retried after a short
        wait when Didar answers with a transient-looking status (417, 429
        or 5xx). Other errors (and transport errors/timeouts, where the
        upload may in fact have gone through) are not retried."""
        file_bytes, filename, content_type = attachment
        delays = list(_ATTACH_RETRY_DELAYS)
        while True:
            try:
                self.attach_photo_to_activity(activity_id, file_bytes, filename, content_type)
                return
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if not delays or not (status in (417, 429) or status >= 500):
                    raise
                wait = delays.pop(0)
                log.warning(
                    "didar: attaching photo '%s' to activity %s got HTTP %s - retrying in %ss",
                    filename, activity_id, status, wait,
                )
                _sleep(wait)

    def attach_photos_to_activity(
        self,
        activity_id: str,
        attachments: list[tuple[bytes, str, str]],
        deal_id: str = "",
    ) -> None:
        """Attach every photo of an order to one Activity. Never raises.

        BUGFIX (2026-09, Digikala #375267085 / shipment 383265431 and
        shipment 382264825): for a 2-product order only the FIRST photo
        ever reached Didar. Photos used to be posted one request per
        photo, back-to-back, and Didar answered the second request with
        "417 Expectation Failed" (empty body) every time - the log shows
        it for both 2-photo Digikala orders since the migration. The
        endpoint is named AttachFilesToActivity (plural), so all photos
        are now sent together in ONE request. If that request is
        rejected, each photo is retried on its own (with a short wait
        and a couple of retries on transient-looking statuses) so a
        failure here degrades to the old behavior at worst.
        """
        if not attachments:
            return

        if len(attachments) > 1:
            try:
                self._post_attachments(activity_id, attachments)
                log.info(
                    "didar: attached %d photo(s) [%s] to activity %s in one request",
                    len(attachments),
                    ", ".join(name for _, name, _ in attachments),
                    activity_id,
                )
                return
            except Exception:
                log.warning(
                    "didar: attaching %d photos to activity %s in one request failed - "
                    "falling back to one request per photo",
                    len(attachments), activity_id, exc_info=True,
                )

        for attachment in attachments:
            try:
                self._attach_with_retry(activity_id, attachment)
            except Exception:
                log.exception(
                    "didar: failed to attach product photo '%s' to activity %s "
                    "(deal %s) - continuing with the rest of this order's photos",
                    attachment[1], activity_id, deal_id,
                )

    def create_ship_only_activity(
        self,
        deal_id: str,
        due_date: datetime,
        ship_attachments: list[tuple[bytes, str, str]] | None = None,
    ) -> None:
        """
        The single "ارسال محصول" Activity for a Digikala FBD deal
        ("ارسال به انبار دیجی‌کالا" - see
        src/marketplaces/warehouse_base.py), with the product photo(s)
        attached.

        EXACTLY ONE ACTIVITY, deliberately: the client's instruction for
        this feature is "فقط فعالیت ارسال محصول". The other five items of
        POST_SALE_CHECKLIST are a CUSTOMER follow-up sequence (calls,
        satisfaction SMS) - there is no customer here at all, Digikala
        itself is the buyer, so creating them would put six meaningless
        todos on every FBD deal. POST_SALE_CHECKLIST and
        create_post_sale_checklist() are untouched by this method.

        Reuses create_activity() as-is (its signature is generic - deal,
        title, type id, due date) and the same
        attach_photo_to_activity() flow, so both stay a single
        implementation rather than being copied for this source.

        A blank DIDAR_ACTIVITY_TYPE_SHIP_ID logs a warning and returns
        WITHOUT raising - same "missing config skips the activity, never
        fails the sync" rule as create_post_sale_checklist()'s
        missing_types guard. Likewise every photo upload is isolated in
        its own try/except: one bad photo must stop neither the other
        photos nor (since the caller treats this whole method as
        fire-and-forget) the Deal that was already created.

        `due_date` is computed by the CALLER (see
        src/didar/warehouse_service.py) - from the item's own
        commitment_date when Digikala gave one, else the same
        created_at + _DEFAULT_SHIP_DELAY fallback used above. No date is
        ever invented here.
        """
        activity_type_id = self._activity_type_id("activity_type_ship_id")
        if not activity_type_id:
            log.warning(
                "didar: skipping the '%s' activity for warehouse deal %s - "
                "DIDAR_ACTIVITY_TYPE_SHIP_ID is not configured "
                "(see DidarConfig.activity_type_ship_id / .env.example)",
                SHIP_ACTIVITY_TITLE, deal_id,
            )
            return

        try:
            activity_id = self.create_activity(
                deal_id=deal_id,
                title=SHIP_ACTIVITY_TITLE,
                activity_type_id=activity_type_id,
                due_date=due_date,
            )
        except Exception:
            log.exception(
                "didar: failed to create the '%s' activity on warehouse deal %s - "
                "the deal itself is already created and stays",
                SHIP_ACTIVITY_TITLE, deal_id,
            )
            return

        self.attach_photos_to_activity(activity_id, ship_attachments or [], deal_id=deal_id)

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
                self.attach_photos_to_activity(activity_id, ship_attachments, deal_id=deal_id)


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