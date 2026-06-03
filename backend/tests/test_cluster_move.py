"""Cross-session cluster moves (POST /api/clusters/{id}/move) + the
aggregator endpoint (GET /api/jobs/{id}/roster-mismatches).

The move endpoint is the genuinely new mutation in Phase 6 — it crosses
session boundaries. Tests lock the two locked-in decisions from the
brainstorm:

  - merge => drop EVERY ImageRole row on both sides (incl. manual_override=1)
    and re-sort the combined image set with `_sort_cluster`. The user
    explicitly chose 'reevaluate the merged cluster' over 'pick a side's
    manual roles to keep'.
  - create => relocate the source cluster intact. manual_label,
    manual_coach_override, and every ImageRole (incl. manual_override=1)
    are preserved across the move.
  - Owned-only image relocation: an image with a face in a non-moving
    cluster (buddy shot) stays in source. Mirrors the within-session
    `reassign` buddy semantics.
  - Auto-unreview target. Source reviewed flag is NEVER touched.
"""
from datetime import datetime

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.cluster_move import MoveRequest, move_cluster
from app.api.roster import list_mismatches
from app.db import Base, get_db
from app.main import app
from app.models.db_models import (
    Cluster, Face, Image, ImageRole, Job, RosterEntry, Session,
)
from app.services.roster import replace_job_roster


@pytest.fixture
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'cm-test.db'}",
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


# ── Builders ─────────────────────────────────────────────────────────────

def _job_with_two_sessions(db, source_name="Source Team", target_name="Target Team",
                            target_reviewed=False) -> tuple[Job, Session, Session]:
    job = Job(name="J", root_path="/tmp", has_lines=0)
    db.add(job); db.commit(); db.refresh(job)
    src = Session(job_id=job.id, name=source_name,
                  source_path=f"/tmp/{source_name}", status="done",
                  created_at=datetime.utcnow())
    tgt = Session(job_id=job.id, name=target_name,
                  source_path=f"/tmp/{target_name}", status="done",
                  created_at=datetime.utcnow(),
                  reviewed=1 if target_reviewed else 0,
                  reviewed_at=datetime.utcnow() if target_reviewed else None)
    db.add_all([src, tgt]); db.commit(); db.refresh(src); db.refresh(tgt)
    return job, src, tgt


def _cluster_with_images(
    db, session: Session, *, label: str, image_count: int = 3,
    manual_label: str | None = None, manual_coach_override: int = 0,
    base_filename: str | None = None,
) -> Cluster:
    """Make a cluster with `image_count` single-face images. Each image gets
    one Face row with minimal-but-realistic metadata so `_sort_cluster` runs
    cleanly. Returns the cluster (refreshed).
    """
    c = Cluster(
        session_id=session.id, image_count=image_count,
        auto_label=label, manual_label=manual_label,
        manual_coach_override=manual_coach_override,
    )
    db.add(c); db.commit(); db.refresh(c)

    fname_base = base_filename or label.replace(" ", "_")
    for i in range(image_count):
        img = Image(
            session_id=session.id,
            path=f"/tmp/{session.name}/{fname_base}_{i}.jpg",
            filename=f"{fname_base}_{i}.jpg",
            capture_time=datetime(2026, 1, 1, 12, 0, i),
            copyright_tag=label,
        )
        db.add(img); db.commit(); db.refresh(img)
        db.add(Face(
            image_id=img.id, cluster_id=c.id, bbox="[0,0,10,10]",
            det_score=0.95, expression="smiling" if i % 2 == 0 else "serious",
            age=10.0, yaw=0.0, pitch=0.0, face_area_ratio=0.15,
        ))
    db.commit(); db.refresh(c)
    return c


def _image_ids_in_session(db, session_id: int) -> set[int]:
    return {i.id for i in db.query(Image).filter_by(session_id=session_id).all()}


# ── mode = create ────────────────────────────────────────────────────────

