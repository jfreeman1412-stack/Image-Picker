"""2026-09-21 override-survival tests (Option 1 — snapshot-and-restore).

Load-bearing regression suite for the "manual overrides wiped by
force run-all" bug reported after session 1123. Operator hand-fixes
pano/coach/role labels, someone force-reruns the pipeline, corrections
are gone because _clear_prior_results deletes Clusters and ImageRoles,
and re-clustering builds a whole new set of rows.

Option 1 solution: snapshot manual state (manual_label,
manual_coach_override, accepted_cross_team, manual_override=1
ImageRoles) BEFORE the wipe; after re-clustering, re-anchor each
snapshot to the new cluster with the largest image-set overlap. Uses
Image.id, which IS stable across re-runs (only Faces/Clusters/
ImageRoles are wiped).

These tests exercise _snapshot_manual_state + _restore_manual_state
directly against synthetic states — no need to run the whole pipeline.
"""
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models.db_models import (
    Cluster, Face, Image, ImageRole, Job, Session as SessionModel,
)
from app.services.face_pipeline import (
    _snapshot_manual_state, _restore_manual_state,
    _mark_session_with_override_loss_flag,
)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'ov-test.db'}",
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


# ── Builders ────────────────────────────────────────────────────────────


def _session(db, name="T"):
    job = Job(name="J", root_path="/tmp", has_lines=0)
    db.add(job); db.commit(); db.refresh(job)
    s = SessionModel(job_id=job.id, name=name, source_path=f"/tmp/{name}",
                     status="done", created_at=datetime.utcnow())
    db.add(s); db.commit(); db.refresh(s)
    return s


def _images(db, session, n):
    imgs = []
    for i in range(n):
        img = Image(session_id=session.id, path=f"/tmp/{session.name}/{i}.jpg",
                    filename=f"{i}.jpg",
                    capture_time=datetime(2026, 1, 1, 12, 0, i))
        db.add(img); db.commit(); db.refresh(img)
        imgs.append(img)
    return imgs


def _cluster(
    db, session, images, *, manual_label=None, manual_coach_override=0,
    accepted_cross_team=0, auto_label=None,
):
    c = Cluster(
        session_id=session.id, image_count=len(images),
        auto_label=auto_label, manual_label=manual_label,
        manual_coach_override=manual_coach_override,
        accepted_cross_team=accepted_cross_team,
    )
    db.add(c); db.commit(); db.refresh(c)
    for img in images:
        db.add(Face(image_id=img.id, cluster_id=c.id,
                    bbox="[0,0,10,10]", det_score=0.9))
    db.commit()
    return c


def _pin(db, image_id, cluster_id, role):
    """Add an ImageRole with manual_override=1 to represent an operator's
    manual pin."""
    db.add(ImageRole(image_id=image_id, cluster_id=cluster_id,
                     role=role, manual_override=1))
    db.commit()


def _reroute_faces(db, session_id, image_to_new_cluster: dict):
    """Simulate the outcome of `_clear_prior_results + re-cluster` in one
    step, without running the actual pipeline. Deletes all Face/Cluster/
    ImageRole rows for the session, then creates fresh Cluster rows and
    Face rows per the provided image→new_cluster_key mapping.
    Returns {new_cluster_key: cluster_id} for the newly-created rows.
    """
    db.query(ImageRole).filter(
        ImageRole.cluster_id.in_(
            db.query(Cluster.id).filter_by(session_id=session_id)
        )
    ).delete(synchronize_session=False)
    db.query(Face).filter(
        Face.image_id.in_(
            db.query(Image.id).filter_by(session_id=session_id)
        )
    ).delete(synchronize_session=False)
    db.query(Cluster).filter_by(session_id=session_id).delete(
        synchronize_session=False,
    )
    db.commit()

    # New clusters keyed as the mapping's values.
    unique_keys = sorted(set(image_to_new_cluster.values()))
    key_to_cid = {}
    for key in unique_keys:
        c = Cluster(session_id=session_id, image_count=0)
        db.add(c); db.commit(); db.refresh(c)
        key_to_cid[key] = c.id
    for image_id, key in image_to_new_cluster.items():
        db.add(Face(image_id=image_id, cluster_id=key_to_cid[key],
                    bbox="[0,0,10,10]", det_score=0.9))
    db.commit()
    return key_to_cid


