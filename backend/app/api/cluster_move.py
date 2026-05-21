"""Cross-session cluster moves — the only Phase 6 mutation that crosses
session boundaries.

POST /api/clusters/{cluster_id}/move
Body: { target_session_id: int, mode: "merge" | "create" }

Constraints:
  - Source cluster and target session must share a job.
  - Target session must not be archived.
  - Target session != source session.

mode="create":
  - Source cluster's session_id flips to target. manual_label,
    manual_coach_override, and every ImageRole (including manual_override=1)
    are preserved.
  - `_sort_cluster` runs in the new context.

mode="merge":
  - Pick a target cluster in target session by normalized-name match against
    source's manual_label OR auto_label. 409 on zero or multiple candidates.
  - Drop EVERY ImageRole row on both sides (full re-evaluate per spec).
  - Re-parent source faces to target cluster; delete source cluster.
  - `_sort_cluster` runs on the merged image set — fresh TEAM/PANO/etc.

In both modes:
  - "Owned" images (every Face row in source cluster) get their session_id
    moved to target. "Shared" images (a Face in another cluster in source)
    stay in source — mirrors the buddy-shot rule in `reassign`.
  - If target session was reviewed=1, flip to 0 / clear reviewed_at. Source
    session's reviewed flag is intentionally untouched.
  - `_resort_session_outliers` runs on both sessions.
"""
import logging

import numpy as np
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session as DbSession

from app.api.clusters import _resort_session_outliers
from app.db import get_db
from app.models.db_models import Cluster, Face, Image, ImageRole, Session
from app.services import matching
from app.services.face_pipeline import _sort_cluster
from app.services.roster import normalize_name

logger = logging.getLogger(__name__)
router = APIRouter()


class MoveRequest(BaseModel):
    target_session_id: int
    mode: str  # "merge" | "create"


def _match_keys(c: Cluster) -> set[str]:
    """Normalized strings any of which could identify this cluster by name.
    Covers both the renamed case (manual_label) and the auto-named case."""
    keys: set[str] = set()
    for s in (c.manual_label, c.auto_label):
        n = normalize_name(s)
        if n:
            keys.add(n)
    return keys


def _owned_image_ids(db: DbSession, source_cluster_id: int) -> set[int]:
    """Image ids whose every Face row is in source_cluster_id (no shared
    buddy face in another cluster). These are the images safe to relocate
    to the target session — shared images stay where they are."""
    in_source = {
        f.image_id for f in db.query(Face).filter_by(cluster_id=source_cluster_id).all()
    }
    if not in_source:
        return set()
    rows = (
        db.query(Face.image_id)
        .filter(Face.image_id.in_(in_source), Face.cluster_id != source_cluster_id)
        .distinct()
        .all()
    )
    shared = {r[0] for r in rows}
    return in_source - shared


def _move_owned_images(
    db: DbSession, owned_ids: set[int], target_session_id: int,
) -> None:
    if not owned_ids:
        return
    db.query(Image).filter(Image.id.in_(owned_ids)).update(
        {"session_id": target_session_id}, synchronize_session=False,
    )


def _unreview_target(target_session: Session) -> bool:
    """Cross-session move invalidates any prior 'I reviewed this team' state
    on the target. Returns True if we just cleared a reviewed pill."""
    if target_session.reviewed:
        target_session.reviewed = 0
        target_session.reviewed_at = None
        return True
    return False


@router.post("/{cluster_id}/move")
def move_cluster(
    cluster_id: int,
    payload: MoveRequest,
    db: DbSession = Depends(get_db),
):
    if payload.mode not in ("merge", "create"):
        raise HTTPException(400, "mode must be 'merge' or 'create'")

    source = db.query(Cluster).get(cluster_id)
    if source is None:
        raise HTTPException(404, "Source cluster not found")
    source_session = db.query(Session).get(source.session_id)
    if source_session is None:
        raise HTTPException(404, "Source session not found")
    target_session = db.query(Session).get(payload.target_session_id)
    if target_session is None:
        raise HTTPException(404, "Target session not found")
    if source_session.job_id is None or source_session.job_id != target_session.job_id:
        raise HTTPException(400, "Cross-job moves are not allowed")
    if target_session.archived:
        raise HTTPException(400, "Target session is archived")
    if source.session_id == target_session.id:
        raise HTTPException(400, "Target is the same as source session")

    owned_ids = _owned_image_ids(db, source.id)

    if payload.mode == "create":
        target_cluster_id, target_unreviewed = _do_create(
            db, source, target_session, owned_ids,
        )
    else:
        target_cluster_id, target_unreviewed = _do_merge(
            db, source, target_session, owned_ids,
        )

    db.commit()
    return {
        "status": "moved",
        "mode": payload.mode,
        "source_cluster_id": cluster_id,
        "target_cluster_id": target_cluster_id,
        "target_session_id": target_session.id,
        "target_unreviewed": target_unreviewed,
    }