def test_create_relocates_cluster_and_owned_images(db):
    _, src, tgt = _job_with_two_sessions(db)
    c = _cluster_with_images(db, src, label="Eleanor-Pederson", image_count=3)
    image_ids = {f.image_id for f in db.query(Face).filter_by(cluster_id=c.id).all()}

    res = move_cluster(c.id, MoveRequest(target_session_id=tgt.id, mode="create"), db)
    assert res["mode"] == "create"
    assert res["source_cluster_id"] == c.id
    assert res["target_cluster_id"] == c.id          # same cluster row, just relocated
    assert res["target_session_id"] == tgt.id

    db.expire_all()
    c = db.query(Cluster).get(c.id)
    assert c.session_id == tgt.id
    assert image_ids == _image_ids_in_session(db, tgt.id)
    assert _image_ids_in_session(db, src.id) == set()


def test_create_preserves_manual_label_and_coach_override(db):
    _, src, tgt = _job_with_two_sessions(db)
    c = _cluster_with_images(
        db, src, label="Eleanor-Pederson", image_count=2,
        manual_label="Coach Eleanor", manual_coach_override=1,
    )
    move_cluster(c.id, MoveRequest(target_session_id=tgt.id, mode="create"), db)
    db.expire_all()
    c = db.query(Cluster).get(c.id)
    assert c.manual_label == "Coach Eleanor"
    assert c.manual_coach_override == 1


def test_create_preserves_manual_override_image_roles(db):
    _, src, tgt = _job_with_two_sessions(db)
    c = _cluster_with_images(db, src, label="Eleanor-Pederson", image_count=2)
    img = db.query(Image).filter_by(session_id=src.id).first()
    # Lock the user's manual TEAM pick before the move.
    db.add(ImageRole(image_id=img.id, cluster_id=c.id, role="team", manual_override=1))
    db.commit()

    move_cluster(c.id, MoveRequest(target_session_id=tgt.id, mode="create"), db)
    db.expire_all()
    rows = db.query(ImageRole).filter_by(cluster_id=c.id, image_id=img.id).all()
    assert len(rows) == 1
    assert rows[0].role == "team"
    assert rows[0].manual_override == 1


def test_create_buddy_image_stays_in_source(db):
    """Image shared with a different cluster in source session must NOT
    move its session_id — only the moving cluster's owned images do."""
    _, src, tgt = _job_with_two_sessions(db)
    moving = _cluster_with_images(db, src, label="Eleanor-Pederson", image_count=2)
    other  = _cluster_with_images(db, src, label="June-Wampach", image_count=2)

    # Make one image a buddy shot: add a second face to `moving`'s first
    # image, pointing at `other`. So image has 2 faces in 2 clusters.
    shared_img = db.query(Image).filter_by(session_id=src.id).first()
    db.add(Face(
        image_id=shared_img.id, cluster_id=other.id, bbox="[10,10,20,20]",
        det_score=0.9, expression="smiling", age=10.0,
        yaw=0.0, pitch=0.0, face_area_ratio=0.12,
    ))
    db.commit()

    move_cluster(moving.id, MoveRequest(target_session_id=tgt.id, mode="create"), db)
    db.expire_all()
    # The shared image stays in source; only the truly-owned image moves.
    moved_ids = _image_ids_in_session(db, tgt.id)
    src_ids = _image_ids_in_session(db, src.id)
    assert shared_img.id in src_ids
    assert shared_img.id not in moved_ids


def test_create_unreviews_target_when_reviewed(db):
    _, src, tgt = _job_with_two_sessions(db, target_reviewed=True)
    c = _cluster_with_images(db, src, label="Eleanor-Pederson", image_count=2)
    assert tgt.reviewed == 1

    res = move_cluster(c.id, MoveRequest(target_session_id=tgt.id, mode="create"), db)
    assert res["target_unreviewed"] is True
    db.expire_all()
    tgt2 = db.query(Session).get(tgt.id)
    assert tgt2.reviewed == 0
    assert tgt2.reviewed_at is None


def test_source_reviewed_is_never_touched(db):
    _, src, tgt = _job_with_two_sessions(db)
    src.reviewed = 1; src.reviewed_at = datetime(2026, 1, 1)
    db.commit()
    c = _cluster_with_images(db, src, label="Eleanor-Pederson", image_count=2)
    _cluster_with_images(db, src, label="June-Wampach", image_count=2)  # so src isn't empty after

    move_cluster(c.id, MoveRequest(target_session_id=tgt.id, mode="create"), db)
    db.expire_all()
    src2 = db.query(Session).get(src.id)
    assert src2.reviewed == 1
    assert src2.reviewed_at is not None


