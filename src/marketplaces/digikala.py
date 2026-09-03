"""
Digikala adapter.

Based on the official Open API (seller.digikala.com/open-api/v1/doc):
  - GET /open-api/v1/ship-by-seller-orders           -> paginated SBS
    shipment list, one row per SHIPMENT (not per line item - each row's
    own "variants" array holds that shipment's line items)
  - GET /open-api/v1/ship-by-seller-orders/{id}       -> single-shipment
    detail, same row shape as one item of the list above

2026-09 MIGRATION (see digikala-sbs-migration-prompt.md for the full
write-up): fetch_new_orders() used to page through
GET /open-api/v1/orders/history and rely on SyncEngine's client-side
5-hour window to drop old orders, because that endpoint's own
order_created_at_from/_to filter does not actually filter server-side
(confirmed live, production log 2026-08-27 - a poll returned orders back
to 2024 despite `since` being "yesterday"). That's not a bug that can be
tuned away: a wider window risks re-syncing old orders (a real incident
synced 43 two-month-old orders on 2026-08-31), while a narrower one
silently drops any order that Digikala's own backend was slow to record,
with no retry path picking it up. The fix is architectural, not a bigger
or smaller number of hours: /ship-by-seller-orders exposes a documented,
monotonic `search[min_shipment_id]` cursor, so fetch_new_orders() below
tracks a persistent `last_shipment_id_seen` watermark (see
src/db/repository.py's get_last_shipment_id/set_last_shipment_id) instead
of any created_at comparison. Every row with shipmentId >= watermark + 1
is new BY DEFINITION - no date logic, no dropped orders. SyncEngine
bypasses its generic created_at window for this adapter accordingly (see
`uses_id_based_watermark` below and sync_engine.py's _sync_source).

One shipment = one Didar Deal now (previously: one Digikala order_id,
grouped from /orders/history's per-line-item rows via
_group_rows_into_orders - removed in this migration since
/ship-by-seller-orders already returns one row per shipment with its own
line items nested in "variants"). A single Digikala order that splits
into multiple parcels now produces multiple Deals, one per shipment - this
is intentional (see digikala-sbs-migration-prompt.md, Decision 1).

Customer name/mobile/address ARE now available directly on every
/ship-by-seller-orders row (customer_name / customer_phone_number /
customer_address / customer_postal_code / address.state / address.city) -
no longer "not requested for this source" as the old docstring here used
to say. fetch_sbs_customer_details()/fetch_shipment_details() (a pair of
separate, narrower endpoints used before this migration) are kept only as
a best-effort FALLBACK for whichever of these fields end up null on a
given row - see their own docstrings and sync_engine.py's
_prepare_and_push_to_didar(), whose existing null-gated enrichment calls
now rarely fire in practice.

AUTO-CONFIRM OF PENDING ORDERS (2026-09, client request, confirmed against
the real seller panel via screenshots): those customer fields are null on
a row while its status is "pending" (a brand-new order, shown in the panel
as "سفارش جدید") - the panel only reveals full customer/item detail once
the seller confirms the order, which advances its status to "processing"
(در حال پردازش). Since this service has no human in the loop to click
"confirm" in the panel, fetch_new_orders()/fetch_order_detail() now call
_confirm_if_pending() on every row first, which does that confirmation via
PUT /ship-by-seller-orders/update-status (using the row's own nextStatus/
verificationCode) and re-fetches the row before normalizing. See
_confirm_if_pending()'s own docstring for the full behavior and failure
handling.

TOKEN LIFECYCLE (confirmed via a real /auth/token exchange):
  - access_token: short-lived, ~24 hours
  - refresh_token: long-lived, ~1 year - matches the ~360-day validity
    shown in the seller panel's own "توکن اختصاصی" screen, which
    reflects the client/refresh-token grant, not the short-lived
    access_token used on every request

There are two SEPARATE processes here, easy to conflate:

  1. Getting the INITIAL access_token/refresh_token pair (manual,
     one-time, or roughly once a year when refresh_token expires):
     the seller panel issues an RSA-encrypted authorization_code,
     which is decrypted locally with a private key (openssl pkeyutl),
     then exchanged via POST /auth/token. This requires the private
     key and is NOT something this service does - it's a manual step
     whose *result* (the two tokens) gets seeded into .env once.

  2. Renewing the access_token day-to-day (automatic, done by THIS
     adapter): POST /auth/refresh-token - CONFIRMED via the official
     docs (Authentication section) that despite the name, this call
     requires BOTH the (expired) access_token AND the refresh_token in
     the request body, not refresh_token alone:
         {"access_token": "<expired token>", "refresh_token": "<...>"}
     Omitting access_token gets a 400 with
     errors.access_token = ["این قسمت نباید خالی باشد"] - this was a
     real bug in an earlier version of this adapter (see git history)
     that broke every scheduled refresh. No private key or
     authorization_code is involved in this step, only in step 1 above.

The private key used in step 1 should never be placed on the server -
this service only ever needs the refresh_token for step 2. Roughly
once a year (when refresh_token approaches its own expiry), step 1
needs to be repeated manually and the new tokens re-seeded into .env
(or directly into data/digikala_tokens.json).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx
import jdatetime

from src.config import DigikalaConfig, settings
from src.currency import to_rial
from src.db.repository import Repository
from src.http_utils import default_retry, raise_for_status_with_body
from src.logger import get_logger
from src.marketplaces.base import MarketplaceAdapter, NormalizedOrder, OrderItem

log = get_logger(__name__)

# Iran has not observed DST since 2022, so a fixed UTC+03:30 offset is
# correct year-round - same constant/rationale as src/telegram.py's
# IRAN_TZ, duplicated here rather than imported to avoid a src.telegram
# -> src.marketplaces.digikala import for what is otherwise an unrelated
# module.
_IRAN_TZ = timezone(timedelta(hours=3, minutes=30))


def _to_decimal(value) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        return Decimal("0")


def _to_int_or_none(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class DigikalaAdapter(MarketplaceAdapter):
    name = "digikala"

    # Tells SyncEngine._sync_source to bypass its generic created_at /
    # FETCH_WINDOW_HOURS drop for this adapter - see this module's
    # docstring and sync_engine.py's _sync_source. fetch_new_orders()
    # already guarantees "new" via a monotonic shipmentId watermark, so a
    # created_at comparison here would be redundant at best (every row is
    # new by construction) and actively wrong at worst (Digikala's
    # orderDate is a Jalali DATE with no time component - see
    # _parse_jalali_date - making it a poor sub-hour freshness signal).
    uses_id_based_watermark = True

    def __init__(
        self,
        config: DigikalaConfig | None = None,
        repository: Repository | None = None,
    ) -> None:
        self._config = config or settings.digikala
        # Own Repository handle for the shipment-ID watermark (get/set_
        # last_shipment_id) - defaults to a fresh Repository() pointed at
        # the same settings.db_path SyncEngine's own Repository uses, so
        # both end up reading/writing the same SQLite file without this
        # adapter needing SyncEngine to inject one explicitly (mirrors
        # `config or settings.digikala` just above).
        self._repo = repository or Repository()
        self._token_cache_path = Path(settings.db_path).resolve().parent / "digikala_tokens.json"
        self._access_token, self._refresh_token = self._load_tokens()
        self._client = httpx.Client(
            base_url=self._config.base_url,
            headers={"content-type": "application/json"},
            timeout=30.0,
        )

    def _load_tokens(self) -> tuple[str, str]:
        """Prefer a previously-refreshed pair over the static .env seed,
        since refresh_token rotates and .env is not rewritten at runtime."""
        if self._token_cache_path.exists():
            try:
                cached = json.loads(self._token_cache_path.read_text())
                return cached["access_token"], cached["refresh_token"]
            except (json.JSONDecodeError, KeyError, OSError):
                log.warning(
                    "digikala: failed to read cached tokens at %s, falling back to .env",
                    self._token_cache_path,
                )
        return self._config.access_token, self._config.refresh_token

    def _save_tokens(self) -> None:
        self._token_cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._token_cache_path.write_text(
            json.dumps({"access_token": self._access_token, "refresh_token": self._refresh_token})
        )

    @default_retry()
    def _refresh_access_token(self) -> None:
        # Confirmed via official docs: both access_token and refresh_token
        # are required in the body, even though this call's purpose is to
        # replace the (expired) access_token - see module docstring.
        resp = self._client.post(
            "/open-api/v1/auth/refresh-token",
            json={"access_token": self._access_token, "refresh_token": self._refresh_token},
        )
        raise_for_status_with_body(resp)
        data = resp.json().get("data", {})
        self._access_token = data["access_token"]
        # Digikala may or may not rotate the refresh_token on each use -
        # keep the old one only if a new one wasn't actually returned.
        self._refresh_token = data.get("refresh_token", self._refresh_token)
        self._save_tokens()
        log.info(
            "digikala: access token refreshed, new expiry=%s",
            data.get("access_token_expires_at", {}).get("date"),
        )

    @default_retry()
    def _get(self, path: str, params: dict, _already_refreshed: bool = False) -> dict:
        resp = self._client.get(
            path, params=params, headers={"Authorization": f"Bearer {self._access_token}"}
        )
        if resp.status_code == 401 and not _already_refreshed:
            log.info("digikala: access token expired (401), refreshing")
            self._refresh_access_token()
            return self._get(path, params, _already_refreshed=True)
        raise_for_status_with_body(resp)
        return resp.json()

    @default_retry()
    def _update_status(
        self,
        shipment_id: int | str,
        new_status: str,
        verification_code: int | None,
        _already_refreshed: bool = False,
    ) -> None:
        """
        PUT /open-api/v1/ship-by-seller-orders/update-status - the API
        equivalent of the seller-panel action that moves a shipment from
        one status to the next (e.g. pending -> processing). Used by
        _confirm_if_pending() below to auto-confirm new orders.

        Same 401 -> refresh -> retry-once pattern as _get() above; only
        order_shipment_id/new_status are documented as required, so
        verification_code is omitted entirely when the row didn't have
        one rather than sending a null.
        """
        body: dict = {"order_shipment_id": int(shipment_id), "new_status": new_status}
        if verification_code is not None:
            body["verification_code"] = verification_code
        resp = self._client.put(
            "/open-api/v1/ship-by-seller-orders/update-status",
            json=body,
            headers={"Authorization": f"Bearer {self._access_token}"},
        )
        if resp.status_code == 401 and not _already_refreshed:
            log.info("digikala: access token expired (401) during update-status, refreshing")
            self._refresh_access_token()
            return self._update_status(
                shipment_id, new_status, verification_code, _already_refreshed=True
            )
        raise_for_status_with_body(resp)

    def fetch_new_orders(self, since: datetime | None) -> list[NormalizedOrder]:
        """
        `since` is accepted only to satisfy MarketplaceAdapter's interface
        and is otherwise IGNORED - see this module's docstring and
        `uses_id_based_watermark` above. "New" is defined entirely by the
        persisted shipmentId watermark (src/db/repository.py's
        get_last_shipment_id/set_last_shipment_id), not by any date.
        """
        watermark = self._repo.get_last_shipment_id(self.name)
        if watermark is None:
            # Cold start (Decision 5, digikala-sbs-migration-prompt.md):
            # the watermark has never been set for this platform - seed it
            # to the account's current highest shipmentId WITHOUT syncing
            # any existing order, rather than walking the entire SBS
            # history on the very first run. Deterministic "clean start",
            # not a guessed time cutoff.
            seeded = self._seed_watermark_from_latest()
            self._repo.set_last_shipment_id(self.name, seeded)
            log.info(
                "digikala: cold start - seeded shipment watermark at %d, no orders synced",
                seeded,
            )
            return []

        rows = self._fetch_sbs_rows_since_watermark(watermark + 1)
        # Client request (2026-09): auto-confirm every "new" (pending) SBS
        # order the moment it's fetched, mirroring the seller panel's own
        # confirm action - see _confirm_if_pending()'s docstring. Must
        # happen BEFORE normalization: a still-pending row's own
        # customer_name/customer_phone_number/customer_address fields may
        # be null, and confirming replaces the row with a freshly
        # re-fetched one that has them populated.
        rows = [self._confirm_if_pending(row) for row in rows]
        orders = [self._normalize_sbs_row(row) for row in rows]
        log.info(
            "digikala: fetched %d new shipment(s) since watermark %d", len(orders), watermark
        )
        return orders

    def _fetch_shipment_row(self, shipment_id: int | str) -> dict:
        """
        GET /open-api/v1/ship-by-seller-orders/{shipment_id} and return the
        raw row dict (NOT normalized) - shared by fetch_order_detail() and
        _confirm_if_pending()'s post-confirm re-fetch, so both go through
        the exact same response-shape handling.

        Official docs (SBSOrdersObjectView) show `data` as the shipment
        object directly. fetch_shipment_details()'s docstring notes a
        DIFFERENT observed shape ({"items": [...]}) for this SAME
        endpoint from a real client-supplied payload - support both
        rather than picking one and silently breaking on the other, until
        that discrepancy is reconciled against a live call.

        Returns {} (never raises) if the shipment genuinely has no data -
        callers decide what that means for them (fetch_order_detail
        raises, _confirm_if_pending falls back to the pre-confirm row).
        """
        payload = self._get(f"/open-api/v1/ship-by-seller-orders/{shipment_id}", params={})
        data = payload.get("data") or {}
        if "shipmentId" not in data and data.get("items"):
            data = data["items"][0]
        return data

    def fetch_order_detail(self, source_order_id: str) -> NormalizedOrder:
        """
        Full detail for a single shipment. `source_order_id` is a
        shipmentId (Decision 1: one shipment = one Deal, so the dedup key
        in synced_orders is now shipmentId, not Digikala's order_id).

        Used by SyncEngine's retry path (retry_pending_failures always
        re-fetches by source_order_id before retrying) and, defensively,
        by _prepare_and_push_to_didar's "order has no items" fallback -
        though that branch shouldn't actually trigger anymore since
        fetch_new_orders already populates items from each row's own
        "variants" array.

        Also runs the same _confirm_if_pending() auto-confirm as
        fetch_new_orders(), since a shipment can still be "pending" the
        first time it's re-fetched here (e.g. the retry path, or a caller
        that never went through fetch_new_orders for this id).
        """
        data = self._fetch_shipment_row(source_order_id)
        if not data:
            raise ValueError(f"digikala: shipment {source_order_id} not found")
        data = self._confirm_if_pending(data)
        return self._normalize_sbs_row(data)

    def _confirm_if_pending(self, row: dict) -> dict:
        """
        Client request (2026-09): confirmed against the real seller panel
        (screenshots) that a "new" SBS order (status.text == "pending")
        shows only a bare summary row there with NO expandable
        customer/item detail - the same shipment's full detail (customer
        name/phone/address, item breakdown) only appears once its status
        becomes "processing" (در حال پردازش), which happens when the
        seller explicitly confirms the order. This method is the API
        equivalent of that confirm action, run automatically for every
        pending row this adapter sees, since this service has no human in
        the loop to click "confirm" in the panel itself.

        Uses PUT /ship-by-seller-orders/update-status via _update_status(),
        with each row's OWN `nextStatus` (falling back to "processing" only
        if a row is missing it) and `verificationCode` - both fields exist
        on this exact row shape specifically to drive this transition (see
        docs), so nothing here is guessed. verification_code is optional
        per the docs' own validation-error example (only order_shipment_id
        and new_status are listed as required), so a row without one is
        confirmed without it rather than blocked on it.

        Non-pending rows (including cancelled/rejected) pass through
        untouched - there's nothing to confirm.

        Best-effort like fetch_sbs_customer_details/fetch_shipment_details:
        ANY failure (the update-status call itself, or the post-confirm
        re-fetch) is logged and the ORIGINAL row is returned unchanged, so
        a confirm failure never blocks the order from syncing - it just
        syncs with whatever data the still-pending row already had
        (sync_engine's existing fetch_sbs_customer_details fallback and
        synthetic-name fallback still apply on top of this).
        """
        status_text = (row.get("status") or {}).get("text")
        if row.get("isCancelled") or status_text != "pending":
            return row

        shipment_id = row.get("shipmentId")
        if shipment_id is None:
            return row

        new_status = row.get("nextStatus") or "processing"
        verification_code = _to_int_or_none(row.get("verificationCode"))

        try:
            self._update_status(shipment_id, new_status, verification_code)
        except Exception:
            log.exception(
                "digikala: failed to auto-confirm pending shipment %s (new_status=%s) - "
                "syncing with the pending row's own data instead",
                shipment_id, new_status,
            )
            return row

        try:
            refreshed = self._fetch_shipment_row(shipment_id)
        except Exception:
            log.exception(
                "digikala: confirmed shipment %s but failed to re-fetch its detail - "
                "syncing with the pre-confirmation row",
                shipment_id,
            )
            return row

        if not refreshed:
            log.warning(
                "digikala: confirmed shipment %s but re-fetch returned no data - "
                "syncing with the pre-confirmation row",
                shipment_id,
            )
            return row

        log.info(
            "digikala: auto-confirmed pending shipment %s -> %s (customer data present=%s)",
            shipment_id, new_status, bool(refreshed.get("customer_name")),
        )
        return refreshed

    def fetch_sbs_customer_details(self, shipment_id: str) -> dict:
        """Fetch customer details for a Digikala Ship-by-Seller (SBS) order.

        Calls GET /open-api/v1/ship-by-seller-orders/customer/{shipment_id}
        with Bearer token and extracts customer data (name, phoneNumber,
        state, city, address, postalCode).

        Returns a dict with keys:
            customer_full_name: str | None
            customer_mobile: str | None
            customer_province: str | None  (from "state")
            customer_city: str | None
            customer_address: str | None
            customer_postal_code: str | None  (from "postalCode")

        The address/province/city/postalCode fields (client request,
        2026-09: a new Didar Contact should carry the customer's full
        address, not just name+mobile - see
        src/didar/contact_client.py's upsert_contact()) were already
        present on this endpoint's response but previously dropped
        here - only name/phoneNumber were ever read. This endpoint's
        response shape (including these four fields) is the same one
        already confirmed for name/phoneNumber above; nothing new is
        being guessed.

        On any error (transport, auth, or malformed response), returns a
        dict with every value None so the caller can fall back to a
        synthetic contact name without breaking the sync flow.
        """
        path = f"/open-api/v1/ship-by-seller-orders/customer/{shipment_id}"
        empty = {
            "customer_full_name": None,
            "customer_mobile": None,
            "customer_province": None,
            "customer_city": None,
            "customer_address": None,
            "customer_postal_code": None,
        }
        try:
            payload = self._get(path, params={})
            data = payload.get("data", {}) or {}
            full_name = data.get("name") or None
            mobile = data.get("phoneNumber") or None
            province = data.get("state") or None
            city = data.get("city") or None
            address = data.get("address") or None
            postal_code = data.get("postalCode") or None
            log.info(
                "digikala: fetched SBS customer details for shipment %s "
                "(name=%r, mobile=%r, province=%r, city=%r, has_address=%s, postal_code=%r)",
                shipment_id, full_name, mobile, province, city, bool(address), postal_code,
            )
            return {
                "customer_full_name": full_name,
                "customer_mobile": mobile,
                "customer_province": province,
                "customer_city": city,
                "customer_address": address,
                "customer_postal_code": postal_code,
            }
        except Exception:
            log.exception(
                "digikala: failed to fetch SBS customer details for shipment %s",
                shipment_id,
            )
            return empty

    def fetch_shipment_details(self, shipment_id: str) -> dict:
        """Fetch shipment/parcel details for a Digikala Ship-by-Seller (SBS)
        order - tracking code and shipping cost, for the Didar deal-item
        description (client request, 2026-09).

        Calls GET /open-api/v1/ship-by-seller-orders/{shipment_id} with
        Bearer token. CONFIRMED response shape (client-supplied real
        payload, 2026-09): {"status": "ok", "data": {"items": [{...}]}} -
        same list-style envelope as /orders/history, but for a single
        shipment_id this should only ever return one item.

        Confirmed fields on that item:
            trackingCode: str  - the actual postal/courier tracking number
                (شماره مرسوله) - distinct from shipment_id (Digikala's own
                internal id, already on NormalizedOrder.shipment_id from
                /orders/history). This is what a customer would actually
                use to track their parcel with the post/courier, so it's
                the more useful value to show in Didar.
            shippingCost: int  - Rial, per every other Digikala money
                field seen so far (src/currency.py's DIGIKALA_PRICE_UNIT
                default) - NOT confirmed by an explicit unit label in
                this payload the way Tapsi Shop's operationalCost was, so
                still routed through to_rial()/price_unit like any other
                Digikala amount rather than assumed Rial outright.

        Returns a dict with keys:
            tracking_code: str | None
            shipping_cost: Decimal | None

        On any error (transport, auth, malformed response, or no items
        found), returns both as None so the caller can proceed without
        this data rather than breaking the sync flow - matching
        fetch_sbs_customer_details's error-handling convention above.
        """
        path = f"/open-api/v1/ship-by-seller-orders/{shipment_id}"
        try:
            payload = self._get(path, params={})
            items = (payload.get("data") or {}).get("items") or []
            if not items:
                log.warning(
                    "digikala: no items in shipment details for shipment %s", shipment_id,
                )
                return {"tracking_code": None, "shipping_cost": None}
            item = items[0]
            tracking_code = str(item["trackingCode"]) if item.get("trackingCode") else None
            raw_cost = item.get("shippingCost")
            shipping_cost = (
                to_rial(_to_decimal(raw_cost), self._config.price_unit)
                if raw_cost is not None
                else None
            )
            log.info(
                "digikala: fetched shipment details for shipment %s "
                "(tracking_code=%r, shipping_cost=%r)",
                shipment_id, tracking_code, shipping_cost,
            )
            return {"tracking_code": tracking_code, "shipping_cost": shipping_cost}
        except Exception:
            log.exception(
                "digikala: failed to fetch shipment details for shipment %s", shipment_id,
            )
            return {"tracking_code": None, "shipping_cost": None}

    def _fetch_sbs_rows_since_watermark(self, min_shipment_id: int) -> list[dict]:
        """
        Page through GET /open-api/v1/ship-by-seller-orders starting at
        `min_shipment_id`, sorted ascending by shipment_id (the documented
        `search[min_shipment_id]` cursor - see this module's docstring and
        digikala-sbs-migration-prompt.md, Section 2).

        Persists the watermark after EVERY page (not just once at the end
        of the whole poll) via self._repo.set_last_shipment_id - so a
        crash mid-pagination resumes from the last completed page instead
        of re-walking (or worse, re-syncing) everything already fetched.
        Safe to persist per-page: order=asc means each page's own max
        shipmentId is guaranteed >= every previous page's max.
        """
        rows: list[dict] = []
        page = 1
        size = 50

        while True:
            params = {
                "page": page,
                "size": size,
                "sort": "shipment_id",
                "order": "asc",
                "search[min_shipment_id]": min_shipment_id,
            }
            payload = self._get("/open-api/v1/ship-by-seller-orders", params=params)
            data = payload.get("data", {})
            items = data.get("items", [])
            rows.extend(items)

            page_shipment_ids = [
                int(item["shipmentId"]) for item in items if item.get("shipmentId") is not None
            ]
            if page_shipment_ids:
                self._repo.set_last_shipment_id(self.name, max(page_shipment_ids))

            pager = data.get("pager", {})
            total_pages = pager.get("total_pages", 0)

            # Same double-signal pagination guard as the old
            # /orders/history fetch: total_pages alone isn't trusted (the
            # docs' own example response shows it as 0 even with items
            # present), so a full page is itself reason enough to keep
            # going regardless of what total_pages claims.
            got_full_page = len(items) == size
            more_by_pager = page < total_pages
            if not items or not (got_full_page or more_by_pager):
                break
            page += 1

        return rows

    def _normalize_sbs_row(self, row: dict) -> NormalizedOrder:
        """
        Build a NormalizedOrder directly from one /ship-by-seller-orders
        row (list or single-shipment detail - both share this shape).
        No cross-row grouping: one shipment = one Deal (Decision 1).
        """
        shipment_id = row.get("shipmentId")
        order_id = row.get("orderId")
        variants = row.get("variants") or []

        items = [
            OrderItem(
                sku=str(v["sellerCode"]) if v.get("sellerCode") is not None else str(v.get("productId", "")),
                title=str(v.get("title", "")),
                quantity=int(v.get("count") or 1),
                # ASSUMPTION, NOT yet confirmed against a real payload -
                # see Decision 2, digikala-sbs-migration-prompt.md: the
                # schema only ever exposes "price" + "count" per variant,
                # with no separate unit_price/total_price split and no
                # discount field anywhere. Treated as price=per-unit,
                # final_price=price*count, no discount subtracted. Must be
                # verified against at least one real shipment whose panel
                # total is known (checking shippingCost as a possible home
                # for any mismatch) before being trusted for real money.
                unit_price=to_rial(_to_decimal(v.get("price")), self._config.price_unit),
                final_price=to_rial(
                    _to_decimal(v.get("price")) * _to_decimal(v.get("count") or 1),
                    self._config.price_unit,
                ),
                product_image_url=str(v["image_url"]) if v.get("image_url") else None,
            )
            for v in variants
        ]

        # Status mapping - Decision 3, digikala-sbs-migration-prompt.md:
        # isCancelled is the primary signal (an explicit, less
        # semantically-drifty boolean vs. a free-text field); "rejected"
        # is the other terminal status.text value. hasFailedDeliveryBefore
        # is deliberately NOT used - it means "failed at least once
        # before", not "currently failed"; a later attempt may still
        # succeed. pending/processing/processed/edited are all active and
        # sync normally.
        if row.get("isCancelled"):
            status_val = "cancelled"
        elif (row.get("status") or {}).get("text") == "rejected":
            status_val = "rejected"
        else:
            status_val = (row.get("status") or {}).get("text") or "unknown"

        address = row.get("address") or {}
        # Order-level fallback photo, same "last resort, per-item photo is
        # the real one" convention this project already uses elsewhere -
        # see OrderItem.product_image_url's docstring in base.py.
        product_image_url = items[0].product_image_url if items else None

        raw_shipping_cost = row.get("shippingCost")
        shipping_cost = (
            to_rial(_to_decimal(raw_shipping_cost), self._config.price_unit)
            if raw_shipping_cost is not None
            else None
        )
        tracking_code = str(row["trackingCode"]) if row.get("trackingCode") else None

        return NormalizedOrder(
            source=self.name,
            # Decision 1: shipmentId (not Digikala's order_id) is the
            # dedup key from here on.
            source_order_id=str(shipment_id),
            order_number=str(order_id) if order_id is not None else str(shipment_id),
            created_at=_parse_jalali_date(row.get("orderDate")),
            total_price=sum((i.final_price for i in items), Decimal("0")),
            status=status_val,
            items=items,
            # Populated directly from this same row now (client_name /
            # client_phone_number / address.* below) - see module
            # docstring. fetch_sbs_customer_details() (a narrower, separate
            # endpoint) remains a fallback in sync_engine.py's
            # _prepare_and_push_to_didar for whichever of these end up
            # null on a given row.
            customer_full_name=row.get("customer_name") or None,
            customer_mobile=row.get("customer_phone_number") or None,
            customer_address=row.get("customer_address") or None,
            customer_postal_code=row.get("customer_postal_code") or None,
            customer_province=address.get("state") or None,
            customer_city=address.get("city") or None,
            shipment_id=str(shipment_id) if shipment_id is not None else None,
            product_image_url=product_image_url,
            shipping_cost=shipping_cost,
            shipment_tracking_code=tracking_code,
        )

    def _seed_watermark_from_latest(self) -> int:
        """
        Cold start only (Decision 5, digikala-sbs-migration-prompt.md): a
        single request for the account's current highest shipmentId, so
        the very first watermark is deterministic rather than a guessed
        time cutoff, and no pre-existing order gets synced just because
        the watermark had never been set. Returns 0 if the account has no
        SBS shipments at all yet, so the very first real shipment (id
        >= 1) is picked up on the next poll.
        """
        payload = self._get(
            "/open-api/v1/ship-by-seller-orders",
            params={"page": 1, "size": 1, "sort": "id", "order": "desc"},
        )
        items = (payload.get("data") or {}).get("items") or []
        if not items or items[0].get("shipmentId") is None:
            return 0
        return int(items[0]["shipmentId"])


def _parse_jalali_date(value: str | None) -> datetime:
    """
    /ship-by-seller-orders' `orderDate` is a Jalali calendar DATE-ONLY
    string ("1403/11/07" - no time component, unlike /orders/history's
    ISO order_created_at used before this migration).

    BUGFIX (client feedback, 2026-09 - "زمان سفارش دیجی‌کالا اشتباه ثبت
    شده"): this used to always resolve to Iran-local MIDNIGHT on that
    date, no matter what time the order actually came in - because
    orderDate carries no time-of-day at all. That midnight then flowed
    straight into two client-visible places: the "زمان" line in the
    Telegram new-order message (src/telegram.py's _format_new_order_message,
    via order.created_at) and, worse, src/didar/scheduling.py's پیامک ۱
    due date (order_registered_at + 5 hours) - which meant EVERY single
    Digikala order's پیامک ۱ got scheduled for 05:00 Iran time, regardless
    of whether the order actually came in at 05:00 or at 21:00.

    Fix: when orderDate's calendar date is TODAY (Iran-local) - which is
    the overwhelmingly common case, since fetch_new_orders() runs on a
    tight poll loop (2 minutes, per main.py) and every row it sees is
    brand new by construction (shipmentId watermark, not a date filter -
    see this module's docstring) - anchor to the actual moment this row
    was fetched/normalized instead of midnight. That's a real timestamp,
    not a guess: it's when our own polling loop first observed the order,
    which for a 2-minute poll interval is a close proxy for when it was
    actually placed - a world better than a constant 00:00 for every
    order regardless of source.
    For any non-today date (e.g. a stale/backdated row hit via the retry
    path days later, or a genuinely malformed value) this deliberately
    falls back to the old Iran-local-midnight behavior rather than
    inventing a time-of-day for a day that isn't "now" - same
    _IRAN_TZ-based convention as src/telegram.py's _iran_midnight_utc().
    Falls back to "now" outright if the value is missing/unparseable,
    matching the old _parse_date()'s convention in this file.

    Root cause is still Digikala's API, not this code: orderDate simply
    has no time-of-day field to read a real value from (confirmed absent
    from every observed /ship-by-seller-orders row - see this module's
    docstring and tests/test_digikala.py's fixtures). If Digikala ever
    exposes a real per-order timestamp, prefer that over this heuristic.
    """
    if not value:
        return datetime.now(timezone.utc)
    try:
        year, month, day = (int(part) for part in value.split("/"))
        gregorian_date = jdatetime.date(year, month, day).togregorian()
        now_iran = datetime.now(_IRAN_TZ)
        if gregorian_date == now_iran.date():
            return now_iran.astimezone(timezone.utc)
        local_midnight = datetime.combine(gregorian_date, datetime.min.time(), tzinfo=_IRAN_TZ)
        return local_midnight.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return datetime.now(timezone.utc)