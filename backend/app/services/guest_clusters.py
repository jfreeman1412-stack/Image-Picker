"""Cross-team guest-cluster detection (Phase 11).

The "phantom sibling" case: two siblings on different teams take a buddy
photo, it lands in one sibling's team folder, and the OTHER sibling's face
forms a cluster there containing only the shared buddy shot(s) — no solo
portrait. That phantom gets nagged for a team/pano pick or mis-flagged as a
coach, even though they're just a guest who belongs to another team.

Detection (validated against job 18 — Elliot-Hurkman in 10U Black matches
his real cluster in Coach Pitch Rockies at distance 0.071):

  1. STRUCTURAL candidate (session-local): a cluster with zero single-face
     images where every image is a buddy shot shared with another cluster.
  2. CONFIRM via face match (reuses Phase 10 centroid math): the candidate's
     centroid matches a SOLO-having cluster in a DIFFERENT team at cosine
     distance <= DEFAULT_EPS. That match is the player's real identity.

Performance: all face data for a session (and, when a session actually has
candidates, the whole job) is pulled in ONE query and processed in memory —
no per-image COUNT round-trips, no per-session index rebuilds. Read-time
only; persists nothing.
"""
from __future__ import annotations

import numpy as np

from app.models.db_models import Cluster, Face, Image, Session
from app.services.cluster import DEFAULT_EPS
from app.services.naming_errors import centroid_distance, cluster_centroid


# ── In-memory face indexes (one query each) ─────────────────────────────

def _index_from_rows(rows) -> dict:
    """rows = iterable of (image_id, cluster_id, embedding_bytes, session_id).
    Build the in-memory structures every computation below needs."""
    image_face_count: dict[int, int] = {}
    image_clusters: dict[int, set[int]] = {}
    cluster_images: dict[int, set[int]] = {}
    cluster_embs: dict[int, list[np.ndarray]] = {}
    cluster_session: dict[int, int] = {}
    for image_id, cluster_id, emb, session_id in rows:
        image_face_count[image_id] = image_face_count.get(image_id, 0) + 1
        if cluster_id is None:
            continue
        cluster_images.setdefault(cluster_id, set()).add(image_id)
        image_clusters.setdefault(image_id, set()).add(cluster_id)
        cluster_session[cluster_id] = session_id
        if emb:
            cluster_embs.setdefault(cluster_id, []).append(
                np.frombuffer(emb, dtype=np.float32)
            )
    return {
        "image_face_count": image_face_count,
        "image_clusters": image_clusters,
        "cluster_images": cluster_images,
        "cluster_embs": cluster_embs,
        "cluster_session": cluster_session,
    }


def _session_index(db, session_id: int) -> dict:
    rows = (
        db.query(Face.image_id, Face.cluster_id, Face.embedding, Image.session_id)
        .join(Image, Face.image_id == Image.id)
        .filter(Image.session_id == session_id)
        .all()
    )
    return _index_from_rows(rows)


def _job_index(db, job_id: int, exclude_session_id: int | None = None):
    sessions = {
        s.id: s for s in db.query(Session).filter_by(job_id=job_id).all()
        if not s.archived and s.id != exclude_session_id
    }
    if not sessions:
        return {}, {}
    rows = (
        db.query(Face.image_id, Face.cluster_id, Face.embedding, Image.session_id)
        .join(Image, Face.image_id == Image.id)
        .filter(Image.session_id.in_(list(sessions.keys())))
        .all()
    )
    return _index_from_rows(rows), sessions


# ── Core logic on an index ──────────────────────────────────────────────

def _candidates(idx: dict, session_id: int) -> set[int]:
    """Cluster ids in `session_id` with 0 single-face images AND every image
    shared with another cluster (the phantom shape)."""
    out: set[int] = set()
    for cid, imgs in idx["cluster_images"].items():
        if idx["cluster_session"].get(cid) != session_id or not imgs:
            continue
        if any(idx["image_face_count"].get(iid, 0) == 1 for iid in imgs):
            continue  # has a solo portrait → real member
        if all(len(idx["image_clusters"].get(iid, set())) > 1 for iid in imgs):
            out.add(cid)
    return out


