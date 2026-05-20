"""Pipeline-level safety guards (Phase 9 follow-up).

When face detection produces 0 faces across the entire session despite
there being images to check, the pipeline marks the session 'error'
instead of silently moving on to clustering / sorting / status=done.
Common cause in the wild: source files unreachable (UNC share offline,
folder renamed post-ingest). Without this guard the team card paints as
successful but empty.
"""
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models.db_models import Image, Job, Session as DbSession
from app.services import face_pipeline
from app.services.face_pipeline import run_pipeline


@pytest.fixture
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'pipeline-err.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    SL = sessionmaker(bind=engine)
    s = SL()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _session_with_images(db, n=3, name="T"):
    job = Job(name="J", root_path="/tmp/J", has_lines=0)
    db.add(job); db.commit(); db.refresh(job)
    sess = DbSession(job_id=job.id, name=name, source_path="/tmp/J/T",
                     status="pending", created_at=datetime.utcnow())
    db.add(sess); db.commit(); db.refresh(sess)
    for i in range(n):
        db.add(Image(session_id=sess.id, path=f"/tmp/J/T/{i}.png",
                     filename=f"{i}.png"))
    db.commit()
    return sess


def test_zero_faces_marks_session_error(db, monkeypatch):
    """detect_faces returns [] for every image (source files unreachable
    in real life) → session.status becomes 'error', not 'done'."""
    monkeypatch.setattr(
        face_pipeline.face_detector, "detect_faces", lambda path: [],
    )
    sess = _session_with_images(db, n=3)
    run_pipeline(db, sess.id)
    db.refresh(sess)
    assert sess.status == "error"
    # progress fields zeroed so the UI doesn't show a stuck stage bar.
    assert sess.progress_stage is None
    assert sess.progress_current == 0
    assert sess.progress_total == 0
    # pipeline_finished_at stamped so the UI knows when the failure happened.
    assert sess.pipeline_finished_at is not None


def test_zero_faces_skips_remaining_pipeline_stages(db, monkeypatch):
    """When detection produces 0 faces, downstream stages (clustering,
    classifying, sorting) must NOT run — otherwise we'd burn time on
    empty work and possibly leave the session half-processed."""
    monkeypatch.setattr(
        face_pipeline.face_detector, "detect_faces", lambda path: [],
    )
    # If clustering were reached, this would observe a non-empty face list.
    sort_calls = []
    monkeypatch.setattr(
        face_pipeline, "_sort_cluster",
        lambda *a, **k: sort_calls.append(1),
    )
    sess = _session_with_images(db, n=2)
    run_pipeline(db, sess.id)
    # Sort never invoked because we bailed at the zero-faces guard.
    assert sort_calls == []


def test_session_with_no_images_does_not_error(db, monkeypatch):
    """A session that has zero Image rows isn't a detection failure —
    there's nothing to detect on. Status should be 'done'."""
    monkeypatch.setattr(
        face_pipeline.face_detector, "detect_faces", lambda path: [],
    )
    job = Job(name="J", root_path="/tmp/J", has_lines=0)
    db.add(job); db.commit(); db.refresh(job)
    sess = DbSession(job_id=job.id, name="empty", source_path="/tmp/J/empty",
                     status="pending", created_at=datetime.utcnow())
    db.add(sess); db.commit(); db.refresh(sess)
    run_pipeline(db, sess.id)
    db.refresh(sess)
    assert sess.status == "done"
