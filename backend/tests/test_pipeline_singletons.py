"""Issue 5 — singleton promotion post-DBSCAN.

DBSCAN with min_samples=2 labels lone faces -1 (noise). The pre-Issue-5
pipeline left those faces with cluster_id=None → no Cluster row → no review
card in the UI → coach with 1 photo went silently uncarded (their image still
landed in To_be_Cropped via the orphan rule, but reviewers couldn't see them
in the cluster grid).

Issue 5: every noise face becomes its own singleton Cluster. By workflow rule
('players always get multiple shots; only coaches ever get one') the singleton
defaults to is_likely_coach=1 via Rule C in coach_detection.py. Operator can
still override via manual_coach_override=-1 for the off-process kid case.

These tests use the same monkeypatched-detector pattern as
test_pipeline_matching.py (no model loads) but stub cluster_embeddings to
return DBSCAN-shaped label arrays (with -1 for noise faces).
"""
import math
from datetime import datetime

import numpy as np
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models.db_models import (
    Cluster, Face, Image, ImageRole, Job, Player, PlayerMembership,
    ReferenceFace, Session as DbSession,
)
from app.services import face_pipeline
from app.services.face_pipeline import run_pipeline
from app.services.roster import normalize_name


def _vec(*coords) -> np.ndarray:
    v = np.zeros(512, dtype=np.float32)
    for i, c in enumerate(coords):
        v[i] = c
    return v


E0 = _vec(1.0)
E1 = _vec(0.0, 1.0)
E2 = _vec(0.0, 0.0, 1.0)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'pipeline-singletons.db'}",
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


def _make_detection(embedding, *, age=15.0):
    return {
        "bbox": [0, 0, 10, 10],
        "embedding": embedding,
        "det_score": 0.9,
        "age": age,
        "yaw": 0.0,
        "pitch": 0.0,
        "face_area_ratio": 0.2,
    }


def _seed_job_session(db, *, team="T", n_images=3, job_name="J"):
    job = Job(name=job_name, root_path=f"/tmp/{job_name}")
    db.add(job); db.flush()
    sess = DbSession(job_id=job.id, name=team, source_path=f"/tmp/{job_name}/{team}",
                     status="pending", created_at=datetime.utcnow())
    db.add(sess); db.commit(); db.refresh(sess)
    for i in range(n_images):
        db.add(Image(
            session_id=sess.id,
            path=f"/tmp/{job_name}/{team}/{i}.jpg",
            filename=f"{i}.jpg",
            capture_time=datetime(2026, 1, 1, 10, i, 0),
        ))
    db.commit()
    return job, sess


def _stub_detector(monkeypatch, embeddings_per_image, *, ages=None,
                   cluster_labels=None):
    """Stub face_detector + cluster_embeddings + expression so no models load.

    embeddings_per_image: list-of-lists; one inner list per image (matched by
        ingest order). Each inner list yields one detection. Use a 1-face inner
        list for single-face images, longer for multi-face (buddy).
    cluster_labels: if given, returned by cluster_embeddings verbatim (the test
        controls which faces become noise). If None, every face → cluster 0.
    """
    ages = ages or {}
    ordered_dets: list[list[dict]] = []
    for i, embs in enumerate(embeddings_per_image):
        ordered_dets.append([
            _make_detection(e, age=ages.get((i, j), 15.0))
            for j, e in enumerate(embs)
        ])
    # detect_faces is called per image — we yield in ingest order.
    iter_dets = iter(ordered_dets)
    monkeypatch.setattr(
        face_pipeline.face_detector, "detect_faces",
        lambda path: next(iter_dets),
    )
    if cluster_labels is None:
        monkeypatch.setattr(
            face_pipeline.clustering, "cluster_embeddings",
            lambda embs: [0] * len(embs),
        )
    else:
        monkeypatch.setattr(
            face_pipeline.clustering, "cluster_embeddings",
            lambda embs: list(cluster_labels),
        )
    monkeypatch.setattr(
        face_pipeline.expression, "classify_expression",
        lambda path, bbox: ("serious", 0.5),
    )
    monkeypatch.setattr(
        face_pipeline.face_detector, "arm_run_provider_check",
        lambda: None,
    )


