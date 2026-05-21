"""Phase A.4 §4 — exposing match data + the match_team_mismatch validation gate.

Covers the read-time helper (cluster_match_team_mismatch / add_match_team_mismatch_flag),
the additive `match` block + flag splice in GET /clusters (existing fields
unchanged), flag-visibility filtering of the new flags, and the readiness-gate
extension. See PHASE_A4_PIPELINE_INTEGRATION.md.
"""
import json
from contextlib import contextmanager
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.sessions import _compute_review_readiness
from app.api.settings import _FLAG_VIS_KEY, KNOWN_FLAGS
from app.db import Base, get_db
from app.main import app
from app.models.db_models import (
    Cluster, Image, ImageRole, Job, Player, PlayerMembership,
    Session as DbSession, Setting,
)
from app.services.roster import normalize_name
from app.services.roster_check import (
    MATCH_TEAM_MISMATCH, add_match_team_mismatch_flag,
    cluster_match_team_mismatch,
)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'match-exposure.db'}",
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


# ── cluster_match_team_mismatch (pure) ─────────────────────────────────────

def _c(tier, pid):
    return Cluster(match_tier=tier, matched_player_id=pid)


def test_mismatch_high_same_team_false():
    assert cluster_match_team_mismatch(_c("high", 5), "teamy", {5: {"teamy"}}) is False


def test_mismatch_high_different_team_true():
    assert cluster_match_team_mismatch(_c("high", 5), "teamy", {5: {"teamx"}}) is True


def test_mismatch_low_tier_abstains():
    assert cluster_match_team_mismatch(_c("low", 5), "teamy", {5: {"teamx"}}) is False


def test_mismatch_none_tier_abstains():
    assert cluster_match_team_mismatch(_c("none", None), "teamy", {}) is False


def test_mismatch_player_not_rostered_for_job_abstains():
    # matched player (99) isn't in this job's roster (e.g. global fallback).
    assert cluster_match_team_mismatch(_c("high", 99), "teamy", {5: {"teamx"}}) is False


def test_add_match_team_mismatch_flag():
    assert add_match_team_mismatch_flag(None, True) == MATCH_TEAM_MISMATCH
    assert add_match_team_mismatch_flag("outlier_high", True) == \
        f"outlier_high,{MATCH_TEAM_MISMATCH}"
    assert add_match_team_mismatch_flag(f"x,{MATCH_TEAM_MISMATCH}", True) == \
        f"x,{MATCH_TEAM_MISMATCH}"
    assert add_match_team_mismatch_flag("x", False) == "x"
    assert add_match_team_mismatch_flag(None, False) is None


def test_new_flags_registered():
    for flag in ("match_label_conflict", "low_confidence_match", "match_team_mismatch"):
        assert flag in KNOWN_FLAGS


# ── GET /clusters: match block + flag splice ───────────────────────────────

def _seed_matched_cluster(seed, *, sess_team, player_team, tier="high",
                          confidence=0.83, review_reason=None):
    job = Job(name="J", root_path="/tmp"); seed.add(job); seed.commit(); seed.refresh(job)
    sess = DbSession(job_id=job.id, name=sess_team, source_path="/tmp/x",
                     status="done", created_at=datetime.utcnow())
    seed.add(sess); seed.commit(); seed.refresh(sess)
    smith = Player(norm_name="smith", display_name="Smith")
    seed.add(smith); seed.flush()
    seed.add(PlayerMembership(
        player_id=smith.id, job_id=job.id, team_name=player_team,
        norm_team=normalize_name(player_team), is_coach=0))
    c = Cluster(session_id=sess.id, image_count=0, matched_player_id=smith.id,
                match_tier=tier, match_scope="roster", match_confidence=confidence,
                review_reason=review_reason)
    seed.add(c); seed.commit(); seed.refresh(c)
    return sess.id, smith.id, c.id


_EXISTING_CLUSTER_KEYS = {
    "cluster_id", "label", "image_count", "needs_review", "review_reason",
    "visible_review_reasons", "is_likely_coach", "is_coach_for_sort",
    "manual_coach_override", "guest_of", "images",
}


def test_clusters_match_block_team_mismatch(tmp_path):
    with _http(tmp_path, "mm.db") as (SL, client):
        seed = SL()
        try:
            sid, smith_id, _ = _seed_matched_cluster(
                seed, sess_team="12U-Orange", player_team="11U-Black")
        finally:
            seed.close()
        body = client.get(f"/api/sessions/{sid}/clusters").json()
        assert len(body) == 1
        cl = body[0]
        # existing fields all still present (additive change only)
        assert _EXISTING_CLUSTER_KEYS <= set(cl)
        m = cl["match"]
        assert m["player_id"] == smith_id
        assert m["player_name"] == "Smith"
        assert m["tier"] == "high"
        assert m["scope"] == "roster"
        assert m["confidence"] == 0.83
        assert m["team_mismatch"] is True
        assert m["roster_team"] == "11U-Black"
        assert "match_team_mismatch" in cl["visible_review_reasons"]


def test_clusters_match_block_same_team_no_flag(tmp_path):
    with _http(tmp_path, "mm-same.db") as (SL, client):
        seed = SL()
        try:
            sid, _, _ = _seed_matched_cluster(
                seed, sess_team="12U-Orange", player_team="12U-Orange")
        finally:
            seed.close()
        cl = client.get(f"/api/sessions/{sid}/clusters").json()[0]
        assert cl["match"]["team_mismatch"] is False
        assert "match_team_mismatch" not in cl["visible_review_reasons"]


