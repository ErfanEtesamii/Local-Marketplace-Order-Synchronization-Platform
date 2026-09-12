"""
Didar CRM - Deal client.

Rewritten per the project's structured-data decision: pricing and
product data go into their own proper Didar fields, not a text blob.
Specifically:

  - Amount            -> DealItems[].UnitPrice (not text)
  - Customer name      -> already handled via Contact/PersonId
  - Product name        -> DealItems[].ProductId, linked to a real
                           catalog Product (auto-created if no match -
                           see product_client.py)
  - Order source (site) -> Deal.LabelIds (a Deal Label - see
                           _label_id_for_source() below; NOT a Tag,
                           corrected 2026-09 after direct confirmation
                           from Didar's own support agent) AND, per a
                           later client request, also written as
                           readable text in Description alongside a
                           link back to the order on its origin
                           platform - see below
  - Title                -> "معامله {display_name}", matching Didar's
                           own default naming convention for manually
                           created deals - NOT "{order_number} - {source}"
                           as originally implemented

Endpoint: POST {DIDAR_BASE_URL}/deal/save_v2?apikey={API_KEY}
Confirmed via live testing: Deal.save expects PersonId (not ContactId).

DESCRIPTION - source label + order link (client request, 2026-08):
Only Faraz Honar (the client's own WooCommerce site) gets a confirmed,
real per-order deep link (standard wp-admin edit-order URL - this
pattern is well established, not a guess). The four marketplaces
(Tapsi Shop, Digikala, Basalam, SnappShop) do NOT have a confirmed
per-order URL pattern for their vendor panels - inventing one risks a
broken/misleading link, so Description instead links to that vendor's
panel *home page* (confirmed URLs, from the original project proposal)
plus the order number as text, to be searched manually. Revisit with a
real per-order URL once one is confirmed from any of those panels.

DealItems are sent as a top-level sibling of "Deal", matching the
request example in the current Didar API documentation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

import httpx

from src.config import DidarConfig, settings
from src.didar.category_mapping import _normalize_fa
from src.didar.contact_client import DidarApiError
from src.didar.product_client import DidarProductClient
from src.http_utils import default_retry, raise_for_status_with_body
from src.logger import get_logger
from src.marketplaces.base import NormalizedOrder
from src.shipping_fees import format_toman, shipping_fee_toman

if TYPE_CHECKING:
    from src.db.repository import Repository

log = get_logger(__name__)


@dataclass(frozen=True)
class DealStatusBreakdown:
    """All/Pending/Won/Lost counts+totals for one source+window, as
    returned in one shot by /deal/search_v2 when Criteria.Status is
    left unset - see DidarDealClient.get_status_breakdown(). All zero
    by default so a failed/label-less source contributes nothing when
    summed (see telegram.py's _aggregate_live_breakdown)."""

    all_count: int = 0
    all_total: Decimal = Decimal("0")
    pending_count: int = 0
    pending_total: Decimal = Decimal("0")
    won_count: int = 0
    won_total: Decimal = Decimal("0")
    lost_count: int = 0
    lost_total: Decimal = Decimal("0")

    def __add__(self, other: "DealStatusBreakdown") -> "DealStatusBreakdown":
        return DealStatusBreakdown(
            all_count=self.all_count + other.all_count,
            all_total=self.all_total + other.all_total,
            pending_count=self.pending_count + other.pending_count,
            pending_total=self.pending_total + other.pending_total,
            won_count=self.won_count + other.won_count,
            won_total=self.won_total + other.won_total,
            lost_count=self.lost_count + other.lost_count,
            lost_total=self.lost_total + other.lost_total,
        )


_SOURCE_DISPLAY_NAMES = {
    "tapsishop": "تپسی‌شاپ",
    "digikala": "دیجی‌کالا",
    # Second Digikala store (src/marketplaces/digikala2.py).
    "digikala2": "دیجی‌کالا (فروشگاه دوم)",
    "basalam": "باسلام",
    "snappshop": "اسنپ‌شاپ",
    "farazhonar": "فرازهنر",
}

# Pagination knobs for list_current_deals_in_stage()'s live stage
# snapshot - same values/reasoning as deal_poller.py's own
# _PAGE_SIZE/_MAX_PAGES (kept as separate module-level constants
# rather than a cross-module import, same tradeoff this file already
# makes for _iso()/_format_rial()).
_STAGE_SNAPSHOT_PAGE_SIZE = 50
_STAGE_SNAPSHOT_MAX_PAGES = 100

# Pagination knobs for get_created_date_stats_for_label() - same
# values/reasoning as deal_poller.py's own _PAGE_SIZE/_MAX_PAGES (that
# method's full-row-pagination-plus-client-side-RegisterTime-check
# pattern is exactly what get_created_date_stats_for_label() reuses).
_CREATED_RANGE_PAGE_SIZE = 50
_CREATED_RANGE_MAX_PAGES = 100

# Vendor panel home page URLs (confirmed - these are the same links
# listed in the original project proposal). NOT per-order deep links -
# see module docstring.
_PANEL_URLS = {
    "tapsishop": "https://vendor.tapsi.shop/dashboard",
    "digikala": "https://seller.digikala.com/pwa",
    # Second Digikala store (src/marketplaces/digikala2.py) - same
    # seller panel URL as the first store (both stores log in via
    # seller.digikala.com with their own separate credentials).
    "digikala2": "https://seller.digikala.com/pwa",
    "basalam": "https://vendor.basalam.com",
    "snappshop": "https://seller.snappshop.ir/dashboard",
}


def _order_link(order: NormalizedOrder) -> str:
    if order.source == "farazhonar":
        # Confirmed real WooCommerce admin URL pattern - opens this
        # exact order directly.
        return (
            f"{settings.farazhonar.base_url}/wp-admin/post.php"
            f"?post={order.source_order_id}&action=edit"
        )
    return _PANEL_URLS.get(order.source, "")


def _order_reference(order: NormalizedOrder) -> str:
    """
    Stable, globally-unique string identifying one order, independent of
    order_number (which is only "human-readable", per NormalizedOrder's
    docstring, and isn't guaranteed unique on its own the way
    source+source_order_id is). This exact string is what
    find_existing_deal_id() searches for and matches against - see that
    method's docstring for why an exact anchor is needed instead of a
    raw keyword hit.
    """
    return f"{order.source}:{order.source_order_id}"


def _build_description(order: NormalizedOrder) -> str:
    source_label = _SOURCE_DISPLAY_NAMES.get(order.source, order.source)
    link = _order_link(order)
    lines = [
        f"فروشگاه: {source_label}",
        f"شماره سفارش: {order.order_number}",
        f"شناسه یکتای هماهنگ‌سازی: {_order_reference(order)}",
    ]
    if link:
        lines.append(f"مشاهده سفارش: {link}")
    return "\n".join(lines)


def _iso(dt: datetime) -> str:
    """UTC timestamp in the exact format Didar's own docs use for
    SearchFromTime/SearchToTime (e.g. "2026-09-03T08:00:00.000Z").
    Matches src/didar/deal_poller.py's own `_iso()` - kept as a separate
    copy rather than a cross-module import, same tradeoff as this
    file's own `_format_rial()` below."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _parse_didar_datetime(value) -> datetime | None:
    """Parses Didar's "...T...Z" timestamps (RegisterTime etc.). Never
    raises - a bad/missing timestamp is handled by the caller. Matches
    src/didar/deal_poller.py's own `_parse_didar_datetime()` - kept as a
    separate copy, same tradeoff as this file's own `_iso()` above."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class DidarDealClient:
    def __init__(
        self,
        config: DidarConfig | None = None,
        product_client: DidarProductClient | None = None,
    ) -> None:
        self._config = config or settings.didar
        self._products = product_client or DidarProductClient(config=self._config)
        self._client = httpx.Client(base_url=self._config.base_url, timeout=30.0)
        self._label_id_by_title: dict[str, str] | None = None  # populated lazily

    @default_retry()
    def _post(self, path: str, json: dict) -> dict:
        resp = self._client.post(path, params={"apikey": self._config.api_key}, json=json)
        raise_for_status_with_body(resp)
        return resp.json()

    @default_retry()
    def _get(self, path: str) -> dict:
        resp = self._client.get(path, params={"apikey": self._config.api_key})
        raise_for_status_with_body(resp)
        return resp.json()

    def _label_id_by_title_map(self) -> dict[str, str]:
        """Title -> Id map for every Deal Label, built once per client
        lifetime from the documented GET /Label/GetDealLabels (confirmed
        2026-09 from Didar's own support agent - see config.py's
        deal_label_title_by_source docstring for the full history of why
        this replaced an earlier, wrong Tag-based implementation).

        Only entries with Type == "Deal" are kept - Didar's Label list
        can include other label Types too (e.g. for Contacts), and a
        same-named non-Deal label must never be matched here.

        Fetched lazily on first use and cached for this client's
        lifetime (one instance per process - see DidarSyncService),
        same tradeoff as product_client.py's category cache. NOT cached
        on failure (unlike product_client's catalog load) - a transient
        network error here is worth retrying on the next order, since
        the cost of one extra GET is negligible next to silently
        disabling labels for the rest of the process's life.
        """
        if self._label_id_by_title is None:
            payload = self._get(self._config.get_deal_labels_path)
            self._label_id_by_title = {
                _normalize_fa(str(item.get("Title", ""))): str(item["Id"])
                for item in payload.get("Response", [])
                if isinstance(item, dict)
                and item.get("Id")
                and item.get("Title")
                and item.get("Type") == "Deal"
            }
        return self._label_id_by_title

    def _label_id_for_source(self, source: str) -> str | None:
        """Resolves one marketplace source to its Deal Label Id, via the
        Title configured in DidarConfig.deal_label_title_by_source (see
        that property's docstring). Returns None - never raises - on
        any failure (no Title configured for this source, the API call
        itself failing, or no live Deal Label matching that Title): a
        missing/wrong label must never be the reason an order's whole
        sync fails, same fire-and-forget philosophy as the post-sale
        checklist.
        """
        title = self._config.deal_label_title_by_source.get(source)
        if not title:
            return None

        try:
            label_id = self._label_id_by_title_map().get(_normalize_fa(title))
        except Exception:
            log.exception(
                "didar: failed to fetch Deal Labels via %s - creating "
                "deal for %s order without a label",
                self._config.get_deal_labels_path, source,
            )
            return None

        if not label_id:
            log.warning(
                "didar: no Deal Label titled %r found via %s for source "
                "%r - creating this deal without a label. Verify the "
                "Title in DidarConfig.deal_label_title_by_source matches "
                "exactly what Didar returns for this account.",
                title, self._config.get_deal_labels_path, source,
            )
        return label_id

    # ------------------------------------------------------------------
    # Live aggregate stats for src/telegram.py's custom-range /report
    # picker (client request, 2026-09: "هر تاریخی رو زدم بره از crm
    # بگیره برام بیاره" - the custom-range report should reflect Didar
    # itself, the account's actual source of truth, rather than only
    # this program's own local sync cache).
    #
    # DELIBERATELY NO PRODUCTS/SHIPPING SPLIT: Didar's Deal.Price is the
    # deal's total amount (see module docstring - "Amount ->
    # DealItems[].UnitPrice"), which already matches this project's own
    # "products total" concept (shipping is never added into Price -
    # see _build_item_description() below, it's text-only). But that
    # shipping text lives on DealItems[].Description, and
    # POST /deal/getdealdetail - the only documented way to read a Deal
    # back - does NOT return DealItems at all (confirmed against
    # Didar's own API reference: DealItems only appears in the
    # save_v2/update_v2 request-body docs, never in any response body).
    # So a shipping figure, once saved, is NOT retrievable from Didar by
    # any client, this one included - there is no endpoint this method
    # could call to get it back. Per client decision (2026-09), the
    # live /report therefore reports count + total sale amount only, no
    # shipping line at all, rather than showing a fabricated/guessed
    # number. (Full products+shipping breakdown is still available from
    # Repository.get_amount_stats_since() for the daily/weekly/monthly/
    # yearly reports, which read this project's OWN sync-time record of
    # both figures - see src/telegram.py - that data was never lost,
    # it's just never round-tripped through Didar itself.)
    def get_won_stats(
        self, source: str, since: datetime, until: datetime
    ) -> tuple[int, Decimal]:
        """Count and total Price of Won deals for one marketplace
        `source` in [since, until), read directly from Didar via
        POST /deal/search_v2 - isolated to that source's own Deal Label
        (see _label_id_for_source()) so a manually-entered deal, or one
        from a different source, is never counted here. Filtering
        Criteria.Status="Won" server-side means the response's own
        TotalCount/TotalPrice (computed by Didar across the FULL
        matched result set, not just the one page requested - hence
        Limit=1: the List rows themselves are never used) already ARE
        the Won-only count/sum for this source and window - no local
        pagination or client-side summing needed.

        Returns (0, Decimal("0")) - never raises - if this source has
        no resolvable Deal Label, or on any request/parsing failure:
        matches _label_id_for_source()'s own fire-and-forget philosophy,
        since a single bad platform must never blank out (or crash) the
        whole /report response for every other platform."""
        label_id = self._label_id_for_source(source)
        if not label_id:
            log.warning(
                "didar: get_won_stats(%r) has no resolvable Deal Label - "
                "reporting 0 for this source rather than counting every "
                "Won deal account-wide (which would silently mix in "
                "other sources/manual deals)",
                source,
            )
            return 0, Decimal("0")

        try:
            payload = self._post(
                "/deal/search_v2",
                json={
                    "Criteria": {
                        "SearchFromTime": _iso(since),
                        "SearchToTime": _iso(until),
                        "Status": "Won",
                        "LabelIds": [label_id],
                        # 2026-09 bugfix: same PipelineId leak as
                        # _status_count_and_total() above - this call shape
                        # is otherwise identical, so it needs the same fix.
                        "PipelineId": self._config.pipeline_id,
                    },
                    "From": 0,
                    "Limit": 1,
                },
            )
        except Exception:
            log.exception(
                "didar: get_won_stats(%r) search_v2 request failed - "
                "reporting 0 for this source",
                source,
            )
            return 0, Decimal("0")

        response = payload.get("Response") if isinstance(payload, dict) else None
        if not isinstance(response, dict):
            log.warning(
                "didar: get_won_stats(%r) - no Response in search_v2 "
                "payload: %r", source, payload,
            )
            return 0, Decimal("0")

        count_raw = response.get("TotalCount", 0)
        try:
            count = int(round(float(count_raw)))
        except (TypeError, ValueError):
            count = 0

        price_raw = response.get("TotalPrice", 0)
        try:
            total = Decimal(str(price_raw))
        except (InvalidOperation, TypeError, ValueError):
            total = Decimal("0")

        return count, total

    # ------------------------------------------------------------------
    # Full status breakdown (all/pending/won/lost counts+sums) per Deal
    # Label, for the /report custom-range picker (client request,
    # 2026-09 follow-up; report display itself later changed, 2026-09
    # follow-up 2, to show only the "all" figures broken down per
    # LABEL instead of per STATUS - see
    # src/telegram.py's _format_live_range_report_message() and
    # list_deal_labels()/get_status_breakdown_for_label() below - but
    # the underlying per-status numbers are still fetched here in one
    # shot in case a future report wants them again).
    def get_status_breakdown(
        self, source: str, since: datetime, until: datetime
    ) -> "DealStatusBreakdown":
        """All/Pending/Won/Lost counts+totals for one marketplace
        `source` in [since, until), isolated to that source's own Deal
        Label (see _label_id_for_source()) same as get_won_stats().
        Returns an all-zero DealStatusBreakdown - never raises - on any
        failure, same fire-and-forget philosophy as get_won_stats()."""
        label_id = self._label_id_for_source(source)
        if not label_id:
            log.warning(
                "didar: get_status_breakdown(%r) has no resolvable Deal "
                "Label - reporting all-zero for this source rather than "
                "counting every deal account-wide",
                source,
            )
            return DealStatusBreakdown()
        return self._status_breakdown_for_label(label_id, since, until)

    def list_deal_labels(self) -> list[tuple[str, str]]:
        """Every Deal-type Label configured in this Didar account, as
        (Title, Id) pairs, in whatever order GET /Label/GetDealLabels
        itself returns them.

        Deliberately NOT filtered down to only the marketplaces this
        local deployment has an adapter/credentials for (client
        request, 2026-09 follow-up: "کل لیبل هارو از گزارش خود دیدار
        بگیره" - the custom-range /report picker should show every
        label Didar itself knows about, not just the sources listed in
        this project's own config/.env). This matters concretely for a
        label like اسنپ (SnappShop): SNAPPSHOP_ENABLED can be false
        locally (no credentials yet - see main.py) while the Label
        still exists in the Didar account (e.g. from earlier manual
        use), and it should still show up in the report - Didar's own
        label list is the source of truth here, not this project's
        enabled-adapter list. See get_status_breakdown_for_label() for
        querying stats per label returned here.

        Returns [] - never raises - on any failure, same fire-and-forget
        philosophy as _label_id_for_source()."""
        try:
            payload = self._get(self._config.get_deal_labels_path)
        except Exception:
            log.exception(
                "didar: failed to fetch Deal Labels via %s for /report "
                "label listing - reporting no labels",
                self._config.get_deal_labels_path,
            )
            return []
        return [
            (str(item["Title"]), str(item["Id"]))
            for item in payload.get("Response", [])
            if isinstance(item, dict)
            and item.get("Id")
            and item.get("Title")
            and item.get("Type") == "Deal"
        ]

    def get_status_breakdown_for_label(
        self, label_id: str, since: datetime, until: datetime
    ) -> "DealStatusBreakdown":
        """Same query as get_status_breakdown() but keyed directly by a
        Didar Label Id rather than one of this project's configured
        marketplace sources - for callers driven by list_deal_labels()
        (the /report custom-range picker) that want every label Didar
        has, independent of this project's own source config."""
        return self._status_breakdown_for_label(label_id, since, until)

    def _status_breakdown_for_label(
        self, label_id: str, since: datetime, until: datetime
    ) -> "DealStatusBreakdown":
        """One already-resolved Label Id's All/Pending/Won/Lost
        counts+sums in [since, until). Used by both
        get_status_breakdown() (source -> label lookup first) and
        get_status_breakdown_for_label() (label id already known, e.g.
        from list_deal_labels()).

        Issues THREE separate POST /deal/search_v2 calls - one per
        Criteria.Status ("Pending"/"Won"/"Lost") - and sums their own
        TotalCount/TotalPrice, rather than one Status-unset call read
        from the response's AllDealsCount/AllDealsTotalPrice fields.

        This was originally a single unset-Status call (per Didar's own
        documented search_v2 response example, which shows
        AllDealsCount/PendingDealsCount/WonDealsCount/LostDealsCount
        all returned together for the full matched set in one shot).
        Confirmed broken in production (2026-09 follow-up 4, live vs.
        the client's own Didar export for one day): for some labels
        (اسنپ, تپسی) the unset-Status AllDealsCount correctly matched
        SearchFromTime/SearchToTime, but for others (فرازهنر,
        دیجی‌کالا, تلفنی) it silently included older deals from outside
        the requested window - Didar's date filter isn't reliably
        applied to that aggregate when Status is left unset. The
        explicit-Status shape used here is exactly what
        get_won_stats() above already does (Status="Won" only) and
        that one has never shown this leak, so per-status queries are
        the only call shape confirmed to respect the date range for
        every label - three calls instead of one, but correct instead
        of occasionally over-counting.

        Returns an all-zero DealStatusBreakdown - never raises - on any
        failure (a failed status call contributes zero for that status
        rather than aborting the other two)."""
        totals: dict[str, tuple[int, Decimal]] = {}
        for status in ("Pending", "Won", "Lost"):
            totals[status] = self._status_count_and_total(label_id, status, since, until)

        pending_count, pending_total = totals["Pending"]
        won_count, won_total = totals["Won"]
        lost_count, lost_total = totals["Lost"]
        return DealStatusBreakdown(
            all_count=pending_count + won_count + lost_count,
            all_total=pending_total + won_total + lost_total,
            pending_count=pending_count,
            pending_total=pending_total,
            won_count=won_count,
            won_total=won_total,
            lost_count=lost_count,
            lost_total=lost_total,
        )

    def _status_count_and_total(
        self, label_id: str, status: str, since: datetime, until: datetime
    ) -> tuple[int, Decimal]:
        """One Status-filtered POST /deal/search_v2 call for a single
        already-resolved Label Id, reading TotalCount/TotalPrice - the
        same field pair and explicit-Status call shape as
        get_won_stats() above (the one call shape confirmed to respect
        SearchFromTime/SearchToTime for every label - see
        _status_breakdown_for_label()'s docstring). Returns
        (0, Decimal("0")) - never raises - on any request/parsing
        failure, so one bad status call can't blank out the other two
        statuses for this label."""
        zero = (0, Decimal("0"))
        try:
            payload = self._post(
                "/deal/search_v2",
                json={
                    "Criteria": {
                        "SearchFromTime": _iso(since),
                        "SearchToTime": _iso(until),
                        "Status": status,
                        "LabelIds": [label_id],
                        # 2026-09 bugfix: without this, a Label reused across
                        # more than one Pipeline pulls in deals from every
                        # pipeline it's attached to, not just this project's
                        # own pipeline (DIDAR_PIPELINE_ID) - see the same
                        # field already used when creating a Deal, below.
                        "PipelineId": self._config.pipeline_id,
                    },
                    "From": 0,
                    "Limit": 1,
                },
            )
        except Exception:
            log.exception(
                "didar: status breakdown search_v2 request failed for "
                "label_id=%r status=%r - reporting 0 for this status",
                label_id, status,
            )
            return zero

        response = payload.get("Response") if isinstance(payload, dict) else None
        if not isinstance(response, dict):
            log.warning(
                "didar: status breakdown - no Response in search_v2 "
                "payload for label_id=%r status=%r: %r",
                label_id, status, payload,
            )
            return zero

        try:
            count = int(round(float(response.get("TotalCount", 0))))
        except (TypeError, ValueError):
            count = 0

        try:
            total = Decimal(str(response.get("TotalPrice", 0)))
        except (InvalidOperation, TypeError, ValueError):
            total = Decimal("0")

        return count, total

    # ------------------------------------------------------------------
    # Creation-date-only breakdown per Deal Label (client request,
    # 2026-09 follow-up 5: "بازه‌ای که میگیرم بر اساس تاریخ ایجاد
    # سفارشات باشه، کاری ندارم وضعیتش چیه" - the custom-range /report
    # picker must count every deal CREATED in [since, until), full
    # stop, regardless of its current Status).
    #
    # WHY get_status_breakdown_for_label()/_status_breakdown_for_label()
    # ABOVE CANNOT BE REUSED FOR THIS: those methods trust Didar's own
    # server-computed TotalCount/TotalPrice for a Status-filtered
    # SearchFromTime/SearchToTime query. That was already known to be
    # unreliable for the Status-unset ("all") case (see
    # _status_breakdown_for_label()'s own docstring - some labels
    # silently leaked in deals from outside the window), which is why
    # that method moved to three separate per-Status calls. But
    # per-Status doesn't fix the real problem, it just hides it
    # differently: for "Won"/"Lost", Didar's own SearchFromTime/
    # SearchToTime window for a deal search is NOT reliably keyed to
    # RegisterTime (creation time) the way it is for a fresh "Pending"
    # deal - a deal created weeks earlier that simply changed status
    # (or stage) during [since, until) can still be matched. That is
    # exactly the symptom reported against a live Didar export filtered
    # by "تاریخ ایجاد معامله" (creation date): the bot's count is a
    # multiple of the real created-in-range count, because it is
    # quietly counting "touched in this window", not "created in this
    # window".
    #
    # THE FIX: never trust the server-side TotalCount/TotalPrice
    # aggregate for this. Instead, paginate through the ACTUAL List
    # rows (same shape/pagination as DidarDealPoller.search_deals() in
    # deal_poller.py, which already solves this exact problem for
    # detecting newly-registered deals) and independently verify each
    # row's own RegisterTime falls inside [since, until) before
    # counting it - Didar's SearchFromTime/SearchToTime is used only as
    # a best-effort narrowing hint, never as the source of truth. No
    # Status is sent in Criteria at all, so a single pass sees every
    # deal (Pending/Won/Lost) for this label in one pagination run
    # instead of three separate calls.
    #
    # Deliberately returns a DealStatusBreakdown with only all_count/
    # all_total populated (pending/won/lost left at zero) - collapsing
    # status is the entire point here, not an oversight. If a future
    # report ever needs a real per-status split that is ALSO correct by
    # creation date, classify each verified row by its own "Status"
    # field (not currently read here) instead of adding back
    # server-side per-Status queries.
    def get_created_date_stats_for_label(
        self, label_id: str, since: datetime, until: datetime
    ) -> "DealStatusBreakdown":
        """Every deal in this Deal Label, in this project's own
        pipeline (Criteria.PipelineId), whose own RegisterTime falls in
        [since, until) - regardless of Status. See the block comment
        above for why this exists instead of
        get_status_breakdown_for_label().

        Returns an all-zero DealStatusBreakdown - never raises - on any
        request failure or if PipelineId is not configured, same
        fire-and-forget philosophy as the rest of this class."""
        if not self._config.pipeline_id:
            log.warning(
                "didar: get_created_date_stats_for_label(%r) has no "
                "DIDAR_PIPELINE_ID configured - reporting 0 rather than "
                "counting every deal account-wide across every pipeline",
                label_id,
            )
            return DealStatusBreakdown()

        criteria = {
            "SearchFromTime": _iso(since),
            "SearchToTime": _iso(until),
            "PipelineId": self._config.pipeline_id,
            "LabelIds": [label_id],
            "Sort": 0,  # 0 = تاریخ ثبت (register time) - same as deal_poller.py
        }

        rows: list[dict] = []
        offset = 0
        for _page in range(_CREATED_RANGE_MAX_PAGES):
            try:
                payload = self._post(
                    "/deal/search_v2",
                    json={
                        "Criteria": criteria,
                        "From": offset,
                        "Limit": _CREATED_RANGE_PAGE_SIZE,
                    },
                )
            except Exception:
                log.exception(
                    "didar: get_created_date_stats_for_label(%r) search_v2 "
                    "request failed at offset=%d - reporting whatever was "
                    "already accumulated rather than aborting entirely",
                    label_id, offset,
                )
                break

            response = payload.get("Response") if isinstance(payload, dict) else None
            page = response.get("List", []) if isinstance(response, dict) else []
            page = [item for item in (page or []) if isinstance(item, dict) and item.get("Id")]
            rows.extend(page)
            if len(page) < _CREATED_RANGE_PAGE_SIZE:
                break
            offset += _CREATED_RANGE_PAGE_SIZE
        else:
            log.warning(
                "didar: get_created_date_stats_for_label(%r) hit the %d-page "
                "pagination cap for window %s..%s - some deals in this "
                "window may be missing from the count",
                label_id, _CREATED_RANGE_MAX_PAGES, since, until,
            )

        count = 0
        total = Decimal("0")
        for row in rows:
            register_time = _parse_didar_datetime(row.get("RegisterTime"))
            if register_time is None:
                log.warning(
                    "didar: get_created_date_stats_for_label(%r) - dropping "
                    "row Id=%s: missing/unparseable RegisterTime %r, cannot "
                    "verify it belongs in window %s..%s",
                    label_id, row.get("Id"), row.get("RegisterTime"), since, until,
                )
                continue
            if not (since <= register_time < until):
                # Didar returned this row anyway - exactly the leak this
                # method exists to guard against. Drop it silently (not a
                # warning): with SearchFromTime/SearchToTime removed as a
                # trustworthy filter, seeing rows outside the window is
                # the expected common case, not a rare anomaly worth
                # logging every time (see deal_poller.search_deals(),
                # which DOES warn - that call site only ever expects
                # brand-new deals, so a stray far-outside-window row
                # there really is anomalous).
                continue
            count += 1
            price_raw = row.get("Price")
            try:
                total += Decimal(str(price_raw))
            except (InvalidOperation, TypeError, ValueError):
                pass

        return DealStatusBreakdown(all_count=count, all_total=total)

    # ------------------------------------------------------------------
    # Live snapshot of the "new customer" ("مشتری جدید") pipeline stage
    # (client request, 2026-09 - see the "تفکیک سفارش‌های مرحله «مشتری
    # جدید»" prompt / src/db/repository.py's new_customer_stage_deals
    # docstring). Didar's API only ever exposes a Deal's CURRENT
    # PipelineStageId, never when it entered that stage - there is no
    # history endpoint. So the only way to eventually answer "how many
    # deals entered this stage today/this week/..." is to take a full,
    # unfiltered snapshot of who is sitting in the stage RIGHT NOW on
    # every poll cycle, and let the caller diff that against what is
    # already recorded locally (see Repository.record_new_stage_deal()
    # - idempotent on deal_id, so re-seeing the same Deal on the next
    # snapshot is a harmless no-op). This method is only the snapshot
    # half: it does no local bookkeeping and does not resolve
    # LabelIds to a label title - that wiring is a separate piece.
    def list_current_deals_in_stage(self, limit: int = _STAGE_SNAPSHOT_PAGE_SIZE) -> list[dict]:
        """Every Deal CURRENTLY sitting in this project's configured
        "new customer" pipeline stage (Criteria.PipelineId =
        DIDAR_PIPELINE_ID AND Criteria.PipelineStageId =
        DIDAR_PIPELINE_STAGE_ID), across as many pages as needed -
        same From/Limit pagination shape as
        DidarDealPoller.search_deals().

        Deliberately sends NO SearchFromTime/SearchToTime: unlike
        search_deals() (which windows by RegisterTime to find newly
        REGISTERED deals), this must return every Deal in the stage
        regardless of when it was registered, so it catches a Deal
        that entered the stage long ago and simply hasn't left it
        yet, every single time it's called - not just once. This also
        means it catches a Deal no matter how it got into the stage -
        created by this program's own SyncEngine.create_deal(), or
        created/moved there by hand in Didar's UI - both look
        identical to this query.

        Returns the raw List rows from /deal/search_v2 (Id/Price/
        LabelIds/Title/... per the documented response shape) as-is -
        unfiltered, unresolved. This method does not touch the
        Repository and does not resolve a row's LabelIds to a label
        title; turning a snapshot page into permanent
        new_customer_stage_deals history is the caller's job (see
        main.py's poll cycle wiring).

        Returns [] - without making any request - if either
        DIDAR_PIPELINE_ID or DIDAR_PIPELINE_STAGE_ID is not
        configured. Unlike search_deals()'s "search every pipeline"
        degraded fallback, there is no sane degraded behaviour for a
        stage-less query here: it would mean treating every Deal in
        every pipeline as a "new customer" entry, which is worse than
        reporting nothing.
        """
        if not self._config.pipeline_id or not self._config.pipeline_stage_id:
            log.warning(
                "didar: list_current_deals_in_stage() skipped - both "
                "DIDAR_PIPELINE_ID and DIDAR_PIPELINE_STAGE_ID must be "
                "configured (pipeline_id=%r, pipeline_stage_id=%r)",
                self._config.pipeline_id, self._config.pipeline_stage_id,
            )
            return []

        criteria = {
            "PipelineId": self._config.pipeline_id,
            "PipelineStageId": self._config.pipeline_stage_id,
        }

        results: list[dict] = []
        offset = 0
        for _page in range(_STAGE_SNAPSHOT_MAX_PAGES):
            payload = self._post(
                "/deal/search_v2",
                json={"Criteria": criteria, "From": offset, "Limit": limit},
            )
            page = payload.get("Response", {}).get("List", []) or []
            results.extend(item for item in page if isinstance(item, dict) and item.get("Id"))
            if len(page) < limit:
                break
            offset += limit
        else:
            log.warning(
                "didar: list_current_deals_in_stage() hit the %d-page "
                "pagination cap - some deals currently in the stage may "
                "not have been fetched this cycle (will be picked up on "
                "a later cycle, since this is a live snapshot re-taken "
                "every poll)",
                _STAGE_SNAPSHOT_MAX_PAGES,
            )
        return results

    # ------------------------------------------------------------------
    # Part B of the "new customer" stage-history feature (see
    # list_current_deals_in_stage()'s docstring for Part A / the
    # overall reasoning). This is the piece main.py's poll cycle
    # actually calls: take one live snapshot, resolve each row's
    # LabelIds to a human label title, and persist every row into
    # Repository.new_customer_stage_deals - idempotently, so this is
    # safe to call every single poll cycle for as long as the process
    # runs.
    def record_current_stage_snapshot(self, repository: "Repository") -> int:
        """One polling step for the "مشتری جدید" stage-history feature.
        Calls list_current_deals_in_stage() for the live snapshot, then
        hands every row to Repository.record_new_stage_deal() with
        entered_at = now (UTC). Returns how many rows were handed to
        the Repository (NOT how many were actually new - see below -
        this is a coarse per-cycle count for logging only).

        LABEL RESOLUTION: deliberately via list_deal_labels() (every
        Deal-type Label this Didar account has), NOT
        _label_id_by_title_map()/_label_id_for_source() (which only
        know about the Titles configured in
        DidarConfig.deal_label_title_by_source for THIS project's own
        marketplace sources). A Deal sitting in this stage can carry
        ANY Deal Label in the account - including one for a source
        this local deployment has no adapter/credentials for yet (see
        list_deal_labels()'s own docstring for exactly this scenario
        with اسنپ/SnappShop), or none at all if it was entered by hand
        in Didar's UI. Restricting resolution to the configured
        sources would silently record label_id=label_title=None for
        every one of those deals instead of their real label.

        A row with more than one entry in LabelIds uses the FIRST one
        that resolves to a known Deal-type Label Title; a row with no
        resolvable Label (none at all, or a Label Id that
        list_deal_labels() doesn't recognise) is still recorded, with
        label_id=label_title=None - label is metadata for the report's
        per-label breakdown, not a filter on whether the deal itself
        counts as having entered the stage at all.

        IDEMPOTENCY: record_new_stage_deal() is INSERT OR IGNORE on
        deal_id (see repository.py) - entered_at is only ever set the
        first time a given deal_id is observed by ANY snapshot, never
        overwritten on a later one. This method therefore does NOT
        pre-check the Repository before calling it for a row: unlike
        deal_poller.py's is_deal_notified() gate (which exists purely
        to skip an expensive extra /deal/getdealdetail call per
        already-seen deal), there is no extra API call to save here -
        every row's data is already in hand from the one snapshot
        request, so re-submitting an already-recorded deal_id on the
        next cycle is a deliberately cheap, harmless no-op.

        Never raises: a failure recording one row is logged and
        skipped so it doesn't stop the rest of the snapshot from being
        recorded (same "one bad item must never blank out the rest"
        philosophy as _status_count_and_total() etc. above); a failure
        that isn't yet recorded today will simply be picked up again
        on a later cycle, since the deal - if still genuinely in the
        stage - will still be in tomorrow's snapshot too.
        """
        rows = self.list_current_deals_in_stage()
        if not rows:
            return 0

        label_title_by_id = {
            label_id: title for title, label_id in self.list_deal_labels()
        }

        now = datetime.now(timezone.utc)
        handled = 0
        for row in rows:
            deal_id = str(row.get("Id") or "")
            if not deal_id:
                continue

            label_id = None
            label_title = None
            for raw_label_id in row.get("LabelIds") or []:
                candidate = str(raw_label_id)
                if candidate in label_title_by_id:
                    label_id = candidate
                    label_title = label_title_by_id[candidate]
                    break

            try:
                repository.record_new_stage_deal(
                    deal_id=deal_id,
                    label_id=label_id,
                    label_title=label_title,
                    amount=row.get("Price"),
                    entered_at=now,
                )
            except Exception:
                log.exception(
                    "didar: record_current_stage_snapshot() failed to record "
                    "deal_id=%s locally - will be retried on a later cycle "
                    "as long as it stays in the stage",
                    deal_id,
                )
                continue
            handled += 1

        log.info(
            "didar: stage snapshot - %d deal(s) currently in \"مشتری جدید\", "
            "%d recorded/confirmed locally this cycle",
            len(rows), handled,
        )
        return handled

    def find_existing_deal_id(self, order: NormalizedOrder) -> str | None:
        """
        Duplicate-prevention safety net that checks Didar itself, not just
        our own local sqlite state.

        WHY THIS IS NEEDED ON TOP OF Repository.is_already_synced():
        that local check only catches a duplicate if mark_synced() ran
        for the earlier attempt. It doesn't run in at least two real
        scenarios:
          1. /deal/save_v2 succeeds on Didar's side, but the response never
             reaches us (timeout, connection drop) - http_utils'
             default_retry then automatically retries the SAME POST
             (TransportError is retryable), which creates a SECOND real
             Deal for the same order. sync_one_order() never reaches
             mark_synced() for the first attempt because it never saw a
             response, so nothing local ever recorded it.
          2. record_failure() is written after such a lost-response
             timeout; retry_pending_failures() later calls create_deal()
             again on a source_order_id that was, in fact, already
             synced.
        Both are exactly the "duplicate order already added to Didar"
        symptom - the local DB has no way to know, only Didar does.

        ENDPOINT: uses the documented global search endpoint
        POST /search/search (Keyword + Types) - there is no dedicated
        "/deal/search" in the official docs, only "/Case/search", and a
        "Case" is a distinct entity in Didar (a kanban card / activity,
        linked to a Deal via a DealId field - see the docs' "نمونه
        پارامترهای زمان ویرایش" example) - not the Deal itself, so it
        cannot be used for this.

        MATCHING: deliberately an exact-line check of the unique
        `_order_reference()` string against each result's Description -
        matching the exact line _build_description() writes it as, NOT
        a raw substring containment check. /search/search does fuzzy
        full-text matching (same as /product/search's Keywords), so a
        raw hit could easily be a different order that merely shares
        digits with this one (e.g. reference "tapsishop:999" is a plain
        substring of "tapsishop:9999" - a real different order - so
        `in` alone is NOT safe here). A false match means silently
        skipping an order that was never actually synced, which is
        worse than the duplicate this is meant to prevent, so "not
        found" is the safe default whenever we can't be sure.

        NOT YET CONFIRMED: the docs' one example of this endpoint shows
        only Keyword+Types in the request body, no From/Limit pagination
        params (unlike /product/search, which documents both) - so none
        are sent here. If Didar caps or paginates /search/search results
        under the hood, an existing deal could theoretically fall
        outside what's returned; revisit if that's ever confirmed live.
        """
        reference = _order_reference(order)
        reference_line = f"شناسه یکتای هماهنگ‌سازی: {reference}"
        payload = self._post("/search/search", json={"Keyword": reference, "Types": ["deal"]})
        results = payload.get("Response", {}).get("List", [])
        for item in results:
            if not isinstance(item, dict) or item.get("_tp") != "deal":
                continue
            description_lines = (item.get("Description") or "").splitlines()
            if reference_line in description_lines:
                deal_id = item.get("Id")
                if deal_id:
                    log.info(
                        "didar: found existing deal for %s order %s -> Id=%s "
                        "(skipping create - already in Didar)",
                        order.source, order.source_order_id, deal_id,
                    )
                    return str(deal_id)
        return None

    def create_deal(self, contact_id: str, display_name: str, order: NormalizedOrder) -> str:
        deal_body = {
            "Title": f"معامله {display_name}".strip(),
            "PersonId": contact_id,
            "PipelineId": self._config.pipeline_id,
            "PipelineStageId": self._config.pipeline_stage_id,
            "Description": _build_description(order),
            # TAX FIX (client feedback, 2026-09 - "عوارض و مالیات باید صفر
            # باشه، کد 10 درصد میذاره"): the old code only sent
            # "TaxPercent": "0" on each DealItem (see _build_deal_item
            # below). An independently-published Didar API client
            # library's own struct definitions show TaxPercent living on
            # the DEAL object, with DealItem exposing no such field at
            # all (only Description/Discount/ProductId/Quantity/
            # UnitPrice) - which would explain the symptom: Didar ignores
            # an unrecognized DealItem field and falls back to the
            # account's own default tax rate.
            #
            # NOT independently confirmed against Didar's own official
            # docs, though - so this is set at BOTH levels deliberately
            # (see "TaxPercent" on the DealItem below too): whichever one
            # Didar actually honors, tax comes out at 0 either way, and
            # sending the extra field at the other level is harmless (the
            # old code already sent it on DealItems with no error). After
            # deploying, open a freshly-created deal in Didar's UI and
            # confirm "عوارض و مالیات" reads 0% - that's the only way to
            # be fully certain which level Didar actually reads, and this
            # comment should be updated once that's confirmed live.
            "TaxPercent": "0",
        }
        label_id = self._label_id_for_source(order.source)
        if label_id:
            deal_body["LabelIds"] = [label_id]

        body = {
            "Deal": deal_body,
            "DealItems": [self._build_deal_item(item, order) for item in order.items],
        }
        payload = self._post("/deal/save_v2", json=body)
        deal_id = _extract_deal_id(payload)
        log.info(
            "didar: created deal for %s order %s -> Id=%s",
            order.source, order.source_order_id, deal_id,
        )
        # Debug-level dump of the raw response (client feedback, 2026-09,
        # re: the TaxPercent placement uncertainty above) - if Didar's
        # save_v2 response ever echoes back the saved Deal/DealItems
        # fields, this makes it visible in the logs without needing a
        # separate manual API call to confirm which TaxPercent Didar
        # actually applied. Debug (not info) since this is a diagnostic
        # aid, not something needed on every routine sync.
        log.debug("didar: deal/save_v2 raw response for Id=%s: %r", deal_id, payload)
        return deal_id

    def _build_deal_item(self, item, order: NormalizedOrder) -> dict:
        # Prefer the client's Excel catalog: most products already exist
        # in Didar under a short internal Code/title that has no
        # relationship to the marketplace's own SKU or title (see
        # product_catalog.py's module docstring). A confident match
        # there means we search/create using the REAL catalog Code and
        # title instead of the marketplace's.
        catalog_match = self._products.resolve_catalog_code(item.title)
        if catalog_match:
            code, title = catalog_match
        else:
            # SKU is the natural upsert key; falls back to the item
            # title for the (rare) case a source provides no SKU, so at
            # least same-titled items resolve to the same product
            # within a run.
            code, title = item.sku or item.title, item.title

        product_id = self._products.upsert_product(
            code=code,
            title=title,
            category=item.category,
            unit_price=item.unit_price,
            final_price=item.final_price,
        )
        # Per-unit discount, from the gap between the source's original
        # per-unit price and what the line actually settled for.
        #
        # WHY THIS MATTERS (found comparing an auto-created deal against
        # a manually-entered one, client feedback 2026-08): Discount was
        # previously hardcoded to 0 for every item, unconditionally -
        # losing real discount data whenever a source actually applies
        # one. unit_price/final_price genuinely diverge for Digikala
        # (unit_price vs total_price), Tapsi Shop (price vs finalPrice,
        # both confirmed distinct fields per the vendor's own API docs),
        # Faraz Honar/WooCommerce (price vs the line's "total", which WC
        # itself defines as post-discount, distinct from "subtotal") and
        # SnappShop (unit_price vs final_price). Only Basalam's
        # final_price is a pure unit_price*quantity restatement with no
        # separate discount concept in its API - this naturally computes
        # to a 0 discount there, same as before.
        #
        # Didar's DealItems Discount is a per-unit CURRENCY AMOUNT, not
        # a percentage - confirmed from the docs' own example
        # (UnitPrice=80000, Discount=4800).
        quantity = item.quantity or 1
        per_unit_discount = item.unit_price - (item.final_price / quantity)
        if per_unit_discount < 0:
            # Never negative - a negative gap means final_price is
            # HIGHER than unit_price*quantity (tax/fees added on top by
            # the source, not a discount), which Discount must not
            # represent.
            per_unit_discount = Decimal("0")

        return {
            "ProductId": product_id,
            "Quantity": item.quantity,
            "UnitPrice": int(item.unit_price),
            "Discount": int(per_unit_discount),
            # Sent here too, redundantly, alongside Deal.TaxPercent above -
            # see the long comment on create_deal() for why: it's not
            # fully confirmed which level Didar actually reads, and
            # sending "0" here is harmless either way (Didar already
            # accepted this field on DealItems with no error before).
            "TaxPercent": "0",
            #
            # Order-traceability text, matching the convention seen on
            # manually-entered deals (client feedback 2026-08) - those
            # have "شماره سفارش: X/شماره مرسوله: Y" typed into each
            # item's توضیحات. NormalizedOrder.shipment_id/shipping_cost
            # are only populated for the sources whose API actually
            # exposes them (Digikala, Basalam, Tapsi Shop - see each
            # adapter's comments; Faraz Honar/WooCommerce has neither),
            # so each line below is added only when that data exists,
            # rather than printing a literal "None" into the deal.
            # shipment_id/shipping_cost are order-level (not per line
            # item), so this text is identical across every DealItem on
            # the same order - matches the client's own request (client
            # feedback, 2026-09) for the case where a single shipment
            # covers the whole order.
            "Description": _build_item_description(order),
        }


def _build_item_description(order: NormalizedOrder) -> str:
    lines = [f"شماره سفارش: {order.order_number}"]
    # Prefer the customer/courier-facing tracking code when the adapter
    # provides one (currently only Digikala, via a separate SBS call -
    # see NormalizedOrder.shipment_tracking_code's docstring); otherwise
    # fall back to shipment_id, which for Basalam/Tapsi Shop already IS
    # that same customer-facing parcel number.
    tracking_number = order.shipment_tracking_code or order.shipment_id
    if tracking_number:
        lines.append(f"شماره مرسوله: {tracking_number}")
    # FIXED SHIPPING FEE (client request, 2026-09): Digikala and Faraz
    # Honar get a flat, client-specified Toman amount here (239 for
    # Digikala; 225/250 for Faraz Honar depending on courier - see
    # src/shipping_fees.py), regardless of whatever real shipping_cost
    # that source's own API reports for this order. Every other source
    # (and a Faraz Honar order shipped by neither Pishtaz nor Tipax)
    # falls back to the original behaviour: show the real
    # order.shipping_cost (Rial) when the adapter provided one, or no
    # shipping line at all otherwise.
    fixed_fee_toman = shipping_fee_toman(order)
    if fixed_fee_toman is not None:
        lines.append(f"هزینه ارسال: {format_toman(fixed_fee_toman)} تومان")
    elif order.shipping_cost is not None:
        lines.append(f"هزینه ارسال: {_format_rial(order.shipping_cost)} ریال")
    return "\n".join(lines)


def _format_rial(amount) -> str:
    """Format a Decimal/int/float Rial amount with thousands separators,
    e.g. 12500000 -> "12,500,000". Matches src/telegram.py's _format_rial
    - kept as a separate copy rather than a cross-module import, since
    this module has no other dependency on src/telegram.py."""
    if amount is None:
        amount = 0
    return f"{int(round(float(amount))):,}"


def _extract_deal_id(payload: dict) -> str:
    candidates = [
        lambda p: p.get("Response", {}).get("Deal", {}).get("Id"),
        lambda p: p.get("Response", {}).get("Id"),
        lambda p: p.get("Deal", {}).get("Id"),
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
        f"didar: could not find Deal Id in response - shape is unconfirmed, "
        f"update _extract_deal_id() once a real payload has been inspected. "
        f"Raw response: {payload!r}"
    )