# ── Tests ───────────────────────────────────────────────────────────────


def test_snapshot_skips_clusters_with_no_manual_state(db):
    """A cluster with no manual_label, no manual_coach_override, no
    accepted_cross_team, and no manual_override=1 ImageRoles has no
    state worth snapshotting. Skip it."""
    s = _session(db)
    imgs = _images(db, s, 3)
    _cluster(db, s, imgs, auto_label="Alice")  # only auto_label — no manuals
    snaps = _snapshot_manual_state(db, s.id)
    assert snaps == []


def test_snapshot_captures_manual_label(db):
    s = _session(db)
    imgs = _images(db, s, 3)
    c = _cluster(db, s, imgs, manual_label="Eleanor")
    snaps = _snapshot_manual_state(db, s.id)
    assert len(snaps) == 1
    assert snaps[0]["manual_label"] == "Eleanor"
    assert snaps[0]["manual_coach_override"] == 0
    assert snaps[0]["accepted_cross_team"] == 0
    assert snaps[0]["manual_role_picks"] == []
    assert snaps[0]["member_image_ids"] == {i.id for i in imgs}
    assert snaps[0]["old_cluster_id"] == c.id


def test_snapshot_captures_coach_override(db):
    s = _session(db)
    imgs = _images(db, s, 2)
    _cluster(db, s, imgs, manual_coach_override=-1)   # forced player
    snaps = _snapshot_manual_state(db, s.id)
    assert len(snaps) == 1
    assert snaps[0]["manual_coach_override"] == -1


def test_snapshot_captures_manual_role_picks(db):
    s = _session(db)
    imgs = _images(db, s, 3)
    c = _cluster(db, s, imgs)  # no cluster-level manuals
    _pin(db, imgs[1].id, c.id, "panoramic")
    _pin(db, imgs[2].id, c.id, "team")
    snaps = _snapshot_manual_state(db, s.id)
    assert len(snaps) == 1
    picks = sorted(snaps[0]["manual_role_picks"])
    assert picks == sorted([
        (imgs[1].id, "panoramic"),
        (imgs[2].id, "team"),
    ])


def test_restore_manual_label_after_perfect_match(db):
    """Old cluster's images all land in one new cluster — perfect
    overlap. Label restored on that new cluster."""
    s = _session(db)
    imgs = _images(db, s, 4)
    _cluster(db, s, imgs, manual_label="Eleanor")
    snaps = _snapshot_manual_state(db, s.id)

    # Simulate a re-cluster that produces one identical cluster.
    key_to_cid = _reroute_faces(
        db, s.id, {img.id: "new_A" for img in imgs},
    )
    _restore_manual_state(db, s.id, snaps)

    new_cluster = db.query(Cluster).get(key_to_cid["new_A"])
    assert new_cluster.manual_label == "Eleanor"


def test_restore_coach_override_and_accepted_cross_team(db):
    """Cluster-level attributes all ride together."""
    s = _session(db)
    imgs = _images(db, s, 3)
    _cluster(db, s, imgs,
             manual_coach_override=-1, accepted_cross_team=1)
    snaps = _snapshot_manual_state(db, s.id)
    key_to_cid = _reroute_faces(db, s.id, {img.id: "A" for img in imgs})
    _restore_manual_state(db, s.id, snaps)
    new = db.query(Cluster).get(key_to_cid["A"])
    assert new.manual_coach_override == -1
    assert new.accepted_cross_team == 1


def test_restore_creates_manual_imagerole_on_new_cluster(db):
    """Manual pano pin follows its image to the new cluster."""
    s = _session(db)
    imgs = _images(db, s, 3)
    c = _cluster(db, s, imgs)
    _pin(db, imgs[1].id, c.id, "panoramic")
    snaps = _snapshot_manual_state(db, s.id)

    key_to_cid = _reroute_faces(db, s.id, {img.id: "A" for img in imgs})
    _restore_manual_state(db, s.id, snaps)

    # ImageRole with manual_override=1, matching image_id + new cluster.
    row = db.query(ImageRole).filter_by(
        image_id=imgs[1].id, cluster_id=key_to_cid["A"],
    ).first()
    assert row is not None
    assert row.role == "panoramic"
    assert row.manual_override == 1


