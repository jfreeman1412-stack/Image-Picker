"""Phase 10: cross-team naming-error detection.

A player name that shows up as clusters in 2+ teams is either one player
mis-foldered (same face) or two kids sharing a name because the camera's
copyright field wasn't updated (different face). Verdict comes from
centroid cosine distance vs cluster.DEFAULT_EPS (0.4).
"""
from datetime import datetime

import numpy as np
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models.db_models import (
    Cluster, Face, Image, Job, Session as DbSession,
)
from app.services.cluster import DEFAULT_EPS
from app.services.naming_errors import (
    centroid_distance, cluster_centroid, find_cross_team_name_collisions,
)
from app.services.roster import replace_job_roster


@pytest.fixture
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'naming-test.db'}",
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


def _basis_vec(idx, size=512):
    """Unit vector along axis `idx` — orthogonal basis vectors are an easy
    way to get cosine distance 0 (same) or 1 (different)."""
    v = np.zeros(size, dtype=np.float32)
    v[idx] = 1.0
    return v


def _emb_bytes(vec):
    v = np.asarray(vec, dtype=np.float32)
    v = v / np.linalg.norm(v)
    return v.astype(np.float32).tobytes()


def _job(db, *team_names):
    job = Job(name="J", root_path="/tmp", has_lines=0)
    db.add(job); db.commit(); db.refresh(job)
    sessions = []
    for t in team_names:
        s = DbSession(job_id=job.id, name=t, source_path=f"/tmp/{t}",
                      status="done", created_at=datetime.utcnow())
        db.add(s); db.commit(); db.refresh(s)
        sessions.append(s)
    return job, sessions


def _cluster(db, session, label, vec, n=3):
    c = Cluster(session_id=session.id, auto_label=label, image_count=n)
    db.add(c); db.commit(); db.refresh(c)
    for i in range(n):
        img = Image(session_id=session.id, path=f"/tmp/{c.id}_{i}.png",
                    filename=f"{c.id}_{i}.png")
        db.add(img); db.commit(); db.refresh(img)
        db.add(Face(image_id=img.id, cluster_id=c.id, bbox="[0,0,1,1]",
                    det_score=0.9, embedding=_emb_bytes(vec)))
    db.commit(); db.refresh(c)
    return c


# ── pure helpers ────────────────────────────────────────────────────────

def test_cluster_centroid_renormalizes():
    embs = [_normed(_basis_vec(0)), _normed(_basis_vec(0))]
    cen = cluster_centroid(embs)
    assert cen is not None
    assert abs(np.linalg.norm(cen) - 1.0) < 1e-5


def test_cluster_centroid_none_for_empty():
    assert cluster_centroid([]) is None


def test_centroid_distance_same_and_orthogonal():
    a = _normed(_basis_vec(0))
    b = _normed(_basis_vec(1))
    assert centroid_distance(a, a) < 1e-6          # identical → 0
    assert abs(centroid_distance(a, b) - 1.0) < 1e-6  # orthogonal → 1


def _normed(v):
    v = np.asarray(v, dtype=np.float32)
    return v / np.linalg.norm(v)


# ── detection ───────────────────────────────────────────────────────────

def test_same_face_two_teams_is_same_face_verdict(db):
    _, (a, b) = _job(db, "10U Black", "10U Orange")
    _cluster(db, a, "Jack-Smith", _basis_vec(0))
    _cluster(db, b, "Jack-Smith", _basis_vec(0))   # identical embeddings
    items = find_cross_team_name_collisions(db, a.job_id)
    assert len(items) == 1
    assert items[0]["verdict"] == "same_face"
    assert items[0]["max_distance"] < DEFAULT_EPS
    assert len(items[0]["clusters"]) == 2