def _do_create(
    db: DbSession, source: Cluster, target_session: Session, owned_ids: set[int],
) -> tuple[int, bool]:
    source_session_id = source.session_id
    source.session_id = target_session.id
    _move_owned_images(db, owned_ids, target_session.id)
    db.flush()
    _sort_cluster(db, source)              # fresh context, new siblings
    _resort_session_outliers(db, source_session_id)
    _resort_session_outliers(db, target_session.id)
    return source.id, _unreview_target(target_session)


def _do_merge(
    db: DbSession, source: Cluster, target_session: Session, owned_ids: set[int],
) -> tuple[int, bool]:
    src_keys = _match_keys(source)
    if not src_keys:
        raise HTTPException(409, detail={
            "error": "source_no_label",
            "message": "Source cluster has no label to match against — use mode='create'.",
        })
    candidates = [
        c for c in target_session.clusters if _match_keys(c) & src_keys
    ]
    if not candidates:
        raise HTTPException(409, detail={
            "error": "no_target_cluster",
            "message": "No cluster in the target session matches by name. Use mode='create'.",
        })
    if len(candidates) > 1:
        raise HTTPException(409, detail={
            "error": "ambiguous_target",
            "message": f"{len(candidates)} clusters in target session match by name.",
            "candidate_cluster_ids": [c.id for c in candidates],
        })
    target = candidates[0]
    source_session_id = source.session_id

    # Full re-evaluate per spec: drop all ImageRole rows on BOTH sides
    # (including manual_override=1). The next _sort_cluster derives fresh.
    db.query(ImageRole).filter(
        ImageRole.cluster_id.in_([source.id, target.id])
    ).delete(synchronize_session=False)

    # Re-parent source faces into target, then move owned images.
    db.query(Face).filter_by(cluster_id=source.id).update(
        {"cluster_id": target.id}, synchronize_session=False,
    )
    _move_owned_images(db, owned_ids, target_session.id)
    db.flush()

    db.delete(source)
    db.flush()

    _sort_cluster(db, target)
    _resort_session_outliers(db, source_session_id)
    _resort_session_outliers(db, target_session.id)
    return target.id, _unreview_target(target_session)


@router.post("/{cluster_id}/global-match-suggest")
def global_match_suggest(cluster_id: int, db: DbSession = Depends(get_db)):
    """Phase A.4: read-only top-N GLOBAL reference matches for a cluster,
    ignoring roster scope — the conservative escape hatch from decision 1 and
    the data layer for A.5's "Try global match" button.

    Mutates nothing: it never writes matched_player_id / auto_label /
    is_likely_coach / review flags. Accepting a suggestion is a separate,
    explicit user action (rename / set-role) in A.5. 404 if the cluster doesn't
    exist; an empty cluster returns a clean 'none' result, not a 500.
    """
    cluster = db.query(Cluster).get(cluster_id)
    if cluster is None:
        raise HTTPException(404, "Cluster not found")

    faces = db.query(Face).filter_by(cluster_id=cluster_id).all()
    embs = [np.frombuffer(f.embedding, dtype=np.float32) for f in faces]
    index = matching.load_reference_index(db)            # GLOBAL — no player_ids
    result = (
        matching.match_against_index(index, np.stack(embs))
        if embs else matching._none_result()
    )
    result["scope"] = "global_fallback"                  # always — a global query
    result["thresholds"] = {                             # echo, like the debug endpoint
        "high": matching.HIGH_THRESHOLD,
        "low": matching.LOW_THRESHOLD,
        "margin": matching.MIN_MARGIN,
    }
    return result
