"""Phase B (2026-06-25) — COPY a cluster to another team.

Use case: a coach photographed once who belongs to multiple teams. The
operator copies the coach cluster from team A's session onto every team
the coach belongs to. Each team's export folder then includes the coach
photo.

Locked design:
  - Faces NOT duplicated — the destination is a static snapshot. Won't get
    face-match auto-labeling, won't survive a pipeline re-run on its
    session (no faces = nothing to re-sort). Surfaced via the
    `_runall_impact.copied_clusters` count + a "📋 copy" badge.
  - Image rows DUPLICATED (option β) — destination Image rows are new
    (new image_id, same path/filename/capture_time/copyright_tag, dest
    session_id). Makes the copy independent in BOTH export modes AND
    sidesteps the reject-leak global rejected-wins rule (different
    image_ids → separate _best_role_map entries).
  - Auto-unreview the destination session — mirror MOVE's
    _unreview_target pattern. No force flag.
  - Guards: target exists, target not archived, same job, source ≠
    target session.

Test coverage:
  - Happy path: cluster row, ImageRole rows, Image rows all duplicate
    correctly; Face rows NOT duplicated.
  - Cluster fields: auto_label/manual_label/is_likely_coach/
    manual_coach_override/image_count copied; matched_player_id/match_*/
    auto_label_source NOT copied (None); accepted_cross_team reset to 0.
  - Image-duplication shape: new image_id, same path/filename, dest
    session_id.
  - Independence (the load-bearing claim): editing source after copy
    must NOT affect destination, and vice versa. One test per edit type
    (rename / role change / reject / delete source cluster / move
    source cluster).
  - Reject-leak independence: source's rejection on image_id X1 does NOT
    suppress destination's export of image_id X2 (the duplicate).
    Different image_ids → independent _best_role_map entries.
  - Guards: archived target / cross-job / same-session / missing source
    or target all refused with appropriate HTTP code.
  - Source cluster unmodified post-copy.
  - has_faces field in the cluster API response surfaces the copy state.
"""
from datetime import datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models.db_models import (
    Cluster, Face, Image, ImageRole, Job, Session as DbSess,
)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'copy-test.db'}",
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


# ── Builders ─────────────────────────────────────────────────────────────


def _job_with_two_sessions(
    db, source_name="Wizards", target_name="Eagles", target_reviewed=False,
):
    job = Job(name="J", root_path="/tmp", has_lines=0)
    db.add(job); db.commit(); db.refresh(job)
    src = DbSess(
        job_id=job.id, name=source_name, source_path=f"/tmp/{source_name}",
        status="done", created_at=datetime.utcnow(),
    )
    tgt = DbSess(
        job_id=job.id, name=target_name, source_path=f"/tmp/{target_name}",
        status="done", created_at=datetime.utcnow(),
        reviewed=1 if target_reviewed else 0,
        reviewed_at=datetime.utcnow() if target_reviewed else None,
    )
    db.add_all([src, tgt]); db.commit(); db.refresh(src); db.refresh(tgt)
    return job, src, tgt


def _cluster_with_images(
    db, session, *, label=None, manual_label=None,
    image_count=3, is_likely_coach=0, manual_coach_override=0,
    add_faces=True,
):
    """Build a cluster with `image_count` single-face images. Pass
    add_faces=False to simulate a copy's faces-absent state."""
    c = Cluster(
        session_id=session.id, image_count=image_count,
        auto_label=label, manual_label=manual_label,
        is_likely_coach=is_likely_coach,
        manual_coach_override=manual_coach_override,
    )
    db.add(c); db.commit(); db.refresh(c)
    for i in range(image_count):
        img = Image(
            session_id=session.id,
            path=f"/tmp/{session.name}/img_{c.id}_{i}.jpg",
            filename=f"img_{c.id}_{i}.jpg",
            capture_time=datetime(2026, 1, 1, 12, 0, i),
            copyright_tag=f"copyright_{i}",
        )
        db.add(img); db.commit(); db.refresh(img)
        # Auto-assigned role for the test
        role = "team" if i == image_count - 1 else "individual"
        db.add(ImageRole(image_id=img.id, cluster_id=c.id,
                         role=role, manual_override=0))
        if add_faces:
            db.add(Face(
                image_id=img.id, cluster_id=c.id, bbox="[0,0,10,10]",
                det_score=0.9, face_area_ratio=0.2,
            ))
    db.commit(); db.refresh(c)
    return c


