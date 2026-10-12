@echo off
REM ---------------------------------------------------------------------------
REM  NETRA-X collector for Windows -- double-click and leave the window open.
REM
REM  First run: creates .venv, installs httpx[socks], asks for the collector
REM  token once and saves it as a user environment variable. Later runs go
REM  straight to the collector. No credentials are stored in this file.
REM ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"
title NETRA-X Collector

echo.
echo   NETRA-X collector
echo   -----------------

REM ---------------------------------------------------------------- python
if exist ".venv\Scripts\python.exe" goto :venv_ok
where py >nul 2>nul
if not errorlevel 1 (
    py -3 -m venv .venv
) else (
    python -m venv .venv
)
if not exist ".venv\Scripts\python.exe" (
    echo   Python 3.10+ is needed. Install it from python.org, then run this again.
    pause
    exit /b 1
)
echo   Installing requirements...
".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
:venv_ok
set "PY=.venv\Scripts\python.exe"

REM ---------------------------------------------------------------- token
if not "%NETRAX_COLLECTOR_TOKEN%"=="" goto :token_ok
echo.
echo   The collector token is not saved on this PC yet.
echo   Ask the NETRA-X admin for it (NETRAX_COLLECTOR_TOKEN on the server).
echo.
set /p "NETRAX_COLLECTOR_TOKEN=  Paste it here and press Enter: "
if "%NETRAX_COLLECTOR_TOKEN%"=="" (
    echo   No token entered. Nothing started.
    pause
    exit /b 1
)
setx NETRAX_COLLECTOR_TOKEN "%NETRAX_COLLECTOR_TOKEN%" >nul
echo   Saved. You will not be asked again.
:token_ok
if "%NETRAX_SERVER%"=="" set "NETRAX_SERVER=https://console.onnetra.in"

REM ---------------------------------------------------------------- Tor
"%PY%" -c "from workers.collection.tor_client import find_running_proxy as p, find_tor_binary as b; import sys; sys.exit(0 if (p() or b()) else 1)"
if errorlevel 1 (
    echo   Tor           : NOT FOUND. Install Tor Browser to the Desktop, or crawls will fail.
) else (
    echo   Tor           : found
)

echo   Server        : %NETRAX_SERVER%
echo   Collector     : %COMPUTERNAME%
echo.
echo   Leave this window open. Queued crawls are picked up within seconds.
echo   Ctrl+C or closing the window stops it.
echo.

"%PY%" -m workers.collector_agent %*

echo.
echo   Collector stopped. Queued crawls will wait until it runs again.
pause
