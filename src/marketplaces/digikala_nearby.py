"""
Digikala 3-hour ("ارسال ۳ ساعته" / SBS nearby stores) order support.

Digikala has three order types on the seller panel:
  - seller orders (سفارش‌های فروشنده)   -> GET /ship-by-seller-orders
  - warehouse shipments (ارسال دیجی‌کالا) -> digikala_warehouse.py (FBD)
  - 3-hour express orders                -> GET /open-api/v1/near-by-stores

The third type was never fetched: /ship-by-seller-orders only returns
`seller` / `seller_post` shipments, so a 3-hour order (e.g. shipment
385944895) never reached Didar. This mixin adds that endpoint to BOTH
Digikala adapters (digikala.py and digikala2.py) without a new source name,
so Didar labels, Telegram, SMS, dedup keys ("digikala-<shipmentId>") and the
retry path all keep working unchanged.

Differences from the normal SBS flow (per the official docs):

  1. near-by-stores has NO `search[min_shipment_id]` filter, so the
     shipmentId watermark cannot be used. Instead every poll fetches the
     open rows (`search[status]` = pending, then processing) and drops the
     ones already handled. Open 3-hour orders are few, so this is cheap.
  2. Already-handled rows are skipped BEFORE any enrichment (confirm call,
     Promotions API lookup - which has a low rate limit), using the
     synced_orders table plus an in-process "already returned" set.
  3. COLD START: the first time this runs for an account, every row that
     is open at that moment is added to the permanent ignore list without
     syncing, so orders the team already registered by hand do not flood
     Didar. Only orders created after that are synced.
  4. Every row is forced to shipping_method="EXPRESS": the endpoint only
     ever lists 3-hour orders, and the row's own `isDigiExpress` flag is
     not guaranteed to be true there. This is what triggers the express
     SMS alert / Didar note.
  5. Auto-confirm (pending -> processing) is OFF by default
     (NEARBY_AUTO_CONFIRM = False). update-status is only documented for
     /ship-by-seller-orders, and confirming is a business action, so it
     must be verified on a real 3-hour order before enabling. With it off,
     a pending row syncs with whatever customer data it already has (the
     existing sync_engine fallbacks still apply).

Any failure here is logged and swallowed: the 3-hour fetch must never
block the normal SBS orders of the same poll.
"""
from __future__ import annotations

from dataclasses import replace

from src.logger import get_logger
from src.marketplaces.base import NormalizedOrder

log = get_logger(__name__)

_NEARBY_PATH = "/open-api/v1/near-by-stores"
_NEARBY_OPEN_STATUSES = ("pending", "processing")
_PAGE_SIZE = 50
# Hard stop so a misbehaving pager can never loop forever.
_MAX_PAGES = 20


def _cold_start_key(name: str) -> str:
    # Reuses the existing digikala_shipment_watermark table purely as a
    # "cold start done" marker (value = max shipmentId seen at seeding);
    # it is never read as a cursor.
    return f"{name}_nearby"