# ── Happy path ───────────────────────────────────────────────────────────


def test_copy_creates_new_cluster_row(db):
    """Cluster row is duplicated with copyable fields preserved + skip
    fields nulled."""
    from app.api.cluster_copy import copy_cluster_to_session
    _, src, tgt = _job_with_two_sessions(db)
    c = _cluster_with_images(
        db, src, label="auto-label", manual_label="Coach Smith",
        is_likely_coach=1, manual_coach_override=1,
    )
    # Stamp the skip fields on source so we can verify they get nulled on copy.
    c.matched_player_id = None  # would be set in production; left None for test simplicity
    c.match_confidence = 0.9
    c.match_tier = "high"
    c.match_scope = "roster"
    c.auto_label_source = "match"
    c.accepted_cross_team = 1
    db.commit()

    result = copy_cluster_to_session(db, c.id, tgt.id)
    db.expire_all()

    new = db.query(Cluster).get(result["new_cluster_id"])
    assert new.session_id == tgt.id
    # Copied fields
    assert new.auto_label == "auto-label"
    assert new.manual_label == "Coach Smith"
    assert new.is_likely_coach == 1
    assert new.manual_coach_override == 1
    assert new.image_count == 3
    # Skip fields nulled
    assert new.match_confidence is None
    assert new.match_tier is None
    assert new.match_scope is None
    assert new.auto_label_source is None
    # accepted_cross_team reset
    assert new.accepted_cross_team == 0
    # New cluster has a distinct id
    assert new.id != c.id


def test_copy_duplicates_image_rows_with_same_path(db):
    """Each source Image row produces a new Image row on the destination:
    same path/filename/capture_time/copyright_tag, new id, dest session_id."""
    from app.api.cluster_copy import copy_cluster_to_session
    _, src, tgt = _job_with_two_sessions(db)
    c = _cluster_with_images(db, src, image_count=3)
    source_image_ids = {ir.image_id for ir in
                        db.query(ImageRole).filter_by(cluster_id=c.id).all()}
    source_paths = {db.query(Image).get(iid).path for iid in source_image_ids}

    result = copy_cluster_to_session(db, c.id, tgt.id)
    db.expire_all()
    new = db.query(Cluster).get(result["new_cluster_id"])
    new_image_ids = {ir.image_id for ir in
                     db.query(ImageRole).filter_by(cluster_id=new.id).all()}
    assert len(new_image_ids) == 3
    assert new_image_ids.isdisjoint(source_image_ids), (
        "destination Image rows must be NEW image_ids, not shared with source"
    )
    new_imgs = [db.query(Image).get(iid) for iid in new_image_ids]
    # All new Image rows are in the destination session
    assert all(i.session_id == tgt.id for i in new_imgs)
    # Same paths as source (point at same physical files)
    new_paths = {i.path for i in new_imgs}
    assert new_paths == source_paths
    # Metadata copied
    for src_iid in source_image_ids:
        src_img = db.query(Image).get(src_iid)
        # Find the destination's row with same path
        matching = [i for i in new_imgs if i.path == src_img.path]
        assert len(matching) == 1
        dst_img = matching[0]
        assert dst_img.filename == src_img.filename
        assert dst_img.capture_time == src_img.capture_time
        assert dst_img.copyright_tag == src_img.copyright_tag


