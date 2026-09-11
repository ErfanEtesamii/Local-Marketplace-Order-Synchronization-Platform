@echo off
setlocal

rem Stops and removes the OrderSyncHealthMonitor Windows Service.
rem Must be run from an elevated (Administrator) command prompt.
rem Does NOT delete status\ or logs\ - only the service registration
rem itself. Safe to run before reinstalling after an update.

set SERVICE_NAME=OrderSyncHealthMonitor
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
