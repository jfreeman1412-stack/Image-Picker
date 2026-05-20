"""Phase 9 Section 6: duplicate_auto_label cross-cluster flag.

Two or more clusters in the same session sharing the same normalized
auto_label is a load-bearing signal: either the photographer's copyright
tag was wrong on some shots, or face clustering split one player across
two clusters. Either way, surface as a flag — and when the operator
hasn't hidden the flag in settings, block "Mark reviewed & next".
"""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.sessions import _compute_review_readiness
from app.api.settings import _FLAG_VIS_KEY
from app.db import Base, get_db
from app.main import app
from app.models.db_models import (
    Cluster, Job, Session as DbSession, Setting,
)
from app.services.roster_check import (
    DUPLICATE_AUTO_LABEL, add_duplicate_label_flag,
    find_duplicate_label_cluster_ids,
)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'dup-test.db'}",
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


def _session(db, name="T"):
    job = Job(name="J", root_path="/tmp", has_lines=0)
    db.add(job); db.commit(); db.refresh(job)
    sess = DbSession(job_id=job.id, name=name, source_path=f"/tmp/{name}",
                     status="done", created_at=datetime.utcnow())
    db.add(sess); db.commit(); db.refresh(sess)
    return sess


def _cluster(db, sess, label, image_count=3):
    c = Cluster(session_id=sess.id, auto_label=label, image_count=image_count)
    db.add(c); db.commit(); db.refresh(c)
    return c


# ── find_duplicate_label_cluster_ids ───────────────────────────────────

def test_no_duplicates_when_all_labels_unique(db):
    sess = _session(db)
    _cluster(db, sess, "Eleanor-Pederson")
    _cluster(db, sess, "June-Wampach")
    assert find_duplicate_label_cluster_ids(sess.clusters) == set()


def test_pair_of_clusters_with_same_label_flagged(db):
    sess = _session(db)
    a = _cluster(db, sess, "Jack-Smith")
    b = _cluster(db, sess, "Jack-Smith")
    assert find_duplicate_label_cluster_ids(sess.clusters) == {a.id, b.id}


def test_three_clusters_sharing_label_all_flagged(db):
    sess = _session(db)
    a = _cluster(db, sess, "Jack-Smith")
    b = _cluster(db, sess, "jack smith")    # same after normalize
    c = _cluster(db, sess, "JackSmith")     # same after normalize
    assert find_duplicate_label_cluster_ids(sess.clusters) == {a.id, b.id, c.id}


def test_null_auto_label_not_grouped(db):
    """Clusters with no auto_label go through `ambiguous_copyright` already."""
    sess = _session(db)
    a = _cluster(db, sess, None)
    b = _cluster(db, sess, None)
    assert find_duplicate_label_cluster_ids(sess.clusters) == set()


def test_per_session_not_cross_session(db):
    """Two sessions sharing the same label aren't a duplicate situation —
    they're presumably the same player photographed for two different teams."""
    s1 = _session(db, "A"); s2 = _session(db, "B")
    a = _cluster(db, s1, "Jack-Smith")
    b = _cluster(db, s2, "Jack-Smith")
    assert find_duplicate_label_cluster_ids(s1.clusters) == set()
    assert find_duplicate_label_cluster_ids(s2.clusters) == set()


# ── add_duplicate_label_flag helper ────────────────────────────────────

def test_add_flag_appends():
    assert add_duplicate_label_flag(None, True) == DUPLICATE_AUTO_LABEL
    assert add_duplicate_label_flag("outlier_high", True) == \
        f"outlier_high,{DUPLICATE_AUTO_LABEL}"


def test_add_flag_no_op_when_not_duplicate():
    assert add_duplicate_label_flag("outlier_high", False) == "outlier_high"
    assert add_duplicate_label_flag(None, False) is None


def test_add_flag_dedupes():
    assert add_duplicate_label_flag(
        f"outlier_high,{DUPLICATE_AUTO_LABEL}", True,
    ) == f"outlier_high,{DUPLICATE_AUTO_LABEL}"


# ── /clusters payload integration ──────────────────────────────────────