def test_copy_does_not_duplicate_face_rows(db):
    """Locked design: Faces NOT duplicated. Destination cluster has zero
    Face rows. Source's Face rows untouched."""
    from app.api.cluster_copy import copy_cluster_to_session
    _, src, tgt = _job_with_two_sessions(db)
    c = _cluster_with_images(db, src, image_count=3)
    source_face_count = db.query(Face).filter_by(cluster_id=c.id).count()
    assert source_face_count == 3  # sanity

    result = copy_cluster_to_session(db, c.id, tgt.id)
    db.expire_all()
    new = db.query(Cluster).get(result["new_cluster_id"])
    assert db.query(Face).filter_by(cluster_id=new.id).count() == 0
    # Source's Face rows untouched
    assert db.query(Face).filter_by(cluster_id=c.id).count() == source_face_count


def test_copy_duplicates_image_roles_with_same_role_and_override(db):
    """ImageRole rows are duplicated 1-for-1 with role + manual_override
    preserved, but pointing at the NEW (image_id, cluster_id) pair."""
    from app.api.cluster_copy import copy_cluster_to_session
    _, src, tgt = _job_with_two_sessions(db)
    c = _cluster_with_images(db, src, image_count=3)
    # Mark one role as manual_override so we can verify it copies.
    src_role = db.query(ImageRole).filter_by(cluster_id=c.id, role="team").first()
    src_role.manual_override = 1
    db.commit()
    source_pairs = {
        (ir.image_id, ir.role, ir.manual_override)
        for ir in db.query(ImageRole).filter_by(cluster_id=c.id).all()
    }

    result = copy_cluster_to_session(db, c.id, tgt.id)
    db.expire_all()
    new_cluster_id = result["new_cluster_id"]
    new_rows = db.query(ImageRole).filter_by(cluster_id=new_cluster_id).all()
    assert len(new_rows) == 3
    # Same set of (role, manual_override) values; image_ids are different but
    # filenames/paths align — check by mapping back through filename.
    src_role_by_filename = {}
    for image_id, role, override in source_pairs:
        fn = db.query(Image).get(image_id).filename
        src_role_by_filename[fn] = (role, override)
    for ir in new_rows:
        fn = db.query(Image).get(ir.image_id).filename
        assert src_role_by_filename[fn] == (ir.role, ir.manual_override)


# ── Auto-unreview ────────────────────────────────────────────────────────


def test_copy_auto_unreviews_target_session(db):
    """If target session was reviewed=1, copy flips it back to 0 (mirror
    MOVE's _unreview_target). Operator must re-confirm review after copy
    brings new content in."""
    from app.api.cluster_copy import copy_cluster_to_session
    _, src, tgt = _job_with_two_sessions(db, target_reviewed=True)
    assert tgt.reviewed == 1
    c = _cluster_with_images(db, src, image_count=2)

    result = copy_cluster_to_session(db, c.id, tgt.id)
    db.expire_all()
    tgt2 = db.query(DbSess).get(tgt.id)
    assert tgt2.reviewed == 0
    assert tgt2.reviewed_at is None
    assert result.get("target_unreviewed") is True


def test_copy_does_not_unreview_already_unreviewed_target(db):
    """A target that wasn't reviewed stays unreviewed. Returns
    target_unreviewed=False so the FE knows nothing changed."""
    from app.api.cluster_copy import copy_cluster_to_session
    _, src, tgt = _job_with_two_sessions(db, target_reviewed=False)
    c = _cluster_with_images(db, src, image_count=2)
    result = copy_cluster_to_session(db, c.id, tgt.id)
    assert result.get("target_unreviewed") is False


# ── Source preservation ─────────────────────────────────────────────────


def test_source_cluster_unmodified_post_copy(db):
    """Source cluster row + its ImageRole rows + its Face rows are
    BYTE-FOR-BYTE unchanged after copy. The whole point of copy vs.
    move is the source stays put."""
    from app.api.cluster_copy import copy_cluster_to_session
    _, src, tgt = _job_with_two_sessions(db)
    c = _cluster_with_images(
        db, src, label="Coach Smith", image_count=3, manual_coach_override=1,
    )
    pre_copy_cluster = {
        col.name: getattr(c, col.name) for col in c.__table__.columns
    }
    pre_copy_roles = {
        (ir.image_id, ir.role, ir.manual_override)
        for ir in db.query(ImageRole).filter_by(cluster_id=c.id).all()
    }
    pre_copy_face_count = db.query(Face).filter_by(cluster_id=c.id).count()

    copy_cluster_to_session(db, c.id, tgt.id)
    db.expire_all()
    c = db.query(Cluster).get(c.id)
    post_copy_cluster = {
        col.name: getattr(c, col.name) for col in c.__table__.columns
    }
    post_copy_roles = {
        (ir.image_id, ir.role, ir.manual_override)
        for ir in db.query(ImageRole).filter_by(cluster_id=c.id).all()
    }
    post_copy_face_count = db.query(Face).filter_by(cluster_id=c.id).count()
    assert pre_copy_cluster == post_copy_cluster
    assert pre_copy_roles == post_copy_roles
    assert pre_copy_face_count == post_copy_face_count


