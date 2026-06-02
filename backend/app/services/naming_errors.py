"""Cross-team duplicate-name detection.

Finds player names (cluster auto_labels) that appear as clusters in 2+
different sessions of one job, then uses face-embedding similarity to tell
two situations apart:

  - same_face      : one player photographed in two folders (mis-foldered).
                     Fix = move one cluster into the other.
  - different_face : two different kids tagged with the same name because
                     the photographer didn't update the camera's copyright
                     field when switching teams. Fix = rename the wrong one.

The cutoff reuses cluster.DEFAULT_EPS (cosine distance 0.4) — the same bar
the pipeline used to group faces — so verdicts stay consistent with the
clustering itself. Within-session duplicates are handled separately by the
`duplicate_auto_label` flag (Phase 9); this is strictly cross-session.

Read-time only — computes on request, persists nothing.

Category 3 noise filter (2026-06-02): the same_face verdict was firing on
every cross-team guest appearance (a kid posing in a friend's buddy photo
on a different team). Under the refined rule, only flag when the case is
actionable:
  - Coach clusters (resolved via is_coach_for_sort() so operator overrides
    are honored) are exempt entirely — coaches legitimately span teams.
  - For non-coach groups, drop sessions whose cluster has no individual
    photo of the player (face_count == 1 image), then re-check the
    >= 2-session cross-team requirement. If <2 sessions still carry
    portraits, suppress the flag.
Category 1 (different_face) and the unknown verdict are unchanged — they're
real labeling errors regardless of role or photo composition.
"""
from __future__ import annotations

import numpy as np
from sqlalchemy import func

from app.models.db_models import Cluster, Face, Image, Session
from app.services.cluster import DEFAULT_EPS
from app.services.roster import build_lookup, normalize_name
from app.services.roster_check import session_norm_team


def cluster_centroid(embeddings: list[np.ndarray]) -> np.ndarray | None:
    """Mean of the (already L2-normalized) face embeddings, re-normalized.
    None when the cluster has no usable embeddings."""
    if not embeddings:
        return None
    mean = np.mean(np.stack(embeddings), axis=0)
    norm = float(np.linalg.norm(mean))
    if norm == 0.0:
        return None
    return mean / norm