# ── mode = merge ─────────────────────────────────────────────────────────

def test_merge_drops_all_image_roles_both_sides_and_resorts(db):
    _, src, tgt = _job_with_two_sessions(db)
    src_c = _cluster_with_images(db, src, label="Eleanor-Pederson",
                                 image_count=3, base_filename="eleanor_src")
    tgt_c = _cluster_with_images(db, tgt, label="Eleanor-Pederson",
                                 image_count=3, base_filename="eleanor_tgt")
    # Lock pre-existing manual picks on BOTH sides — they must vanish.
    src_img = db.query(Face).filter_by(cluster_id=src_c.id).first().image_id
    tgt_img = db.query(Face).filter_by(cluster_id=tgt_c.id).first().image_id
    db.add(ImageRole(image_id=src_img, cluster_id=src_c.id, role="team",
                     manual_override=1))
    db.add(ImageRole(image_id=tgt_img, cluster_id=tgt_c.id, role="panoramic",
                     manual_override=1))
    db.commit()

    res = move_cluster(src_c.id, MoveRequest(target_session_id=tgt.id, mode="merge"), db)
    assert res["mode"] == "merge"
    assert res["target_cluster_id"] == tgt_c.id

    db.expire_all()
    # No manual_override=1 rows survive on the merged cluster.
    surviving_manual = db.query(ImageRole).filter_by(
        cluster_id=tgt_c.id, manual_override=1,
    ).count()
    assert surviving_manual == 0
    # _sort_cluster ran and produced fresh auto rows on the combined set.
    fresh_auto = db.query(ImageRole).filter_by(
        cluster_id=tgt_c.id, manual_override=0,
    ).count()
    assert fresh_auto >= 1


def test_merge_deletes_source_cluster_and_re_parents_faces(db):
    _, src, tgt = _job_with_two_sessions(db)
    src_c = _cluster_with_images(db, src, label="Eleanor-Pederson",
                                 image_count=2, base_filename="eleanor_src")
    tgt_c = _cluster_with_images(db, tgt, label="Eleanor-Pederson",
                                 image_count=2, base_filename="eleanor_tgt")

    move_cluster(src_c.id, MoveRequest(target_session_id=tgt.id, mode="merge"), db)
    db.expire_all()
    assert db.query(Cluster).get(src_c.id) is None
    # All faces now point at the target cluster.
    leftover = db.query(Face).filter_by(cluster_id=src_c.id).count()
    assert leftover == 0
    combined = db.query(Face).filter_by(cluster_id=tgt_c.id).count()
    assert combined == 4


def test_merge_moves_owned_image_session_ids(db):
    _, src, tgt = _job_with_two_sessions(db)
    src_c = _cluster_with_images(db, src, label="Eleanor-Pederson",
                                 image_count=2, base_filename="eleanor_src")
    _cluster_with_images(db, tgt, label="Eleanor-Pederson",
                         image_count=2, base_filename="eleanor_tgt")
    src_img_ids = {f.image_id for f in db.query(Face).filter_by(cluster_id=src_c.id).all()}

    move_cluster(src_c.id, MoveRequest(target_session_id=tgt.id, mode="merge"), db)
    db.expire_all()
    assert src_img_ids.issubset(_image_ids_in_session(db, tgt.id))


def test_merge_409_when_no_matching_target_cluster(db):
    _, src, tgt = _job_with_two_sessions(db)
    src_c = _cluster_with_images(db, src, label="Eleanor-Pederson", image_count=2)
    _cluster_with_images(db, tgt, label="Someone-Else", image_count=2)

    with pytest.raises(HTTPException) as exc:
        move_cluster(src_c.id, MoveRequest(target_session_id=tgt.id, mode="merge"), db)
    assert exc.value.status_code == 409
    assert exc.value.detail["error"] == "no_target_cluster"