# ── Core property (a): multi-face clusters unchanged ─────────────────────────


def test_multi_face_clusters_produce_same_count_pre_post(db, monkeypatch):
    """Pre/post property: when DBSCAN returns no noise, the resulting cluster
    set is identical to today's behavior. This pins 'don't regress the 99.2%
    case' — a session of 4 player images all clustering together yields 1
    cluster, with all 4 faces tied to it and no orphan Cluster rows."""
    job, sess = _seed_job_session(db, n_images=4)
    _stub_detector(
        monkeypatch,
        [[E0]] * 4,
        cluster_labels=[0, 0, 0, 0],     # no noise
    )
    run_pipeline(db, sess.id)

    clusters = db.query(Cluster).filter_by(session_id=sess.id).all()
    assert len(clusters) == 1
    faces = db.query(Face).join(Image).filter(Image.session_id == sess.id).all()
    assert {f.cluster_id for f in faces} == {clusters[0].id}


# ── Core property (a): a session of mixed noise + cluster keeps both shapes ──


def test_noise_face_becomes_singleton_cluster(db, monkeypatch):
    """A 3-image session: 2 images cluster together (one player), 1 image's
    face is noise (a 1-shot coach). Pre-Issue-5: 1 cluster + 1 orphan face.
    Post-Issue-5: 2 clusters (the player + the singleton-coach), both faces
    assigned to clusters, no orphan."""
    job, sess = _seed_job_session(db, n_images=3)
    _stub_detector(
        monkeypatch,
        [[E0], [E0], [E1]],
        cluster_labels=[0, 0, -1],       # 3rd face is noise
    )
    run_pipeline(db, sess.id)

    clusters = db.query(Cluster).filter_by(session_id=sess.id).all()
    assert len(clusters) == 2

    faces = db.query(Face).join(Image).filter(Image.session_id == sess.id).all()
    cluster_ids = {f.cluster_id for f in faces}
    assert None not in cluster_ids
    assert len(cluster_ids) == 2

    # The singleton cluster has exactly 1 face.
    singletons = [c for c in clusters
                  if db.query(Face).filter_by(cluster_id=c.id).count() == 1]
    assert len(singletons) == 1


# ── Core property (d): singleton emerges with is_likely_coach=1 ──────────────


def test_singleton_defaults_to_coach_by_rule_c(db, monkeypatch):
    """Rule C in coach_detection: image_count == 1 → coach. The promoted
    singleton's is_likely_coach must be 1 by the end of the pipeline. Use a
    3-image session (2 clustered + 1 noise) so the singleton is distinguishable
    from the 2-image kid cluster."""
    job, sess = _seed_job_session(db, n_images=3)
    _stub_detector(
        monkeypatch,
        [[E0], [E0], [E1]],
        cluster_labels=[0, 0, -1],       # images 0/1 cluster, image 2 noise
    )
    run_pipeline(db, sess.id)

    clusters = db.query(Cluster).filter_by(session_id=sess.id).all()
    singletons = [c for c in clusters
                  if db.query(Face).filter_by(cluster_id=c.id).count() == 1]
    assert len(singletons) == 1
    assert singletons[0].is_likely_coach == 1
    assert singletons[0].is_coach_for_sort() is True

    # The 2-image clustered group is NOT flagged by Rule C.
    multi = [c for c in clusters
             if db.query(Face).filter_by(cluster_id=c.id).count() > 1]
    assert len(multi) == 1
    assert multi[0].is_likely_coach == 0    # kid; Rule C image_count == 1 fails


