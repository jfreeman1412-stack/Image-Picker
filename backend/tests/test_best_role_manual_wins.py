"""2026-09-21 pano-export bug regression tests: _best_role and
_best_role_map must honor manual_override=1 over auto roles on the
same image.

Bug: operator manually pinned image X as pano in cluster A (via
set-role, manual_override=1). Image X was also a buddy shot with a
face in cluster B, where auto-sort tagged it 'team'. Export ran
_best_role_map, which picked the min-priority role across ALL of the
image's ImageRole rows without regard to manual_override — team (1)
beat panoramic (2), and the file exported to the Team folder instead
of the Pano folder. Same theme as the manual-overrides-wiped-by-rerun
issue: operator intent silently lost.

Fix: if any ImageRole row for the image has manual_override=1, ONLY
those manual rows compete for priority. Auto rows on the same image
are ignored. Otherwise the full set competes (unchanged behavior for
images that have no manual overrides at all).
"""
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.jobs import _best_role, _best_role_map
from app.db import Base
from app.models.db_models import (
    Cluster, Face, Image, ImageRole, Job, Session as SessionModel,
)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'br-test.db'}",
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


def _seed_two_clusters_one_shared_image(db):
    """Create session + two clusters. Insert an image that has faces in
    both clusters (a buddy shot). Returns (image_id, cluster_a_id,
    cluster_b_id).
    """
    job = Job(name="J", root_path="/tmp", has_lines=0)
    db.add(job); db.commit(); db.refresh(job)
    s = SessionModel(job_id=job.id, name="T", source_path="/tmp",
                     status="done", created_at=datetime.utcnow())
    db.add(s); db.commit(); db.refresh(s)
    a = Cluster(session_id=s.id, auto_label="Alice", image_count=1)
    b = Cluster(session_id=s.id, auto_label="Bob", image_count=1)
    db.add_all([a, b]); db.commit(); db.refresh(a); db.refresh(b)
    img = Image(session_id=s.id, path="/tmp/shared.jpg",
                filename="shared.jpg",
                capture_time=datetime(2026, 1, 1))
    db.add(img); db.commit(); db.refresh(img)
    # Two faces on the same image, one per cluster (buddy shot).
    db.add_all([
        Face(image_id=img.id, cluster_id=a.id, bbox="[0,0,10,10]",
             det_score=0.9),
        Face(image_id=img.id, cluster_id=b.id, bbox="[10,10,20,20]",
             det_score=0.9),
    ])
    db.commit()
    return img.id, a.id, b.id


# ── The load-bearing test ────────────────────────────────────────────────


def test_manual_pano_beats_auto_team_on_same_image(db):
    """The pano-export bug reproduction. Operator pins image X as pano
    in cluster A (manual_override=1). Auto-sort in cluster B assigned
    X as team (manual_override=0). Pre-fix _best_role_map returned
    'team' (priority 1 beats priority 2). Post-fix it returns
    'panoramic' — manual intent wins."""
    image_id, a_id, b_id = _seed_two_clusters_one_shared_image(db)
    db.add_all([
        ImageRole(image_id=image_id, cluster_id=a_id, role="panoramic",
                  manual_override=1),
        ImageRole(image_id=image_id, cluster_id=b_id, role="team",
                  manual_override=0),
    ])
    db.commit()

    # _best_role (per-image) and _best_role_map (bulk) must agree.
    assert _best_role(db, image_id) == "panoramic", (
        "manual pano lost to auto team — pano-export bug is back"
    )
    assert _best_role_map(db, [image_id]) == {image_id: "panoramic"}


def test_manual_team_beats_auto_panoramic_on_same_image(db):
    """Symmetric: manual team wins over auto pano. The fix is not
    'pano always wins' — it's 'manual always wins'."""
    image_id, a_id, b_id = _seed_two_clusters_one_shared_image(db)
    db.add_all([
        ImageRole(image_id=image_id, cluster_id=a_id, role="team",
                  manual_override=1),
        ImageRole(image_id=image_id, cluster_id=b_id, role="panoramic",
                  manual_override=0),
    ])
    db.commit()
    assert _best_role_map(db, [image_id]) == {image_id: "team"}


