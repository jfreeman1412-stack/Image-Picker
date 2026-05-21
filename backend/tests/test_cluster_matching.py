"""Phase A.4 §2 — the matching pipeline stage (services/cluster_matching.py).

Seed-and-call: build Job/Session/Image/Face/Player/ReferenceFace/PlayerMembership
rows directly and call match_session_clusters, then assert on the Cluster rows.
No detector, no clustering, no model — synthetic unit vectors (the A.3
2-D-in-512-D trick). See PHASE_A4_PIPELINE_INTEGRATION.md.
"""
import logging
import math

import numpy as np
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models.db_models import (
    Cluster, Face, Image, Job, Player, PlayerMembership,
    ReferenceFace, Session as DbSession,
)
from app.services import cluster_matching
from app.services.roster import normalize_name


# ── synthetic unit-vector helpers (cosine(QUERY, _ref_at(s)) == s) ─────────

def _vec(*coords) -> np.ndarray:
    v = np.zeros(512, dtype=np.float32)
    for i, c in enumerate(coords):
        v[i] = c
    return v


def _ref_at(sim: float) -> np.ndarray:
    return _vec(sim, math.sqrt(max(0.0, 1.0 - sim * sim)))


QUERY = _vec(1.0)   # == _ref_at(1.0); cosine with _ref_at(s) is s


@pytest.fixture
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'cluster-matching.db'}",
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


def _job_session(db, *, team="T", with_job=True):
    job = None
    job_id = None
    if with_job:
        job = Job(name="J", root_path="/tmp/J")
        db.add(job); db.flush()
        job_id = job.id
    sess = DbSession(job_id=job_id, name=team, source_path="/tmp/J/T", status="done")
    db.add(sess); db.commit(); db.refresh(sess)
    return job, sess


def _player(db, name, *, refs=(), job_id=None, team="T", is_coach=False) -> Player:
    p = Player(norm_name=name.lower().replace("-", ""), display_name=name)
    db.add(p); db.flush()
    for s in refs:
        db.add(ReferenceFace(
            player_id=p.id, image_path="/tmp/r.jpg",
            embedding=_ref_at(s).tobytes(), det_score=0.9,
        ))
    if job_id is not None:
        db.add(PlayerMembership(
            player_id=p.id, job_id=job_id, team_name=team,
            norm_team=normalize_name(team), is_coach=1 if is_coach else 0,
        ))
    db.commit()
    return p


def _cluster(db, session, face_vecs, *, auto_label=None) -> Cluster:
    c = Cluster(session_id=session.id, auto_label=auto_label, image_count=0)
    db.add(c); db.flush()
    for v in face_vecs:
        img = Image(session_id=session.id, path="/tmp/i.jpg", filename="i.jpg")
        db.add(img); db.flush()
        db.add(Face(
            image_id=img.id, bbox="[0,0,10,10]",
            embedding=v.astype(np.float32).tobytes(), det_score=0.9,
            cluster_id=c.id,
        ))
    db.commit()
    return c


def _run(db, sess):
    cluster_matching.match_session_clusters(db, sess)
    db.commit()


# ── label precedence (decision 2) ─────────────────────────────────────────

def test_gap_fill_no_copyright(db):
    job, sess = _job_session(db)
    alice = _player(db, "Alice", refs=[1.0], job_id=job.id)
    c = _cluster(db, sess, [QUERY], auto_label=None)   # cosine 1.0 → high
    _run(db, sess)
    db.refresh(c)
    assert c.match_tier == "high"
    assert c.matched_player_id == alice.id
    assert c.auto_label == "Alice"
    assert c.auto_label_source == "match"
    assert c.match_scope == "roster"
    assert (c.review_reason or "") == ""        # no flag on the clean path


def test_agree_no_flag(db):
    job, sess = _job_session(db)
    _player(db, "Alice", refs=[1.0], job_id=job.id)
    c = _cluster(db, sess, [QUERY], auto_label="Alice")   # copyright agrees
    _run(db, sess)
    db.refresh(c)
    assert c.auto_label == "Alice"
    assert c.auto_label_source != "match"       # left as-is (copyright origin)
    assert "match_label_conflict" not in (c.review_reason or "")


def test_conflict_keeps_copyright_and_flags(db):
    job, sess = _job_session(db)
    alice = _player(db, "Alice", refs=[1.0], job_id=job.id)
    c = _cluster(db, sess, [QUERY], auto_label="Bob")     # copyright disagrees
    _run(db, sess)
    db.refresh(c)
    assert c.matched_player_id == alice.id
    assert c.auto_label == "Bob"                # copyright wins
    assert "match_label_conflict" in (c.review_reason or "")


def test_low_tier_flag_no_label(db):
    job, sess = _job_session(db)
    _player(db, "Alice", refs=[0.5], job_id=job.id)   # cosine 0.5 → low
    c = _cluster(db, sess, [QUERY], auto_label=None)
    _run(db, sess)
    db.refresh(c)
    assert c.match_tier == "low"
    assert "low_confidence_match" in (c.review_reason or "")
    assert c.auto_label is None
    assert c.auto_label_source is None