def test_merge_409_when_ambiguous_target(db):
    _, src, tgt = _job_with_two_sessions(db)
    src_c = _cluster_with_images(db, src, label="Eleanor-Pederson", image_count=2)
    a = _cluster_with_images(db, tgt, label="Eleanor-Pederson",
                             image_count=2, base_filename="eleanor_a")
    b = _cluster_with_images(db, tgt, label="Eleanor-Pederson",
                             image_count=2, base_filename="eleanor_b")

    with pytest.raises(HTTPException) as exc:
        move_cluster(src_c.id, MoveRequest(target_session_id=tgt.id, mode="merge"), db)
    assert exc.value.status_code == 409
    assert exc.value.detail["error"] == "ambiguous_target"
    assert set(exc.value.detail["candidate_cluster_ids"]) == {a.id, b.id}


def test_merge_unreviews_target_when_reviewed(db):
    _, src, tgt = _job_with_two_sessions(db, target_reviewed=True)
    _cluster_with_images(db, tgt, label="Eleanor-Pederson", image_count=2,
                         base_filename="eleanor_tgt")
    src_c = _cluster_with_images(db, src, label="Eleanor-Pederson",
                                 image_count=2, base_filename="eleanor_src")

    res = move_cluster(src_c.id, MoveRequest(target_session_id=tgt.id, mode="merge"), db)
    assert res["target_unreviewed"] is True
    db.expire_all()
    assert db.query(Session).get(tgt.id).reviewed == 0


# ── Validation paths ────────────────────────────────────────────────────

def test_400_on_invalid_mode(db):
    _, src, tgt = _job_with_two_sessions(db)
    c = _cluster_with_images(db, src, label="x", image_count=1)
    with pytest.raises(HTTPException) as exc:
        move_cluster(c.id, MoveRequest(target_session_id=tgt.id, mode="nuke"), db)
    assert exc.value.status_code == 400


def test_404_on_missing_source_cluster(db):
    _, _, tgt = _job_with_two_sessions(db)
    with pytest.raises(HTTPException) as exc:
        move_cluster(99999, MoveRequest(target_session_id=tgt.id, mode="create"), db)
    assert exc.value.status_code == 404


def test_404_on_missing_target_session(db):
    _, src, _ = _job_with_two_sessions(db)
    c = _cluster_with_images(db, src, label="x", image_count=1)
    with pytest.raises(HTTPException) as exc:
        move_cluster(c.id, MoveRequest(target_session_id=99999, mode="create"), db)
    assert exc.value.status_code == 404


def test_400_on_cross_job(db):
    _, src, _ = _job_with_two_sessions(db)
    # Make a second job with a session, attempt to move source's cluster there.
    other_job = Job(name="Other", root_path="/tmp", has_lines=0)
    db.add(other_job); db.commit(); db.refresh(other_job)
    foreign = Session(job_id=other_job.id, name="Foreign Team",
                      source_path="/tmp/foreign", status="done",
                      created_at=datetime.utcnow())
    db.add(foreign); db.commit(); db.refresh(foreign)
    c = _cluster_with_images(db, src, label="x", image_count=1)
    with pytest.raises(HTTPException) as exc:
        move_cluster(c.id, MoveRequest(target_session_id=foreign.id, mode="create"), db)
    assert exc.value.status_code == 400


def test_400_on_archived_target(db):
    _, src, tgt = _job_with_two_sessions(db)
    tgt.archived = 1; db.commit()
    c = _cluster_with_images(db, src, label="x", image_count=1)
    with pytest.raises(HTTPException) as exc:
        move_cluster(c.id, MoveRequest(target_session_id=tgt.id, mode="create"), db)
    assert exc.value.status_code == 400


def test_400_on_same_session(db):
    _, src, _ = _job_with_two_sessions(db)
    c = _cluster_with_images(db, src, label="x", image_count=1)
    with pytest.raises(HTTPException) as exc:
        move_cluster(c.id, MoveRequest(target_session_id=src.id, mode="create"), db)
    assert exc.value.status_code == 400


# ── Aggregator: GET /api/jobs/{id}/roster-mismatches ───────────────────