def test_restore_split_smaller_side_loses(db):
    """Old cluster's images split into two new clusters. Only the
    LARGER-overlap side gets the manual state; the smaller side's
    portion of the images gets no restored ImageRoles."""
    s = _session(db)
    imgs = _images(db, s, 6)     # 6 old-cluster images
    c = _cluster(db, s, imgs, manual_label="Eleanor")
    # Pin two images: one that lands in "big" side, one in "small" side.
    _pin(db, imgs[0].id, c.id, "team")       # will end in big
    _pin(db, imgs[5].id, c.id, "panoramic")  # will end in small
    snaps = _snapshot_manual_state(db, s.id)

    # 4 images → big, 2 → small.
    routing = {img.id: ("big" if i < 4 else "small")
               for i, img in enumerate(imgs)}
    key_to_cid = _reroute_faces(db, s.id, routing)
    _restore_manual_state(db, s.id, snaps)

    big = db.query(Cluster).get(key_to_cid["big"])
    small = db.query(Cluster).get(key_to_cid["small"])
    # Big side gets the label; small side stays clean.
    assert big.manual_label == "Eleanor"
    assert small.manual_label is None
    # Manual pin on imgs[0] (big side) survives; imgs[5] (small side)
    # does NOT get its old pin because the winning new cluster is big.
    assert db.query(ImageRole).filter_by(
        image_id=imgs[0].id, cluster_id=big.id, manual_override=1,
    ).first() is not None
    assert db.query(ImageRole).filter_by(
        image_id=imgs[5].id, manual_override=1,
    ).first() is None


def test_restore_merge_larger_snapshot_wins_the_new_cluster(db):
    """Two old clusters contend for the same new cluster (merge case).
    Snapshots are processed largest-first — the bigger old cluster's
    state wins. The runner-up is reported as a loss."""
    s = _session(db)
    big_imgs = _images(db, s, 6)
    small_imgs = _images(db, s, 3)
    _cluster(db, s, big_imgs, manual_label="Eleanor-Big")
    _cluster(db, s, small_imgs, manual_label="Alice-Small")
    snaps = _snapshot_manual_state(db, s.id)

    # All 9 images end up in one merged new cluster.
    all_imgs = big_imgs + small_imgs
    key_to_cid = _reroute_faces(
        db, s.id, {img.id: "merged" for img in all_imgs},
    )
    losses = _restore_manual_state(db, s.id, snaps)

    merged = db.query(Cluster).get(key_to_cid["merged"])
    assert merged.manual_label == "Eleanor-Big"    # bigger wins
    # Loss reported for the smaller.
    loss_labels = [l for l in losses]
    assert len(loss_labels) == 1


def test_restore_low_overlap_reports_loss_no_attach(db):
    """Old cluster's images scatter across many new tiny clusters — no
    single new one has enough overlap to attribute. Reported as loss;
    no attributes get attached anywhere.

    Threshold check: min_overlap = max(1, len(members) // 2). Snapshot
    has 6 members → needs 3. Each new cluster gets 2 → below threshold."""
    s = _session(db)
    imgs = _images(db, s, 6)
    _cluster(db, s, imgs, manual_label="Eleanor")
    snaps = _snapshot_manual_state(db, s.id)

    # Each pair of images → own new cluster (3 clusters, 2 images each).
    routing = {}
    for i, img in enumerate(imgs):
        routing[img.id] = f"tiny_{i // 2}"
    key_to_cid = _reroute_faces(db, s.id, routing)
    losses = _restore_manual_state(db, s.id, snaps)

    assert len(losses) == 1
    assert losses[0]["reason"] == "no_matching_new_cluster"
    for cid in key_to_cid.values():
        assert db.query(Cluster).get(cid).manual_label is None


def test_restore_no_snapshots_is_noop(db):
    """No manual state to snapshot → nothing to restore → no-op."""
    s = _session(db)
    imgs = _images(db, s, 3)
    _cluster(db, s, imgs)     # no manuals
    snaps = _snapshot_manual_state(db, s.id)
    assert snaps == []

    key_to_cid = _reroute_faces(db, s.id, {img.id: "A" for img in imgs})
    losses = _restore_manual_state(db, s.id, snaps)

    assert losses == []
    # The new cluster stays clean.
    new = db.query(Cluster).get(key_to_cid["A"])
    assert new.manual_label is None
    assert new.manual_coach_override == 0
    assert new.accepted_cross_team == 0
    assert db.query(ImageRole).filter_by(cluster_id=new.id).count() == 0


