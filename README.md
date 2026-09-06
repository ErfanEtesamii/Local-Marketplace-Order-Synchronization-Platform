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
| Tapsi Shop | `src/marketplaces/tapsishop.py` | Bearer token | ✅ live orders syncing |
| Basalam | `src/marketplaces/basalam.py` | Bearer token (official Salam API) | ✅ live orders syncing |
| SnappShop | `src/marketplaces/snappshop.py` | Bearer token + Agent-User header | ⏸️ disabled by default — client hasn't been granted API access yet (`SNAPPSHOP_ENABLED=false`); code is written but unverified against real data |
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
│   ├── basalam.py
│   ├── snappshop.py
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
data/                       # sync.db, digikala_tokens.json, Didar product-catalog export (gitignored)
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

- **SnappShop**: disabled by default (`SNAPPSHOP_ENABLED=false`) — the
  client hasn't been granted API access yet. The adapter code is
  written and unit-tested, but its order field names aren't confirmed
  against a real populated response (only prose docs were available,
  no JSON example) — see the module docstring in `snappshop.py`. Set
  `SNAPPSHOP_ENABLED=true` and fill in credentials once access exists,
  then verify a real sync before trusting it unattended.
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