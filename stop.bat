@echo off
setlocal

rem ============================================================
rem  stop.bat - one-click stop for non-technical office staff.
rem  Stops the OrderSyncPlatform Windows service.
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

echo Stopping %SERVICE_NAME%...
"%NSSM_EXE%" stop %SERVICE_NAME%

echo.
echo سرویس متوقف شد.
pause

endlocal