def test_singleton_with_kid_age_still_coach_by_rule_c(db, monkeypatch):
    """Even when the singleton's detected age is squarely 'kid' (12), Rule C
    still fires — the operator overrides if it's actually a 1-shot kid."""
    job, sess = _seed_job_session(db, n_images=1)
    _stub_detector(
        monkeypatch,
        [[E0]],
        cluster_labels=[-1],
        ages={(0, 0): 12.0},
    )
    run_pipeline(db, sess.id)

    cluster = db.query(Cluster).filter_by(session_id=sess.id).one()
    assert cluster.is_likely_coach == 1


# ── Core property (b): singleton matching, both miss and hit ─────────────────


def test_singleton_with_no_match_shows_player_id(db, monkeypatch):
    """Singleton with no roster reference → auto_label stays None →
    display_label() returns 'Player {id}'."""
    job, sess = _seed_job_session(db, n_images=1)
    _stub_detector(monkeypatch, [[E0]], cluster_labels=[-1])
    run_pipeline(db, sess.id)

    cluster = db.query(Cluster).filter_by(session_id=sess.id).one()
    assert cluster.auto_label is None
    assert cluster.manual_label is None
    assert cluster.display_label() == f"Player {cluster.id}"


def test_singleton_with_high_tier_match_takes_match_name(db, monkeypatch):
    """A singleton that matches a roster player at high tier gets the player's
    name as auto_label (cluster_matching writes match-name + source='match',
    same precedence as multi-face clusters under C.3)."""
    job, sess = _seed_job_session(db, n_images=1)

    # A roster coach whose reference == the singleton's face (cosine 1.0).
    coach = Player(norm_name="coachjoe", display_name="Coach-Joe")
    db.add(coach); db.flush()
    db.add(ReferenceFace(player_id=coach.id, image_path="/tmp/r.jpg",
                         embedding=E0.tobytes(), det_score=0.9))
    db.add(PlayerMembership(player_id=coach.id, job_id=job.id, team_name="T",
                            norm_team=normalize_name("T"), is_coach=1))
    db.commit()

    _stub_detector(monkeypatch, [[E0]], cluster_labels=[-1])
    run_pipeline(db, sess.id)

    cluster = db.query(Cluster).filter_by(session_id=sess.id).one()
    assert cluster.match_tier == "high"
    assert cluster.auto_label == "Coach-Joe"
    assert cluster.auto_label_source == "match"
    assert cluster.is_likely_coach == 1


# ── Core property (e): operator override on singleton flips dispatch ─────────


def test_singleton_override_to_player_flips_is_coach_for_sort(db, monkeypatch):
    """Surprise #1 (accepted): override changes is_coach_for_sort to False
    and the cluster card's coach badge clears. Routing role stays 'team'
    because there's only one image — operator override is the recovery hatch,
    not a routing-changer (consistent with how multi-image kid clusters
    already work)."""
    from app.api.clusters import SetCoachOverrideRequest, set_coach_override

    job, sess = _seed_job_session(db, n_images=1)
    _stub_detector(monkeypatch, [[E0]], cluster_labels=[-1])
    run_pipeline(db, sess.id)

    cluster = db.query(Cluster).filter_by(session_id=sess.id).one()
    assert cluster.is_coach_for_sort() is True   # default before override

    resp = set_coach_override(
        sess.id,
        SetCoachOverrideRequest(cluster_id=cluster.id, override=-1),
        db,
    )
    assert resp["manual_coach_override"] == -1
    assert resp["is_coach_for_sort"] is False

    db.refresh(cluster)
    assert cluster.is_coach_for_sort() is False
    assert cluster.is_likely_coach == 1          # heuristic unchanged

    # The image was still routed through sort — role got assigned.
    roles = [r.role for r in
             db.query(ImageRole).filter_by(cluster_id=cluster.id).all()]
    assert "team" in roles


# ── Core property (c): idempotency on full pipeline re-run ───────────────────