def test_aggregator_empty_when_no_roster(db):
    job, src, tgt = _job_with_two_sessions(db)
    _cluster_with_images(db, src, label="Eleanor-Pederson", image_count=2)
    assert list_mismatches(job.id, db) == {"items": []}


def test_aggregator_lists_mismatched_cluster_with_target(db):
    job, src, tgt = _job_with_two_sessions(db,
                                            source_name="10U-Black-Softball",
                                            target_name="11UA-Baseball")
    src_c = _cluster_with_images(db, src, label="Eleanor-Pederson",
                                 image_count=3, base_filename="eleanor_s")
    tgt_c = _cluster_with_images(db, tgt, label="Eleanor-Pederson",
                                 image_count=2, base_filename="eleanor_t")
    # Roster says Eleanor belongs on 11UA-Baseball (not src's 10U-Black-Softball).
    replace_job_roster(db, job.id, [("Eleanor-Pederson", "11UA-Baseball")])
    db.commit()

    result = list_mismatches(job.id, db)
    assert len(result["items"]) == 1
    row = result["items"][0]
    assert row["source_session_id"] == src.id
    assert row["source_cluster_id"] == src_c.id
    assert row["source_image_count"] == 3
    assert row["expected_team_name"] == "11UA-Baseball"
    assert row["target_session_id"] == tgt.id
    assert row["target_cluster_id"] == tgt_c.id


def test_aggregator_null_target_cluster_when_team_has_no_matching_cluster(db):
    job, src, tgt = _job_with_two_sessions(db,
                                            source_name="10U-Black-Softball",
                                            target_name="11UA-Baseball")
    src_c = _cluster_with_images(db, src, label="Eleanor-Pederson",
                                 image_count=3, base_filename="eleanor_s")
    _cluster_with_images(db, tgt, label="Different-Name",
                         image_count=2, base_filename="diff")
    replace_job_roster(db, job.id, [("Eleanor-Pederson", "11UA-Baseball")])
    db.commit()

    row = list_mismatches(job.id, db)["items"][0]
    assert row["target_session_id"] == tgt.id
    assert row["target_cluster_id"] is None      # panel will show "Create new" only


def test_aggregator_null_target_session_when_team_not_in_job(db):
    job, src, _tgt = _job_with_two_sessions(db,
                                             source_name="10U-Black-Softball",
                                             target_name="Some-Other-Team")
    _cluster_with_images(db, src, label="Eleanor-Pederson",
                         image_count=2, base_filename="eleanor_s")
    # Roster expects Eleanor on a team that doesn't exist as a session.
    replace_job_roster(db, job.id, [("Eleanor-Pederson", "Nonexistent-Team")])
    db.commit()

    row = list_mismatches(job.id, db)["items"][0]
    assert row["target_session_id"] is None
    assert row["target_cluster_id"] is None
    assert row["expected_team_name"] == "Nonexistent-Team"


def test_aggregator_includes_source_image_ids(db):
    """Frontend uses source_image_ids to drive the 'Reject as stray' loop
    without a follow-up fetch."""
    job, src, tgt = _job_with_two_sessions(db,
                                            source_name="10U-Black-Softball",
                                            target_name="11UA-Baseball")
    src_c = _cluster_with_images(db, src, label="Eleanor-Pederson",
                                 image_count=3, base_filename="eleanor_s")
    replace_job_roster(db, job.id, [("Eleanor-Pederson", "11UA-Baseball")])
    db.commit()

    row = list_mismatches(job.id, db)["items"][0]
    img_ids = {f.image_id for f in db.query(Face).filter_by(cluster_id=src_c.id).all()}
    assert set(row["source_image_ids"]) == img_ids
    assert len(row["source_image_ids"]) == 3


def test_aggregator_reports_target_reviewed_state(db):
    job, src, tgt = _job_with_two_sessions(db,
                                            source_name="10U-Black-Softball",
                                            target_name="11UA-Baseball",
                                            target_reviewed=True)
    _cluster_with_images(db, src, label="Eleanor-Pederson", image_count=2)
    _cluster_with_images(db, tgt, label="Eleanor-Pederson",
                         image_count=2, base_filename="eleanor_t")
    replace_job_roster(db, job.id, [("Eleanor-Pederson", "11UA-Baseball")])
    db.commit()

    row = list_mismatches(job.id, db)["items"][0]
    assert row["target_session_reviewed"] is True


