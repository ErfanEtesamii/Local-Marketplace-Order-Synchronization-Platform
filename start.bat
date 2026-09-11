@echo off
setlocal

rem ============================================================
rem  start.bat - one-click start for non-technical office staff.
rem  Starts both Windows services: the order-sync platform and
rem  the Telegram/proxy health monitor.
rem
rem  Both are registered with SERVICE_AUTO_START, so they normally
rem  come back on their own after a reboot with nobody needing to
rem  touch this file. It's here for the times one/both need a
rem  manual nudge (first-time setup, or after running stop.bat).
rem ============================================================

set SERVICE_NAME=OrderSyncPlatform
set HEALTH_SERVICE_NAME=OrderSyncHealthMonitor
set SCRIPT_DIR=%~dp0
set PROJECT_DIR=%SCRIPT_DIR%
set NSSM_EXE=%PROJECT_DIR%deploy\nssm.exe

rem --- 1. Make sure proxy.txt exists and isn't empty ---
if not exist "%PROJECT_DIR%proxy.txt" (
    echo فایل proxy.txt خالیه یا پیدا نشد - لطفاً لینک پروکسی رو داخلش بذار
    pause
    exit /b 1
)

for %%A in ("%PROJECT_DIR%proxy.txt") do set PROXY_FILE_SIZE=%%~zA
if "%PROXY_FILE_SIZE%"=="0" (
    echo فایل proxy.txt خالیه یا پیدا نشد - لطفاً لینک پروکسی رو داخلش بذار
    pause
    exit /b 1
)

if not exist "%NSSM_EXE%" (
    echo ERROR: nssm.exe not found at %NSSM_EXE%
    pause
    exit /b 1
)

rem --- 2. Start the main order-sync service ---
rem "start" (not "restart") is intentional: the service hot-reloads
rem proxy.txt on the next network failure (see src/telegram.py's
rem _reset_connection), so there's no need to bounce the whole
rem order-sync process just to pick up a new proxy link.
echo Starting %SERVICE_NAME%...
"%NSSM_EXE%" start %SERVICE_NAME%
if errorlevel 1 (
    echo توجه: سرویس اصلی احتمالاً از قبل روشن بوده - این خطای واقعی نیست.
) else (
    echo سرویس اصلی با موفقیت استارت شد.
)

rem --- 3. Start the health monitor service ---
rem This is its own NSSM service (see deploy\install_health_monitor_service.bat)
rem rather than a plain background process, so NSSM manages it the
rem exact same way it manages the main service - no PID tracking needed here.
echo Starting %HEALTH_SERVICE_NAME%...
"%NSSM_EXE%" start %HEALTH_SERVICE_NAME%
if errorlevel 1 (
    echo توجه: نمایشگر وضعیت احتمالاً از قبل روشن بوده - این خطای واقعی نیست.
) else (
    echo نمایشگر وضعیت با موفقیت استارت شد.
)

echo.
echo هر دو سرویس استارت شدند. فایل status\status.html رو باز نگه دارید تا وضعیت اتصال رو ببینید.
pause

endlocal
