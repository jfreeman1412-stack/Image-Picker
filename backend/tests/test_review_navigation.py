"""Tests for the review-state flag + per-job navigation helpers."""
from datetime import datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.sessions import (
    SetReviewedRequest, next_unreviewed, set_reviewed, siblings,
)
from app.db import Base
from app.models.db_models import Job, Session as DbSessionModel


@pytest.fixture
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'review-nav-test.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _make_job_with_sessions(db, n: int, name_prefix="Team"):
    job = Job(name="Test", root_path="/tmp/test", has_lines=0)
    db.add(job)
    db.commit()
    db.refresh(job)

    sessions = []
    for i in range(n):
        s = DbSessionModel(
            job_id=job.id,
            name=f"{name_prefix} {i + 1}",
            source_path=f"/tmp/test/team_{i + 1}",
            status="done",
            created_at=datetime.utcnow(),
        )
        db.add(s)
        db.commit()
        db.refresh(s)
        sessions.append(s)
    return job, sessions


# ── set_reviewed ──────────────────────────────────────────────────────────────


def test_mark_reviewed_sets_flag_and_timestamp(db):
    _, sessions = _make_job_with_sessions(db, 3)
    target = sessions[1]

    result = set_reviewed(target.id, SetReviewedRequest(reviewed=True), db)

    assert result["reviewed"] is True
    assert result["reviewed_at"] is not None
    db.refresh(target)
    assert target.reviewed == 1
    assert target.reviewed_at is not None


def test_unmark_reviewed_clears_timestamp(db):
    _, sessions = _make_job_with_sessions(db, 3)
    target = sessions[1]

    set_reviewed(target.id, SetReviewedRequest(reviewed=True), db)
    db.refresh(target)
    assert target.reviewed_at is not None

    set_reviewed(target.id, SetReviewedRequest(reviewed=False), db)
    db.refresh(target)
    assert target.reviewed == 0
    assert target.reviewed_at is None


def test_set_reviewed_404_when_missing(db):
    with pytest.raises(HTTPException) as exc:
        set_reviewed(9999, SetReviewedRequest(reviewed=True), db)
    assert exc.value.status_code == 404


# ── next_unreviewed ───────────────────────────────────────────────────────────


def test_next_unreviewed_simple_forward(db):
    """From session 1, with all others unreviewed → returns session 2."""
    _, sessions = _make_job_with_sessions(db, 5)
    result = next_unreviewed(sessions[0].id, db)
    assert result["session_id"] == sessions[1].id


def test_next_unreviewed_wraps_around(db):
    """From session 3 of 5, with sessions 4 and 5 already reviewed → wrap to session 1."""
    _, sessions = _make_job_with_sessions(db, 5)
    sessions[3].reviewed = 1
    sessions[4].reviewed = 1
    db.commit()

    result = next_unreviewed(sessions[2].id, db)
    assert result["session_id"] == sessions[0].id


def test_next_unreviewed_skips_already_reviewed_in_forward_search(db):
    """Forward search should skip reviewed sessions to find the next unreviewed one."""
    _, sessions = _make_job_with_sessions(db, 5)
    sessions[1].reviewed = 1  # session 2 reviewed
    sessions[2].reviewed = 1  # session 3 reviewed
    db.commit()

    # Starting from session 1, forward: skip 2 (reviewed), skip 3 (reviewed), land on 4.
    result = next_unreviewed(sessions[0].id, db)
    assert result["session_id"] == sessions[3].id


def test_next_unreviewed_returns_null_when_all_reviewed(db):
    _, sessions = _make_job_with_sessions(db, 3)
    for s in sessions:
        s.reviewed = 1
    db.commit()

    result = next_unreviewed(sessions[0].id, db)
    assert result["session_id"] is None


def test_next_unreviewed_does_not_return_self(db):
    """A job containing only this session, unreviewed → returns null (not self)."""
    _, sessions = _make_job_with_sessions(db, 1)
    result = next_unreviewed(sessions[0].id, db)
    assert result["session_id"] is None


def test_next_unreviewed_returns_null_when_session_has_no_job(db):
    """Legacy sessions (no job_id) → null, no error."""
    orphan = DbSessionModel(
        job_id=None, name="Lone", source_path="/tmp/lone", status="done",
        created_at=datetime.utcnow(),
    )
    db.add(orphan)
    db.commit()
    db.refresh(orphan)

    result = next_unreviewed(orphan.id, db)
    assert result["session_id"] is None


# ── siblings ──────────────────────────────────────────────────────────────────


def test_siblings_middle_of_job(db):
    _, sessions = _make_job_with_sessions(db, 5)
    result = siblings(sessions[2].id, db)
    assert result["previous_session_id"] == sessions[1].id
    assert result["next_session_id"] == sessions[3].id


def test_siblings_first_in_job_has_no_previous(db):
    _, sessions = _make_job_with_sessions(db, 3)
    result = siblings(sessions[0].id, db)
    assert result["previous_session_id"] is None
    assert result["next_session_id"] == sessions[1].id


def test_siblings_last_in_job_has_no_next(db):
    _, sessions = _make_job_with_sessions(db, 3)
    result = siblings(sessions[-1].id, db)
    assert result["previous_session_id"] == sessions[-2].id
    assert result["next_session_id"] is None


def test_siblings_lone_session_returns_nulls(db):
    _, sessions = _make_job_with_sessions(db, 1)
    result = siblings(sessions[0].id, db)
    assert result["previous_session_id"] is None
    assert result["next_session_id"] is None


def test_siblings_legacy_session_returns_nulls(db):
    orphan = DbSessionModel(
        job_id=None, name="Lone", source_path="/tmp/lone", status="done",
        created_at=datetime.utcnow(),
    )
    db.add(orphan)
    db.commit()
    db.refresh(orphan)

    result = siblings(orphan.id, db)
    assert result["previous_session_id"] is None
    assert result["next_session_id"] is None