def test_re_pipeline_wipes_and_recreates_singletons(db, monkeypatch):
    """Re-running run_pipeline clears the prior session results (per the
    existing _clear_prior_results contract) and recreates singletons cleanly.
    Use a 3-image session (2 clustered + 1 noise) so the multi-face cluster
    is distinguishable from the singleton in shape assertions."""
    job, sess = _seed_job_session(db, n_images=3)
    _stub_detector(
        monkeypatch,
        [[E0], [E0], [E1]],
        cluster_labels=[0, 0, -1],
    )
    run_pipeline(db, sess.id)

    before = db.query(Cluster).filter_by(session_id=sess.id).all()
    assert len(before) == 2
    before_face_count = (
        db.query(Face).join(Image)
        .filter(Image.session_id == sess.id).count()
    )

    # Stub again and re-run (face_pipeline._clear_prior_results wipes Faces
    # AND Clusters; the next run rebuilds them). expunge_all clears the
    # SQLAlchemy session-level identity map so reused autoincrement IDs
    # don't trip the identity-map warning (production uses a fresh
    # SessionLocal per run, so this never happens outside tests).
    db.expunge_all()
    _stub_detector(
        monkeypatch,
        [[E0], [E0], [E1]],
        cluster_labels=[0, 0, -1],
    )
    run_pipeline(db, sess.id)

    after = db.query(Cluster).filter_by(session_id=sess.id).all()
    assert len(after) == 2
    # Shape preserved: 1 multi-face cluster + 1 singleton.
    singletons = [c for c in after
                  if db.query(Face).filter_by(cluster_id=c.id).count() == 1]
    multi = [c for c in after
             if db.query(Face).filter_by(cluster_id=c.id).count() > 1]
    assert len(singletons) == 1
    assert len(multi) == 1
    assert singletons[0].is_likely_coach == 1
    assert multi[0].is_likely_coach == 0

    # Face count preserved across re-run; every face is bound to a cluster
    # (no orphans from the noise-singleton path under Issue 5). SQLite
    # primary-key reuse means Face.ids may collide pre/post — count is the
    # safe invariant.
    after_face_count = (
        db.query(Face).join(Image)
        .filter(Image.session_id == sess.id).count()
    )
    assert after_face_count == before_face_count == 3
    no_orphan_count = (
        db.query(Face).join(Image)
        .filter(Image.session_id == sess.id, Face.cluster_id.is_(None))
        .count()
    )
    assert no_orphan_count == 0


# ── Property (g): cluster_matching against a singleton's 1-row stack ─────────


def test_cluster_matching_on_singleton_1_row_stack(db, monkeypatch):
    """Direct check that the matching service handles a (1, 512) query stack
    without raising. The pipeline calls match_against_index on every cluster
    including new singletons — verify the edge."""
    from app.services import matching

    coach = Player(norm_name="coach", display_name="Coach")
    db.add(coach); db.flush()
    db.add(ReferenceFace(player_id=coach.id, image_path="/tmp/r.jpg",
                         embedding=E0.tobytes(), det_score=0.9))
    db.commit()

    index = matching.load_reference_index(db)
    result = matching.match_against_index(index, np.stack([E0]))
    assert result["tier"] == "high"
    assert result["player_id"] == coach.id

    # 1-D embedding (the A.3 single-embedding path) also works.
    result2 = matching.match_against_index(index, E0)
    assert result2["tier"] == "high"


# ── Property (f) — sanity: multi-face cluster still produces team+pano ───────


def test_multi_face_player_still_gets_team_and_pano(db, monkeypatch):
    """Routing rule B (verified pre-build): a multi-face PLAYER cluster
    should produce one team-role image AND one pano-role image. Issue 5
    must not regress this."""
    job, sess = _seed_job_session(db, n_images=3)
    _stub_detector(
        monkeypatch,
        [[E0]] * 3,
        cluster_labels=[0, 0, 0],
    )
    run_pipeline(db, sess.id)

    cluster = db.query(Cluster).filter_by(session_id=sess.id).one()
    assert cluster.is_coach_for_sort() is False
    roles = [r.role for r in
             db.query(ImageRole).filter_by(cluster_id=cluster.id).all()]
    assert "team" in roles
    assert "panoramic" in roles
