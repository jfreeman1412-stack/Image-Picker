"""Phase 11: cross-team guest-cluster detection.

Structural candidate = cluster with 0 single-face images where every image
is shared with another cluster in the same session. Confirmed guest = that
candidate's centroid matches a solo-having cluster in a DIFFERENT team
(distance <= DEFAULT_EPS).
"""
from datetime import datetime

import numpy as np
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.sessions import _cluster_is_buddy_only, _compute_review_readiness
from app.db import Base
from app.models.db_models import (
    Cluster, Face, Image, ImageRole, Job, Session as DbSession,
)
from app.services.guest_clusters import (
    find_guest_clusters, guest_map_for_session, structural_candidate_ids,
)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'guest-test.db'}",
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


def _basis(idx, size=512):
    v = np.zeros(size, dtype=np.float32)
    v[idx] = 1.0
    return v


def _emb(vec):
    v = np.asarray(vec, dtype=np.float32)
    v = v / np.linalg.norm(v)
    return v.astype(np.float32).tobytes()


def _job(db, *teams):
    job = Job(name="J", root_path="/tmp", has_lines=0)
    db.add(job); db.commit(); db.refresh(job)
    out = []
    for t in teams:
        s = DbSession(job_id=job.id, name=t, source_path=f"/tmp/{t}",
                      status="done", created_at=datetime.utcnow())
        db.add(s); db.commit(); db.refresh(s)
        out.append(s)
    return job, out


def _img(db, session, n_faces=1):
    """Create an image with `n_faces` face placeholders (to control single
    vs multi-face). Returns the Image."""
    img = Image(session_id=session.id, path=f"/tmp/{session.id}_{id(object())}.png",
                filename="x.png")
    db.add(img); db.commit(); db.refresh(img)
    return img


def _solo_cluster(db, session, label, vec, n=3):
    """A normal player: n single-face images, all in this one cluster."""
    c = Cluster(session_id=session.id, auto_label=label, image_count=n)
    db.add(c); db.commit(); db.refresh(c)
    for _ in range(n):
        img = _img(db, session)
        db.add(Face(image_id=img.id, cluster_id=c.id, bbox="[0,0,1,1]",
                    det_score=0.9, embedding=_emb(vec)))
    db.commit(); db.refresh(c)
    return c


def _buddy_phantom(db, session, partner_cluster, vec, n=2):
    """A phantom: n buddy images, each ALSO containing a face in
    `partner_cluster` (so the image is multi-face + shared). The phantom
    cluster itself has 0 single-face images."""
    c = Cluster(session_id=session.id, auto_label=None, image_count=n)
    db.add(c); db.commit(); db.refresh(c)
    for _ in range(n):
        img = _img(db, session)
        # partner's face in the same image
        db.add(Face(image_id=img.id, cluster_id=partner_cluster.id,
                    bbox="[0,0,1,1]", det_score=0.9, embedding=_emb(_basis(0))))
        # the guest's face
        db.add(Face(image_id=img.id, cluster_id=c.id, bbox="[5,5,1,1]",
                    det_score=0.9, embedding=_emb(vec)))
    db.commit(); db.refresh(c)
    return c


# ── structural candidate detection ──────────────────────────────────────

def test_buddy_only_cluster_is_candidate(db):
    _, (a,) = _job(db, "TeamA")
    partner = _solo_cluster(db, a, "Real-Player", _basis(0), n=3)
    phantom = _buddy_phantom(db, a, partner, _basis(5), n=2)
    cands = structural_candidate_ids(db, a)
    assert phantom.id in cands
    assert partner.id not in cands       # has solo images


def test_cluster_with_a_solo_image_not_candidate(db):
    _, (a,) = _job(db, "TeamA")
    c = _solo_cluster(db, a, "Has-Solo", _basis(1), n=2)
    assert structural_candidate_ids(db, a) == set()


# ── cross-team match ────────────────────────────────────────────────────

def test_guest_confirmed_by_cross_team_match(db):
    job, (a, b) = _job(db, "10U Black", "Coach Pitch Rockies")
    # Barrett on team A with solo portraits
    barrett = _solo_cluster(db, a, "Barrett-Hurkman", _basis(1), n=5)
    # Elliot is a real player on team B (solo portraits), embedding _basis(7)
    _solo_cluster(db, b, "Elliot-Hurkman", _basis(7), n=5)
    # Phantom in team A: Elliot caught in Barrett's buddy shots (same _basis(7))
    phantom = _buddy_phantom(db, a, barrett, _basis(7), n=2)

    gmap = guest_map_for_session(db, a)
    assert phantom.id in gmap
    assert gmap[phantom.id]["name"] == "Elliot-Hurkman"
    assert gmap[phantom.id]["team"] == "Coach Pitch Rockies"
    assert gmap[phantom.id]["distance"] < 0.4


