"""Fix-3 tests for the 2026-09-10 pipeline concurrency wedge — watchdog +
Session.status_updated_at auto-stamp.

Covers:
  - status_updated_at auto-stamps on every session.status assignment
    (SQLAlchemy attribute event, applies uniformly across all code paths).
  - Steady-state reaper (startup=False) reaps only stale (>20 min)
    running sessions.
  - Startup reaper (startup=True) reaps every session whose status was
    set BEFORE this process booted.
  - The critical boot-race safety: a legitimately-triggered /run right
    after boot has status_updated_at > _STARTUP_AT and MUST NOT be
    reaped by the startup pass.
  - NULL status_updated_at (pre-migration rows) is treated as "very
    old" and reaped.
  - Non-running sessions (pending, done, error) are never touched.
"""
import time
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models.db_models import Session as SessionModel
from app.services import pipeline_watchdog
from app.services.pipeline_watchdog import (
    reap_stale, _WATCHDOG_STAGE_STALE_SECONDS,
)


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def test_engine(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'watchdog-test.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    return engine


@pytest.fixture
def SessionFactory(test_engine):
    return sessionmaker(autocommit=False, autoflush=False, bind=test_engine)


@pytest.fixture(autouse=True)
def reset_startup_at():
    """Module-global _STARTUP_AT survives across tests since the module
    is only imported once. Reset before + after each test so no test's
    startup time leaks into the next."""
    pipeline_watchdog._STARTUP_AT = None
    yield
    pipeline_watchdog._STARTUP_AT = None


def _seed_session(
    SessionFactory, *, status: str, status_updated_at, name: str = "t",
) -> int:
    """Seed a Session row. status_updated_at is set EXPLICITLY after the
    initial insert so the SQLAlchemy event's auto-stamp is overwritten
    with the test value. Pass None to simulate pre-migration rows."""
    db = SessionFactory()
    try:
        s = SessionModel(name=name, status=status, source_path=f"/tmp/{name}")
        db.add(s); db.commit(); db.refresh(s)
        # The event listener stamped status_updated_at = now during the
        # initial INSERT. Override to the test's intended value.
        s.status_updated_at = status_updated_at
        db.commit()
        return s.id
    finally:
        db.close()


# ── status_updated_at auto-stamp ────────────────────────────────────────


def test_auto_stamps_status_updated_at_on_insert(SessionFactory):
    """Creating a Session with status='pending' triggers the attribute
    event and populates status_updated_at."""
    db = SessionFactory()
    try:
        before = datetime.utcnow()
        s = SessionModel(name="s", status="pending", source_path="/tmp/s")
        db.add(s); db.commit(); db.refresh(s)
        after = datetime.utcnow()
        assert s.status_updated_at is not None
        assert before <= s.status_updated_at <= after
    finally:
        db.close()


def test_auto_stamps_status_updated_at_on_status_change(SessionFactory):
    """Assigning a new value to session.status re-stamps the timestamp."""
    db = SessionFactory()
    try:
        s = SessionModel(name="s", status="pending", source_path="/tmp/s")
        db.add(s); db.commit(); db.refresh(s)
        initial_stamp = s.status_updated_at

        time.sleep(0.05)   # measurable gap to be sure the new stamp differs
        s.status = "running"
        db.commit(); db.refresh(s)
        assert s.status_updated_at > initial_stamp

        time.sleep(0.05)
        second_stamp = s.status_updated_at
        s.status = "done"
        db.commit(); db.refresh(s)
        assert s.status_updated_at > second_stamp
    finally:
        db.close()


# ── Steady-state reaper (startup=False) ─────────────────────────────────


def test_reaps_stale_running_session(SessionFactory):
    """status='running' with status_updated_at older than the 20-min
    threshold gets reaped."""
    old = datetime.utcnow() - timedelta(
        seconds=_WATCHDOG_STAGE_STALE_SECONDS + 60,
    )
    sid = _seed_session(SessionFactory, status="running",
                        status_updated_at=old, name="stale")

    reaped = reap_stale(startup=False, session_factory=SessionFactory)

    assert sid in reaped
    db = SessionFactory()
    try:
        s = db.query(SessionModel).get(sid)
        assert s.status == "error"
        assert s.progress_stage == "error"
    finally:
        db.close()


def test_does_not_reap_fresh_running_session(SessionFactory):
    """status='running' updated within threshold is left alone."""
    fresh = datetime.utcnow() - timedelta(seconds=30)
    sid = _seed_session(SessionFactory, status="running",
                        status_updated_at=fresh, name="fresh")

    reaped = reap_stale(startup=False, session_factory=SessionFactory)

    assert sid not in reaped
    db = SessionFactory()
    try:
        assert db.query(SessionModel).get(sid).status == "running"
    finally:
        db.close()


def test_reaps_null_status_updated_at(SessionFactory):
    """Pre-migration rows have NULL status_updated_at. Any such row
    still marked 'running' is by definition a zombie (its status was
    set before we started tracking timestamps). This is how the FIRST
    reap after deploying Fix 3 will catch sid=642 without any backfill."""
    sid = _seed_session(SessionFactory, status="running",
                        status_updated_at=None, name="pre_migration")

    reaped = reap_stale(startup=False, session_factory=SessionFactory)

    assert sid in reaped
    db = SessionFactory()
    try:
        s = db.query(SessionModel).get(sid)
        assert s.status == "error"
    finally:
        db.close()


def test_does_not_reap_non_running_sessions(SessionFactory):
    """Sessions in 'pending', 'done', or 'error' — regardless of how old
    their status_updated_at is — are outside the reaper's scope."""
    long_ago = datetime.utcnow() - timedelta(days=30)
    for status in ("pending", "done", "error"):
        _seed_session(SessionFactory, status=status,
                      status_updated_at=long_ago, name=status)

    reaped = reap_stale(startup=False, session_factory=SessionFactory)

    assert reaped == []


# ── Startup pass ────────────────────────────────────────────────────────


def test_startup_pass_reaps_pre_startup_running(SessionFactory):
    """Baseline startup pass: session whose status was set BEFORE
    _STARTUP_AT is reaped (its pipeline died with the previous process)."""
    # Session set to running 1 hour ago
    old = datetime.utcnow() - timedelta(hours=1)
    sid = _seed_session(SessionFactory, status="running",
                        status_updated_at=old, name="pre_startup")

    # Simulate backend boot RIGHT NOW (after the session was already
    # marked running).
    pipeline_watchdog._set_startup_at(datetime.utcnow())

    reaped = reap_stale(startup=True, session_factory=SessionFactory)

    assert sid in reaped


def test_startup_pass_does_not_reap_post_startup_running(SessionFactory):
    """CRITICAL — the boot-race safety test Joey called out.

    Scenario: backend boots at T0. A user triggers /run at T1 > T0,
    which sets session.status='running' + stamps status_updated_at = T1.
    The startup reaper wakes and checks — must NOT reap this session,
    because its status_updated_at > _STARTUP_AT means it was set AFTER
    boot, i.e. by a live pipeline invocation, not a leftover zombie.

    Without this guarantee, any /run triggered during the startup
    grace period would false-positive-reap. This is exactly the
    'startup pass can't race a pipeline triggered right at boot' case
    Joey asked to be pinned by a test.
    """
    # Boot happens FIRST.
    boot_time = datetime.utcnow()
    pipeline_watchdog._set_startup_at(boot_time)

    # A moment later, a /run endpoint sets status='running' — the event
    # listener stamps status_updated_at = now, which is AFTER boot_time.
    # Simulate that by seeding with a timestamp > boot_time.
    time.sleep(0.02)   # ensure the timestamp is measurably after boot_time
    post_boot_stamp = datetime.utcnow()
    assert post_boot_stamp > boot_time, "test precondition"

    sid = _seed_session(SessionFactory, status="running",
                        status_updated_at=post_boot_stamp,
                        name="legit_post_boot")

    reaped = reap_stale(startup=True, session_factory=SessionFactory)

    assert sid not in reaped, (
        "startup reaper false-positive on a legitimate post-boot "
        "pipeline. This is the boot-race Joey specifically asked "
        "the test suite to guard against."
    )
    # And the session's state is untouched.
    db = SessionFactory()
    try:
        s = db.query(SessionModel).get(sid)
        assert s.status == "running"
        assert s.status_updated_at == post_boot_stamp
    finally:
        db.close()


def test_startup_pass_reaps_null_status_updated_at(SessionFactory):
    """NULL status_updated_at on the startup pass is still reaped —
    pre-migration zombies are caught by the first startup pass after
    deploy."""
    pipeline_watchdog._set_startup_at(datetime.utcnow())
    sid = _seed_session(SessionFactory, status="running",
                        status_updated_at=None, name="null_pre_migration")

    reaped = reap_stale(startup=True, session_factory=SessionFactory)

    assert sid in reaped


def test_startup_pass_refuses_when_startup_at_unset(SessionFactory):
    """If start_watchdog() was never called, _STARTUP_AT is None. The
    startup pass must FAIL CLOSED (return no reaps) — otherwise a
    misconfigured deploy could mass-reap on first tick."""
    # _STARTUP_AT is None per autouse fixture reset.
    old = datetime.utcnow() - timedelta(hours=1)
    sid = _seed_session(SessionFactory, status="running",
                        status_updated_at=old, name="stale_but_no_startup")

    reaped = reap_stale(startup=True, session_factory=SessionFactory)

    # Nothing reaped — the guard fired.
    assert reaped == []
    db = SessionFactory()
    try:
        assert db.query(SessionModel).get(sid).status == "running"
    finally:
        db.close()


# ── Mixed-scenario integration ──────────────────────────────────────────


def test_reaper_handles_mixed_bag(SessionFactory):
    """One pass reaps stale + null but leaves fresh + non-running alone.
    Real-world sweep shape."""
    old = datetime.utcnow() - timedelta(hours=1)
    fresh = datetime.utcnow() - timedelta(seconds=30)

    stale_id = _seed_session(SessionFactory, status="running",
                             status_updated_at=old, name="stale")
    fresh_id = _seed_session(SessionFactory, status="running",
                             status_updated_at=fresh, name="fresh")
    null_id = _seed_session(SessionFactory, status="running",
                            status_updated_at=None, name="null")
    done_id = _seed_session(SessionFactory, status="done",
                            status_updated_at=old, name="already_done")

    reaped = reap_stale(startup=False, session_factory=SessionFactory)

    assert set(reaped) == {stale_id, null_id}
    db = SessionFactory()
    try:
        assert db.query(SessionModel).get(stale_id).status == "error"
        assert db.query(SessionModel).get(fresh_id).status == "running"
        assert db.query(SessionModel).get(null_id).status == "error"
        assert db.query(SessionModel).get(done_id).status == "done"
    finally:
        db.close()