def test_manual_rejected_still_wins_by_priority_within_manuals(db):
    """When multiple manuals exist for the same image, priority still
    orders WITHIN the manual pool. Manual rejected beats manual team."""
    image_id, a_id, b_id = _seed_two_clusters_one_shared_image(db)
    db.add_all([
        ImageRole(image_id=image_id, cluster_id=a_id, role="rejected",
                  manual_override=1),
        ImageRole(image_id=image_id, cluster_id=b_id, role="team",
                  manual_override=1),
    ])
    db.commit()
    assert _best_role_map(db, [image_id]) == {image_id: "rejected"}


def test_all_auto_roles_priority_unchanged(db):
    """No manual overrides anywhere on this image — full priority set
    competes exactly as pre-fix. Regression guard for the happy path."""
    image_id, a_id, b_id = _seed_two_clusters_one_shared_image(db)
    db.add_all([
        ImageRole(image_id=image_id, cluster_id=a_id, role="team",
                  manual_override=0),
        ImageRole(image_id=image_id, cluster_id=b_id, role="panoramic",
                  manual_override=0),
    ])
    db.commit()
    # Full priority selection: team(1) < panoramic(2) → team wins.
    assert _best_role_map(db, [image_id]) == {image_id: "team"}


def test_auto_rejected_still_wins_across_auto_pool(db):
    """The rejected-wins invariant across the full pool is preserved
    when no manuals exist — an auto-rejected buddy still can't leak
    into another cluster's export via a non-rejected role."""
    image_id, a_id, b_id = _seed_two_clusters_one_shared_image(db)
    db.add_all([
        ImageRole(image_id=image_id, cluster_id=a_id, role="buddy",
                  manual_override=0),
        ImageRole(image_id=image_id, cluster_id=b_id, role="rejected",
                  manual_override=0),
    ])
    db.commit()
    assert _best_role_map(db, [image_id]) == {image_id: "rejected"}


def test_manual_reject_beats_auto_reject_at_same_role(db):
    """Sanity: when the role tier is the same (both rejected), the
    manual row is preferred (chosen from the manual pool), and the
    output is still 'rejected'. Locks in that the fix doesn't drop
    manual precedence when it happens to be redundant."""
    image_id, a_id, b_id = _seed_two_clusters_one_shared_image(db)
    db.add_all([
        ImageRole(image_id=image_id, cluster_id=a_id, role="rejected",
                  manual_override=1),
        ImageRole(image_id=image_id, cluster_id=b_id, role="rejected",
                  manual_override=0),
    ])
    db.commit()
    assert _best_role_map(db, [image_id]) == {image_id: "rejected"}


def test_manual_pano_wins_when_only_one_imagerole_row(db):
    """Simple no-cross-cluster case: one image, one ImageRole row with
    manual_override=1. Nothing to compete against. Result = that role.
    Regression guard for the single-row happy path."""
    image_id, a_id, _ = _seed_two_clusters_one_shared_image(db)
    db.add(ImageRole(image_id=image_id, cluster_id=a_id,
                     role="panoramic", manual_override=1))
    db.commit()
    assert _best_role_map(db, [image_id]) == {image_id: "panoramic"}


def test_bulk_map_matches_per_image_result(db):
    """_best_role and _best_role_map must return the same answer for
    the same image (bulk fetcher is just a batched version)."""
    image_id, a_id, b_id = _seed_two_clusters_one_shared_image(db)
    db.add_all([
        ImageRole(image_id=image_id, cluster_id=a_id, role="panoramic",
                  manual_override=1),
        ImageRole(image_id=image_id, cluster_id=b_id, role="team",
                  manual_override=0),
    ])
    db.commit()
    assert _best_role(db, image_id) == _best_role_map(db, [image_id])[image_id]


def test_no_image_roles_returns_none_or_absent(db):
    """No ImageRole rows at all for an image → not in the bulk map,
    per-image call returns None. Regression guard."""
    image_id, _, _ = _seed_two_clusters_one_shared_image(db)
    assert _best_role(db, image_id) is None
    assert _best_role_map(db, [image_id]) == {}
