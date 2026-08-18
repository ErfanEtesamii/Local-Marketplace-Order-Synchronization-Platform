@echo off
setlocal

rem Restarts the OrderSyncPlatform service - use this after editing .env,
rem pulling new code, or any other change that needs a fresh process.
rem Must be run from an elevated (Administrator) command prompt.

set SERVICE_NAME=OrderSyncPlatform
set SCRIPT_DIR=%~dp0
set NSSM_EXE=%SCRIPT_DIR%nssm.exe

if not exist "%NSSM_EXE%" (
    echo ERROR: nssm.exe not found at %NSSM_EXE%
    exit /b 1
)

"%NSSM_EXE%" restart %SERVICE_NAME%
echo Done. Check logs\order-sync.log to confirm it came back up cleanly.

endlocal
