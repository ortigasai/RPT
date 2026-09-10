@echo off
title RPT Assessment Extractor
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo First-time setup: creating the Python environment...
  where py >nul 2>nul && (py -3 -m venv .venv) || (python -m venv .venv)
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)

echo.
echo Starting the app - a browser window will open in a few seconds.
echo Keep this window open while you use it. Close it (or press Ctrl+C) to stop.
echo.

start "" http://127.0.0.1:5000
".venv\Scripts\python.exe" app.py

echo.
echo The app has stopped. You can close this window.
pause