# ── Independence (the load-bearing claim) ───────────────────────────────


def test_independence_source_rename_does_not_affect_destination(db):
    """Renaming source cluster (manual_label change) leaves destination's
    manual_label unchanged."""
    from app.api.cluster_copy import copy_cluster_to_session
    _, src, tgt = _job_with_two_sessions(db)
    c = _cluster_with_images(db, src, manual_label="Original")
    result = copy_cluster_to_session(db, c.id, tgt.id)
    new_id = result["new_cluster_id"]

    # Mutate source
    c.manual_label = "Renamed-on-Source"
    db.commit()

    new = db.query(Cluster).get(new_id)
    assert new.manual_label == "Original"


def test_independence_source_role_change_does_not_affect_destination(db):
    """Changing a source image's role (set_role) modifies the SOURCE's
    ImageRole row only. Destination's parallel ImageRole row keeps its
    original role."""
    from app.api.cluster_copy import copy_cluster_to_session
    _, src, tgt = _job_with_two_sessions(db)
    c = _cluster_with_images(db, src, image_count=3)
    result = copy_cluster_to_session(db, c.id, tgt.id)
    new_id = result["new_cluster_id"]

    # Mutate source's first ImageRole
    src_first_role = (
        db.query(ImageRole)
        .filter_by(cluster_id=c.id, role="individual")
        .first()
    )
    src_first_role.role = "panoramic"
    src_first_role.manual_override = 1
    db.commit()

    # Find the destination's parallel row (same filename, different image_id)
    src_filename = db.query(Image).get(src_first_role.image_id).filename
    dst_imgs = (
        db.query(Image)
        .filter(Image.session_id == tgt.id, Image.filename == src_filename)
        .all()
    )
    assert len(dst_imgs) == 1
    dst_role = db.query(ImageRole).filter_by(
        cluster_id=new_id, image_id=dst_imgs[0].id,
    ).one()
    assert dst_role.role == "individual"
    assert dst_role.manual_override == 0


def test_independence_source_reject_does_not_propagate_to_destination(db):
    """Reject-leak independence: rejecting an image on the source's
    ImageRole row sets that row's role='rejected'. _best_role_map keys by
    image_id, and source/destination have DIFFERENT image_ids (option β),
    so source's rejection of image_id X1 does NOT enter the destination's
    image_id X2's resolution. Destination still exports the photo."""
    from app.api.jobs import _best_role_map
    from app.api.cluster_copy import copy_cluster_to_session
    _, src, tgt = _job_with_two_sessions(db)
    c = _cluster_with_images(db, src, image_count=3)
    result = copy_cluster_to_session(db, c.id, tgt.id)
    new_id = result["new_cluster_id"]

    # Reject one image on the source
    src_role = db.query(ImageRole).filter_by(cluster_id=c.id, role="individual").first()
    rejected_image_id = src_role.image_id
    src_role.role = "rejected"
    src_role.manual_override = 1
    db.commit()

    # _best_role_map for source's image_id resolves to 'rejected'
    src_map = _best_role_map(db, [rejected_image_id])
    assert src_map[rejected_image_id] == "rejected"

    # Find the destination's parallel image_id (same filename, dest session)
    src_filename = db.query(Image).get(rejected_image_id).filename
    dst_img = db.query(Image).filter(
        Image.session_id == tgt.id, Image.filename == src_filename,
    ).one()

    # _best_role_map for destination's image_id resolves to its OWN role,
    # NOT rejected. This is the load-bearing independence claim.
    dst_map = _best_role_map(db, [dst_img.id])
    assert dst_map[dst_img.id] == "individual"
    assert dst_map[dst_img.id] != "rejected"


