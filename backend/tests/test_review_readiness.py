"""review-readiness logic + the force gate on POST /reviewed."""
from datetime import datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.sessions import (
    SetReviewedRequest, _compute_review_readiness, review_readiness,
    set_reviewed,
)
from app.db import Base
from app.models.db_models import (
    Cluster, Image, ImageRole, Job, Session as DbSessionModel,
)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'rr-test.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _session(db):
    job = Job(name="J", root_path="/tmp", has_lines=0)
    db.add(job); db.commit(); db.refresh(job)
    sess = DbSessionModel(job_id=job.id, name="T", source_path="/tmp/T",
                          status="done", created_at=datetime.utcnow())
    db.add(sess); db.commit(); db.refresh(sess)
    return sess


def _cluster(db, sess, *, coach=False, roles=()):
    c = Cluster(session_id=sess.id, image_count=1,
                manual_coach_override=1 if coach else 0)
    db.add(c); db.commit(); db.refresh(c)
    for i, role in enumerate(roles):
        img = Image(session_id=sess.id, path=f"/tmp/{c.id}_{i}.png",
                    filename=f"{c.id}_{i}.png")
        db.add(img); db.commit(); db.refresh(img)
        db.add(ImageRole(image_id=img.id, cluster_id=c.id, role=role))
    db.commit()
    return c


def test_player_with_team_and_pano_ready(db):
    sess = _session(db)
    _cluster(db, sess, roles=("team", "panoramic", "individual"))
    r = _compute_review_readiness(db, sess)
    assert r["ready"] is True
    assert r["incomplete_clusters"] == []


def test_coach_with_team_ready(db):
    sess = _session(db)
    _cluster(db, sess, coach=True, roles=("team", "individual"))
    r = _compute_review_readiness(db, sess)
    assert r["ready"] is True


def test_player_missing_team(db):
    sess = _session(db)
    c = _cluster(db, sess, roles=("panoramic",))
    r = _compute_review_readiness(db, sess)
    assert r["ready"] is False
    assert r["incomplete_clusters"][0]["cluster_id"] == c.id
    assert r["incomplete_clusters"][0]["missing"] == ["team"]


def test_player_missing_pano(db):
    sess = _session(db)
    _cluster(db, sess, roles=("team",))
    r = _compute_review_readiness(db, sess)
    assert r["ready"] is False
    assert r["incomplete_clusters"][0]["missing"] == ["pano"]


def test_player_missing_both(db):
    sess = _session(db)
    _cluster(db, sess, roles=("individual",))
    r = _compute_review_readiness(db, sess)
    assert r["incomplete_clusters"][0]["missing"] == ["team", "pano"]


def test_coach_missing_team(db):
    sess = _session(db)
    _cluster(db, sess, coach=True, roles=("individual",))
    r = _compute_review_readiness(db, sess)
    assert r["ready"] is False
    assert r["incomplete_clusters"][0]["missing"] == ["team"]


def test_coach_does_not_require_pano(db):
    """Coach with only team (no pano) is ready — pano not required."""
    sess = _session(db)
    _cluster(db, sess, coach=True, roles=("team",))
    assert _compute_review_readiness(db, sess)["ready"] is True


def test_mixed_incomplete_lists_all(db):
    sess = _session(db)
    _cluster(db, sess, roles=("team", "panoramic"))      # ok
    _cluster(db, sess, roles=("panoramic",))             # missing team
    _cluster(db, sess, coach=True, roles=("individual",))  # coach missing team
    r = _compute_review_readiness(db, sess)
    assert r["ready"] is False
    assert len(r["incomplete_clusters"]) == 2


def test_endpoint_404(db):
    with pytest.raises(HTTPException) as exc:
        review_readiness(99999, db)
    assert exc.value.status_code == 404


def test_post_reviewed_blocked_without_force(db):
    sess = _session(db)
    _cluster(db, sess, roles=("team",))  # missing pano
    with pytest.raises(HTTPException) as exc:
        set_reviewed(sess.id, SetReviewedRequest(reviewed=True), db)
    assert exc.value.status_code == 400
    assert exc.value.detail["error"] == "session_not_ready"
    assert exc.value.detail["incomplete_clusters"]
    db.refresh(sess)
    assert not sess.reviewed  # not marked


def test_post_reviewed_force_succeeds(db, caplog):
    import logging
    sess = _session(db)
    _cluster(db, sess, roles=("team",))  # missing pano
    with caplog.at_level(logging.WARNING):
        res = set_reviewed(sess.id, SetReviewedRequest(reviewed=True, force=True), db)
    assert res["reviewed"] is True
    db.refresh(sess)
    assert sess.reviewed == 1
    assert any("force=true" in m for m in caplog.messages)


def test_post_unreviewed_never_gated(db):
    """Unmarking a session is always allowed regardless of completeness."""
    sess = _session(db)
    _cluster(db, sess, roles=("individual",))  # incomplete
    sess.reviewed = 1
    db.commit()
    res = set_reviewed(sess.id, SetReviewedRequest(reviewed=False), db)
    assert res["reviewed"] is False


def test_ready_session_marks_without_force(db):
    sess = _session(db)
    _cluster(db, sess, roles=("team", "panoramic"))
    res = set_reviewed(sess.id, SetReviewedRequest(reviewed=True), db)
    assert res["reviewed"] is True