class NearbyStoresMixin:
    """Expects the host adapter to provide: name, _repo, _get,
    _update_status, _normalize_sbs_row, _build_promotion_map."""

    # See module docstring, point 5.
    NEARBY_AUTO_CONFIRM = False

    def _nearby_seen(self) -> set[str]:
        seen = getattr(self, "_nearby_seen_ids", None)
        if seen is None:
            seen = set()
            self._nearby_seen_ids = seen
        return seen

    def _fetch_nearby_rows(self, statuses=_NEARBY_OPEN_STATUSES) -> list[dict]:
        rows: list[dict] = []
        seen_ids: set = set()
        for status in statuses:
            page = 1
            while page <= _MAX_PAGES:
                payload = self._get(
                    _NEARBY_PATH,
                    params={
                        "page": page,
                        "size": _PAGE_SIZE,
                        "sort": "id",
                        "order": "asc",
                        "search[status]": status,
                    },
                )
                data = payload.get("data") or {}
                items = data.get("items") or []
                for item in items:
                    sid = item.get("shipmentId")
                    if sid is None or sid in seen_ids:
                        continue
                    seen_ids.add(sid)
                    rows.append(item)
                total_pages = (data.get("pager") or {}).get("total_pages", 0)
                # Same double-signal guard as the SBS pager: a full page
                # keeps going even if total_pages under-reports.
                if not items or not (len(items) == _PAGE_SIZE or page < total_pages):
                    break
                page += 1
        return rows

    def _fetch_nearby_row(self, shipment_id) -> dict:
        """One 3-hour shipment row via search[search_term]; {} if absent.
        Used for the post-confirm re-fetch and the detail/retry path,
        because /ship-by-seller-orders/{id} is not documented to return
        nearby shipments."""
        for status in (None, *_NEARBY_OPEN_STATUSES):
            params = {"page": 1, "size": _PAGE_SIZE, "search[search_term]": str(shipment_id)}
            if status:
                params["search[status]"] = status
            payload = self._get(_NEARBY_PATH, params=params)
            for item in (payload.get("data") or {}).get("items") or []:
                if str(item.get("shipmentId")) == str(shipment_id):
                    return item
        return {}

    def _lookup_nearby_for_detail(self, shipment_id) -> dict:
        """Best-effort wrapper for the detail/retry path: never raises."""
        try:
            return self._fetch_nearby_row(shipment_id)
        except Exception:
            log.exception("%s: 3-hour lookup failed for shipment %s", self.name, shipment_id)
            return {}

    def _confirm_nearby_if_pending(self, row: dict) -> dict:
        if not self.NEARBY_AUTO_CONFIRM:
            return row
        status_text = (row.get("status") or {}).get("text")
        if row.get("isCancelled") or status_text != "pending":
            return row
        shipment_id = row.get("shipmentId")
        if shipment_id is None:
            return row
        new_status = row.get("nextStatus") or "processing"
        code = row.get("verificationCode")
        try:
            code = int(code) if code is not None else None
        except (TypeError, ValueError):
            code = None
        try:
            self._update_status(shipment_id, new_status, code)
        except Exception:
            log.exception(
                "%s: failed to auto-confirm pending 3-hour shipment %s", self.name, shipment_id
            )
            return row
        try:
            refreshed = self._fetch_nearby_row(shipment_id)
        except Exception:
            log.exception(
                "%s: confirmed 3-hour shipment %s but re-fetch failed", self.name, shipment_id
            )
            return row
        return refreshed or row

    def _normalize_nearby_row(self, row: dict) -> NormalizedOrder:
        order = self._normalize_sbs_row(row, promotion_map=self._build_promotion_map(row))
        return replace(order, shipping_method="EXPRESS")

    def fetch_new_nearby_orders(self) -> list[NormalizedOrder]:
        """New 3-hour orders for this account. Never raises."""
        try:
            return self._fetch_new_nearby_orders()
        except Exception:
            log.exception("%s: 3-hour (near-by-stores) fetch failed - skipping this poll", self.name)
            return []

    def _fetch_new_nearby_orders(self) -> list[NormalizedOrder]:
        marker = _cold_start_key(self.name)
        rows = self._fetch_nearby_rows()

        if self._repo.get_last_shipment_id(marker) is None:
            ids = [str(r["shipmentId"]) for r in rows]
            self._repo.add_ignored_ids(self.name, ids, reason="nearby_cold_start")
            max_id = max((int(i) for i in ids), default=0)
            self._repo.set_last_shipment_id(marker, max_id)
            log.info(
                "%s: 3-hour cold start - %d open shipment(s) marked ignored, none synced",
                self.name, len(ids),
            )
            return []

        ignored = self._repo.get_ignored_ids(self.name)
        seen = self._nearby_seen()
        fresh = []
        for row in rows:
            sid = str(row["shipmentId"])
            if sid in ignored or sid in seen or self._repo.is_already_synced(self.name, sid):
                continue
            fresh.append(row)

        orders = []
        for row in fresh:
            sid = str(row["shipmentId"])
            try:
                row = self._confirm_nearby_if_pending(row)
                orders.append(self._normalize_nearby_row(row))
                seen.add(sid)
            except Exception:
                # Not added to `seen`, so it is retried next poll.
                log.exception("%s: could not normalize 3-hour shipment %s", self.name, sid)
        if orders:
            log.info("%s: fetched %d new 3-hour shipment(s)", self.name, len(orders))
        return orders
