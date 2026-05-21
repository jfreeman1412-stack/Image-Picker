"""Phase A.4 §5 — read-only global-match suggestion endpoint.

POST /api/clusters/{cluster_id}/global-match-suggest ignores roster scope and
returns top-N global suggestions, mutating nothing. See
PHASE_A4_PIPELINE_INTEGRATION.md.
"""
from contextlib import contextmanager
from datetime import datetime

import numpy as np
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base, get_db
from app.main import app
from app.models.db_models import (
    Cluster, Face, Image, Job, Player, ReferenceFace, Session as DbSession,
)


def _vec(*coords) -> np.ndarray:
    v = np.zeros(512, dtype=np.float32)
    for i, c in enumerate(coords):
        v[i] = c
    return v


E0 = _vec(1.0)


@contextmanager
def _http(tmp_path, name):
    engine = create_engine(
        f"sqlite:///{tmp_path / name}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    SL = sessionmaker(bind=engine)

    def _override():
        s = SL()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override
    try:
        yield SL, TestClient(app)
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _seed_cluster_with_faces(seed, *, face_vecs=(E0,), auto_label=None):
    job = Job(name="J", root_path="/tmp"); seed.add(job); seed.commit(); seed.refresh(job)
    sess = DbSession(job_id=job.id, name="T", source_path="/tmp/T",
                     status="done", created_at=datetime.utcnow())
    seed.add(sess); seed.commit(); seed.refresh(sess)
    c = Cluster(session_id=sess.id, auto_label=auto_label, image_count=0)
    seed.add(c); seed.commit(); seed.refresh(c)
    for v in face_vecs:
        img = Image(session_id=sess.id, path="/tmp/i.jpg", filename="i.jpg")
        seed.add(img); seed.commit(); seed.refresh(img)
        seed.add(Face(image_id=img.id, bbox="[0,0,10,10]",
                      embedding=v.astype(np.float32).tobytes(),
                      det_score=0.9, cluster_id=c.id))
    seed.commit()
    return c.id


def _seed_global_player(seed, name="Zed", ref=E0):
    """A player with a reference but NO membership for the cluster's job — the
    roster scope would never match them; the global endpoint can."""
    p = Player(norm_name=name.lower(), display_name=name)
    seed.add(p); seed.flush()
    seed.add(ReferenceFace(player_id=p.id, image_path="/tmp/r.jpg",
                           embedding=ref.astype(np.float32).tobytes(), det_score=0.9))
    seed.commit()
    return p.id


def test_global_suggest_reaches_unrostered_player(tmp_path):
    with _http(tmp_path, "g1.db") as (SL, client):
        seed = SL()
        try:
            cid = _seed_cluster_with_faces(seed, face_vecs=(E0,))
            zed_id = _seed_global_player(seed, "Zed", E0)
        finally:
            seed.close()
        res = client.post(f"/api/clusters/{cid}/global-match-suggest")
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["scope"] == "global_fallback"
        assert body["tier"] == "high"
        assert body["player_id"] == zed_id
        assert body["candidates"]                  # top-N populated
        assert body["thresholds"]["high"] == 0.6


def test_global_suggest_does_not_mutate_cluster(tmp_path):
    with _http(tmp_path, "g2.db") as (SL, client):
        seed = SL()
        try:
            cid = _seed_cluster_with_faces(seed, face_vecs=(E0,), auto_label="orig")
            _seed_global_player(seed, "Zed", E0)
        finally:
            seed.close()
        res = client.post(f"/api/clusters/{cid}/global-match-suggest")
        assert res.status_code == 200
        check = SL()
        try:
            c = check.query(Cluster).get(cid)
            assert c.matched_player_id is None       # nothing written
            assert c.match_tier is None
            assert c.match_scope is None
            assert c.auto_label == "orig"
            assert (c.is_likely_coach or 0) == 0
            assert c.review_reason is None
        finally:
            check.close()


def test_global_suggest_unknown_cluster_404(tmp_path):
    with _http(tmp_path, "g3.db") as (SL, client):
        res = client.post("/api/clusters/99999/global-match-suggest")
        assert res.status_code == 404


def test_global_suggest_empty_cluster_returns_none(tmp_path):
    with _http(tmp_path, "g4.db") as (SL, client):
        seed = SL()
        try:
            cid = _seed_cluster_with_faces(seed, face_vecs=())   # no faces
            _seed_global_player(seed, "Zed", E0)
        finally:
            seed.close()
        res = client.post(f"/api/clusters/{cid}/global-match-suggest")
        assert res.status_code == 200
        body = res.json()
        assert body["tier"] == "none"
        assert body["scope"] == "global_fallback"
        assert body["player_id"] is None
