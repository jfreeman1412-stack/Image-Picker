"""Phase B.5 §0 — SQLite concurrency pragmas (WAL + busy_timeout).

Verifies the connect-time pragma helper on an isolated tmp engine, so it never
touches the app's real DB file or any concurrently-running instance.
"""
from sqlalchemy import create_engine, event

from app.db import _set_sqlite_pragmas


def test_connect_sets_wal_and_busy_timeout(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'pragma-test.db'}",
        connect_args={"check_same_thread": False},
    )
    event.listen(engine, "connect", _set_sqlite_pragmas)
    try:
        with engine.connect() as conn:
            journal_mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
            busy_timeout = conn.exec_driver_sql("PRAGMA busy_timeout").scalar()
    finally:
        engine.dispose()
    assert str(journal_mode).lower() == "wal"
    assert busy_timeout == 10000
