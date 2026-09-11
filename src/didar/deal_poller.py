"""
Didar CRM - order-pipeline deal poller.

CLIENT REQUIREMENT (2026-09, revised 2026-09): every Deal registered
in Didar's کاریز سفارشات (order pipeline, Criteria.PipelineId =
DIDAR_PIPELINE_ID) - typed in by hand in Didar's own UI, or created
automatically by this program from a marketplace order - must trigger
a Telegram notification. Deals in any OTHER pipeline must never reach
Telegram from this poller. This account has no Webhook support
(confirmed in the client conversation), so the only way to detect "a
Deal was registered" regardless of *how* is to poll Didar's own Search
Deal endpoint on an interval and diff against what's already been
notified.

This is deliberately a SEPARATE code path from the existing per-order
notification (TelegramNotifier.notify_new_order(), driven by
SyncEngine._sync_one_order()/retry_pending_failures()): that path only
ever fires for deals THIS program itself created from a marketplace
order, and always with the full order/money breakdown. A deal entered
by hand in Didar never goes through SyncEngine at all, so it could
never reach Telegram through that path no matter what. This poller is
the only thing that can see it.

FLOW (matches the client-approved design worked out over chat):

    every poll cycle (main.py's _poll_cycle, same interval as the
    marketplace polling - POLL_INTERVAL_SECONDS):
        POST /deal/search_v2 with Criteria.SearchFromTime/SearchToTime
            = [last watermark - _OVERLAP_SECONDS, now] AND
            Criteria.PipelineId = DIDAR_PIPELINE_ID (کاریز سفارشات only)
        drop any returned row whose own RegisterTime doesn't actually
            fall inside that window (see search_deals()) - a second,
            independent guard in case Didar's own SearchFromTime/
            SearchToTime filtering ever lets an older Deal through
        for every remaining Deal.Id not already in
        Repository.notified_deals:
            POST /deal/getdealdetail to get the full record
            -> build a NewDealInfo -> caller sends it to Telegram
            Repository.mark_deal_notified(deal_id)
        advance the watermark to `now`

WHY AN OVERLAP WINDOW, NOT JUST [last_watermark, now) EXACTLY:
there's no documented guarantee about clock skew between Didar's
servers and this one, or about exactly how a Deal saved right at a
poll boundary is timestamped. Re-scanning a small overlap
(_OVERLAP_SECONDS) on every poll means a boundary deal can never be
silently skipped. This is only safe because of the Id-based dedup
below - seeing the same Id again on the next poll is an expected,
harmless no-op, not a bug.

WHY EVERY ROW'S RegisterTime IS RE-CHECKED AFTER THE SEARCH RESPONSE
COMES BACK (see search_deals()): SearchFromTime/SearchToTime tell
Didar what window to search, but this poller does not treat that as
a guarantee - if the API ever returns a row whose own RegisterTime
falls outside the requested window, that row is dropped rather than
forwarded to Telegram. RegisterTime is the Deal's original creation
time (not last-edit time), so this is the same signal Sort=0 already
sorts by; it's just also verified per-row instead of only trusted at
the query level.

WHY DEDUP IS BY DEAL ID IN THE REPOSITORY, NOT BY TIME ALONE:
  1. The overlap window above deliberately re-fetches some Ids more
     than once.
  2. A deal created by THIS program via the normal order-sync path
     will *also* show up in this poller's search results a few
     seconds/minutes later. SyncEngine marks it notified up front (see
     Repository.mark_deal_notified() and sync_engine.py's
     _sync_one_order()/retry_pending_failures()) specifically so this
     poller doesn't send a second, less-detailed Telegram message for
     a deal that already got the rich per-order one.

WHY /deal/getdealdetail AT ALL, RATHER THAN JUST THE SEARCH ROW:
search_v2's own List rows already carry Title/Price/PersonId/OwnerId/
PipelineStageId (confirmed from the documented response example), but
only as raw Ids for the contact/owner - getdealdetail additionally
resolves those to Person.DisplayName / Owner.DisplayName, which is
what actually makes a readable Telegram message. One extra call per
NEW deal (never per poll) is a cheap price for that.

FIRST RUN: deliberately does NOT notify about the account's existing
deal history - see poll_new_deals()'s docstring. Only deals whose
RegisterTime falls after this program's very first poll cycle are
ever sent to Telegram.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

import httpx

from src.config import DidarConfig, settings
from src.didar.contact_client import DidarApiError
from src.http_utils import default_retry, raise_for_status_with_body
from src.logger import get_logger

if TYPE_CHECKING:
    from src.db.repository import Repository

log = get_logger(__name__)

# Re-scan this many seconds of the previous window on every poll, as a
# safety margin against clock skew / a Deal landing right on a poll
# boundary. Safe only because of the Id-based dedup - see module
# docstring.
_OVERLAP_SECONDS = 90

# Defensive cap on pagination within a single poll (at 50/page this is
# 5,000 deals in one window - far more than any real poll interval
# could ever produce). Stops a pathological response, or a bug, from
# looping forever.
_MAX_PAGES = 100

# Deals per page for /deal/search_v2 - the docs' own example uses 5;
# 50 keeps this to one page for any normal poll interval while still
# being a small, cheap request.
_PAGE_SIZE = 50

_ZERO_GUID = "00000000-0000-0000-0000-000000000000"


@dataclass(frozen=True)
class NewDealInfo:
    """Everything TelegramNotifier.notify_new_deal() needs, already
    resolved to human-readable strings - see
    DidarDealPoller._deal_info_from_detail()."""

    deal_id: str
    code: int | None
    title: str
    customer_name: str | None
    price: Decimal
    owner_name: str | None
    stage_name: str | None
    register_time: datetime | None


def _iso(dt: datetime) -> str:
    """UTC timestamp in the exact format Didar's own docs use for
    SearchFromTime/SearchToTime (e.g. "2026-09-03T08:00:00.000Z")."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _parse_didar_datetime(value) -> datetime | None:
    """Parses Didar's "...T...Z" timestamps (RegisterTime etc.). Never
    raises - a bad/missing timestamp just means the notification falls
    back to "now" (see TelegramNotifier._format_jalali_datetime)."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class DidarDealPoller:
    """Polls POST /deal/search_v2 for every Deal registered in Didar in
    a given time window - manual entries and this program's own
    marketplace-driven creates alike - and fetches full detail (POST
    /deal/getdealdetail) for each one not yet seen. Pure Didar-API +
    dedup/watermark concern: it doesn't know about Telegram at all -
    see main.py for how the two are wired together.
    """

    def __init__(self, config: DidarConfig | None = None) -> None:
        self._config = config or settings.didar
        self._client = httpx.Client(base_url=self._config.base_url, timeout=30.0)
        self._pipeline_stage_titles: dict[str, str] | None = None  # populated lazily

    @default_retry()
    def _post(self, path: str, json: dict) -> dict:
        resp = self._client.post(path, params={"apikey": self._config.api_key}, json=json)
        raise_for_status_with_body(resp)
        return resp.json()

    @default_retry()
    def _post_no_body(self, path: str) -> dict:
        """/pipeline/list/0 is documented with no request body at all -
        POST with only the apikey query param, same shape exception as
        DidarConfig.get_locations_path's GET-with-no-apikey case."""
        resp = self._client.post(path, params={"apikey": self._config.api_key})
        raise_for_status_with_body(resp)
        return resp.json()

    # ------------------------------------------------------------------
    # Raw Didar API calls
    # ------------------------------------------------------------------
    def search_deals(self, since: datetime, until: datetime, limit: int = _PAGE_SIZE) -> list[dict]:
        """Deals in the کاریز سفارشات pipeline (Criteria.PipelineId =
        DIDAR_PIPELINE_ID) registered in [since, until), across as many
        pages as needed (From/Limit pagination - see the docs' Search
        Deal request body). Returns the raw List rows (Id/Title/
        RegisterTime/Price/PersonId/OwnerId/PipelineStageId/... per the
        documented response shape) - NOT full detail; see
        get_deal_detail().

        Rows are additionally filtered here by their own RegisterTime
        against [since, until] before being returned. This is a second,
        independent guard on top of the SearchFromTime/SearchToTime
        query params: those params tell Didar what to search for, but
        this poller must never trust them as the ONLY thing standing
        between an old Deal and Telegram - see poll_new_deals() and the
        module docstring for why. A row missing/with an unparseable
        RegisterTime is dropped rather than risk letting an
        unverifiable old Deal through.
        """
        if not self._config.pipeline_id:
            log.warning(
                "didar: DIDAR_PIPELINE_ID is not configured - deal poller will "
                "search ALL pipelines, not just کاریز سفارشات"
            )
        criteria = {
            "SearchFromTime": _iso(since),
            "SearchToTime": _iso(until),
            "Sort": 0,  # 0 = تاریخ ثبت (register time)
        }
        if self._config.pipeline_id:
            criteria["PipelineId"] = self._config.pipeline_id

        results: list[dict] = []
        offset = 0
        for _page in range(_MAX_PAGES):
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
                "didar: deal search hit the %d-page pagination cap for window "
                "%s..%s - some deals in this window may not have been fetched",
                _MAX_PAGES, since, until,
            )

        filtered: list[dict] = []
        for row in results:
            register_time = _parse_didar_datetime(row.get("RegisterTime"))
            if register_time is None:
                log.warning(
                    "didar: deal search - dropping row Id=%s: missing/unparseable "
                    "RegisterTime %r, cannot verify it belongs in window %s..%s",
                    row.get("Id"), row.get("RegisterTime"), since, until,
                )
                continue
            if not (since <= register_time <= until):
                log.warning(
                    "didar: deal search - dropping row Id=%s: RegisterTime %s is "
                    "outside the queried window %s..%s (Didar returned it anyway)",
                    row.get("Id"), register_time, since, until,
                )
                continue
            filtered.append(row)
        return filtered

    def get_deal_detail(self, deal_id: str) -> dict:
        """Full record for one Deal (POST /deal/getdealdetail) - see
        module docstring for why this is fetched per new deal instead
        of relying on the search row alone."""
        payload = self._post("/deal/getdealdetail", json={"Id": deal_id})
        detail = payload.get("Response")
        if not isinstance(detail, dict):
            raise DidarApiError(
                f"didar: getdealdetail returned no Response for Id={deal_id!r}: {payload!r}"
            )
        return detail

    def pipeline_stage_title(self, stage_id: str | None) -> str | None:
        """Resolves a PipelineStageId to its human-readable Title via
        POST /pipeline/list/0 (confirmed endpoint - see the docs' List
        Deal Pipelines). Cached for this poller's lifetime, same
        tradeoff as DidarDealClient's Deal Label cache. Returns None -
        never raises - on any failure or unrecognized Id: a missing
        stage name must never break a notification."""
        if not stage_id or stage_id == _ZERO_GUID:
            return None
        try:
            titles = self._pipeline_stage_title_map()
        except Exception:
            log.exception("didar: failed to fetch pipelines for stage-title lookup")
            return None
        return titles.get(stage_id)

    def _pipeline_stage_title_map(self) -> dict[str, str]:
        if self._pipeline_stage_titles is None:
            payload = self._post_no_body("/pipeline/list/0")
            titles: dict[str, str] = {}
            for pipeline in payload.get("Response", []) or []:
                if not isinstance(pipeline, dict):
                    continue
                for stage in pipeline.get("Stages", []) or []:
                    if isinstance(stage, dict) and stage.get("Id") and stage.get("Title"):
                        titles[str(stage["Id"])] = str(stage["Title"])
            self._pipeline_stage_titles = titles
        return self._pipeline_stage_titles

    # ------------------------------------------------------------------
    # Detail -> NewDealInfo
    # ------------------------------------------------------------------
    def _deal_info_from_detail(self, detail: dict) -> NewDealInfo:
        deal_id = str(detail.get("Id") or "")
        title = str(detail.get("Title") or "").strip() or "بدون عنوان"

        person = detail.get("Person") if isinstance(detail.get("Person"), dict) else None
        company = detail.get("Company") if isinstance(detail.get("Company"), dict) else None
        customer_name = None
        if person and person.get("DisplayName"):
            customer_name = str(person["DisplayName"])
        elif company and company.get("DisplayName"):
            customer_name = str(company["DisplayName"])

        owner = detail.get("Owner") if isinstance(detail.get("Owner"), dict) else None
        owner_name = str(owner["DisplayName"]) if owner and owner.get("DisplayName") else None

        price_raw = detail.get("Price")
        try:
            price = Decimal(str(price_raw)) if price_raw is not None else Decimal("0")
        except (InvalidOperation, ValueError):
            price = Decimal("0")

        code = detail.get("Code")
        code = code if isinstance(code, int) else None

        return NewDealInfo(
            deal_id=deal_id,
            code=code,
            title=title,
            customer_name=customer_name,
            price=price,
            owner_name=owner_name,
            stage_name=self.pipeline_stage_title(detail.get("PipelineStageId")),
            register_time=_parse_didar_datetime(detail.get("RegisterTime")),
        )

    # ------------------------------------------------------------------
    # Orchestration - one polling step
    # ------------------------------------------------------------------
    def poll_new_deals(self, repository: "Repository") -> list[NewDealInfo]:
        """One polling step: figure out the window since the last
        watermark, search it, fetch full detail for every not-yet-
        notified Id (marking each notified as soon as its detail fetch
        succeeds - fire-and-forget from here on, same convention as
        SyncEngine.mark_synced() before notify_new_order()), and
        advance the watermark. Returns the NewDealInfo list the caller
        should hand to TelegramNotifier.notify_new_deal() - the
        Repository bookkeeping is already done by the time this
        returns, so a later Telegram failure can't cause a deal to be
        re-detected and retried forever.

        FIRST RUN: if there's no persisted watermark yet, this seeds it
        to `now` and returns an empty list rather than scanning (in
        practice) the account's entire deal history - matches
        TelegramNotifier's own report-rollover convention (see
        _check_daily_rollover et al. in src/telegram.py) of never
        retroactively firing off of a bootstrap run.

        A failed search leaves the watermark untouched so the same
        window is retried next cycle rather than silently skipped.
        """
        now = datetime.now(timezone.utc)
        watermark = repository.get_deal_poll_watermark()
        if watermark is None:
            repository.set_deal_poll_watermark(now)
            log.info(
                "didar: deal poller - first run, watermark seeded to %s (no backfill)", now
            )
            return []

        since = watermark - timedelta(seconds=_OVERLAP_SECONDS)
        try:
            rows = self.search_deals(since, now)
        except Exception:
            log.exception(
                "didar: deal search failed for window %s..%s - will retry next "
                "cycle (watermark NOT advanced)", since, now,
            )
            return []

        new_deals: list[NewDealInfo] = []
        for row in rows:
            deal_id = str(row.get("Id") or "")
            if not deal_id or repository.is_deal_notified(deal_id):
                continue
            try:
                detail = self.get_deal_detail(deal_id)
            except Exception:
                log.exception(
                    "didar: failed to fetch detail for new deal %s - will retry "
                    "next cycle (not marked notified)", deal_id,
                )
                continue

            # Recorded BEFORE the caller sends it to Telegram - a
            # Telegram outage/failure must never cause the same deal
            # to be re-detected forever (see TelegramNotifier.
            # notify_new_deal(), which already swallows its own
            # errors same as notify_new_order()).
            repository.mark_deal_notified(deal_id)
            new_deals.append(self._deal_info_from_detail(detail))

        repository.set_deal_poll_watermark(now)
        log.info(
            "didar: deal poller - window %s..%s: %d deal(s) seen, %d new",
            since, now, len(rows), len(new_deals),
        )
        return new_deals
