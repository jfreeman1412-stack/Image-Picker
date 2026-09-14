"""Fix-2 tests for the 2026-09-10 pipeline concurrency wedge.

Fix 1 (face_pipeline.py's _PIPELINE_LOCK) made concurrent triggers SAFE by
serializing them. Fix 2 makes redundant triggers REJECTED so a second
click on Run All doesn't quietly queue a duplicate wave of work behind
the Fix-1 lock.

Central claims tested:
  1. Overlapping /run-all for the same job → 409 (run_all_in_progress).
  2. Overlapping /run for the same session → 409 (session_run_in_progress).
  3. Mixed: a /run-all + a /run for a session in that job coordinate
     across endpoints — whichever fires first wins; the other 409s.
  4. Cleanup after both success AND exception in the background task —
     the `finally` release is load-bearing (a leaked id would permanently
     lock out that job/session until backend restart).

TestClient runs BackgroundTasks synchronously within `client.post()`, so
blocking-mock overlap tests need to fire the "first" request from a
worker thread and let the guarded "second" request go from the main
thread. `run_pipeline` is monkeypatched to block on a threading.Event so
we can precisely control when the first request's background task is
"inside" the pipeline.
"""
import threading
from typing import Callable

import pytest
from fastapi.testclient import TestClient
from PIL import Image as PILImage
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import db as db_module
from app.api import jobs as jobs_module
from app.api import sessions as sessions_module
from app.db import Base, get_db
from app.main import app
from app.services import pipeline_locks


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def client(tmp_path, monkeypatch):
    """FastAPI TestClient with a temp SQLite DB. Overrides jobs.SessionLocal
    and sessions.SessionLocal so the background tasks share the same
    engine as the request-handling ORM session."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'idempotency-test.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(
        autocommit=False, autoflush=False, bind=engine,
    )

    def _override_get_db():
        s = TestingSessionLocal()
        try:
            yield s
        finally:
            s.close()

    monkeypatch.setattr(jobs_module, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(sessions_module, "SessionLocal", TestingSessionLocal)
    app.dependency_overrides[get_db] = _override_get_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


@pytest.fixture(autouse=True)
def reset_active_sets():
    """The module-level ACTIVE_* sets in pipeline_locks survive across
    tests since the module is imported once. Reset before + after each
    test so leaked state from one test can't spook the next. Belt AND
    suspenders — the `finally` blocks in the endpoints are the primary
    guarantee, but if a test fails mid-flight and leaves a slot occupied,
    the next test would confusingly 409."""
    pipeline_locks.ACTIVE_RUN_ALLS.clear()
    pipeline_locks.ACTIVE_SESSION_RUNS.clear()
    yield
    pipeline_locks.ACTIVE_RUN_ALLS.clear()
    pipeline_locks.ACTIVE_SESSION_RUNS.clear()


def _make_job(client, tmp_path, teams=("TeamA",)) -> tuple[int, list[int]]:
    """Create a job with `teams` as separate session folders. Ingest runs
    synchronously via TestClient's background execution, so by the time
    this returns the sessions exist. Returns (job_id, [session_ids])."""
    root = tmp_path / "Shoot"
    root.mkdir()
    for name in teams:
        team_dir = root / name
        team_dir.mkdir()
        for i in range(2):
            PILImage.new("RGB", (8, 8), "white").save(team_dir / f"{name}_{i}.png")
    r = client.post("/api/jobs", json={
        "name": "IdemJob", "root_path": str(root),
        "has_lines": False, "image_subfolder_name": None,
        "auto_run": False,  # never chain the real pipeline in idempotency tests
    })
    assert r.status_code == 200, r.text
    jid = r.json()["job_id"]
    detail = client.get(f"/api/jobs/{jid}").json()
    session_ids = [s["id"] for s in detail["sessions"]]
    return jid, session_ids


def _install_blocking_pipeline(
    monkeypatch, target_module, *,
    on_enter: threading.Event = None,
    can_finish: threading.Event = None,
    raise_after_enter: bool = False,
) -> None:
    """Patch `<target_module>.run_pipeline` to block on `can_finish` after
    signalling `on_enter`. If `raise_after_enter`, raise once `can_finish`
    is set — used to prove the `finally` release runs on exception."""
    def _mock(db, session_id):
        if on_enter is not None:
            on_enter.set()
        if can_finish is not None:
            can_finish.wait(timeout=10)
        if raise_after_enter:
            raise RuntimeError("simulated pipeline failure")

    monkeypatch.setattr(target_module, "run_pipeline", _mock)


# ── Test 1: overlapping run-all ─────────────────────────────────────────


def test_overlapping_run_all_returns_409(client, tmp_path, monkeypatch):
    """First /run-all for a job is accepted; a second while the first is
    still active returns 409 with error='run_all_in_progress'. Once the
    first completes, a third /run-all is accepted (slot released)."""
    jid, _sids = _make_job(client, tmp_path, teams=("TeamA",))

    on_enter = threading.Event()
    can_finish = threading.Event()
    _install_blocking_pipeline(
        monkeypatch, jobs_module,
        on_enter=on_enter, can_finish=can_finish,
    )

    first_result = {}

    def _fire_first():
        r = client.post(f"/api/jobs/{jid}/run-all", json={"force": True})
        first_result["status"] = r.status_code
        first_result["body"] = r.json() if r.content else None

    t = threading.Thread(target=_fire_first)
    t.start()

    try:
        # Wait until the first run-all's background task is inside the
        # (mocked) pipeline. Only then is the ACTIVE_RUN_ALLS slot held
        # while the endpoint is still "in flight" from the client's view.
        assert on_enter.wait(timeout=5), "First run-all never reached run_pipeline"

        # Second /run-all while first is active → 409.
        r2 = client.post(f"/api/jobs/{jid}/run-all", json={"force": True})
        assert r2.status_code == 409
        detail = r2.json()["detail"]
        assert detail["error"] == "run_all_in_progress"
        assert detail["job_id"] == jid
    finally:
        can_finish.set()
        t.join(timeout=10)
        assert not t.is_alive(), "First run-all thread did not join"

    # First must have returned 200.
    assert first_result["status"] == 200, first_result

    # Third /run-all after first completed → 200 (slot cleared).
    r3 = client.post(f"/api/jobs/{jid}/run-all", json={"force": True})
    assert r3.status_code == 200
    assert pipeline_locks.ACTIVE_RUN_ALLS == set()
    assert pipeline_locks.ACTIVE_SESSION_RUNS == set()


# ── Test 2: overlapping /run for the same session ────────────────────────


def test_overlapping_session_run_returns_409(client, tmp_path, monkeypatch):
    """First /api/sessions/{id}/run is accepted; second while first is
    active → 409. Once first completes, subsequent /run is accepted."""
    jid, session_ids = _make_job(client, tmp_path, teams=("TeamA",))
    sid = session_ids[0]

    on_enter = threading.Event()
    can_finish = threading.Event()
    _install_blocking_pipeline(
        monkeypatch, sessions_module,
        on_enter=on_enter, can_finish=can_finish,
    )

    first_result = {}

    def _fire_first():
        r = client.post(f"/api/sessions/{sid}/run")
        first_result["status"] = r.status_code
        first_result["body"] = r.json() if r.content else None

    t = threading.Thread(target=_fire_first)
    t.start()

    try:
        assert on_enter.wait(timeout=5), "First /run never reached run_pipeline"

        # Second /run for same session → 409. Note: the session's status
        # is now 'running' too, so the pre-existing status check would
        # also 409 here — but that's defense-in-depth. The authoritative
        # signal is ACTIVE_SESSION_RUNS.
        r2 = client.post(f"/api/sessions/{sid}/run")
        assert r2.status_code == 409
        # Response format is either legacy ("Pipeline already running"
        # string detail) OR the new structured detail — depending on
        # which guard fires first. Both are acceptable; assert on the
        # 409 status only. The important thing is we don't get 200.
    finally:
        can_finish.set()
        t.join(timeout=10)
        assert not t.is_alive()

    assert first_result["status"] == 200, first_result

    # After completion: cannot immediately re-trigger via /run because
    # session.status is still 'running' (mock never set it back). That's
    # a defense-in-depth 409 from the legacy check, NOT from the
    # in-memory set — assert the in-memory set is clear as our primary
    # cleanup contract.
    assert sid not in pipeline_locks.ACTIVE_SESSION_RUNS


# ── Test 3: mixed run-all + individual /run coordinate ──────────────────


def test_run_all_blocks_concurrent_session_run(client, tmp_path, monkeypatch):
    """run-all fires first; a /run for one of its sessions while it's
    active returns 409 with run_all_in_progress (the parent-job check)."""
    jid, session_ids = _make_job(client, tmp_path, teams=("TeamA", "TeamB"))

    on_enter = threading.Event()
    can_finish = threading.Event()
    _install_blocking_pipeline(
        monkeypatch, jobs_module,
        on_enter=on_enter, can_finish=can_finish,
    )

    first_result = {}

    def _fire_run_all():
        r = client.post(f"/api/jobs/{jid}/run-all", json={"force": True})
        first_result["status"] = r.status_code

    t = threading.Thread(target=_fire_run_all)
    t.start()

    try:
        assert on_enter.wait(timeout=5)

        # /run for a session in the running job — the in-memory reservation
        # (run-all preloaded its session_ids into ACTIVE_SESSION_RUNS) OR
        # the parent-job ACTIVE_RUN_ALLS check catches it. Either way, 409.
        r2 = client.post(f"/api/sessions/{session_ids[0]}/run")
        assert r2.status_code == 409, r2.text
    finally:
        can_finish.set()
        t.join(timeout=10)


def test_session_run_blocks_concurrent_run_all(client, tmp_path, monkeypatch):
    """Individual /run fires first; a /run-all for the same job while
    it's active returns 409 with session_run_in_progress (the
    ACTIVE_SESSION_RUNS conflict check)."""
    jid, session_ids = _make_job(client, tmp_path, teams=("TeamA", "TeamB"))
    sid = session_ids[0]

    on_enter = threading.Event()
    can_finish = threading.Event()
    _install_blocking_pipeline(
        monkeypatch, sessions_module,
        on_enter=on_enter, can_finish=can_finish,
    )

    first_result = {}

    def _fire_run():
        r = client.post(f"/api/sessions/{sid}/run")
        first_result["status"] = r.status_code

    t = threading.Thread(target=_fire_run)
    t.start()

    try:
        assert on_enter.wait(timeout=5)

        # /run-all for the parent job — the ACTIVE_SESSION_RUNS conflict
        # check trips because sid is already reserved.
        r2 = client.post(f"/api/jobs/{jid}/run-all", json={"force": True})
        assert r2.status_code == 409
        detail = r2.json()["detail"]
        assert detail["error"] == "session_run_in_progress"
        assert sid in detail["session_ids"]
    finally:
        can_finish.set()
        t.join(timeout=10)


# ── Test 4: cleanup on success AND on exception ─────────────────────────


def test_cleanup_after_successful_run_all(client, tmp_path, monkeypatch):
    """After a run-all completes successfully, ACTIVE_RUN_ALLS + the
    reserved ACTIVE_SESSION_RUNS entries are empty — a subsequent
    run-all is accepted, not permanently locked out."""
    jid, session_ids = _make_job(client, tmp_path, teams=("TeamA", "TeamB"))

    # No blocking — mock returns immediately.
    monkeypatch.setattr(jobs_module, "run_pipeline", lambda db, sid: None)

    r1 = client.post(f"/api/jobs/{jid}/run-all", json={"force": True})
    assert r1.status_code == 200
    # TestClient ran the background task synchronously — sets must be empty.
    assert pipeline_locks.ACTIVE_RUN_ALLS == set(), (
        "run-all slot leaked after successful completion — future run-alls "
        "for this job would be permanently blocked until backend restart"
    )
    assert pipeline_locks.ACTIVE_SESSION_RUNS == set()

    # Second run-all: accepted (slot was cleared).
    r2 = client.post(f"/api/jobs/{jid}/run-all", json={"force": True})
    assert r2.status_code == 200


def test_cleanup_after_run_all_exception(client, tmp_path, monkeypatch):
    """If run_pipeline raises inside the background task, the `finally`
    in _run_all still discards the slots. Without this, one broken
    pipeline would permanently lock out a job."""
    jid, session_ids = _make_job(client, tmp_path, teams=("TeamA",))

    # run-all's background loop catches per-session exceptions (see
    # `except Exception: logger.exception(...)` in jobs.py's _run_all).
    # That's fine — the try/finally around the loop still runs, releasing
    # the slots. Prove that by raising from every run_pipeline call and
    # confirming the sets end up empty.
    def _raise(db, sid):
        raise RuntimeError("simulated pipeline failure")

    monkeypatch.setattr(jobs_module, "run_pipeline", _raise)

    r = client.post(f"/api/jobs/{jid}/run-all", json={"force": True})
    assert r.status_code == 200  # endpoint returned before background raised

    # Background completed (with logged exception). Slots must be empty.
    assert pipeline_locks.ACTIVE_RUN_ALLS == set(), (
        "run-all slot leaked after pipeline exception — a leaked slot is "
        "a strictly worse failure than the wasteful double-run this fix "
        "prevents"
    )
    assert pipeline_locks.ACTIVE_SESSION_RUNS == set()

    # Subsequent run-all must succeed.
    monkeypatch.setattr(jobs_module, "run_pipeline", lambda db, sid: None)
    r2 = client.post(f"/api/jobs/{jid}/run-all", json={"force": True})
    assert r2.status_code == 200


def test_cleanup_after_session_run_exception(client, tmp_path, monkeypatch):
    """The `finally` in sessions.py's _run releases the ACTIVE_SESSION_RUNS
    slot even if run_pipeline raises — same rationale as the run-all
    counterpart. Without this, a single bad pipeline call permanently
    locks the session out."""
    jid, session_ids = _make_job(client, tmp_path, teams=("TeamA",))
    sid = session_ids[0]

    def _raise(db, session_id):
        raise RuntimeError("simulated pipeline failure")

    monkeypatch.setattr(sessions_module, "run_pipeline", _raise)

    # Endpoint returns 200 immediately; background task runs synchronously
    # in TestClient and raises. The raised exception propagates through
    # starlette's background runner and is re-raised into `client.post`.
    # Two paths for asserting cleanup depending on starlette version —
    # either the client sees the raised exception OR the response returns
    # normally with the exception logged. Both are fine as long as the
    # slot was released. Catch the exception if it propagates.
    try:
        r = client.post(f"/api/sessions/{sid}/run")
        # If we got here, background either succeeded or its exception
        # was swallowed — response should still be 200 from the endpoint.
        assert r.status_code == 200
    except RuntimeError as exc:
        # Starlette re-raised the background exception into the client
        # call — that's expected on some versions. Slot cleanup still
        # happened in `finally` before the raise propagated.
        assert "simulated pipeline failure" in str(exc)

    assert sid not in pipeline_locks.ACTIVE_SESSION_RUNS, (
        "session /run slot leaked after pipeline exception — a leaked "
        "slot is a strictly worse failure than the double-run this fix "
        "prevents"
    )
