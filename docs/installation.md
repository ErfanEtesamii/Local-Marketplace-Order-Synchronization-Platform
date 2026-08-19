# Installation Guide (Windows)

This guide sets up the Order Sync Platform as an always-on Windows
Service on the client's local server, per the project proposal
(local-only deployment, no cloud dependency, survives reboots).

## Prerequisites

- Windows 10/11 or Windows Server, configured to stay powered on
- [Python 3.11+](https://python.org/downloads) installed, with **"Add
  python.exe to PATH"** checked during install
- Administrator access to the machine (required for service install)

## 1. Get the code onto the server

Copy the project folder to the server, e.g. `C:\OrderSyncPlatform`, or
clone it with Git if the server has Git installed:

```powershell
git clone <repository-url> C:\OrderSyncPlatform
```

## 2. Set up the virtual environment

From the project root (`C:\OrderSyncPlatform`):

```powershell
python -m venv venv
venv\Scripts\pip install -r requirements.txt
```

## 3. Configure credentials

```powershell
copy .env.example .env
notepad .env
```

Fill in every token/key. See `.env.example`'s comments for where each
one comes from (developer panel, wp-admin, etc.) - the same values used
during development testing. **Never commit `.env`** - it's already in
`.gitignore`.

## 4. Download NSSM

NSSM (Non-Sucking Service Manager) is what turns the always-running
Python process into a proper Windows Service.

1. Download from <https://nssm.cc/download> (get the 64-bit build)
2. Extract `nssm.exe` (from the `win64` folder) into `deploy\nssm.exe`
   in the project - i.e. it should sit right next to
   `install_service.bat`

## 5. Install the service

Open **Command Prompt or PowerShell as Administrator** (right-click →
"Run as administrator"), then:

```powershell
cd C:\OrderSyncPlatform\deploy
install_service.bat
```

This registers the service (auto-start, restart-on-crash, rotating logs)
but does not start it yet.

## 6. Start it

```powershell
nssm.exe start OrderSyncPlatform
```

Or open `services.msc`, find **"Local Marketplace Order Sync"**, and
click Start.

## 7. Verify it's working

- `logs\order-sync.log` - the application's own structured log (from
  `src/logger.py`); should show each adapter being polled
- `logs\service-stdout.log` / `service-stderr.log` - raw process
  output, useful if something crashes before logging is set up
- `services.msc` should show status **Running** and startup type
  **Automatic**

## 8. Confirm it survives a reboot

Don't just trust the "Automatic" startup setting - actually reboot the
server once during setup and confirm the service comes back up and
resumes polling on its own. This is one of the project's acceptance
criteria.

## Updating the service later

After pulling new code or editing `.env`:

```powershell
deploy\restart_service.bat
```

## Uninstalling

```powershell
cd C:\OrderSyncPlatform\deploy
uninstall_service.bat
```

This only removes the service registration - `.env`, logs, and the
local database (`data\sync.db`) are left untouched.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Service won't start | Check `service-stderr.log` first - usually a missing `.env` value or a typo'd path |
| Service starts then immediately stops | An unhandled exception in `src/main.py` on startup - check `order-sync.log` |
| One marketplace never syncs, others work fine | That source's token likely expired - see the relevant adapter's module docstring for its renewal procedure (Basalam and Digikala both need periodic token refresh; see also `docs/architecture.md`) |
| Basalam specifically stops working | Its access token needs manual renewal - see `src/marketplaces/basalam.py` module docstring |
| Digikala specifically stops working after ~1 year | `refresh_token` itself has expired (unlike the daily `access_token` refresh, which is automatic) - a manual re-authorization is needed: get a fresh `authorization_code` from the seller panel, decrypt it with the private key (kept off the server - see `src/marketplaces/digikala.py` module docstring), exchange it for new tokens, and update `data/digikala_tokens.json` (or `.env` as the seed) |