def test_independence_destination_role_change_does_not_affect_source(db):
    """Symmetric: changing a destination role doesn't bleed back to source."""
    from app.api.cluster_copy import copy_cluster_to_session
    _, src, tgt = _job_with_two_sessions(db)
    c = _cluster_with_images(db, src, image_count=3)
    result = copy_cluster_to_session(db, c.id, tgt.id)
    new_id = result["new_cluster_id"]

    dst_role = db.query(ImageRole).filter_by(
        cluster_id=new_id, role="individual",
    ).first()
    dst_role.role = "rejected"
    dst_role.manual_override = 1
    db.commit()

    # Look up source's parallel by filename
    dst_filename = db.query(Image).get(dst_role.image_id).filename
    src_img = db.query(Image).filter(
        Image.session_id == src.id, Image.filename == dst_filename,
    ).one()
    src_parallel_role = db.query(ImageRole).filter_by(
        cluster_id=c.id, image_id=src_img.id,
    ).one()
    assert src_parallel_role.role == "individual"


def test_independence_delete_source_cluster_does_not_break_destination(db):
    """Deleting the source cluster (cascade removes its ImageRole rows
    too) leaves the destination's cluster + ImageRole rows + Image rows
    intact."""
    from app.api.cluster_copy import copy_cluster_to_session
    _, src, tgt = _job_with_two_sessions(db)
    c = _cluster_with_images(db, src, image_count=3)
    result = copy_cluster_to_session(db, c.id, tgt.id)
    new_id = result["new_cluster_id"]

    # Delete source cluster + its ImageRole rows
    db.query(ImageRole).filter_by(cluster_id=c.id).delete(synchronize_session=False)
    db.delete(c)
    db.commit()
    db.expire_all()

    new = db.query(Cluster).get(new_id)
    assert new is not None
    assert new.session_id == tgt.id
    role_count = db.query(ImageRole).filter_by(cluster_id=new_id).count()
    assert role_count == 3
    # Destination Image rows still exist
    dst_image_count = db.query(Image).filter_by(session_id=tgt.id).count()
    assert dst_image_count >= 3


# ── Guards ──────────────────────────────────────────────────────────────


def test_guard_archived_target_refused(db):
    from app.api.cluster_copy import copy_cluster_to_session
    _, src, tgt = _job_with_two_sessions(db)
    tgt.archived = 1; db.commit()
    c = _cluster_with_images(db, src, image_count=2)
    with pytest.raises(HTTPException) as exc:
        copy_cluster_to_session(db, c.id, tgt.id)
    assert exc.value.status_code == 400


def test_guard_cross_job_refused(db):
    from app.api.cluster_copy import copy_cluster_to_session
    job_a, src, _ = _job_with_two_sessions(db)
    job_b = Job(name="OtherJob", root_path="/tmp/other", has_lines=0)
    db.add(job_b); db.commit(); db.refresh(job_b)
    foreign = DbSess(
        job_id=job_b.id, name="Foreign", source_path="/tmp/foreign",
        status="done", created_at=datetime.utcnow(),
    )
    db.add(foreign); db.commit(); db.refresh(foreign)
    c = _cluster_with_images(db, src, image_count=2)
    with pytest.raises(HTTPException) as exc:
        copy_cluster_to_session(db, c.id, foreign.id)
    assert exc.value.status_code == 400


def test_guard_same_session_refused(db):
    """Refusing same-session copy per Phase B design decision."""
    from app.api.cluster_copy import copy_cluster_to_session
    _, src, _ = _job_with_two_sessions(db)
    c = _cluster_with_images(db, src, image_count=2)
    with pytest.raises(HTTPException) as exc:
        copy_cluster_to_session(db, c.id, src.id)
    assert exc.value.status_code == 400


