"""Phase A.4 §3 — the matching stage wired into run_pipeline.

Follows the existing pipeline-test pattern (test_pipeline_errors.py): monkeypatch
face_detector.detect_faces / clustering.cluster_embeddings /
expression.classify_expression so no model loads. Asserts the matching stage
runs once, after labeling and before sorting, and that an end-to-end run
gap-fills a confident match. See PHASE_A4_PIPELINE_INTEGRATION.md.
"""
import math

import numpy as np
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models.db_models import (
    Cluster, Job, Player, PlayerMembership, ReferenceFace,
    Image, Session as DbSession,
)
from app.services import face_pipeline
from app.services.face_pipeline import run_pipeline
from app.services.roster import normalize_name


def _vec(*coords) -> np.ndarray:
    v = np.zeros(512, dtype=np.float32)
    for i, c in enumerate(coords):
        v[i] = c
    return v


def _ref_at(sim: float) -> np.ndarray:
    return _vec(sim, math.sqrt(max(0.0, 1.0 - sim * sim)))


E0 = _vec(1.0)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'pipeline-matching.db'}",
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


def _detection(embedding):
    return {
        "bbox": [0, 0, 10, 10],
        "embedding": embedding,
        "det_score": 0.9,
        "age": 15.0,            # a kid → age coach-rule never fires
        "yaw": 0.0,
        "pitch": 0.0,
        "face_area_ratio": 0.2,
    }


def _stub_detector(monkeypatch, embedding):
    """detect_faces → one face (embedding) per image. cluster_embeddings → one
    cluster. classify_expression → deterministic. No model loads."""
    monkeypatch.setattr(
        face_pipeline.face_detector, "detect_faces",
        lambda path: [_detection(embedding)],
    )
    monkeypatch.setattr(
        face_pipeline.clustering, "cluster_embeddings",
        lambda embs: [0] * len(embs),     # all faces → single cluster
    )
    monkeypatch.setattr(
        face_pipeline.expression, "classify_expression",
        lambda path, bbox: ("serious", 0.5),
    )


def _seed_job_session(db, *, team="T", n_images=3, copyright_tag=None):
    job = Job(name="J", root_path="/tmp/J")
    db.add(job); db.flush()
    sess = DbSession(job_id=job.id, name=team, source_path="/tmp/J/T", status="pending")
    db.add(sess); db.commit(); db.refresh(sess)
    for i in range(n_images):
        db.add(Image(session_id=sess.id, path=f"/tmp/J/T/{i}.jpg",
                     filename=f"{i}.jpg", copyright_tag=copyright_tag))
    db.commit()
    return job, sess


def test_matching_stage_runs_after_labeling_before_sorting(db, monkeypatch):
    job, sess = _seed_job_session(db, n_images=3, copyright_tag="Alice")
    _stub_detector(monkeypatch, E0)

    calls = []
    seen = {}

    def spy_match(db_, session_):
        calls.append("match")
        seen["labels"] = [
            c.auto_label
            for c in db_.query(Cluster).filter_by(session_id=session_.id).all()
        ]

    orig_sort = face_pipeline._sort_cluster

    def spy_sort(db_, c):
        calls.append("sort")
        return orig_sort(db_, c)

    monkeypatch.setattr(face_pipeline, "match_session_clusters", spy_match)
    monkeypatch.setattr(face_pipeline, "_sort_cluster", spy_sort)

    run_pipeline(db, sess.id)

    assert calls.count("match") == 1
    assert "sort" in calls
    assert calls.index("match") < calls.index("sort")   # matching before sorting
    assert seen["labels"] == ["Alice"]                  # labeling ran before matching


def test_end_to_end_gap_fill(db, monkeypatch):
    job, sess = _seed_job_session(db, n_images=2, copyright_tag=None)
    alice = Player(norm_name="alice", display_name="Alice")
    db.add(alice); db.flush()
    db.add(ReferenceFace(player_id=alice.id, image_path="/tmp/r.jpg",
                         embedding=E0.tobytes(), det_score=0.9))
    db.add(PlayerMembership(player_id=alice.id, job_id=job.id, team_name="T",
                            norm_team=normalize_name("T"), is_coach=0))
    db.commit()

    _stub_detector(monkeypatch, E0)        # faces == reference → cosine 1.0
    run_pipeline(db, sess.id)

    sess_after = db.query(DbSession).get(sess.id)
    assert sess_after.status == "done"
    cluster = db.query(Cluster).filter_by(session_id=sess.id).one()
    assert cluster.match_tier == "high"
    assert cluster.match_scope == "roster"
    assert cluster.matched_player_id == alice.id
    assert cluster.auto_label == "Alice"          # gap-filled (no copyright)
    assert cluster.auto_label_source == "match"


def test_end_to_end_no_references_completes(db, monkeypatch):
    """A shoot with a roster but no reference photos still completes cleanly —
    every cluster resolves to tier 'none', nothing is gap-filled."""
    job, sess = _seed_job_session(db, n_images=2, copyright_tag=None)
    alice = Player(norm_name="alice", display_name="Alice")
    db.add(alice); db.flush()
    db.add(PlayerMembership(player_id=alice.id, job_id=job.id, team_name="T",
                            norm_team=normalize_name("T"), is_coach=0))
    db.commit()

    _stub_detector(monkeypatch, E0)
    run_pipeline(db, sess.id)

    sess_after = db.query(DbSession).get(sess.id)
    assert sess_after.status == "done"
    cluster = db.query(Cluster).filter_by(session_id=sess.id).one()
    assert cluster.match_scope == "roster"
    assert cluster.match_tier == "none"
    assert cluster.matched_player_id is None
    assert cluster.auto_label is None