# ── (b) 2026-09-21: loss-visibility flag on the largest cluster ─────────
#
# When _restore_manual_state returns losses, _run_pipeline_locked calls
# _mark_session_with_override_loss_flag AFTER outlier flagging to set a
# review flag on the largest surviving cluster. The flag is
# 'manual_overrides_lost_on_rerun' — a KNOWN_FLAGS entry that lights up
# the JobDetail badge count so the operator knows to re-check that
# session instead of trusting the re-run's output blindly.


def test_loss_flag_marks_largest_cluster_needs_review(db):
    """Session with 3 clusters of different sizes → the LARGEST gets
    the flag, the others stay clean."""
    s = _session(db)
    imgs = _images(db, s, 9)
    # Cluster sizes: 5, 3, 1.
    _cluster(db, s, imgs[0:5])
    small = _cluster(db, s, imgs[5:8])
    tiny = _cluster(db, s, imgs[8:9])
    # Set image_count so ORDER BY image_count DESC picks the biggest
    # deterministically (the _cluster helper already does this).
    _mark_session_with_override_loss_flag(db, s.id, loss_count=2)

    clusters = db.query(Cluster).filter_by(session_id=s.id).all()
    largest = max(clusters, key=lambda c: c.image_count)
    assert largest.needs_review == 1
    assert largest.review_reason == "manual_overrides_lost_on_rerun"
    for c in clusters:
        if c.id != largest.id:
            assert c.needs_review == 0
            assert c.review_reason is None


def test_loss_flag_appended_to_existing_review_reason(db):
    """Sorting/outlier stages might have written a review_reason on the
    largest cluster before this helper runs (e.g. 'pano_smiling_fallback').
    The helper APPENDS, never overwrites."""
    s = _session(db)
    imgs = _images(db, s, 5)
    c = _cluster(db, s, imgs)
    c.review_reason = "pano_smiling_fallback"
    c.needs_review = 1
    db.commit()

    _mark_session_with_override_loss_flag(db, s.id, loss_count=1)

    db.refresh(c)
    reasons = c.review_reason.split(",")
    assert "pano_smiling_fallback" in reasons
    assert "manual_overrides_lost_on_rerun" in reasons


def test_loss_flag_idempotent_no_duplicate_on_second_call(db):
    """Calling twice (e.g., someone runs the pipeline again without
    fixing losses) doesn't stack duplicates of the flag."""
    s = _session(db)
    imgs = _images(db, s, 4)
    _cluster(db, s, imgs)

    _mark_session_with_override_loss_flag(db, s.id, loss_count=1)
    _mark_session_with_override_loss_flag(db, s.id, loss_count=1)

    largest = db.query(Cluster).filter_by(session_id=s.id).first()
    assert largest.review_reason.count("manual_overrides_lost_on_rerun") == 1


def test_loss_flag_noop_when_session_has_no_clusters(db):
    """A session with zero clusters (unusual — the pipeline shouldn't
    reach this helper on an empty session) is a silent no-op, not a
    crash. Logs a warning instead."""
    s = _session(db)
    # No _cluster() call — zero clusters in the session.
    _mark_session_with_override_loss_flag(db, s.id, loss_count=2)
    # No crash, and no cluster to check.
    assert db.query(Cluster).filter_by(session_id=s.id).count() == 0