def test_clusters_match_team_mismatch_hidden_when_flag_off(tmp_path):
    with _http(tmp_path, "mm-hidden.db") as (SL, client):
        seed = SL()
        try:
            sid, _, _ = _seed_matched_cluster(
                seed, sess_team="12U-Orange", player_team="11U-Black")
            seed.add(Setting(key=_FLAG_VIS_KEY,
                             value=json.dumps({"match_team_mismatch": False})))
            seed.commit()
        finally:
            seed.close()
        cl = client.get(f"/api/sessions/{sid}/clusters").json()[0]
        # The chip is filtered out, but the structured data flag is unaffected.
        assert "match_team_mismatch" not in cl["visible_review_reasons"]
        assert cl["match"]["team_mismatch"] is True


def test_clusters_stored_match_flags_visible_then_hidden(tmp_path):
    with _http(tmp_path, "mm-stored.db") as (SL, client):
        seed = SL()
        try:
            sid, _, _ = _seed_matched_cluster(
                seed, sess_team="12U-Orange", player_team="12U-Orange",
                review_reason="match_label_conflict,low_confidence_match")
        finally:
            seed.close()
        cl = client.get(f"/api/sessions/{sid}/clusters").json()[0]
        assert "match_label_conflict" in cl["visible_review_reasons"]
        assert "low_confidence_match" in cl["visible_review_reasons"]

    with _http(tmp_path, "mm-stored2.db") as (SL, client):
        seed = SL()
        try:
            sid, _, _ = _seed_matched_cluster(
                seed, sess_team="12U-Orange", player_team="12U-Orange",
                review_reason="match_label_conflict,low_confidence_match")
            seed.add(Setting(key=_FLAG_VIS_KEY,
                             value=json.dumps({"match_label_conflict": False})))
            seed.commit()
        finally:
            seed.close()
        cl = client.get(f"/api/sessions/{sid}/clusters").json()[0]
        assert "match_label_conflict" not in cl["visible_review_reasons"]
        assert "low_confidence_match" in cl["visible_review_reasons"]


# ── readiness-gate extension ───────────────────────────────────────────────

def _job_session_player(db, *, sess_team="12U-Orange", player_team="11U-Black"):
    job = Job(name="J", root_path="/tmp"); db.add(job); db.commit(); db.refresh(job)
    sess = DbSession(job_id=job.id, name=sess_team, source_path="/tmp/x",
                     status="done", created_at=datetime.utcnow())
    db.add(sess); db.commit(); db.refresh(sess)
    p = Player(norm_name="smith", display_name="Smith"); db.add(p); db.flush()
    db.add(PlayerMembership(player_id=p.id, job_id=job.id, team_name=player_team,
                            norm_team=normalize_name(player_team), is_coach=0))
    db.commit()
    return job, sess, p


def _role_complete_matched_cluster(db, sess, *, matched_player_id, tier="high"):
    """Cluster with team+pano roles (passes the role gate) and a stored match."""
    c = Cluster(session_id=sess.id, image_count=2, matched_player_id=matched_player_id,
                match_tier=tier, match_scope="roster")
    db.add(c); db.commit(); db.refresh(c)
    img1 = Image(session_id=sess.id, path=f"/tmp/{c.id}-t.png", filename=f"{c.id}-t.png")
    img2 = Image(session_id=sess.id, path=f"/tmp/{c.id}-p.png", filename=f"{c.id}-p.png")
    db.add_all([img1, img2]); db.commit(); db.refresh(img1); db.refresh(img2)
    db.add(ImageRole(image_id=img1.id, cluster_id=c.id, role="team"))
    db.add(ImageRole(image_id=img2.id, cluster_id=c.id, role="panoramic"))
    db.commit()
    return c


def test_readiness_blocks_on_visible_match_team_mismatch(db):
    _, sess, p = _job_session_player(db)
    _role_complete_matched_cluster(db, sess, matched_player_id=p.id)
    r = _compute_review_readiness(db, sess)
    assert r["ready"] is False
    rows = [x for x in r["incomplete_clusters"]
            if MATCH_TEAM_MISMATCH in x["missing"]]
    assert len(rows) == 1


def test_readiness_passes_when_match_flag_hidden(db):
    _, sess, p = _job_session_player(db)
    _role_complete_matched_cluster(db, sess, matched_player_id=p.id)
    db.add(Setting(key=_FLAG_VIS_KEY,
                   value=json.dumps({MATCH_TEAM_MISMATCH: False})))
    db.commit()
    r = _compute_review_readiness(db, sess)
    assert r["ready"] is True


def test_readiness_ready_when_match_is_same_team(db):
    _, sess, p = _job_session_player(db, sess_team="12U-Orange", player_team="12U-Orange")
    _role_complete_matched_cluster(db, sess, matched_player_id=p.id)
    r = _compute_review_readiness(db, sess)
    assert r["ready"] is True


def test_readiness_low_tier_match_does_not_block(db):
    _, sess, p = _job_session_player(db)
    _role_complete_matched_cluster(db, sess, matched_player_id=p.id, tier="low")
    r = _compute_review_readiness(db, sess)
    assert r["ready"] is True
