@echo off
setlocal

rem ============================================================
rem  Installs the Order Sync Platform as a Windows Service using
rem  NSSM (Non-Sucking Service Manager - https://nssm.cc).
rem
rem  Must be run from an elevated (Administrator) command prompt.
rem  Expects:
rem    - nssm.exe sitting next to this script (deploy\nssm.exe)
rem    - a virtual environment already created at ..\venv
rem      (python -m venv venv && venv\Scripts\pip install -r requirements.txt)
rem    - a filled-in .env at the project root
rem ============================================================

set SERVICE_NAME=OrderSyncPlatform
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
    echo The service will install, but every adapter will fail to authenticate
    echo until you copy .env.example to .env and fill in real credentials.
)

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

echo Installing service "%SERVICE_NAME%"...
"%NSSM_EXE%" install %SERVICE_NAME% "%PYTHON_EXE%" "-m src.main"
"%NSSM_EXE%" set %SERVICE_NAME% AppDirectory "%PROJECT_DIR%"
"%NSSM_EXE%" set %SERVICE_NAME% DisplayName "Local Marketplace Order Sync"
"%NSSM_EXE%" set %SERVICE_NAME% Description "Polls Tapsi Shop, Digikala, Basalam, SnappShop, and Faraz Honar for new orders and syncs them into Didar CRM."
"%NSSM_EXE%" set %SERVICE_NAME% Start SERVICE_AUTO_START

rem Route stdout/stderr into rotating log files, separate from the
rem application's own logger.py output (logs\order-sync.log) - this
rem catches anything that happens before logging is configured, or
rem an uncaught exception that kills the process outright.
"%NSSM_EXE%" set %SERVICE_NAME% AppStdout "%LOG_DIR%\service-stdout.log"
"%NSSM_EXE%" set %SERVICE_NAME% AppStderr "%LOG_DIR%\service-stderr.log"
"%NSSM_EXE%" set %SERVICE_NAME% AppRotateFiles 1
"%NSSM_EXE%" set %SERVICE_NAME% AppRotateOnline 1
"%NSSM_EXE%" set %SERVICE_NAME% AppRotateBytes 10485760

rem If the process ever exits (crash, unhandled exception), restart it
rem automatically after a short delay rather than leaving sync silently
rem stopped until someone notices.
"%NSSM_EXE%" set %SERVICE_NAME% AppExit Default Restart
"%NSSM_EXE%" set %SERVICE_NAME% AppRestartDelay 5000

echo.
echo Done. The service is installed but not yet started.
echo.
echo Start it now with:
echo   "%NSSM_EXE%" start %SERVICE_NAME%
echo.
echo Or open services.msc and start "Local Marketplace Order Sync" from there.

endlocal