def test_aggregator_skips_archived_sessions(db):
    job, src, tgt = _job_with_two_sessions(db,
                                            source_name="10U-Black-Softball",
                                            target_name="11UA-Baseball")
    src.archived = 1; db.commit()
    _cluster_with_images(db, src, label="Eleanor-Pederson", image_count=2)
    replace_job_roster(db, job.id, [("Eleanor-Pederson", "11UA-Baseball")])
    db.commit()
    # Archived source is excluded — no row.
    assert list_mismatches(job.id, db) == {"items": []}


# ── Move-card Phase 1 (2026-06-03) ───────────────────────────────────────────
#
# New endpoints layered on top of cluster_move.py's existing machinery:
#   POST /api/clusters/{cluster_id}/move-with-guards  — adds the
#       manual_override=1 and Session.reviewed=1 safety guards; clears
#       accepted_cross_team after the move; force=True bypasses both
#       guards explicitly.
#   POST /api/clusters/{cluster_id}/dismiss-cross-team — operator confirms
#       this cluster is a legitimate cross-team appearance (guest /
#       sibling / multi-team kid). Sets Cluster.accepted_cross_team=1; the
#       read-time match_team_mismatch flag is then suppressed from
#       visible_review_reasons. Inverse endpoint /undismiss-cross-team
#       restores the flag.
#
# Existing /move endpoint is intentionally unchanged — RosterModal's
# Phase 6 mismatch flow continues to work bit-for-bit.


from app.api.cluster_move import (
    DismissCrossTeamRequest,
    MoveWithGuardsRequest,
    _check_move_safety,
    dismiss_cross_team,
    move_cluster_with_guards,
    undismiss_cross_team,
)


def _add_image_role(db, cluster: Cluster, manual_override: int = 0) -> ImageRole:
    """Attach a role row to the cluster's first image (or any image if it
    has multiple). Used to seed manual_override=1 for guard tests."""
    face = db.query(Face).filter_by(cluster_id=cluster.id).first()
    role = ImageRole(image_id=face.image_id, cluster_id=cluster.id,
                     role="individual", manual_override=manual_override)
    db.add(role); db.commit()
    return role


# ── accepted_cross_team field defaults ──────────────────────────────────────


def test_accepted_cross_team_defaults_to_zero(db):
    """New schema field: accepted_cross_team defaults to 0 on cluster create."""
    _, src, _ = _job_with_two_sessions(db)
    c = _cluster_with_images(db, src, label="L", image_count=1)
    db.refresh(c)
    assert c.accepted_cross_team == 0


# ── Dismiss / undismiss endpoints ───────────────────────────────────────────


def test_dismiss_sets_accepted_cross_team(db):
    _, src, _ = _job_with_two_sessions(db)
    c = _cluster_with_images(db, src, label="L", image_count=1)
    res = dismiss_cross_team(c.id, DismissCrossTeamRequest(), db)
    db.refresh(c)
    assert c.accepted_cross_team == 1
    assert res["accepted_cross_team"] is True
    assert res["cluster_id"] == c.id


def test_dismiss_idempotent(db):
    _, src, _ = _job_with_two_sessions(db)
    c = _cluster_with_images(db, src, label="L", image_count=1)
    dismiss_cross_team(c.id, DismissCrossTeamRequest(), db)
    res = dismiss_cross_team(c.id, DismissCrossTeamRequest(), db)
    db.refresh(c)
    assert c.accepted_cross_team == 1
    assert res["accepted_cross_team"] is True


def test_undismiss_clears_accepted_cross_team(db):
    _, src, _ = _job_with_two_sessions(db)
    c = _cluster_with_images(db, src, label="L", image_count=1)
    c.accepted_cross_team = 1; db.commit()
    res = undismiss_cross_team(c.id, db)
    db.refresh(c)
    assert c.accepted_cross_team == 0
    assert res["accepted_cross_team"] is False