def centroid_distance(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine distance between two normalized centroids (1 - cosine sim)."""
    return 1.0 - float(np.dot(a, b))


def _cluster_centroid_from_db(db, cluster_id: int) -> np.ndarray | None:
    rows = db.query(Face.embedding).filter_by(cluster_id=cluster_id).all()
    embs = [
        np.frombuffer(r[0], dtype=np.float32)
        for r in rows if r[0]
    ]
    return cluster_centroid(embs)


def _job_image_face_counts(db, job_id: int) -> dict[int, int]:
    """For every image in this job's non-archived sessions, return the
    total face count (independent of which cluster each face belongs to).
    One grouped query — used by the Category 3 filter to identify which
    clusters have at least one individual photo of the player."""
    rows = (
        db.query(Face.image_id, func.count(Face.id))
        .join(Image, Image.id == Face.image_id)
        .join(Session, Session.id == Image.session_id)
        .filter(Session.job_id == job_id, Session.archived == 0)
        .group_by(Face.image_id)
        .all()
    )
    return dict(rows)


def _cluster_has_individual_photo(
    db, cluster_id: int, image_face_counts: dict[int, int],
) -> bool:
    """True iff this cluster has at least one image whose total face count
    is exactly 1 (an individual photo of the player). Buddy-only clusters
    return False — they're the guest-appearance case the Category 3 filter
    suppresses."""
    img_ids = {
        r[0] for r in db.query(Face.image_id).filter_by(cluster_id=cluster_id).all()
    }
    return any(image_face_counts.get(iid, 0) == 1 for iid in img_ids)


def find_cross_team_name_collisions(db, job_id: int) -> list[dict]:
    """Return one row per player name that appears as clusters in 2+ distinct
    non-archived sessions of this job, with a same_face/different_face
    verdict from pairwise centroid distance."""
    sessions = [
        s for s in db.query(Session).filter_by(job_id=job_id).all()
        if not s.archived
    ]
    if not sessions:
        return []

    lookup = build_lookup(db, job_id)            # norm_name -> norm_team
    # raw team display for a normalized team name.
    raw_team_by_norm: dict[str, str] = {}
    from app.models.db_models import RosterEntry  # local import: avoid cycle
    for tn, nt in (
        db.query(RosterEntry.team_name, RosterEntry.norm_team)
        .filter_by(job_id=job_id).all()
    ):
        raw_team_by_norm.setdefault(nt, tn)

    sess_by_id = {s.id: s for s in sessions}

    # Pre-compute image-level face counts once for the whole job — used by
    # the Category 3 filter to identify clusters with at least one
    # individual photo of the player. Single grouped query.
    face_counts_by_image_cache = _job_image_face_counts(db, job_id)

    # Group clusters (with a real auto_label) by normalized name.
    groups: dict[str, list[Cluster]] = {}
    raw_by_norm: dict[str, str] = {}
    for s in sessions:
        for c in s.clusters:
            label = (c.auto_label or "").strip()
            if not label:
                continue
            norm = normalize_name(label)
            if not norm:
                continue
            groups.setdefault(norm, []).append(c)
            raw_by_norm.setdefault(norm, c.display_label())

    items: list[dict] = []
    for norm, clusters in groups.items():
        session_ids = {c.session_id for c in clusters}
        if len(session_ids) < 2:
            continue  # not cross-team — within-session dupes handled elsewhere

        # Centroids per cluster (skip clusters with no embeddings).
        centroids: dict[int, np.ndarray] = {}
        for c in clusters:
            cen = _cluster_centroid_from_db(db, c.id)
            if cen is not None:
                centroids[c.id] = cen

        # Pairwise distances; verdict = different_face if any pair > eps.
        dists: list[float] = []
        cluster_list = [c for c in clusters if c.id in centroids]
        for i in range(len(cluster_list)):
            for j in range(i + 1, len(cluster_list)):
                dists.append(centroid_distance(
                    centroids[cluster_list[i].id],
                    centroids[cluster_list[j].id],
                ))
        if dists:
            max_d, min_d = max(dists), min(dists)
            verdict = "different_face" if max_d > DEFAULT_EPS else "same_face"
        else:
            # No embeddings to compare — can't classify; still report so the
            # operator can eyeball it.
            max_d = min_d = None
            verdict = "unknown"

        # Category 3 filter — applies only to same_face verdicts. Cat 1 and
        # unknown emit unchanged.
        if verdict == "same_face":
            # Coach exemption: any coach in the group means the whole flag
            # is dropped (coaches legitimately span teams). Resolved via
            # is_coach_for_sort() so manual_coach_override is honored.
            if any(c.is_coach_for_sort() for c in clusters):
                continue
            # Buddy-only suppression: filter the cluster list to clusters
            # that have at least one individual photo of the player. If
            # fewer than 2 distinct sessions remain, suppress entirely.
            face_counts_by_image = face_counts_by_image_cache
            kept = [
                c for c in clusters
                if _cluster_has_individual_photo(db, c.id, face_counts_by_image)
            ]
            if len({c.session_id for c in kept}) < 2:
                continue
            clusters = kept   # rewrite the working set for downstream rendering

        expected_norm_team = lookup.get(norm)
        expected_raw_team = raw_team_by_norm.get(expected_norm_team) if expected_norm_team else None

        cluster_rows = []
        for c in clusters:
            s = sess_by_id.get(c.session_id)
            in_correct = (
                expected_norm_team is not None
                and session_norm_team(s) == expected_norm_team
            )
            cluster_rows.append({
                "session_id": c.session_id,
                "session_name": s.name if s else "?",
                "session_reviewed": bool(s.reviewed) if s else False,
                "cluster_id": c.id,
                "image_count": c.image_count or 0,
                "roster_team_raw": expected_raw_team,
                "in_correct_team": in_correct,
            })
        # Order clusters: correct-team one last (so the "wrong" ones read first).
        cluster_rows.sort(key=lambda r: r["in_correct_team"])

        items.append({
            "norm_name": norm,
            "raw_name": raw_by_norm.get(norm, norm),
            "verdict": verdict,
            "max_distance": round(max_d, 4) if max_d is not None else None,
            "min_distance": round(min_d, 4) if min_d is not None else None,
            "clusters": cluster_rows,
        })

    # different_face first (more urgent), then by name.
    order = {"different_face": 0, "unknown": 1, "same_face": 2}
    items.sort(key=lambda it: (order.get(it["verdict"], 9), it["raw_name"].lower()))
    return items