def test_different_face_two_teams_is_naming_error(db):
    _, (a, b) = _job(db, "12AAA", "12U White")
    _cluster(db, a, "James-Wrzosek", _basis_vec(0))
    _cluster(db, b, "James-Wrzosek", _basis_vec(1))  # orthogonal → different
    items = find_cross_team_name_collisions(db, a.job_id)
    assert len(items) == 1
    assert items[0]["verdict"] == "different_face"
    assert items[0]["max_distance"] > DEFAULT_EPS


def test_same_session_duplicate_not_returned(db):
    """Two clusters with the same name in ONE session is the within-session
    duplicate_auto_label case (Phase 9), not a cross-team collision."""
    _, (a,) = _job(db, "10U Black")
    _cluster(db, a, "Jack-Smith", _basis_vec(0))
    _cluster(db, a, "Jack-Smith", _basis_vec(1))
    assert find_cross_team_name_collisions(db, a.job_id) == []


def test_name_in_single_session_not_returned(db):
    _, (a, b) = _job(db, "10U Black", "10U Orange")
    _cluster(db, a, "Solo-Player", _basis_vec(0))
    _cluster(db, b, "Other-Player", _basis_vec(1))
    assert find_cross_team_name_collisions(db, a.job_id) == []


def test_in_correct_team_flag(db):
    _, (a, b) = _job(db, "12AAA", "12U White")
    _cluster(db, a, "James-Wrzosek", _basis_vec(0))   # mis-foldered here
    _cluster(db, b, "James-Wrzosek", _basis_vec(1))   # this is the rostered team
    replace_job_roster(db, a.job_id, [("James-Wrzosek", "12U White")])
    db.commit()
    item = find_cross_team_name_collisions(db, a.job_id)[0]
    by_team = {c["session_name"]: c for c in item["clusters"]}
    assert by_team["12U White"]["in_correct_team"] is True
    assert by_team["12AAA"]["in_correct_team"] is False
    assert by_team["12AAA"]["roster_team_raw"] == "12U White"


def test_three_clusters_with_one_stranger_is_different_face(db):
    _, (a, b, c) = _job(db, "T1", "T2", "T3")
    _cluster(db, a, "Sam-Lee", _basis_vec(0))
    _cluster(db, b, "Sam-Lee", _basis_vec(0))   # matches a
    _cluster(db, c, "Sam-Lee", _basis_vec(7))   # stranger
    item = find_cross_team_name_collisions(db, a.job_id)[0]
    assert item["verdict"] == "different_face"
    assert len(item["clusters"]) == 3


def test_archived_session_excluded(db):
    _, (a, b) = _job(db, "10U Black", "10U Orange")
    b.archived = 1
    _cluster(db, a, "Jack-Smith", _basis_vec(0))
    _cluster(db, b, "Jack-Smith", _basis_vec(0))
    db.commit()
    # b is archived → only one live session has the name → not a collision.
    assert find_cross_team_name_collisions(db, a.job_id) == []


def test_no_roster_still_detects_collisions(db):
    _, (a, b) = _job(db, "12AAA", "12U White")
    _cluster(db, a, "James-Wrzosek", _basis_vec(0))
    _cluster(db, b, "James-Wrzosek", _basis_vec(1))
    items = find_cross_team_name_collisions(db, a.job_id)
    assert len(items) == 1
    # No roster → no expected team known.
    assert all(c["roster_team_raw"] is None for c in items[0]["clusters"])
    assert all(c["in_correct_team"] is False for c in items[0]["clusters"])


def test_different_face_sorts_before_same_face(db):
    _, (a, b) = _job(db, "T1", "T2")
    # collision 1: same face (Apple)
    _cluster(db, a, "Apple-Same", _basis_vec(0))
    _cluster(db, b, "Apple-Same", _basis_vec(0))
    # collision 2: different face (Zed)
    _cluster(db, a, "Zed-Diff", _basis_vec(2))
    _cluster(db, b, "Zed-Diff", _basis_vec(3))
    items = find_cross_team_name_collisions(db, a.job_id)
    assert [it["verdict"] for it in items] == ["different_face", "same_face"]
