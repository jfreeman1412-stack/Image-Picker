"""F1 regression test: /set-role response when the cluster was moved.

Bug diagnosed 2026-09-14 (see conversation trail after Fix A): after a
successful move-with-guards, the cluster's session_id is updated in
place. A follow-up POST /api/sessions/{OLD_session_id}/set-role with
the same cluster_id used to fall through the (id, session_id) filter
and return a bare 404 "Cluster not found" — technically correct but
useless to the operator, who saw "That change didn't save (HTTP 404)"
and had no idea the cluster was fine and living on a different team's
page. F1 upgrades this specific case to 409 error='cluster_moved' with
a message the frontend can surface as "cluster moved — refreshed"
instead. Genuinely-nonexistent clusters still return 404.

Companion frontend fix F2 optimistically clears the moved cluster from
local state so the race window that triggers this 409 rarely fires in
single-user single-tab use — but the backend guard catches multi-tab,
multi-user, and RosterModal cases.
"""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.cluster_move import MoveRequest, move_cluster
from app.db import Base, get_db
from app.main import app
from app.models.db_models import (
    Cluster, Face, Image, ImageRole, Job, Session,
)


@pytest.fixture
def client(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'setrole-stale.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(bind=engine)

    def _override_get_db():
        s = TestingSessionLocal()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override_get_db
    try:
        yield TestClient(app), TestingSessionLocal
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _seed(SessionLocal):
    """Create a job with source + target sessions, cluster in source
    with one single-face image. Returns (source_id, target_id, cluster_id,
    image_id) so tests can hit endpoints and observe post-move state."""
    db = SessionLocal()
    try:
        job = Job(name="J", root_path="/tmp", has_lines=0)
        db.add(job); db.commit(); db.refresh(job)
        src = Session(job_id=job.id, name="Source Team",
                      source_path="/tmp/src", status="done",
                      created_at=datetime.utcnow())
        tgt = Session(job_id=job.id, name="Target Team",
                      source_path="/tmp/tgt", status="done",
                      created_at=datetime.utcnow())
        db.add_all([src, tgt]); db.commit()
        db.refresh(src); db.refresh(tgt)

        c = Cluster(session_id=src.id, image_count=1, auto_label="Alice")
        db.add(c); db.commit(); db.refresh(c)

        img = Image(
            session_id=src.id, path="/tmp/src/alice.jpg", filename="alice.jpg",
            capture_time=datetime(2026, 1, 1, 12, 0, 0),
        )
        db.add(img); db.commit(); db.refresh(img)
        db.add(Face(
            image_id=img.id, cluster_id=c.id, bbox="[0,0,10,10]",
            det_score=0.95, expression="smiling", age=10.0,
            yaw=0.0, pitch=0.0, face_area_ratio=0.15,
        ))
        db.commit()
        return src.id, tgt.id, c.id, img.id
    finally:
        db.close()


def _move_cluster_directly(SessionLocal, cluster_id, target_session_id):
    """Call the move_cluster service directly (bypassing the HTTP layer)
    to relocate the cluster from source to target. Same effect as
    /clusters/{id}/move-with-guards mode='create' — cluster.session_id
    mutates, owned images relocate."""
    db = SessionLocal()
    try:
        move_cluster(
            cluster_id,
            MoveRequest(target_session_id=target_session_id, mode="create"),
            db,
        )
    finally:
        db.close()


# ── Tests ────────────────────────────────────────────────────────────────


def test_setrole_after_move_returns_409_cluster_moved(client):
    """After the cluster's session_id has changed, set-role targeting the
    OLD session must return 409 error='cluster_moved' with the current
    session_id in the detail so the frontend can auto-navigate/refresh."""
    tc, SessionLocal = client
    src_id, tgt_id, cluster_id, image_id = _seed(SessionLocal)

    # Sanity: set-role against source works BEFORE the move.
    pre = tc.post(f"/api/sessions/{src_id}/set-role", json={
        "image_id": image_id, "cluster_id": cluster_id, "role": "team",
    })
    assert pre.status_code == 200, pre.text

    # Move the cluster to target. cluster.session_id changes in place.
    _move_cluster_directly(SessionLocal, cluster_id, tgt_id)

    # Now set-role against the OLD session — F1: 409 cluster_moved.
    r = tc.post(f"/api/sessions/{src_id}/set-role", json={
        "image_id": image_id, "cluster_id": cluster_id, "role": "panoramic",
    })
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "cluster_moved"
    assert detail["cluster_id"] == cluster_id
    assert detail["current_session_id"] == tgt_id
    assert "moved" in detail["message"].lower()

    # Sanity: set-role against the CORRECT (target) session works — F1
    # only rejects the stale-session-id case; legitimate edits at the
    # cluster's current location succeed unchanged.
    r_ok = tc.post(f"/api/sessions/{tgt_id}/set-role", json={
        "image_id": image_id, "cluster_id": cluster_id, "role": "panoramic",
    })
    assert r_ok.status_code == 200, r_ok.text


def test_setrole_genuinely_missing_cluster_still_returns_404(client):
    """F1 must NOT swallow the pre-existing 404 case: a cluster_id that
    doesn't exist anywhere in the DB is still 404, not 409. Guards
    against a lookup regression where the by-id-only query would return
    something and misroute the response."""
    tc, SessionLocal = client
    src_id, _, _, image_id = _seed(SessionLocal)

    r = tc.post(f"/api/sessions/{src_id}/set-role", json={
        "image_id": image_id, "cluster_id": 999999, "role": "team",
    })
    assert r.status_code == 404
    # Body is a plain string detail from HTTPException, not a dict.
    assert "not found" in r.text.lower()


def test_setrole_same_session_still_works(client):
    """Regression: F1's by-id-then-check-session refactor must not
    change behavior for the happy path — cluster still in its original
    session, set-role returns 200 with the applied role."""
    tc, SessionLocal = client
    src_id, _, cluster_id, image_id = _seed(SessionLocal)

    r = tc.post(f"/api/sessions/{src_id}/set-role", json={
        "image_id": image_id, "cluster_id": cluster_id, "role": "team",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "set"
    assert body["role"] == "team"