def _centroid(idx: dict, cid: int):
    return cluster_centroid(idx["cluster_embs"].get(cid, []))


def _solo_cluster_ids(idx: dict) -> list[int]:
    return [
        cid for cid, imgs in idx["cluster_images"].items()
        if any(idx["image_face_count"].get(iid, 0) == 1 for iid in imgs)
    ]


# ── Public API ──────────────────────────────────────────────────────────

def structural_candidate_ids(db, session: Session) -> set[int]:
    """Phantom-shaped clusters in this session (session-local, one query)."""
    return _candidates(_session_index(db, session.id), session.id)


def guest_map_for_session(db, session: Session) -> dict[int, dict]:
    """{cluster_id: {name, team, session_id, distance}} for confirmed guests
    in this session. Cheap when the session has no candidates — the whole-job
    load only happens if at least one phantom-shaped cluster exists here."""
    if session.job_id is None:
        return {}
    sidx = _session_index(db, session.id)
    candidates = _candidates(sidx, session.id)
    if not candidates:
        return {}

    other_idx, other_sessions = _job_index(
        db, session.job_id, exclude_session_id=session.id,
    )
    if not other_idx:
        return {}
    # Precompute other-team solo-cluster centroids once.
    targets = []
    for cid in _solo_cluster_ids(other_idx):
        cen = _centroid(other_idx, cid)
        if cen is not None:
            targets.append((cid, other_idx["cluster_session"][cid], cen))
    if not targets:
        return {}

    out: dict[int, dict] = {}
    for cand in candidates:
        cen = _centroid(sidx, cand)
        if cen is None:
            continue
        best = None
        for (ocid, osid, ocen) in targets:
            d = centroid_distance(cen, ocen)
            if d <= DEFAULT_EPS and (best is None or d < best[0]):
                best = (d, ocid, osid)
        if best is not None:
            tgt_cluster = db.query(Cluster).get(best[1])
            tgt_session = other_sessions.get(best[2])
            out[cand] = {
                "name": tgt_cluster.display_label() if tgt_cluster else "?",
                "team": tgt_session.name if tgt_session else "?",
                "session_id": best[2],
                "distance": round(best[0], 4),
            }
    return out


def find_guest_clusters(db, job_id: int) -> list[dict]:
    """Job-wide confirmed cross-team guests. Single bulk load of the job's
    faces; candidates matched against the shared solo-cluster index."""
    idx, sessions = _job_index(db, job_id)
    if not idx:
        return []

    # All solo-having cluster centroids across the job (the match targets).
    targets = []
    for cid in _solo_cluster_ids(idx):
        cen = _centroid(idx, cid)
        if cen is not None:
            targets.append((cid, idx["cluster_session"][cid], cen))

    items: list[dict] = []
    for sid in sessions:
        for cand in _candidates(idx, sid):
            cen = _centroid(idx, cand)
            if cen is None:
                continue
            best = None
            for (ocid, osid, ocen) in targets:
                if osid == sid:
                    continue  # must be a DIFFERENT team
                d = centroid_distance(cen, ocen)
                if d <= DEFAULT_EPS and (best is None or d < best[0]):
                    best = (d, ocid, osid)
            if best is None:
                continue
            cand_cluster = db.query(Cluster).get(cand)
            tgt_cluster = db.query(Cluster).get(best[1])
            items.append({
                "session_id": sid,
                "session_name": sessions[sid].name,
                "cluster_id": cand,
                "cluster_label": cand_cluster.display_label() if cand_cluster else f"Player {cand}",
                "image_count": (cand_cluster.image_count if cand_cluster else 0) or 0,
                "guest_name": tgt_cluster.display_label() if tgt_cluster else "?",
                "guest_team": sessions[best[2]].name if best[2] in sessions else "?",
                "guest_session_id": best[2],
                "distance": round(best[0], 4),
            })
    items.sort(key=lambda it: it["distance"])
    return items
