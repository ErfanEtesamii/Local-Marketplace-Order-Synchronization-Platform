# Local Marketplace Order Synchronization Platform

An always-on, local Windows service that polls five order sources —
**Tapsi Shop**, **Digikala**, **Basalam**, **SnappShop**, and the
client's own **Faraz Honar** WooCommerce store — and automatically
creates matching Contact + Deal records (with structured line items,
a post-sale follow-up checklist, and CRM tags per source) in
**Didar CRM**.

Runs entirely on the client's local server, no cloud dependency, and
uses official/documented APIs wherever they exist.

## Status

| Source | Adapter | Auth model | End-to-end verified against real API |
|---|---|---|---|
| Faraz Honar (WooCommerce) | `src/marketplaces/farazhonar.py` | Basic Auth (Consumer Key/Secret) | ✅ live orders syncing |
| Digikala | `src/marketplaces/digikala.py` | OAuth-style, auto-refreshing | ✅ live orders syncing |
| Digikala (فروشگاه دوم / second store) | `src/marketplaces/digikala2.py` | OAuth-style, auto-refreshing (same model as the first Digikala store) | ⏳ code written and unit-tested against the documented Open API shape, pending a live sync against this store's own credentials before being treated as verified |
| Tapsi Shop | `src/marketplaces/tapsishop.py` | Bearer token | ✅ live orders syncing |
| Basalam | `src/marketplaces/basalam.py` | Bearer token (official Salam API) | ✅ live orders syncing |
| SnappShop | `src/marketplaces/snappshop.py` | Bearer token + Agent-User header | ⏸️ disabled by default — client hasn't been granted API access yet (`SNAPPSHOP_ENABLED=false`); schema confirmed against the official vendor API doc and a real order, code is written and tested, just waiting on credentials |
| SnappShop (فروشگاه دوم / second vendor account) | `src/marketplaces/snappshop2.py` | Bearer token + Agent-User header (same model as the first SnappShop account) | ⏸️ disabled by default (`SNAPPSHOP2_ENABLED=false`) — code written and unit-tested against the same confirmed schema as the first account, but this account's own `SNAPPSHOP2_VENDOR_ID` is still needed before it can run |
| Didar CRM | `src/didar/*.py` | API key (query param) | ✅ Contact, Product, Deal, and post-sale checklist Activity creation all confirmed live |

243 automated tests passing (`pytest tests/ -v`). See [`docs/architecture.md`](docs/architecture.md)
for design decisions and [`docs/installation.md`](docs/installation.md) for
Windows deployment.

## Architecture

```
Marketplace / WooCommerce orders
        │
        ▼
Per-source Adapter (auth + retrieval + normalization → NormalizedOrder)
        │
        ▼
Sync Engine (dedupe, retry, per-source watermark, drops anything
             older than the watermark even if a source's own date
             filter returns it anyway)
        │
        ▼
Didar sync, per order:
  1. Contact  - search first (by CustomerCode, then by MobilePhone as
                a fallback), reuse the existing Id if found, only
                create when genuinely new
  2. Product  - one per line item; search first (by Code), reuse if
                found, otherwise auto-create in a matching catalog
                category (exact marketplace category name, falling
                back to a keyword guess from the item title, falling
                back to one fixed default category)
  3. Deal     - search first (dedupe against Didar itself, not just
                the local DB), structured DealItems (real ProductId/
                Quantity/UnitPrice/Discount, not a text dump), tagged
                with a per-source Label
  4. Checklist- 6-item post-sale follow-up Activities auto-created on
                every new Deal (skipped, not partially created, if
                any of the 6 ActivityType ids aren't configured)
        │
        ▼
Didar CRM
```

Every adapter implements the same small interface
(`src/marketplaces/base.py::MarketplaceAdapter`), so adding a sixth
source later means writing one new adapter class — nothing else in the
system changes.

Every Didar write client (`contact_client.py`, `product_client.py`,
`deal_client.py`) follows the same **search-first** pattern: look the
record up in Didar before ever calling the corresponding `*/save`
endpoint, reuse its Id if found, and recover from a create/search race
(another process creating the same record in between) with one retry
via search rather than failing the whole order. This exists because
none of the three `*/save` endpoints reliably upsert on their own —
each one was confirmed live to reject a blind re-create as a duplicate
instead. See each client's module docstring for the specific
production incident that proved this.

## Project layout

