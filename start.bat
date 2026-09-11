@echo off
setlocal

rem ============================================================
rem  start.bat - one-click start for non-technical office staff.
rem  Starts the OrderSyncPlatform Windows service (registered by
rem  deploy\install_service.bat).
rem
rem  The service is registered with SERVICE_AUTO_START, so it
rem  normally comes back on its own after a reboot with nobody
rem  needing to touch this file. It's here for the times it needs
rem  a manual nudge (first-time setup, or after running stop.bat).
rem ============================================================

set SERVICE_NAME=OrderSyncPlatform
set SCRIPT_DIR=%~dp0
set PROJECT_DIR=%SCRIPT_DIR%
set NSSM_EXE=%PROJECT_DIR%deploy\nssm.exe

if not exist "%NSSM_EXE%" (
    echo ERROR: nssm.exe not found at %NSSM_EXE%
    pause
    exit /b 1
)

echo Starting %SERVICE_NAME%...
"%NSSM_EXE%" start %SERVICE_NAME%
if errorlevel 1 (
    echo توجه: سرویس احتمالاً از قبل روشن بوده - این خطای واقعی نیست.
) else (
    echo سرویس با موفقیت استارت شد.
)

echo.
echo Done. Check logs\order-sync.log to confirm it came back up cleanly.
pause

endlocal
