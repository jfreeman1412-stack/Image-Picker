@echo off
REM Start the backend for code editing. Reloads on .py changes in app/ only.
REM Don't use this for actual pipeline runs — the watcher can still trip on
REM OneDrive sync events and kill in-flight work.
call "%~dp0.venv\Scripts\activate.bat"
uvicorn app.main:app --reload --reload-dir app --host 0.0.0.0 --port 8020