def test_clusters_endpoint_surfaces_flag_for_duplicates(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'dup-http.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    SL = sessionmaker(bind=engine)
    def _override():
        s = SL()
        try: yield s
        finally: s.close()
    app.dependency_overrides[get_db] = _override
    try:
        seed = SL()
        try:
            job = Job(name="J", root_path="/tmp", has_lines=0)
            seed.add(job); seed.commit(); seed.refresh(job)
            sess = DbSession(job_id=job.id, name="T", source_path="/tmp/T",
                             status="done", created_at=datetime.utcnow())
            seed.add(sess); seed.commit(); seed.refresh(sess)
            for label in ("Dup", "Dup", "Unique"):
                seed.add(Cluster(session_id=sess.id, auto_label=label,
                                 image_count=2))
            seed.commit()
            sid = sess.id
        finally:
            seed.close()
        client = TestClient(app)
        res = client.get(f"/api/sessions/{sid}/clusters")
        assert res.status_code == 200
        body = res.json()
        flagged = [c for c in body
                   if DUPLICATE_AUTO_LABEL in c["visible_review_reasons"]]
        # Two "Dup" clusters flagged; the "Unique" one isn't.
        assert len(flagged) == 2
        labels = {c["label"] for c in flagged}
        assert labels == {"Dup"}
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


# ── review-readiness gate integration ─────────────────────────────────

def _role_complete_cluster(db, sess, label, n=0):
    """Cluster with one team-image and one pano-image so it passes the
    existing team/pano gate. Use a different n per cluster to keep paths
    unique and avoid PK collisions on (image_id, cluster_id)."""
    from app.models.db_models import Image, ImageRole
    c = Cluster(session_id=sess.id, auto_label=label, image_count=2)
    db.add(c); db.commit(); db.refresh(c)
    img1 = Image(session_id=sess.id, path=f"/tmp/{label}-{n}-team.png",
                 filename=f"{label}-{n}-team.png")
    img2 = Image(session_id=sess.id, path=f"/tmp/{label}-{n}-pano.png",
                 filename=f"{label}-{n}-pano.png")
    db.add_all([img1, img2]); db.commit(); db.refresh(img1); db.refresh(img2)
    db.add(ImageRole(image_id=img1.id, cluster_id=c.id, role="team"))
    db.add(ImageRole(image_id=img2.id, cluster_id=c.id, role="panoramic"))
    db.commit()
    return c


def test_gate_blocks_when_visible_and_duplicates_exist(db):
    """With default visibility (true), duplicates block 'Mark reviewed & next'."""
    sess = _session(db)
    _role_complete_cluster(db, sess, "Dup", n=1)
    _role_complete_cluster(db, sess, "Dup", n=2)
    r = _compute_review_readiness(db, sess)
    assert r["ready"] is False
    dup_rows = [
        x for x in r["incomplete_clusters"]
        if DUPLICATE_AUTO_LABEL in x["missing"]
    ]
    assert len(dup_rows) == 2


def test_gate_passes_when_flag_visibility_off(db):
    """Hiding `duplicate_auto_label` in settings turns OFF both the chip AND
    the gate — single source of truth."""
    sess = _session(db)
    _role_complete_cluster(db, sess, "Dup", n=1)
    _role_complete_cluster(db, sess, "Dup", n=2)
    # Hide the flag.
    import json as _json
    db.add(Setting(key=_FLAG_VIS_KEY,
                   value=_json.dumps({DUPLICATE_AUTO_LABEL: False})))
    db.commit()
    r = _compute_review_readiness(db, sess)
    assert r["ready"] is True
    assert r["incomplete_clusters"] == []


def test_gate_still_blocks_role_incompleteness_independent_of_flag(db):
    """Even with the duplicate flag hidden, missing team/pano roles still
    block — they're a separate concern."""
    sess = _session(db)
    _cluster(db, sess, "Dup")
    _cluster(db, sess, "Dup")    # missing roles AND duplicate
    import json as _json
    db.add(Setting(key=_FLAG_VIS_KEY,
                   value=_json.dumps({DUPLICATE_AUTO_LABEL: False})))
    db.commit()
    r = _compute_review_readiness(db, sess)
    assert r["ready"] is False
    # All `missing` lists should contain team/pano, NOT duplicate_auto_label.
    for row in r["incomplete_clusters"]:
        assert DUPLICATE_AUTO_LABEL not in row["missing"]
        assert "team" in row["missing"]


def test_flag_not_persisted_to_db(db):
    """Read-time only: Cluster.review_reason in the DB stays untouched."""
    sess = _session(db)
    a = _cluster(db, sess, "Dup")
    b = _cluster(db, sess, "Dup")
    # The find_* helper doesn't mutate either cluster.
    find_duplicate_label_cluster_ids(sess.clusters)
    db.expire(a); db.refresh(a)
    db.expire(b); db.refresh(b)
    assert a.review_reason is None
    assert b.review_reason is None