def test_no_cross_team_match_is_not_a_guest(db):
    """Phantom whose face matches nobody in another team (e.g. a real
    coach) is omitted."""
    job, (a, b) = _job(db, "TeamA", "TeamB")
    partner = _solo_cluster(db, a, "Player-A", _basis(1), n=4)
    _solo_cluster(db, b, "Player-B", _basis(2), n=4)
    # phantom face _basis(9) matches neither solo cluster
    _buddy_phantom(db, a, partner, _basis(9), n=2)
    assert guest_map_for_session(db, a) == {}


def test_same_session_match_does_not_count(db):
    """A candidate matching a solo cluster in the SAME session isn't a
    cross-team guest."""
    job, (a,) = _job(db, "TeamA")
    partner = _solo_cluster(db, a, "Partner", _basis(1), n=3)
    # Real player in same session with embedding _basis(7)
    _solo_cluster(db, a, "SameTeamPlayer", _basis(7), n=3)
    _buddy_phantom(db, a, partner, _basis(7), n=2)   # matches same-session
    assert guest_map_for_session(db, a) == {}


def test_closest_match_wins(db):
    job, (a, b, c) = _job(db, "TeamA", "TeamB", "TeamC")
    partner = _solo_cluster(db, a, "Partner", _basis(1), n=3)
    # Two near matches in other teams; the phantom is exactly _basis(7).
    _solo_cluster(db, b, "Near", _basis(7), n=3)              # distance ~0
    # a slightly-off vector in team C
    off = _basis(7).copy(); off[8] = 0.3
    _solo_cluster(db, c, "Far", off, n=3)
    _buddy_phantom(db, a, partner, _basis(7), n=2)
    gmap = guest_map_for_session(db, a)
    pid = next(iter(gmap))
    assert gmap[pid]["name"] == "Near"


def test_archived_target_excluded(db):
    job, (a, b) = _job(db, "TeamA", "TeamB")
    b.archived = 1
    partner = _solo_cluster(db, a, "Partner", _basis(1), n=3)
    _solo_cluster(db, b, "Elliot", _basis(7), n=5)   # archived team
    _buddy_phantom(db, a, partner, _basis(7), n=2)
    assert guest_map_for_session(db, a) == {}


def test_find_guest_clusters_job_level_shape(db):
    job, (a, b) = _job(db, "10U Black", "Coach Pitch Rockies")
    barrett = _solo_cluster(db, a, "Barrett-Hurkman", _basis(1), n=5)
    _solo_cluster(db, b, "Elliot-Hurkman", _basis(7), n=5)
    _buddy_phantom(db, a, barrett, _basis(7), n=2)
    items = find_guest_clusters(db, job.id)
    assert len(items) == 1
    row = items[0]
    assert row["session_name"] == "10U Black"
    assert row["guest_name"] == "Elliot-Hurkman"
    assert row["guest_team"] == "Coach Pitch Rockies"


# ── review-readiness gate fix ──────────────────────────────────────────

def test_buddy_only_cluster_skipped_by_gate(db):
    job, (a,) = _job(db, "TeamA")
    partner = _solo_cluster(db, a, "Real", _basis(1), n=3)
    # give the partner a team+pano so it's complete
    imgs = db.query(Image).join(Face).filter(Face.cluster_id == partner.id).all()
    db.add(ImageRole(image_id=imgs[0].id, cluster_id=partner.id, role="team"))
    db.add(ImageRole(image_id=imgs[1].id, cluster_id=partner.id, role="panoramic"))
    db.commit()
    phantom = _buddy_phantom(db, a, partner, _basis(7), n=2)

    assert _cluster_is_buddy_only(db, phantom.id) is True
    assert _cluster_is_buddy_only(db, partner.id) is False
    db.refresh(a)
    r = _compute_review_readiness(db, a)
    # The phantom must NOT appear in incomplete_clusters.
    assert all(row["cluster_id"] != phantom.id for row in r["incomplete_clusters"])


def test_gate_still_blocks_normal_incomplete_cluster(db):
    """A cluster WITH a solo image but missing team/pano still blocks —
    proves the Phase 11 skip didn't loosen the real gate."""
    job, (a,) = _job(db, "TeamA")
    c = _solo_cluster(db, a, "Real", _basis(1), n=3)  # has solo, no roles
    db.refresh(a)
    r = _compute_review_readiness(db, a)
    assert not r["ready"]
    assert any(row["cluster_id"] == c.id for row in r["incomplete_clusters"])