def test_guard_missing_source_404(db):
    from app.api.cluster_copy import copy_cluster_to_session
    _, _, tgt = _job_with_two_sessions(db)
    with pytest.raises(HTTPException) as exc:
        copy_cluster_to_session(db, 99999, tgt.id)
    assert exc.value.status_code == 404


def test_guard_missing_target_404(db):
    from app.api.cluster_copy import copy_cluster_to_session
    _, src, _ = _job_with_two_sessions(db)
    c = _cluster_with_images(db, src, image_count=1)
    with pytest.raises(HTTPException) as exc:
        copy_cluster_to_session(db, c.id, 99999)
    assert exc.value.status_code == 404


# ── _runall_impact extension ─────────────────────────────────────────────


def test_runall_impact_counts_copied_clusters(db):
    """A cluster with zero Face rows (i.e., a copy) is counted by
    _runall_impact so the run-all confirm dialog can warn the operator
    before a wipe deletes content that cannot be re-derived."""
    from app.api.jobs import _runall_impact
    _, src, tgt = _job_with_two_sessions(db)
    # Source has a normal cluster (with faces)
    _cluster_with_images(db, src, image_count=3, add_faces=True)
    # Target gets a simulated copy: cluster row + ImageRole rows, NO faces
    _cluster_with_images(db, tgt, image_count=3, add_faces=False)
    impact = _runall_impact(db, [src.id, tgt.id])
    assert impact.get("copied_clusters") == 1


def test_runall_impact_zero_copied_when_all_have_faces(db):
    """Sanity: when every cluster has faces (normal pipeline output),
    copied_clusters count is 0."""
    from app.api.jobs import _runall_impact
    _, src, _ = _job_with_two_sessions(db)
    _cluster_with_images(db, src, image_count=3, add_faces=True)
    impact = _runall_impact(db, [src.id])
    assert impact.get("copied_clusters") == 0


# ── has_faces field in cluster API response ──────────────────────────────


def test_cluster_response_includes_has_faces(db):
    """list_clusters response surfaces has_faces=True for normal clusters
    and has_faces=False for copies. The FE uses this to render the
    "📋 copy" badge."""
    from app.api.clusters import list_clusters
    _, src, _ = _job_with_two_sessions(db)
    normal = _cluster_with_images(db, src, image_count=2, add_faces=True)
    copy_like = _cluster_with_images(db, src, image_count=2, add_faces=False)
    out = list_clusters(src.id, db)
    by_id = {c["cluster_id"]: c for c in out}
    assert by_id[normal.id]["has_faces"] is True
    assert by_id[copy_like.id]["has_faces"] is False


def test_list_clusters_returns_images_for_copied_cluster_with_no_faces(db):
    """Regression for the Phase B display bug: list_clusters must surface a
    copied cluster's images (read via ImageRole, not c.faces). Otherwise the
    card shows image_count=N in the header and 0 thumbnails in the body —
    exactly what the screenshot showed for cluster 6825 (American Legion).
    End-to-end: real copy via the endpoint, then list_clusters on the target."""
    from app.api.clusters import list_clusters
    from app.api.cluster_copy import copy_cluster_to_session
    _, src, tgt = _job_with_two_sessions(db)
    c = _cluster_with_images(db, src, image_count=5)
    result = copy_cluster_to_session(db, c.id, tgt.id)
    db.expire_all()
    out = list_clusters(tgt.id, db)
    copy_row = next(r for r in out if r["cluster_id"] == result["new_cluster_id"])
    # Header and body now agree — the regression.
    assert copy_row["image_count"] == 5
    assert len(copy_row["images"]) == 5
    assert copy_row["has_faces"] is False
    # Each thumbnail points at the copy's NEW image_ids (not the source's) —
    # confirms the read path goes through ImageRole.cluster_id and not some
    # path-keyed shortcut that would collide source/dest.
    for img in copy_row["images"]:
        assert img["thumb_url"] == f"/api/images/{img['image_id']}/thumb"
        assert img["full_url"] == f"/api/images/{img['image_id']}/full"
        assert img["role"] in ("individual", "team", "panoramic", "buddy")
