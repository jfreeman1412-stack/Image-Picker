@echo off
REM Reset any sessions stuck in status='running' back to 'pending'.
REM Run this after a server crash or force-quit before re-running the pipeline.
call "%~dp0.venv\Scripts\activate.bat"
python -c "from app.db import SessionLocal; from app.models.db_models import Session; db = SessionLocal(); n = db.query(Session).filter(Session.status=='running').update({'status': 'pending', 'progress_stage': None, 'progress_current': 0, 'progress_total': 0}); db.commit(); print(f'Reset {n} stuck sessions back to pending.')"
pause
