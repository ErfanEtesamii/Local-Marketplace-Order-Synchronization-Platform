"""
Digikala "ارسال به انبار دیجی‌کالا" (FBD) adapter - stage 2 of the FBD
feature.

WHAT THIS IS: a completely separate source from src/marketplaces/
digikala.py. That adapter syncs CUSTOMER orders (ship-by-seller); this
one reads GET /open-api/v1/orders ("getting details of all active order
items seller") - items Digikala itself has bought from the seller and
expects delivered to its own warehouse. There is no customer on this
endpoint at all (no name, phone or address field exists in the payload),
so nothing here produces a NormalizedOrder and nothing here implements
MarketplaceAdapter - see src/marketplaces/warehouse_base.py's module
docstring for why WarehouseShipmentItem is its own dataclass.

READ-ONLY, DELIBERATELY: the only other documented verb on this endpoint
is DELETE /open-api/v1/orders/{order_item_id}, which most likely
cancels/rejects the item. It is never called from this file and must not
be added.

AUTH IS A COPY, NOT AN IMPORT (project isolation convention - same note
as src/marketplaces/digikala2.py): _load_tokens / _save_tokens /
_refresh_access_token / _get below are copied verbatim (behaviour-wise)
from DigikalaAdapter, including the 401 -> refresh -> retry-once path.
Two consequences, both intentional:

  1. This adapter reads AND WRITES THE SAME token cache file the
     production DigikalaAdapter uses - data/digikala_tokens.json, derived
     from settings.db_path exactly like DigikalaAdapter.__init__ does.
     This is NOT an oversight. Digikala may rotate refresh_token on every
     refresh (see _refresh_access_token), so if this adapter kept its own
     cache file, whichever of the two refreshed last would silently
     invalidate the other's stored refresh_token and break the live
     customer-order sync. The only exception is `token_cache_path`, an
     optional constructor argument used by tests to point at a tmp file
     so a test run never touches (or depends on) the real cache.
  2. Any bug fixed in digikala.py's auth block must be re-applied HERE
     BY HAND. There is no shared code to fix once.

COLD START (the single most dangerous thing about this source)
--------------------------------------------------------------
Unlike /ship-by-seller-orders, this endpoint exposes NO monotonic,
documented cursor (no min_shipment_id equivalent - confirmed by the
stage-0 probe, see scripts/probe_digikala_warehouse.py). The only
"already seen" state would otherwise be the local dedupe table
(synced_warehouse_shipments, stage 3), which is empty on a fresh
database - so a first run on a fresh DB would push EVERY currently
active FBD item into Didar at once. That exact failure has already
happened once in this project with the old date-filter approach (43
two-month-old orders synced on 2026-08-31, see digikala.py's module
docstring), and it must not happen again.

So this adapter keeps an explicit CREATED-AT FLOOR, persisted via
Repository.get_last_sync_time/set_last_sync_time under this adapter's
own `name` ("digikala_warehouse"):

  - First ever run: the floor is written as "now" and NOTHING is
    synced. Every item already active at install time is, by
    definition, pre-existing backlog - the client's own answer was that
    only items arriving from now on should reach Didar.
  - Every later run: only rows whose order_created_at is strictly newer
    than that floor are returned. The floor is NEVER advanced
    afterwards, on purpose: advancing it to "now" each poll would drop
    any item Digikala's own backend recorded a few seconds late (the
    exact silent-drop failure mode the SBS migration moved away from).
    The list only ever contains ACTIVE items, so it drains as items are
    delivered - this does not grow without bound.
  - Duplicate protection on top of that floor is the local dedupe table
    (stage 3): an item newer than the floor is re-seen on every poll
    until it leaves the active list, and the dedupe table is what stops
    it being pushed to Didar twice.

`since` (the argument) is accepted only for symmetry with
fetch_new_orders() and is IGNORED - the floor above is the whole
definition of "new" here. Honouring a caller-supplied 5-hour window on
top of it would silently drop anything older than that window but newer
than the floor, with no retry path to pick it back up.

WHAT IS CONFIRMED vs GUESSED IN THE RESPONSE PARSING BELOW
-----------------------------------------------------------
CONFIRMED by the 2026-09-18 live probe (see warehouse_base.py's field
provenance section): order_item_id is present and unique per row;
order_created_at / commitment_date are ISO-8601 with a numeric offset,
not Jalali; selling_price is PER UNIT; supplier_code exists but was an
empty string on both sample rows; a product image URL is present.

UNCONFIRMED / tolerated: the exact KEY NAMES for the title and image
fields, and whether the rows always live at data.items[]. The SBS
endpoint's documentation and its real payload disagreed about the
response envelope (see digikala.py's _fetch_shipment_row), so the same
caution is applied here: _rows_of()/_first_present() accept the
documented shape plus the obvious alternatives instead of hard-failing
on one guess. A row that genuinely cannot be read is SKIPPED with a
warning - never defaulted to a fabricated title, quantity or price.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx

from src.config import DigikalaConfig, settings
from src.currency import to_rial
from src.db.repository import Repository
from src.http_utils import default_retry, raise_for_status_with_body
from src.logger import get_logger
from src.marketplaces.warehouse_base import WarehouseShipmentItem
from src.token_utils import (
    jwt_seconds_left,
    prefer_cached_token,
    read_token_cache,
    write_token_cache_atomic,
)

log = get_logger(__name__)

# After a failed proactive refresh, wait this long before trying again
# (each poll cycle would otherwise retry every POLL_INTERVAL_SECONDS).
_PROACTIVE_REFRESH_RETRY_BACKOFF_SECONDS = 300

_ORDERS_PATH = "/open-api/v1/orders"

# Same page size as digikala.py's SBS pagination - large enough that a
# normal poll is a single request, small enough to stay well inside any
# undocumented server cap.
_PAGE_SIZE = 50

# Hard stop so an unhonoured sort/filter (a real, documented risk on
# this API - see digikala.py's module docstring on /orders/history)
# can't turn one poll into a full-history walk. 40 pages = 2000 rows,
# far beyond any plausible active FBD list.
_MAX_PAGES = 40

# Candidate key names, most-documented first. See this module's
# docstring: the exact spelling of the title/image keys is the one thing
# the probe could not pin down beyond doubt, so the obvious variants are
# tolerated rather than guessed at once and hard-coded.
_ORDER_ITEM_ID_KEYS = ("order_item_id", "id")
_TITLE_KEYS = ("product_variant_title", "product_title", "title")
_IMAGE_KEYS = ("product_image_url", "image_url", "product_image", "image")
_CREATED_AT_KEYS = ("order_created_at", "created_at")
_COMMITMENT_KEYS = ("commitment_date", "commitment_at")


def _to_decimal(value) -> Decimal | None:
    """None means "the API did not give us a usable number" - NOT zero.
    Deliberately different from digikala.py's _to_decimal (which returns
    Decimal("0") on failure): a zero price here would be written into a
    Didar deal as a real amount."""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _to_int_or_none(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_iso_datetime(value) -> datetime | None:
    """
    CONFIRMED (stage-0 probe, 2026-09-18): this endpoint's dates are
    Gregorian ISO-8601 with a numeric offset, e.g.
    "2026-09-11T08:49:01.000000+03:30" - so this is the /orders/history
    style (_parse_history_date in digikala.py), NOT the Jalali
    date-only orderDate style of /ship-by-seller-orders.

    Returns None - never "now" - on a missing/unparseable value, unlike
    digikala.py's _parse_history_date. "Now" would be a fabricated
    timestamp, and for this source created_at drives the cold-start
    floor comparison: a fabricated "now" would make an old backlog item
    look brand new and push it to Didar.
    """
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # A naive timestamp can't be compared against the tz-aware floor;
    # the probe only ever showed offsets, so this is a defensive branch.
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _first_present(row: dict, keys: tuple[str, ...]):
    """First non-empty value among `keys`. Empty string counts as
    absent: the probe showed supplier_code arriving as "" rather than
    null, and an empty string is "the API had nothing", not a value."""
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def _rows_of(data: dict) -> list:
    """
    Rows out of one response's `data` object. data.items[] is the
    documented/observed shape; the alternatives below exist for the same
    reason digikala.py's _fetch_shipment_row tolerates two shapes - on
    this API the docs and the real payload have already been caught
    disagreeing once.
    """
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for key in ("items", "orders", "order_items", "results"):
        value = data.get(key)
        if isinstance(value, list):
            return value
    return []


class DigikalaWarehouseAdapter:
    """
    Reader for the first Digikala store's FBD queue.

    NOT a MarketplaceAdapter subclass on purpose (see warehouse_base.py):
    there is no fetch_order_detail() to implement - this endpoint has no
    per-item detail call - and SyncEngine drives this source through its
    own separate loop rather than the NormalizedOrder pipeline (stage 6).

    Second store (digikala2): NOT handled here. Per this project's
    convention that would be its own copied file
    (digikala2_warehouse.py), not a parameterised store id - and the
    client's answer for this feature was "first store only", so no such
    file exists yet.
    """

    name = "digikala_warehouse"

    def __init__(
        self,
        config: DigikalaConfig | None = None,
        repository: Repository | None = None,
        token_cache_path: str | Path | None = None,
    ) -> None:
        self._config = config or settings.digikala
        # Own Repository handle for the created-at floor (see the COLD
        # START section of this module's docstring), defaulting to the
        # same settings.db_path SyncEngine's own Repository uses - same
        # arrangement as DigikalaAdapter.__init__.
        self._repo = repository or Repository()
        # SHARED with DigikalaAdapter by design - see the AUTH IS A COPY
        # section above. The override exists only so tests don't read or
        # clobber the real cache file.
        self._token_cache_path = (
            Path(token_cache_path)
            if token_cache_path is not None
            else Path(settings.db_path).resolve().parent / "digikala_tokens.json"
        )
        self._access_token, self._refresh_token = self._load_tokens()
        self._client = httpx.Client(
            base_url=self._config.base_url,
            headers={"content-type": "application/json"},
            timeout=30.0,
        )

    # --- auth (copied from src/marketplaces/digikala.py) ----------------

    def _load_tokens(self) -> tuple[str, str]:
        """Prefer a previously-refreshed pair over the static .env seed,
        since refresh_token rotates and .env is not rewritten at runtime."""
        if self._token_cache_path.exists():
            try:
                cached = json.loads(self._token_cache_path.read_text())
                return cached["access_token"], cached["refresh_token"]
            except (json.JSONDecodeError, KeyError, OSError):
                log.warning(
                    "digikala_warehouse: failed to read cached tokens at %s, "
                    "falling back to .env",
                    self._token_cache_path,
                )
        return self._config.access_token, self._config.refresh_token

    def _save_tokens(self) -> None:
        # Atomic (temp file + os.replace): Faraz-Honar reads this file
        # from another process and must never see half a JSON document.
        write_token_cache_atomic(
            self._token_cache_path, self._access_token, self._refresh_token
        )

    # --- proactive refresh / shared-cache adoption (2026-09 token fix) ---
    #
    # Access tokens live ~2h. Refreshing only AFTER a 401 (the old
    # behaviour) left the shared cache file holding an expired token for a
    # couple of minutes after every expiry, which broke Faraz-Honar's
    # price updater (it reads this file and may not refresh itself). So:
    #   1. refresh proactively while the current token still has less than
    #      settings.digikala_token_refresh_lead_seconds of life left, and
    #   2. before refreshing (proactively or after a 401) first look at the
    #      cache file - if another adapter/process already wrote a newer
    #      pair, adopt it instead of spending another refresh.
    # Same block is copied into digikala.py / digikala2.py /
    # digikala_warehouse.py (project convention: auth is a copy).

    # monotonic() deadline before which a failed proactive refresh is not
    # retried (the 401 path still works regardless).
    _next_proactive_refresh_at: float = 0.0

    def _adopt_cached_tokens(self, *, current_known_bad: bool = False) -> bool:
        cached = read_token_cache(self._token_cache_path)
        if cached is None:
            return False
        cached_access, cached_refresh = cached
        if not prefer_cached_token(
            self._access_token, cached_access, current_known_bad=current_known_bad
        ):
            return False
        self._access_token, self._refresh_token = cached_access, cached_refresh
        return True

    def _refresh_if_expiring(self) -> None:
        lead = settings.digikala_token_refresh_lead_seconds
        if lead <= 0:
            return
        left = jwt_seconds_left(self._access_token)
        if left is None or left > lead:
            return
        if self._adopt_cached_tokens():
            left = jwt_seconds_left(self._access_token)
            if left is None or left > lead:
                log.info(
                    "digikala_warehouse: adopted a fresher access token from the shared cache "
                    "(expires in %d s)",
                    int(left) if left is not None else -1,
                )
                return
        if time.monotonic() < self._next_proactive_refresh_at:
            return
        log.info(
            "digikala_warehouse: access token expires in %d s (lead %d s), refreshing proactively",
            int(left),
            lead,
        )
        try:
            self._refresh_access_token()
        except Exception as exc:  # noqa: BLE001 - never let this break a poll
            self._next_proactive_refresh_at = (
                time.monotonic() + _PROACTIVE_REFRESH_RETRY_BACKOFF_SECONDS
            )
            log.warning(
                "digikala_warehouse: proactive token refresh failed (%s); continuing with the "
                "current token - the 401 path will still refresh if needed",
                exc,
            )

    @default_retry()
    def _refresh_access_token(self) -> None:
        # Confirmed via official docs: BOTH access_token and
        # refresh_token are required in the body, despite the endpoint's
        # name - omitting access_token returns 400 with
        # errors.access_token = ["این قسمت نباید خالی باشد"]. This was a
        # real production bug in digikala.py once; do not "simplify" it.
        resp = self._client.post(
            "/open-api/v1/auth/refresh-token",
            json={"access_token": self._access_token, "refresh_token": self._refresh_token},
        )
        raise_for_status_with_body(resp)
        data = resp.json().get("data", {})
        self._access_token = data["access_token"]
        self._refresh_token = data.get("refresh_token", self._refresh_token)
        self._save_tokens()
        log.info(
            "digikala_warehouse: access token refreshed, new expiry=%s",
            data.get("access_token_expires_at", {}).get("date"),
        )

    @default_retry()
    def _get(self, path: str, params: dict, _already_refreshed: bool = False) -> dict:
        if not _already_refreshed:
            self._refresh_if_expiring()
        resp = self._client.get(
            path, params=params, headers={"Authorization": f"Bearer {self._access_token}"}
        )
        if resp.status_code == 401 and not _already_refreshed:
            if self._adopt_cached_tokens(current_known_bad=True):
                log.info(
                    "digikala_warehouse: access token rejected (401) but the shared token cache "
                    "already holds a newer pair, using it instead of refreshing"
                )
            else:
                log.info("digikala_warehouse: access token expired (401), refreshing")
                self._refresh_access_token()
            return self._get(path, params, _already_refreshed=True)
        raise_for_status_with_body(resp)
        return resp.json()

    # --- main entry point -----------------------------------------------

    def fetch_new_warehouse_shipments(
        self, since: datetime | None = None
    ) -> list[WarehouseShipmentItem]:
        """
        Every active FBD item created after this adapter's created-at
        floor, newest first. See the COLD START section of this module's
        docstring for what the floor is and why `since` is ignored.

        Transport/auth errors PROPAGATE (they are not swallowed into an
        empty list) - same contract as fetch_new_orders(): the caller
        logs and retries on the next poll, and an empty list must only
        ever mean "Digikala really has nothing new", never "the request
        failed".
        """
        floor = self._repo.get_last_sync_time(self.name)
        if floor is None:
            floor = datetime.now(timezone.utc)
            self._repo.set_last_sync_time(self.name, floor)
            log.info(
                "digikala_warehouse: cold start - created-at floor seeded at %s, "
                "no pre-existing FBD item synced (see module docstring)",
                floor.isoformat(),
            )
            return []

        rows = self._fetch_rows_created_after(floor)
        items = [self._normalize_row(row) for row in rows]
        items = [item for item in items if item is not None]
        log.info(
            "digikala_warehouse: fetched %d FBD item(s) created after the floor %s "
            "(%d row(s) read, %d unusable and skipped)",
            len(items), floor.isoformat(), len(rows), len(rows) - len(items),
        )
        return items

    def _fetch_rows_created_after(self, floor: datetime) -> list[dict]:
        """
        Page through GET /open-api/v1/orders newest-first and keep only
        rows created strictly after `floor`.

        `sort=order_created_at&order=desc` is requested but NOT trusted
        as a filter - the early stop below only needs rows to ARRIVE
        newest-first, and rows older than the floor are dropped
        individually anyway, so a server that ignores the sort costs
        extra pages (bounded by _MAX_PAGES) rather than producing wrong
        results.

        Pagination uses the same two-signal guard as
        digikala.py's _fetch_sbs_rows_since_watermark: pager.total_pages
        is reported as 0 even with items present on this API, so a FULL
        PAGE is itself reason enough to keep going.
        """
        rows: list[dict] = []
        page = 1

        while True:
            payload = self._get(
                _ORDERS_PATH,
                params={
                    "page": page,
                    "size": _PAGE_SIZE,
                    "sort": "order_created_at",
                    "order": "desc",
                },
            )
            data = payload.get("data") or {}
            items = _rows_of(data)

            created_on_page: list[datetime] = []
            for row in items:
                if not isinstance(row, dict):
                    continue
                created = _parse_iso_datetime(_first_present(row, _CREATED_AT_KEYS))
                if created is None:
                    # No usable creation date means we cannot tell this
                    # row apart from a pre-existing backlog item, and the
                    # cold-start floor is the only thing protecting Didar
                    # from that backlog - so it is skipped loudly rather
                    # than assumed new.
                    log.warning(
                        "digikala_warehouse: row %r has no parseable creation date - "
                        "skipping it rather than assuming it is new",
                        _first_present(row, _ORDER_ITEM_ID_KEYS),
                    )
                    continue
                created_on_page.append(created)
                if created > floor:
                    rows.append(row)

            # Client-side early stop: once the oldest row on a page is
            # already at/below the floor, everything after it is older
            # still (rows arrive desc) - nothing left to find.
            if created_on_page and min(created_on_page) <= floor:
                break

            pager = data.get("pager") or {}
            total_pages = pager.get("total_pages") or 0
            got_full_page = len(items) == _PAGE_SIZE
            more_by_pager = page < total_pages
            if not items or not (got_full_page or more_by_pager):
                break
            if page >= _MAX_PAGES:
                log.warning(
                    "digikala_warehouse: hit the %d-page safety cap while paging "
                    "/open-api/v1/orders - stopping here; remaining rows (if any) "
                    "will be seen on the next poll",
                    _MAX_PAGES,
                )
                break
            page += 1

        return rows

    def _normalize_row(self, row: dict) -> WarehouseShipmentItem | None:
        """
        One raw row -> WarehouseShipmentItem, or None if the row is
        missing something this feature genuinely cannot invent.

        Deliberately pure (no I/O), same as digikala.py's
        _normalize_sbs_row, so tests can exercise it directly without
        mocking the network.

        SKIPPED (None + warning) rather than defaulted, per the
        project's no-guessing rule, when any of these is unusable:
        order_item_id (the dedupe key - without it a repeat poll would
        create a duplicate Deal), product title, quantity, selling_price
        or order_created_at. A fabricated title/price here would land in
        a real Didar deal and in the client's own reports.
        """
        shipment_id = _first_present(row, _ORDER_ITEM_ID_KEYS)
        if shipment_id is None:
            log.warning(
                "digikala_warehouse: row without order_item_id - skipping "
                "(no safe dedupe key; syncing it would risk a duplicate Deal "
                "on the next poll). Raw keys: %r",
                sorted(row.keys()),
            )
            return None

        title = _first_present(row, _TITLE_KEYS)
        quantity = _to_int_or_none(row.get("quantity"))
        selling_price = _to_decimal(row.get("selling_price"))
        created_at = _parse_iso_datetime(_first_present(row, _CREATED_AT_KEYS))

        missing = [
            field_name
            for field_name, value in (
                ("product title", title),
                ("quantity", quantity),
                ("selling_price", selling_price),
                ("order_created_at", created_at),
            )
            if value is None
        ]
        if missing:
            log.warning(
                "digikala_warehouse: order_item_id=%s is missing %s - skipping this "
                "item rather than inventing a value for it",
                shipment_id, ", ".join(missing),
            )
            return None

        image_url = _first_present(row, _IMAGE_KEYS)
        order_id = row.get("order_id")
        supplier_code = _first_present(row, ("supplier_code",))

        return WarehouseShipmentItem(
            source=self.name,
            source_shipment_id=str(shipment_id),
            product_title=str(title),
            quantity=quantity,
            # CONFIRMED per-unit (probe: selling_price * quantity ==
            # total_price on every sample row), and routed through
            # to_rial()/price_unit exactly like every other money value
            # in this project - see src/currency.py.
            unit_price=to_rial(selling_price, self._config.price_unit),
            created_at=created_at,
            product_image_url=str(image_url) if image_url is not None else None,
            commitment_date=_parse_iso_datetime(_first_present(row, _COMMITMENT_KEYS)),
            order_id=str(order_id) if order_id is not None else None,
            # Empty string is treated as absent (the probe saw "" on both
            # sample rows). Display-only downstream - never used as a
            # Didar product Code, see warehouse_base.py.
            supplier_code=str(supplier_code) if supplier_code is not None else None,
        )
