@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
if exist ".audit-browsers" set "PLAYWRIGHT_BROWSERS_PATH=%CD%\.audit-browsers"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" checkin.py
) else if exist ".audit-venv\Scripts\python.exe" (
  ".audit-venv\Scripts\python.exe" checkin.py
) else (
  python checkin.py
)
pause
