@echo off
setlocal

rem ============================================================
rem  Installs the standalone Telegram/proxy health monitor
rem  (scripts/health_monitor.py) as its OWN separate Windows
rem  Service using NSSM - kept completely independent from the
rem  OrderSyncPlatform service.
rem
rem  Why a second service instead of start.bat launching it as a
rem  plain minimized process: a plain process only comes back when
rem  someone logs in and double-clicks start.bat. A real service
rem  auto-starts on boot (SERVICE_AUTO_START below) even with
rem  nobody logged in, and NSSM restarts it if it ever crashes -
rem  matching the whole point of this monitor (section 7.4 of the
rem  report: it must keep working, and keep showing status, even
rem  if the main service or the proxy itself is down).
rem
rem  Must be run from an elevated (Administrator) command prompt.
rem  Expects the same venv and .env as the main service - see
rem  install_service.bat. Run that one first if you haven't.
rem ============================================================

set SERVICE_NAME=OrderSyncHealthMonitor
set SCRIPT_DIR=%~dp0
set PROJECT_DIR=%SCRIPT_DIR%..
set PYTHON_EXE=%PROJECT_DIR%\venv\Scripts\python.exe
set NSSM_EXE=%SCRIPT_DIR%nssm.exe
set LOG_DIR=%PROJECT_DIR%\logs

if not exist "%NSSM_EXE%" (
    echo ERROR: nssm.exe not found at %NSSM_EXE%
    echo Download it from https://nssm.cc/download and place nssm.exe in this folder.
    exit /b 1
)

if not exist "%PYTHON_EXE%" (
    echo ERROR: virtual environment not found at %PYTHON_EXE%
    echo Run this first, from the project root:
    echo   python -m venv venv
    echo   venv\Scripts\pip install -r requirements.txt
    exit /b 1
)

if not exist "%PROJECT_DIR%\.env" (
    echo WARNING: no .env file found at %PROJECT_DIR%\.env
    echo The health monitor reads TELEGRAM_BOT_TOKEN from .env to call getMe -
    echo it will still install and run, but every check will fail until .env exists.
)

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

echo Installing service "%SERVICE_NAME%"...
"%NSSM_EXE%" install %SERVICE_NAME% "%PYTHON_EXE%" "scripts\health_monitor.py"
"%NSSM_EXE%" set %SERVICE_NAME% AppDirectory "%PROJECT_DIR%"
"%NSSM_EXE%" set %SERVICE_NAME% DisplayName "Order Sync - Telegram Health Monitor"
"%NSSM_EXE%" set %SERVICE_NAME% Description "Independently checks Telegram/proxy connectivity every couple of minutes and writes status\status.html for office staff. Runs as its own service, separate from OrderSyncPlatform, so it keeps working (and keeps showing status) even if that service or the proxy itself is down."
"%NSSM_EXE%" set %SERVICE_NAME% Start SERVICE_AUTO_START

rem Route stdout/stderr into their own log files - separate from both
rem logs\order-sync.log and health_monitor.py's own
rem logs\health_monitor.log. Catches anything that happens before its
rem own logging is set up, or an uncaught exception that kills the
rem process outright (same reasoning as install_service.bat).
"%NSSM_EXE%" set %SERVICE_NAME% AppStdout "%LOG_DIR%\health-monitor-service-stdout.log"
"%NSSM_EXE%" set %SERVICE_NAME% AppStderr "%LOG_DIR%\health-monitor-service-stderr.log"
"%NSSM_EXE%" set %SERVICE_NAME% AppRotateFiles 1
"%NSSM_EXE%" set %SERVICE_NAME% AppRotateOnline 1
"%NSSM_EXE%" set %SERVICE_NAME% AppRotateBytes 10485760

rem If it ever exits or crashes, restart it automatically after a
rem short delay - this service is the only thing showing staff a live
rem connectivity status, so it should never stay down silently.
"%NSSM_EXE%" set %SERVICE_NAME% AppExit Default Restart
"%NSSM_EXE%" set %SERVICE_NAME% AppRestartDelay 5000

echo.
echo Done. The service is installed but not yet started.
echo.
echo Start it now with:
echo   "%NSSM_EXE%" start %SERVICE_NAME%
echo.
echo Or just run start.bat from the project root, which starts both services.

endlocal
