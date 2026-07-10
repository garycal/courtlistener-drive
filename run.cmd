@echo off
rem Mount a CourtListener docket as a read-only drive.
rem Usage:  run.cmd <docket-url-or-id> [--mount V:] [--token ...] [--cache DIR] [-d]
rem Example: run.cmd https://www.courtlistener.com/docket/69536831/utherverse-inc-v-quinn/
setlocal
cd /d "%~dp0"
if "%~1"=="" (
    echo Usage: run.cmd ^<courtlistener-docket-url-or-id^> [--mount V:] [options]
    echo Example: run.cmd 69536831 --mount V:
    exit /b 1
)
".venv\Scripts\python.exe" mount.py %*
