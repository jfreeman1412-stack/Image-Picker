"""Tests for manual coach override: is_coach_for_sort() resolution + the
set-coach-override endpoint re-running sort, and override surviving a full
pipeline re-run."""
from datetime import datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.clusters import SetCoachOverrideRequest, set_coach_override
from app.db import Base
from app.models.db_models import (
    Cluster, Face, Image, ImageRole, Job, Session as DbSessionModel,
)


# ── is_coach_for_sort() resolution (pure model logic) ────────────────────────


def test_auto_coach_no_override_is_coach():
    c = Cluster(is_likely_coach=1, manual_coach_override=0)
    assert c.is_coach_for_sort() is True


def test_auto_player_forced_coach():
    c = Cluster(is_likely_coach=0, manual_coach_override=1)
    assert c.is_coach_for_sort() is True


def test_auto_coach_forced_player():
    c = Cluster(is_likely_coach=1, manual_coach_override=-1)
    assert c.is_coach_for_sort() is False


def test_auto_player_no_override_is_player():
    c = Cluster(is_likely_coach=0, manual_coach_override=0)
    assert c.is_coach_for_sort() is False


def test_none_override_treated_as_zero():
    c = Cluster(is_likely_coach=1, manual_coach_override=None)
    assert c.is_coach_for_sort() is True


# ── Endpoint: set override → re-sort ─────────────────────────────────────────


@pytest.fixture
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'coach-test.db'}",
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


def _seed_player_cluster(db):
    """A normal player cluster: 3 single-face images, last smiling.
    Under player sort: team=last single-face, pano=walk-back. Under coach
    sort: team=last image, no pano."""
    job = Job(name="J", root_path="/tmp", has_lines=0)
    db.add(job); db.commit(); db.refresh(job)
    sess = DbSessionModel(job_id=job.id, name="T", source_path="/tmp/T",
                           status="done", created_at=datetime.utcnow())
    db.add(sess); db.commit(); db.refresh(sess)
    cluster = Cluster(session_id=sess.id, image_count=0,
                      is_likely_coach=0, manual_coach_override=0)
    db.add(cluster); db.commit(); db.refresh(cluster)

    for i, expr in enumerate(["serious", "serious", "smiling"], start=1):
        img = Image(session_id=sess.id, path=f"/tmp/T/{i}.png", filename=f"{i}.png",
                    capture_time=datetime(2026, 1, 1, 10, i, 0))
        db.add(img); db.commit(); db.refresh(img)
        face = Face(image_id=img.id, bbox="[0,0,50,50]", det_score=0.9,
                    expression=expr, yaw=0.0, pitch=0.0, face_area_ratio=0.1,
                    cluster_id=cluster.id)
        db.add(face)
    db.commit()
    return sess, cluster


def test_set_override_to_coach_resorts_with_coach_rule(db):
    sess, cluster = _seed_player_cluster(db)

    # Force coach → assign_roles_coach: last image = team, no pano.
    resp = set_coach_override(
        sess.id, SetCoachOverrideRequest(cluster_id=cluster.id, override=1), db,
    )
    assert resp["manual_coach_override"] == 1
    assert resp["is_coach_for_sort"] is True

    roles = {r.image_id: r.role for r in
             db.query(ImageRole).filter_by(cluster_id=cluster.id).all()}
    # 3 images; coach rule makes the last one (by capture order) team, rest individual.
    assert sorted(roles.values()) == ["individual", "individual", "team"]
    # Coach clusters never get a panoramic.
    assert "panoramic" not in roles.values()


def test_set_override_back_to_player_restores_pano(db):
    sess, cluster = _seed_player_cluster(db)
    set_coach_override(sess.id, SetCoachOverrideRequest(cluster_id=cluster.id, override=1), db)
    # Now force player again.
    resp = set_coach_override(
        sess.id, SetCoachOverrideRequest(cluster_id=cluster.id, override=-1), db,
    )
    assert resp["is_coach_for_sort"] is False
    roles = {r.image_id: r.role for r in
             db.query(ImageRole).filter_by(cluster_id=cluster.id).all()}
    # Player rule: a pano gets picked again (walk-back over single-face shots).
    assert "panoramic" in roles.values()
    assert "team" in roles.values()


def test_set_override_invalid_value_400(db):
    sess, cluster = _seed_player_cluster(db)
    with pytest.raises(HTTPException) as exc:
        set_coach_override(sess.id, SetCoachOverrideRequest(cluster_id=cluster.id, override=2), db)
    assert exc.value.status_code == 400


def test_set_override_missing_cluster_404(db):
    sess, _ = _seed_player_cluster(db)
    with pytest.raises(HTTPException) as exc:
        set_coach_override(sess.id, SetCoachOverrideRequest(cluster_id=99999, override=1), db)
    assert exc.value.status_code == 404


def test_override_survives_full_pipeline_resort(db):
    """A full pipeline re-run calls _sort_cluster, which dispatches on
    is_coach_for_sort() — so a manual override is honored, not reset."""
    from app.services.face_pipeline import _sort_cluster
    sess, cluster = _seed_player_cluster(db)
    cluster.manual_coach_override = 1  # forced coach
    db.commit()

    _sort_cluster(db, cluster)  # what the pipeline calls per cluster
    db.commit()
    db.refresh(cluster)

    assert cluster.manual_coach_override == 1  # not reset
    roles = {r.role for r in db.query(ImageRole).filter_by(cluster_id=cluster.id).all()}
    assert "panoramic" not in roles  # coach rule applied
