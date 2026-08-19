# Architecture

Technical reference for the Local Marketplace Order Synchronization
Platform - what each piece does, why it's built the way it is, and
every undocumented API quirk discovered while building it. Written for
whoever maintains this next (including a future version of whoever
wrote it).

## 1. Overview

A single Windows background service polls five order sources every
`POLL_INTERVAL_SECONDS` (default 120s) and creates a matching
Contact + Deal in Didar CRM for every new order, exactly once each.

```
Tapsi Shop, Digikala, Basalam,        Didar CRM
SnappShop, Faraz Honar (WooCommerce)
        │                                  ▲
        ▼                                  │
┌───────────────────┐   NormalizedOrder   ┌┴──────────────────┐
│  Adapter (per      │────────────────────▶│   Sync Engine     │
│  source)            │                     │  - dedupe          │
│  auth + fetch +     │                     │  - retry            │
│  normalize          │                     │  - per-source       │
└───────────────────┘                     │    watermark        │
                                            └──────┬─────────────┘
                                                    │
                                            ┌───────▼───────────┐
                                            │  DidarSyncService  │
                                            │  Contact upsert →  │
                                            │  Deal create        │
                                            └────────────────────┘
```

Every adapter implements one interface
(`src/marketplaces/base.py::MarketplaceAdapter`, two methods:
`fetch_new_orders(since)`, `fetch_order_detail(id)`), and produces the
same `NormalizedOrder` shape regardless of source. The Sync Engine and
Didar module never know which marketplace an order came from - adding
a sixth source later is one new adapter class, nothing else changes.

## 2. Why polling, not webhooks

Tapsi Shop, Digikala, and SnappShop all offer webhooks. This project
deliberately uses polling instead, because the original proposal's
core requirement was **local-only deployment with no cloud
dependency** - the client's server sits behind a router with no public
IP or port forwarding. A webhook requires the reverse: the marketplace
needs to reach *us*, which means exposing the server to the internet
(port forwarding, dynamic DNS, a TLS certificate to maintain). Polling
keeps the server as a pure outbound client, matching the proposal's
security posture.

This is a real trade-off, not a free win: polling means up to
`POLL_INTERVAL_SECONDS` of latency, and (as documented per-source
below) some data that's only available in webhook payloads - notably
customer name/mobile for Tapsi Shop - simply isn't available via
polling. If low-latency sync or fuller customer data ever becomes a
priority, webhooks are worth revisiting, but that's a deliberate
architecture change, not a bug.

## 3. Per-source integration notes

Everything below was confirmed either from official documentation or,
more often, from live testing against the real API - several
constraints were **not** in any vendor documentation and were only
discovered by hitting a real 400/401/429 and reading the response body
(which is why `raise_for_status_with_body()` exists in
`http_utils.py` - the default `httpx.raise_for_status()` throws away
the response body, which hid the actual reason for every failure until
that was fixed).

### Tapsi Shop (`marketplaces/tapsishop.py`)