def test_lossy_restore_when_wired_to_flag_helper_lights_up(db):
    """End-to-end for the (b) surface: seed manual state that will be
    dropped by re-clustering (low-overlap scatter — the reproducible
    loss scenario), run snapshot → wipe/re-cluster → restore → call
    the flag helper with the losses count exactly the way
    _run_pipeline_locked does. Assert the largest cluster is flagged."""
    s = _session(db)
    imgs = _images(db, s, 6)
    _cluster(db, s, imgs, manual_label="Eleanor")
    snaps = _snapshot_manual_state(db, s.id)

    # Scatter the 6 old images into 3 tiny new clusters of 2 each →
    # no single new cluster has 3+ overlap with the 6-member snapshot
    # (threshold = max(1, 6//2) = 3). One overlap of 2 falls below it.
    routing = {img.id: f"tiny_{i // 2}" for i, img in enumerate(imgs)}
    _reroute_faces(db, s.id, routing)
    losses = _restore_manual_state(db, s.id, snaps)
    assert len(losses) == 1, "test harness didn't produce a loss"

    # Simulate the run_pipeline hook exactly.
    _mark_session_with_override_loss_flag(db, s.id, len(losses))

    # The largest of the 3 new clusters (all size 2 — any of them by
    # id-asc tiebreak) has the flag.
    clusters = db.query(Cluster).filter_by(session_id=s.id).all()
    flagged = [c for c in clusters
               if c.review_reason
               and "manual_overrides_lost_on_rerun" in c.review_reason]
    assert len(flagged) == 1
    assert flagged[0].needs_review == 1


def test_clean_restore_does_not_flag_anything(db):
    """The negative — clean re-run with no losses must NOT light any
    review flag. Snapshot has perfect overlap with the new cluster →
    losses list is empty → helper is not called (pipeline check
    guards this) → no flag anywhere in the session."""
    s = _session(db)
    imgs = _images(db, s, 4)
    _cluster(db, s, imgs, manual_label="Alice")
    snaps = _snapshot_manual_state(db, s.id)

    # Perfect match — all 4 images land in one new cluster.
    _reroute_faces(db, s.id, {img.id: "A_prime" for img in imgs})
    losses = _restore_manual_state(db, s.id, snaps)
    assert losses == []

    # Simulate the run_pipeline guard: only call flag helper on losses.
    if losses:
        _mark_session_with_override_loss_flag(db, s.id, len(losses))

    clusters = db.query(Cluster).filter_by(session_id=s.id).all()
    for c in clusters:
        assert c.needs_review == 0, (
            "clean re-run wrongly set needs_review — helper must be "
            "gated on losses being non-empty"
        )
        assert (c.review_reason or "").find(
            "manual_overrides_lost_on_rerun"
        ) == -1


def test_loss_flag_in_known_flags_registry(db):
    """The flag code must appear in settings.KNOWN_FLAGS so the
    visibility map, filter_visible_reasons, and the frontend chip
    all know about it. Without this registration, the flag STORES
    on Cluster.review_reason but hides from the UI — the failure mode
    (b) is meant to prevent."""
    from app.api.settings import KNOWN_FLAGS
    assert "manual_overrides_lost_on_rerun" in KNOWN_FLAGS


def test_restore_drifted_image_does_not_carry_role(db):
    """Old cluster: Alice with 4 images, pano manually pinned on
    imgs[1]. Re-cluster: imgs[0], [2], [3] stay together in new cluster
    A'; imgs[1] drifts to new cluster B'. The manual pano pin's image
    is no longer with Alice's majority — its role is NOT re-created
    on either A' or B'. Alice's cluster-level state (if any) rides
    with A'.

    Rationale: a manual role ('pano') is meaningful in the context of
    ONE player-cluster. If the operator pinned image X as Alice's pano
    and re-clustering decides X actually belongs to Bob, the operator's
    original intent ('X = pano on Alice') doesn't translate to 'X =
    pano on Bob'. Auto-sort in Bob's cluster picks a fresh role for X.
    """
    s = _session(db)
    imgs = _images(db, s, 4)
    c = _cluster(db, s, imgs, manual_label="Alice")
    _pin(db, imgs[1].id, c.id, "panoramic")
    snaps = _snapshot_manual_state(db, s.id)

    # imgs[1] drifts to a different new cluster than the rest.
    routing = {img.id: "A_prime" for img in imgs}
    routing[imgs[1].id] = "B_prime"
    key_to_cid = _reroute_faces(db, s.id, routing)
    _restore_manual_state(db, s.id, snaps)

    a_prime = db.query(Cluster).get(key_to_cid["A_prime"])
    assert a_prime.manual_label == "Alice"     # cluster label survives
    # But the drifted image gets NO manual ImageRole on either side.
    assert db.query(ImageRole).filter_by(
        image_id=imgs[1].id, manual_override=1,
    ).count() == 0