```
src/
├── config.py                # all environment/config loading, one place
├── logger.py                # rotating file + console logging (Windows-safe rollover)
├── http_utils.py             # shared retry policy used by every HTTP client
├── currency.py               # Toman -> Rial conversion (per-source unit)
├── finglish.py               # Finglish (Latin-typed Persian names) -> Persian script
├── shipping_fees.py          # fixed client-specified shipping-fee display amounts
├── telegram.py               # per-order alerts + daily/weekly/monthly/yearly reports + /report custom-range picker
├── sync_engine.py            # orchestrates adapters + dedupe + watermark + Didar sync
├── reporting.py              # daily summary report + per-cycle health check
├── main.py                   # service entrypoint (APScheduler polling loop)
├── db/
│   └── repository.py         # SQLite: dedupe, retry tracking, sync watermark
├── memory/
│   └── didar-sbs-api-integration.md   # engineering notes on the Didar SBS integration
├── marketplaces/
│   ├── base.py                # NormalizedOrder + MarketplaceAdapter interface
│   ├── tapsishop.py
│   ├── digikala.py
│   ├── digikala2.py            # second Digikala store - independent copy, see docs/architecture.md
│   ├── basalam.py
│   ├── snappshop.py
│   ├── snappshop2.py           # second SnappShop vendor account - independent copy, see docs/architecture.md
│   └── farazhonar.py
└── didar/
    ├── contact_client.py       # upsert Contact - search-first by CustomerCode/MobilePhone
    ├── product_client.py       # upsert Product per line item - search-first by Code + category resolution
    ├── product_catalog.py      # marketplace title -> Didar catalog Code, from client's Excel export
    ├── category_mapping.py     # keyword→category-title guesses for items with no marketplace category
    ├── deal_client.py          # create Deal with structured DealItems - dedupe against Didar first
    ├── activity_client.py      # post-sale follow-up checklist Activities
    ├── scheduling.py           # checklist due-date rules (anchored to ship time / registration time)
    └── service.py               # combines Contact + Deal + checklist per order

tests/                      # pytest + respx (HTTP mocking), one file per module
deploy/                     # NSSM Windows Service install/uninstall/restart scripts
docs/                       # installation guide, architecture notes
scripts/                    # one-off ops helpers (e.g. list_activity_types.py)
memory/                     # project-level engineering notes (e.g. sliding-window algorithm)
data/                       # sync.db, digikala_tokens.json, digikala2_tokens.json, Didar product-catalog export (gitignored)
logs/                       # rotating order-sync.log + NSSM service-stdout/stderr logs (gitignored)
```

## Local development setup

```powershell
python -m venv venv
venv\Scripts\pip install -r requirements.txt
copy .env.example .env
# fill in .env with real credentials - see comments in .env.example
# for where each one comes from
```

Run the test suite:

```powershell
python -m pytest tests/ -v
```

Run the service directly (foreground, for local testing):

```powershell
python -m src.main
```

## Production deployment (Windows Service)

See [`docs/installation.md`](docs/installation.md) for the full guide.
Short version:

```powershell
cd deploy
install_service.bat
nssm.exe start OrderSyncPlatform
```

## Configuration

All configuration lives in `.env` (never committed — see `.gitignore`).
`.env.example` documents every variable, including where to obtain each
credential (developer panel, wp-admin, etc.) and every Didar-side Id
(pipeline/stage, product categories, per-source tags, checklist
ActivityTypes).

## Known limitations

- **SnappShop**: disabled by default (`SNAPPSHOP_ENABLED=false`) —
  purely a credentials gap, not a schema one: the client hasn't been
  granted API access yet. The adapter's order field names are
  confirmed from SnappShop's official vendor API doc (v2.1.2 PDF) and
  cross-checked against a real order in the vendor panel — see the
  module docstring in `snappshop.py`. Set `SNAPPSHOP_ENABLED=true` and
  fill in credentials once access exists; no further code changes
  should be needed, but do one real sync before trusting it
  unattended, same as any newly-enabled source.
- **Didar Product categories**: only Faraz Honar's orders carry a real
  marketplace category name. For the other four sources, the category
  is guessed from a keyword table (`src/didar/category_mapping.py`)
  matched against the item title — a first draft, not verified against
  the full real catalog. Check the sync logs periodically for "no
  keyword matched" entries and extend the keyword lists as needed.
- **Basalam**: uses an official Personal Access Token, but no confirmed
  refresh-token endpoint for this project yet — a 401 requires manually
  issuing a fresh token from `developers.basalam.com/panel`.
- **Digikala**: `access_token` is short-lived (~24h) but refreshes
  automatically using `refresh_token` (~1 year validity). Roughly once a
  year, `refresh_token` itself needs manual renewal via a separate
  RSA-encrypted authorization flow — see the module docstring in
  `digikala.py` for the full explanation.
- **Digikala (second store)**: gated behind `DIGIKALA2_ENABLED` the same
  way SnappShop is gated behind its own flag — off by default until this
  store's own credentials (`DIGIKALA2_CLIENT_CODE`/`_CLIENT_SECRET`/
  `_ACCESS_TOKEN`/`_REFRESH_TOKEN`) are filled into `.env`. The adapter
  (`src/marketplaces/digikala2.py`) is a deliberately independent copy of
  `digikala.py`, not a subclass — see
  [`docs/architecture.md`](docs/architecture.md) for why, and note that
  any future Digikala bugfix needs to be applied to both files by hand.
- **SnappShop (second vendor account)**: gated behind `SNAPPSHOP2_ENABLED`,
  same opt-in pattern as the sources above — off by default until this
  account's own `SNAPPSHOP2_VENDOR_ID` (plus `_AUTH_TOKEN`/`_AGENT_USER`)
  is filled into `.env`. The adapter (`src/marketplaces/snappshop2.py`)
  is a deliberately independent copy of `snappshop.py`, same tradeoff as
  the second Digikala store. Unlike the second Digikala store, it
  reuses the FIRST SnappShop account's own Didar Deal Label ("اسنپ")
  rather than getting a distinct one — a specific client instruction
  for this account.
- **Didar Contact MobilePhone matching**: the fallback search assumes
  Didar stores phone numbers in the same digit format marketplaces
  send (e.g. `0912...`). Not yet confirmed whether Didar normalizes
  differently (e.g. `+98` prefix) — if a "Duplicate contacts" error
  ever recurs after this fix, check the stored format in the Didar UI
  directly.

## Git workflow

Incremental commits, [Conventional Commits](https://www.conventionalcommits.org/)
style (`feat:`, `fix:`, `chore:`, `docs:`, `refactor:`), one logical
change per commit.