def test_none_tier_no_change(db):
    job, sess = _job_session(db)
    _player(db, "Alice", refs=[0.3], job_id=job.id)   # cosine 0.3 → none
    c = _cluster(db, sess, [QUERY], auto_label=None)
    _run(db, sess)
    db.refresh(c)
    assert c.match_tier == "none"
    assert c.matched_player_id is None
    assert c.auto_label is None
    assert (c.review_reason or "") == ""


# ── coach signal (decision 3) ──────────────────────────────────────────────

def test_coach_promotion_high(db):
    job, sess = _job_session(db)
    _player(db, "Coach-Bob", refs=[1.0], job_id=job.id, is_coach=True)
    c = _cluster(db, sess, [QUERY], auto_label=None)
    assert (c.is_likely_coach or 0) == 0       # not a coach before matching
    _run(db, sess)
    db.refresh(c)
    assert c.match_tier == "high"
    assert c.is_likely_coach == 1              # promoted by the roster-coach match


def test_coach_promotion_high_only(db):
    job, sess = _job_session(db)
    _player(db, "Coach-Bob", refs=[0.5], job_id=job.id, is_coach=True)  # low
    c = _cluster(db, sess, [QUERY], auto_label=None)
    _run(db, sess)
    db.refresh(c)
    assert c.match_tier == "low"
    assert (c.is_likely_coach or 0) == 0       # low-tier never promotes


# ── candidate scope (decision 1) ───────────────────────────────────────────

def test_roster_scope_excludes_other_job_player(db):
    job, sess = _job_session(db)
    other = Job(name="J2", root_path="/tmp/J2"); db.add(other); db.commit()
    rostered = _player(db, "Alice", refs=[0.65], job_id=job.id)   # high, in roster
    # Outsider with a PERFECT reference but rostered only on another job:
    _player(db, "Zed", refs=[1.0], job_id=other.id)
    c = _cluster(db, sess, [QUERY], auto_label=None)
    _run(db, sess)
    db.refresh(c)
    assert c.match_scope == "roster"
    assert c.matched_player_id == rostered.id     # never the (excluded) outsider


def test_global_fallback_and_log(db, caplog):
    _, sess = _job_session(db, with_job=False)    # job_id None → no roster
    alice = _player(db, "Alice", refs=[1.0])      # global reference, no membership
    c = _cluster(db, sess, [QUERY], auto_label=None)
    with caplog.at_level(logging.WARNING):
        _run(db, sess)
    db.refresh(c)
    assert c.match_scope == "global_fallback"
    assert c.matched_player_id == alice.id
    assert "falling back to global match" in caplog.text


def test_job_with_empty_roster_falls_back(db):
    job, sess = _job_session(db)                  # job exists, zero memberships
    _player(db, "Alice", refs=[1.0])              # global ref, not in this roster
    c = _cluster(db, sess, [QUERY], auto_label=None)
    _run(db, sess)
    db.refresh(c)
    assert c.match_scope == "global_fallback"


def test_global_fallback_no_coach_promotion(db):
    other = Job(name="J2", root_path="/tmp/J2"); db.add(other); db.commit()
    _, sess = _job_session(db, with_job=False)    # no roster → fallback
    coach_elsewhere = _player(
        db, "Coach-X", refs=[1.0], job_id=other.id, is_coach=True)
    c = _cluster(db, sess, [QUERY], auto_label=None)
    _run(db, sess)
    db.refresh(c)
    assert c.match_scope == "global_fallback"
    assert c.matched_player_id == coach_elsewhere.id
    assert (c.is_likely_coach or 0) == 0          # coach status unknown in fallback


def test_empty_scoped_index_stays_roster_none(db):
    job, sess = _job_session(db)
    _player(db, "Alice", refs=[], job_id=job.id)  # rostered but no references
    c = _cluster(db, sess, [QUERY], auto_label=None)
    _run(db, sess)
    db.refresh(c)
    assert c.match_scope == "roster"              # NOT global fallback
    assert c.match_tier == "none"
    assert c.matched_player_id is None


# ── aggregation + idempotency ──────────────────────────────────────────────

def test_multi_face_cluster_max_high(db):
    job, sess = _job_session(db)
    alice = _player(db, "Alice", refs=[1.0], job_id=job.id)   # ref == e0
    c = _cluster(db, sess, [_ref_at(0.5), _ref_at(0.7)])      # faces 0.5 & 0.7
    _run(db, sess)
    db.refresh(c)
    assert c.match_tier == "high"
    assert c.match_confidence == pytest.approx(0.7, abs=1e-3)  # MAX over faces
    assert c.matched_player_id == alice.id


def test_empty_cluster_left_untouched(db):
    job, sess = _job_session(db)
    _player(db, "Alice", refs=[1.0], job_id=job.id)
    c = _cluster(db, sess, [])                    # no faces
    _run(db, sess)
    db.refresh(c)
    assert c.match_tier is None
    assert c.matched_player_id is None
    assert c.match_scope is None


def test_rerun_idempotent(db):
    job, sess = _job_session(db)
    _player(db, "Bob", refs=[1.0], job_id=job.id)
    c = _cluster(db, sess, [QUERY], auto_label="Carl")   # conflict → flag
    _run(db, sess)
    db.refresh(c)
    first = c.review_reason
    _run(db, sess)
    db.refresh(c)
    assert c.review_reason == first
    assert (c.review_reason or "").count("match_label_conflict") == 1
