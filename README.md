# Local Marketplace Order Synchronization Platform

An always-on, local Windows service that polls five order sources —
**Tapsi Shop**, **Digikala**, **Basalam**, **SnappShop**, and the
client's own **Faraz Honar** WooCommerce store — and automatically
creates matching Contact + Deal records in **Didar CRM**.

Runs entirely on the client's local server, no cloud dependency, and
uses official/documented APIs wherever they exist.

## Status

| Source | Adapter | Auth model | End-to-end verified against real API |
|---|---|---|---|
| Faraz Honar (WooCommerce) | `src/marketplaces/farazhonar.py` | Basic Auth (Consumer Key/Secret) | ✅ |
| Digikala | `src/marketplaces/digikala.py` | OAuth-style, auto-refreshing | ✅ |
| Tapsi Shop | `src/marketplaces/tapsishop.py` | Bearer token | ⏳ code complete, awaiting live token |
| Basalam | `src/marketplaces/basalam.py` | Bearer token (official Salam API) | ⏳ code complete, awaiting live token |
| SnappShop | `src/marketplaces/snappshop.py` | Bearer token + Agent-User header | ⏳ code complete, schema partially unconfirmed |
| Didar CRM | `src/didar/contact_client.py`, `src/didar/deal_client.py` | API key (query param) | ✅ |

33 automated tests passing (`pytest tests/`). See [`docs/architecture.md`](docs/architecture.md)
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
Sync Engine (dedupe, retry, per-source watermark)
        │
        ▼
Didar sync: Contact upsert (by CustomerCode) → Deal create (linked via PersonId)
        │
        ▼
Didar CRM
```

Every adapter implements the same small interface
(`src/marketplaces/base.py::MarketplaceAdapter`), so adding a sixth
source later means writing one new adapter class — nothing else in the
system changes.

## Project layout

```
src/
├── config.py            # all environment/config loading, one place
├── logger.py             # rotating file + console logging
├── http_utils.py         # shared retry policy used by every HTTP client
├── sync_engine.py        # orchestrates adapters + dedupe + Didar sync
├── main.py                # service entrypoint (APScheduler polling loop)
├── db/
│   └── repository.py      # SQLite: dedupe, retry tracking, sync watermark
├── marketplaces/
│   ├── base.py             # NormalizedOrder + MarketplaceAdapter interface
│   ├── tapsishop.py
│   ├── digikala.py
│   ├── basalam.py
│   ├── snappshop.py
│   └── farazhonar.py
└── didar/
    ├── contact_client.py    # upsert Contact by CustomerCode
    ├── deal_client.py        # create Deal linked to a Contact
    └── service.py             # combines the two per order

tests/                     # pytest + respx (HTTP mocking), one file per module
deploy/                    # NSSM Windows Service install/uninstall/restart scripts
docs/                      # installation guide, architecture notes
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
credential (developer panel, wp-admin, etc.).

## Known limitations

- **SnappShop**: order field names for the list/detail endpoints are
  not confirmed against a real populated response (only prose docs were
  available, no JSON example) — see the module docstring in
  `snappshop.py`. Auth and pagination mechanics *are* confirmed and
  tested.
- **Didar Deal line items**: `Deal.save` takes an `InvoiceId`, not an
  embedded item list. Until the Invoice/Product endpoints are confirmed,
  order line items are written as a readable summary into the Deal's
  `Description` field rather than structured data — see the
  `TODO(didar-invoice)` note in `deal_client.py`.
- **Basalam**: uses an official Personal Access Token, but no confirmed
  refresh-token endpoint for this project yet — a 401 requires manually
  issuing a fresh token from `developers.basalam.com/panel`.
- **Digikala**: `access_token` is short-lived (~24h) but refreshes
  automatically using `refresh_token` (~1 year validity). Roughly once a
  year, `refresh_token` itself needs manual renewal via a separate
  RSA-encrypted authorization flow — see the module docstring in
  `digikala.py` for the full explanation.

## Git workflow

Incremental commits, [Conventional Commits](https://www.conventionalcommits.org/)
style (`feat:`, `fix:`, `chore:`, `docs:`, `refactor:`), one logical
change per commit.