def test_dismiss_404_on_missing_cluster(db):
    with pytest.raises(HTTPException) as exc:
        dismiss_cross_team(99999, DismissCrossTeamRequest(), db)
    assert exc.value.status_code == 404


# ── Read-time match_team_mismatch suppression when accepted ─────────────────


def test_accepted_cross_team_suppresses_match_team_mismatch(db):
    """Backend-side: the read-time filter in clusters.py:list_clusters
    drops match_team_mismatch from visible_review_reasons when
    accepted_cross_team=1. End-to-end: dismiss → flag clears in UI without
    moving the cluster."""
    from app.api.clusters import list_clusters
    from app.models.db_models import Player, PlayerMembership

    job, src, _ = _job_with_two_sessions(db, source_name="WrongTeam")
    # Player is rostered on RealTeam; cluster is in WrongTeam session.
    p = Player(norm_name="alice", display_name="Alice")
    db.add(p); db.commit(); db.refresh(p)
    db.add(PlayerMembership(player_id=p.id, job_id=job.id,
                            team_name="RealTeam", norm_team="realteam"))
    db.commit()
    c = _cluster_with_images(db, src, label="Alice", image_count=2)
    c.match_tier = "high"
    c.matched_player_id = p.id
    db.commit()
    # Without dismiss: match_team_mismatch fires.
    before = list_clusters(src.id, db)
    assert any("match_team_mismatch" in cl["visible_review_reasons"]
               for cl in before)
    # After dismiss: flag suppressed.
    c.accepted_cross_team = 1
    db.commit()
    after = list_clusters(src.id, db)
    for cl in after:
        assert "match_team_mismatch" not in cl["visible_review_reasons"]


# ── Safety guards: _check_move_safety helper + endpoint behavior ────────────


def test_check_move_safety_clean_returns_none(db):
    """No blockers (no manual_override=1 roles, source not reviewed)."""
    _, src, _ = _job_with_two_sessions(db)
    c = _cluster_with_images(db, src, label="L", image_count=2)
    assert _check_move_safety(db, c, force=False) is None


def test_check_move_safety_manual_override_blocks(db):
    """An ImageRole.manual_override=1 on any of the cluster's images
    blocks the move without force=True."""
    _, src, _ = _job_with_two_sessions(db)
    c = _cluster_with_images(db, src, label="L", image_count=2)
    _add_image_role(db, c, manual_override=1)
    impact = _check_move_safety(db, c, force=False)
    assert impact is not None
    assert impact["manual_role_overrides"] >= 1


def test_check_move_safety_reviewed_session_blocks(db):
    """Source session reviewed=1 blocks the move without force=True."""
    _, src, _ = _job_with_two_sessions(db)
    src.reviewed = 1; db.commit()
    c = _cluster_with_images(db, src, label="L", image_count=2)
    impact = _check_move_safety(db, c, force=False)
    assert impact is not None
    assert impact["source_session_reviewed"] is True


def test_check_move_safety_force_bypasses_all_guards(db):
    """force=True returns None even when blockers are present."""
    _, src, _ = _job_with_two_sessions(db)
    src.reviewed = 1; db.commit()
    c = _cluster_with_images(db, src, label="L", image_count=2)
    _add_image_role(db, c, manual_override=1)
    assert _check_move_safety(db, c, force=True) is None


# ── move-with-guards endpoint: integration over the helper ──────────────────


def test_move_with_guards_happy_path_moves_cluster(db):
    """No blockers → move proceeds via existing /move machinery. Target
    cluster sits in the target session; accepted_cross_team cleared on
    moved cluster (now in the right team — no longer a cross-team case)."""
    _, src, tgt = _job_with_two_sessions(db)
    c = _cluster_with_images(db, src, label="L", image_count=2)
    c.accepted_cross_team = 1; db.commit()  # was dismissed previously
    res = move_cluster_with_guards(
        c.id, MoveWithGuardsRequest(target_session_id=tgt.id), db,
    )
    db.expire_all()
    moved = db.query(Cluster).get(c.id)
    assert moved.session_id == tgt.id
    assert moved.accepted_cross_team == 0   # cleared by the move
    assert res["status"] == "moved"
    assert res["target_session_id"] == tgt.id


