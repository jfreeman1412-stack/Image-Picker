@echo off
REM Start the backend for actual pipeline work. No --reload, no watcher,
REM zero chance of mid-pipeline interruption. Use this for real runs.
call "%~dp0.venv\Scripts\activate.bat"
uvicorn app.main:app --host 0.0.0.0 --port 8020
