@echo off
setlocal

rem Stops and removes the OrderSyncPlatform Windows Service.
rem Must be run from an elevated (Administrator) command prompt.
rem Does NOT delete logs, .env, or the local database - only the service
rem registration itself. Safe to run before reinstalling after an update.

set SERVICE_NAME=OrderSyncPlatform
set SCRIPT_DIR=%~dp0
set NSSM_EXE=%SCRIPT_DIR%nssm.exe

if not exist "%NSSM_EXE%" (
    echo ERROR: nssm.exe not found at %NSSM_EXE%
    exit /b 1
)

echo Stopping service "%SERVICE_NAME%" (if running)...
"%NSSM_EXE%" stop %SERVICE_NAME%

echo Removing service "%SERVICE_NAME%"...
"%NSSM_EXE%" remove %SERVICE_NAME% confirm

echo Done.

endlocal