- Auth: `TapsiShop.Hub.Authorization` header, vendor-panel token.
- `POST /v1/orders` (list) + `GET /v1/orders/{id}` (detail, has line
  items - list doesn't).
- **Not in the docs, confirmed live:**
  - `dateFilterTypeCode` is required whenever `fromDate`/`toDate` are
    sent. The PDF's example value of `0` is a placeholder, not valid -
    `1` is confirmed to work.
  - The `[fromDate, toDate]` window is capped at **7 days** - a longer
    span is rejected with a 400. `fetch_new_orders` chunks any longer
    requested range into ≤7-day windows automatically.
  - Rate limit: **1 request per 5 seconds**, confirmed via a live 429.
    `_throttle()` enforces this proactively before every request.
- Customer name/mobile are **not** present in either REST response -
  only in the webhook payload (see Section 2). Orders from this source
  therefore use a synthetic Didar `CustomerCode`
  (`tapsishop-{order_id}`), not the customer's phone number.

### Digikala (`marketplaces/digikala.py`)

- Auth: OAuth-style. `GET /open-api/v1/orders/history` for the list
  (item-level rows - a 3-item order produces 3 rows sharing the same
  `order_id`; the adapter groups them).
- **Token lifecycle** (confirmed via a real token exchange):
  - `access_token`: ~24 hours.
  - `refresh_token`: ~1 year (matches the ~360-day figure shown in the
    seller panel's own token screen - that panel figure is the
    refresh-token/client grant, not the short-lived access token).
  - The adapter refreshes `access_token` automatically on a 401 via
    `POST /open-api/v1/auth/refresh-token`, and persists the refreshed
    pair to `data/digikala_tokens.json` so a service restart doesn't
    need a fresh manual authorization.
  - Roughly once a year, `refresh_token` itself expires and a **manual**
    step is needed: the seller panel issues an RSA-encrypted
    `authorization_code`, decrypted locally with a private key
    (`openssl pkeyutl`), then exchanged via `POST /auth/token`. The
    private key should never be on the server - it's only needed for
    this yearly bootstrap, done from a laptop, with the resulting
    tokens copied into `.env` / the token cache file.
- Customer contact details are intentionally not requested for this
  source (client decision - not a technical limitation) - orders use a
  synthetic `CustomerCode` (`digikala-{order_id}`).

### Basalam (`marketplaces/basalam.py`)

- Official developer platform: **Salam API** (`developers.basalam.com`),
  discovered mid-project after an earlier draft of this adapter was
  built against an *unofficial* internal endpoint
  (`services.basalam.com/...`) found via browser network inspection.
  That draft was fully replaced once the official API was found -
  worth knowing if `git log` looks like the integration was rebuilt,
  because it was.
- `GET /v3/vendor-parcels` (list, cursor pagination) +
  `GET /v3/vendor-parcels/{id}` (detail).
- Basalam's unit is the **parcel**, not the order directly - a
  platform order can span multiple vendors' parcels; since this API is
  vendor-scoped, each parcel already represents exactly this vendor's
  share, which is the natural sync unit.
- Auth: Bearer Personal Access Token (scope `vendor.parcel.read`) from
  `developers.basalam.com/panel`. No confirmed refresh-token endpoint
  for this project yet - a 401 requires manually issuing a fresh PAT.
- `CustomerResponse`'s exact field names weren't in the schema excerpt
  captured during development - `_normalize_detail()` uses defensive
  `.get()` fallbacks for customer name/mobile; verify against a real
  populated response.

### SnappShop (`marketplaces/snappshop.py`)

- Source: two vendor-onboarding blog posts (prose documentation, not a
  Swagger/Postman spec) - so auth and pagination mechanics are
  confirmed and tested, but individual order field names are not.
- Auth: `Authorization: Bearer` + `Agent-User` header (vendor
  identifier).
- `GET /vendors/{vendor_id}/orders` (history, cursor pagination via
  `meta.pagination.{has_more,next_cursor}`) +
  `GET /vendors/{vendor_id}/orders/{order_number}` (detail).
- `_SCHEMA_CONFIRMED = False` in the module - `_normalize_*()` uses
  defensive `.get()`-with-fallbacks, same pattern as Basalam's earlier
  draft, and logs a warning on every call until this is verified
  against a real populated response.
- Base URL (`apix.snappshop.ir`) is **inferred** from Snapp's public
  bug-bounty domain scope, not stated in the docs available - verify
  against the seller panel's own "تنظیمات فروشگاه » مشاهده و ثبت API"
  page before relying on it.

### Faraz Honar (`marketplaces/farazhonar.py`)

- The one source that's the client's own WordPress/WooCommerce site
  (admin access), not a third-party marketplace - uses WooCommerce's
  public, stable, officially documented REST API v3
  (`/wp-json/wc/v3/`). Confidence here is high; this is the only
  adapter *not* built from partial docs or discovery.
- Auth: HTTP Basic Auth via Consumer Key/Secret
  (WooCommerce → Settings → Advanced → REST API, Read permission).
- `GET /wp-json/wc/v3/orders` already returns full `line_items[]` per
  order - unlike every other adapter, no separate detail call is
  needed to sync an order end to end.
- Uses `date_created_gmt` (UTC) throughout, not the site-local
  `date_created`, to avoid timezone bugs.

## 4. Didar CRM integration

Two-step process per order (`src/didar/service.py`):

1. **Upsert Contact** (`src/didar/contact_client.py`) - keyed on
   `CustomerCode`. Didar finds-and-updates if a match exists,
   otherwise creates. `CustomerCode` is the customer's mobile number
   when the source provides one (Basalam; not Tapsi Shop or Digikala -
   see Section 3), otherwise a synthetic `{source}-{order_id}`.
2. **Create Deal** (`src/didar/deal_client.py`) - linked to the
   Contact, placed in the confirmed `PipelineStageId` (currently:
   pipeline "سفارشات", stage "مشتری جدید").

Both use `POST .../contact/save` / `POST .../deal/save` with the API
key as a **query parameter** (`?apikey=...`), confirmed from
`didar.me/api-help` - not an `Authorization` header.

**Confirmed the hard way, via live 400s:**

- `Deal.save` expects **`PersonId`**, not `ContactId` - sending
  `ContactId` fails with `"person and company both are empty"`.
- `Contact.save` rejects an **empty `LastName`** - any source without
  a customer name (Tapsi Shop, Digikala) needs a non-empty fallback;
  `contact_client.py` falls back to `customer_code` itself.
- Didar rejects a duplicate `MobilePhone` across different
  `CustomerCode`s (`"Duplicate contacts is not allowed"`) - encountered
  when testing against a manually-entered admin contact that already
  existed under a different code. Not currently handled automatically;
  such orders land in `sync_failures` for manual review rather than
  silently succeeding or crashing the whole cycle.

**Known gap - line items:** `Deal` takes an `InvoiceId`, not an
embedded item list, as the original proposal assumed. Didar appears to
model priced line items through a separate `Product` entity, but the
exact `Invoice`-creation endpoint and how it links `Product`s to a
`Deal` were never confirmed (would need a live token + reading the
Postman docs' Invoice/Product sections). Until then,
`deal_client.py::_build_description()` writes an itemized, human-
readable summary into the Deal's `Description` field - no financial
detail is lost, it just isn't structured data on the Didar side yet.
Tracked as `TODO(didar-invoice)` in the code.

**Response envelope:** Contact.save's shape
(`{"Response": {"Contact": {"Id": ...}}}`) is confirmed correct via
live testing. `_extract_contact_id`/`_extract_deal_id` still try
several shapes defensively as a safety net, but the primary shape is
no longer a guess.

## 5. Sync Engine (`src/sync_engine.py`)

One `run_once()` call = one full poll cycle:

```
for each adapter:
    since = repository.get_last_sync_time(source) or (now - 1 day)
    try: orders = adapter.fetch_new_orders(since)
    except: log + skip this source this cycle, watermark NOT advanced
    for each order:
        if already synced (repository): skip
        if order has no line items: adapter.fetch_order_detail(id)
        try: didar_service.sync_order(order); repository.mark_synced(...)
        except: repository.record_failure(...)
    repository.set_last_sync_time(source, cycle_start - 10min overlap)
retry_pending_failures()  # runs every cycle too - see below
```

Key properties, each backed by a dedicated test in
`tests/test_sync_engine.py`:

- **Isolation**: one source failing to fetch does not block the
  others, and does *not* advance that source's watermark - the full
  window is retried next cycle rather than silently skipped.
- **Watermark overlap**: advances by `now - 10 minutes` rather than
  exactly `now`, as a safety margin against clock skew / API latency
  right at the boundary. Any resulting duplicate fetch is absorbed for
  free by `Repository.is_already_synced()` - the overlap costs nothing
  but a few redundant API calls.
- **Self-healing within a cycle**: `run_once()` ends with a retry pass
  over previously-failed orders, so a transient Didar failure (e.g. a
  momentary 503) can resolve itself before the cycle even finishes,
  without waiting for the next poll.

## 6. Persistence (`src/db/repository.py`)

SQLite, three tables:

- `synced_orders (source, source_order_id) PRIMARY KEY` - dedupe. This
  is the source of truth for "has this order already been synced" -
  nothing else should be trusted for that check.
- `sync_failures (source, source_order_id) PRIMARY KEY` - retry
  tracking, with an `attempt_count` cap (default 5) so a permanently
  broken order doesn't retry forever.
- `sync_state (source) PRIMARY KEY` - the per-source watermark
  described above.

Deliberately no ORM, one file - matches the scale of a single-server
background service. `data/sync.db` (path from `DB_PATH` in `.env`) and
`data/digikala_tokens.json` are the only files that need backing up to
preserve sync history across a server rebuild; everything else is
either in `.env` or reconstructible from source.

## 7. Reporting and health checks (`src/reporting.py`)

No HTTP endpoint, by design - the whole point of this deployment is
staying off the network (Section 2). Instead:

- `check_health()` runs after **every** poll cycle (cheap - a handful
  of indexed SQLite lookups) and logs a `WARNING` for any source that
  hasn't completed a successful sync within `STALE_AFTER` (2 hours) or
  has orders stuck in retry. Shows up in the normal log file - no
  separate monitoring surface to maintain.
- `generate_daily_report()` runs once a day (00:05 UTC) and writes a
  plain-text summary to `reports/YYYY-MM-DD.txt`: per-source order
  counts for the last 24h, last successful poll time, pending failure
  counts.

## 8. Testing strategy

`pytest` + `respx` (HTTP mocking) throughout - no test hits a real
network endpoint. 49 tests at time of writing. A few patterns worth
knowing:

- Every adapter has at least one test locking in a *previously real*
  bug (e.g. `test_pagination_continues_even_when_total_pages_is_wrong`,
  `test_client_errors_are_not_retried`) - these exist because the bug
  actually happened once, not speculatively.
- `tests/test_sync_engine.py` and `tests/test_repository.py` use
  pytest's `tmp_path` fixture for a real (file-backed) SQLite database
  per test, not `tempfile.NamedTemporaryFile` - the latter holds its
  own file handle open, which blocks `sqlite3.connect()` on Windows
  (worked fine on Linux, failed on a real Windows test run - see git
  history for `fix: use pytest tmp_path...`).
- `tests/test_tapsishop.py` mocks `time.sleep` (autouse fixture) so
  the confirmed 5-second rate limit doesn't make the suite itself slow
  - the throttle logic is verified separately with mocked timing.

## 9. Deployment

See [`installation.md`](installation.md) for the full Windows setup
guide. Summary: NSSM-managed Windows Service (`deploy/*.bat`),
auto-start, restart-on-crash, `src/main.py` as the entrypoint
(APScheduler-driven polling loop).

## 10. Known limitations (consolidated)

Also listed in `README.md`; repeated here with more context:

| Limitation | Where | Impact |
|---|---|---|
| SnappShop order field names unconfirmed | `snappshop.py` | Sync may silently produce incomplete `NormalizedOrder`s until verified against real data |
| Didar Deal has no structured line items | `deal_client.py` | Order items land in `Description` text, not queryable/reportable fields in Didar |
| Basalam has no confirmed token-refresh endpoint | `basalam.py` | A 401 needs a manual PAT renewal from the developer panel |
| Digikala `refresh_token` needs yearly manual renewal | `digikala.py` | Requires the private-key bootstrap process, off-server |
| Duplicate-mobile-number Contacts aren't auto-resolved | `contact_client.py` / Didar | Such orders land in `sync_failures` for manual handling |
| No webhook support | adapters generally | Sync latency bounded by `POLL_INTERVAL_SECONDS`, not instant |