def test_move_with_guards_manual_override_blocks_without_force(db):
    """ImageRole.manual_override=1 → 409 with impact dict; cluster
    unchanged."""
    _, src, tgt = _job_with_two_sessions(db)
    c = _cluster_with_images(db, src, label="L", image_count=2)
    _add_image_role(db, c, manual_override=1)
    orig_session = c.session_id
    with pytest.raises(HTTPException) as exc:
        move_cluster_with_guards(
            c.id, MoveWithGuardsRequest(target_session_id=tgt.id), db,
        )
    assert exc.value.status_code == 409
    assert "manual_role_overrides" in exc.value.detail["impact"]
    db.refresh(c)
    assert c.session_id == orig_session   # unchanged


def test_move_with_guards_reviewed_session_blocks_without_force(db):
    """Source session reviewed=1 → 409; cluster unchanged."""
    _, src, tgt = _job_with_two_sessions(db)
    src.reviewed = 1; db.commit()
    c = _cluster_with_images(db, src, label="L", image_count=2)
    orig_session = c.session_id
    with pytest.raises(HTTPException) as exc:
        move_cluster_with_guards(
            c.id, MoveWithGuardsRequest(target_session_id=tgt.id), db,
        )
    assert exc.value.status_code == 409
    assert exc.value.detail["impact"]["source_session_reviewed"] is True
    db.refresh(c)
    assert c.session_id == orig_session


def test_move_with_guards_force_true_overrides_both(db):
    """force=True bypasses BOTH guards. Move proceeds."""
    _, src, tgt = _job_with_two_sessions(db)
    src.reviewed = 1; db.commit()
    c = _cluster_with_images(db, src, label="L", image_count=2)
    _add_image_role(db, c, manual_override=1)
    res = move_cluster_with_guards(
        c.id, MoveWithGuardsRequest(target_session_id=tgt.id, force=True), db,
    )
    db.expire_all()
    moved = db.query(Cluster).get(c.id)
    assert moved.session_id == tgt.id
    assert res["status"] == "moved"


def test_move_with_guards_combined_blockers_both_surfaced(db):
    """If multiple blockers are present, the impact dict surfaces both
    counts so the UI can show the operator everything they're being
    asked to override."""
    _, src, tgt = _job_with_two_sessions(db)
    src.reviewed = 1; db.commit()
    c = _cluster_with_images(db, src, label="L", image_count=2)
    _add_image_role(db, c, manual_override=1)
    with pytest.raises(HTTPException) as exc:
        move_cluster_with_guards(
            c.id, MoveWithGuardsRequest(target_session_id=tgt.id), db,
        )
    impact = exc.value.detail["impact"]
    assert impact["manual_role_overrides"] >= 1
    assert impact["source_session_reviewed"] is True


def test_move_with_guards_validates_target_same_job(db):
    """Cross-job moves still rejected — defers to the underlying
    cluster_move validation."""
    job, src, tgt = _job_with_two_sessions(db)
    # Stand up a session in a different job.
    other_job = Job(name="OtherJob", root_path="/tmp/other", has_lines=0)
    db.add(other_job); db.commit(); db.refresh(other_job)
    other_session = Session(
        job_id=other_job.id, name="Other", source_path="/tmp/other/Other",
        status="done", created_at=datetime.utcnow(),
    )
    db.add(other_session); db.commit(); db.refresh(other_session)
    c = _cluster_with_images(db, src, label="L", image_count=2)
    with pytest.raises(HTTPException) as exc:
        move_cluster_with_guards(
            c.id, MoveWithGuardsRequest(target_session_id=other_session.id), db,
        )
    assert exc.value.status_code == 400


def test_move_with_guards_target_archived_rejected(db):
    """Archived target rejected — defers to underlying validation."""
    _, src, tgt = _job_with_two_sessions(db)
    tgt.archived = 1; db.commit()
    c = _cluster_with_images(db, src, label="L", image_count=2)
    with pytest.raises(HTTPException) as exc:
        move_cluster_with_guards(
            c.id, MoveWithGuardsRequest(target_session_id=tgt.id), db,
        )
    assert exc.value.status_code == 400
