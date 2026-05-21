"""Cross-team guest-cluster detection (Phase 11).

The "phantom sibling" case: two siblings on different teams take a buddy
photo, it lands in one sibling's team folder, and the OTHER sibling's face
forms a cluster there containing only the shared buddy shot(s) — no solo
portrait. That phantom gets nagged for a team/pano pick or mis-flagged as a
coach, even though they're just a guest who belongs to another team.

Detection (validated against job 18 — Elliot-Hurkman in 10U Black matches
his real cluster in Coach Pitch Rockies at distance 0.071):

  1. STRUCTURAL candidate (cheap, no embeddings): a cluster with zero
     single-face images where every image is a buddy shot shared with
     another cluster in the same session.
  2. CONFIRM via face match (reuses Phase 10 centroid math): the candidate's
     centroid matches a SOLO-having cluster in a DIFFERENT team at cosine
     distance <= DEFAULT_EPS. That match is the player's real identity.

Match found ⇒ guest from another team. No match ⇒ leave it alone (could be
a real coach). Read-time only; persists nothing. Embeddings are only loaded
when a session actually has structural candidates (usually none).
"""
from __future__ import annotations

from app.models.db_models import Cluster, Face, Image, Session
from app.services.cluster import DEFAULT_EPS
from app.services.naming_errors import (
    _cluster_centroid_from_db, centroid_distance,
)


def _single_face_image_ids(db, session_id: int) -> set[int]:
    """Image ids in this session that have exactly one detected face."""
    rows = (
        db.query(Image.id)
        .filter(Image.session_id == session_id)
        .all()
    )
    out: set[int] = set()
    for (iid,) in rows:
        if db.query(Face).filter_by(image_id=iid).count() == 1:
            out.add(iid)
    return out


def _cluster_image_ids(db, cluster_id: int) -> set[int]:
    return {
        r[0] for r in
        db.query(Face.image_id).filter_by(cluster_id=cluster_id).distinct().all()
    }


def _image_is_shared(db, image_id: int, cluster_id: int) -> bool:
    """True if this image has a face in some OTHER cluster too (buddy shot)."""
    return (
        db.query(Face)
        .filter(Face.image_id == image_id,
                Face.cluster_id.isnot(None),
                Face.cluster_id != cluster_id)
        .count() > 0
    )


def structural_candidate_ids(db, session: Session) -> set[int]:
    """Clusters in `session` with ZERO single-face images AND every image
    shared with another cluster — the phantom shape. Cheap; no embeddings."""
    single_face = _single_face_image_ids(db, session.id)
    candidates: set[int] = set()
    for c in session.clusters:
        img_ids = _cluster_image_ids(db, c.id)
        if not img_ids:
            continue
        if img_ids & single_face:
            continue  # has at least one solo portrait → a real member
        if all(_image_is_shared(db, iid, c.id) for iid in img_ids):
            candidates.add(c.id)
    return candidates


def _cluster_has_solo(db, cluster_id: int, single_face_ids: set[int]) -> bool:
    return bool(_cluster_image_ids(db, cluster_id) & single_face_ids)


def _solo_cluster_index(db, job_id: int, exclude_session_id: int):
    """Build [(cluster_id, session_id, session_name, label, centroid)] for
    every solo-having cluster in the job's non-archived sessions EXCEPT the
    excluded one. Only called when a candidate exists, so the cost is paid
    rarely."""
    index = []
    sessions = [
        s for s in db.query(Session).filter_by(job_id=job_id).all()
        if not s.archived and s.id != exclude_session_id
    ]
    for s in sessions:
        single_face = _single_face_image_ids(db, s.id)
        if not single_face:
            continue
        for c in s.clusters:
            if not _cluster_has_solo(db, c.id, single_face):
                continue
            cen = _cluster_centroid_from_db(db, c.id)
            if cen is None:
                continue
            index.append((c.id, s.id, s.name, c.display_label(), cen))
    return index


def guest_map_for_session(db, session: Session) -> dict[int, dict]:
    """{cluster_id: {name, team, session_id, distance}} for confirmed guest
    clusters in this session. Empty when the session has no structural
    candidates (the common case — no embedding work done)."""
    if session.job_id is None:
        return {}
    candidates = structural_candidate_ids(db, session)
    if not candidates:
        return {}
    index = _solo_cluster_index(db, session.job_id, exclude_session_id=session.id)
    if not index:
        return {}

    out: dict[int, dict] = {}
    for cid in candidates:
        cen = _cluster_centroid_from_db(db, cid)
        if cen is None:
            continue
        best = None
        for (ocid, osid, osname, olabel, ocen) in index:
            d = centroid_distance(cen, ocen)
            if d <= DEFAULT_EPS and (best is None or d < best[0]):
                best = (d, osid, osname, olabel)
        if best is not None:
            out[cid] = {
                "name": best[3],
                "team": best[2],
                "session_id": best[1],
                "distance": round(best[0], 4),
            }
    return out


def find_guest_clusters(db, job_id: int) -> list[dict]:
    """Job-wide list of confirmed cross-team guest clusters (one row each)."""
    items: list[dict] = []
    sessions = [
        s for s in db.query(Session).filter_by(job_id=job_id).all()
        if not s.archived
    ]
    for s in sessions:
        gmap = guest_map_for_session(db, s)
        for cluster_id, info in gmap.items():
            c = db.query(Cluster).get(cluster_id)
            items.append({
                "session_id": s.id,
                "session_name": s.name,
                "cluster_id": cluster_id,
                "cluster_label": c.display_label() if c else f"Player {cluster_id}",
                "image_count": (c.image_count if c else 0) or 0,
                "guest_name": info["name"],
                "guest_team": info["team"],
                "guest_session_id": info["session_id"],
                "distance": info["distance"],
            })
    items.sort(key=lambda it: it["distance"])  # surest matches first
    return items
