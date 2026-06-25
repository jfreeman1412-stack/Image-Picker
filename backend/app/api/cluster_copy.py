"""Phase B (2026-06-25) — COPY a cluster to another team in the same job.

Use case: a coach photographed once who belongs to multiple teams. The
operator copies the coach cluster from team A's session onto every team
the coach belongs to. Each team's export folder then includes the coach
photo.

Locked design — see [[copy-cluster-phase-b]] memory + the scope-verification
trace for the load-bearing rationale:

  - **Faces NOT duplicated.** The destination cluster is a static
    snapshot. It won't get face-match auto-labeling and won't survive a
    pipeline re-run on its session (face_pipeline._clear_prior_results
    wipes Faces + Clusters + ImageRoles, then re-derives from Faces —
    no Faces means the cluster effectively disappears on re-run). This
    is surfaced operationally via:
      (1) `_runall_impact.copied_clusters` count in the run-all confirm
          dialog,
      (2) the `has_faces` field on the cluster API response, which the
          frontend renders as a "📋 copy" badge.

  - **Image rows DUPLICATED (option β).** Destination Image rows have a
    new image_id, same path/filename/capture_time/copyright_tag, and the
    destination's session_id. Why option β over ImageRole-only:
      (a) Legacy export iterates `session.images` — without duplicate
          Image rows on the destination, legacy mode would silently skip
          the copy. Option β makes BOTH export modes work.
      (b) The reject-leak global rejected-wins rule operates per-
          image_id (`_best_role_map` keys by image_id). Different
          image_ids on source vs. destination means a source rejection
          does NOT propagate to the destination — they're fully
          independent for export purposes.

  - **Auto-unreview destination session.** Mirrors MOVE's
    `_unreview_target` (cluster_move.py:96-103). Adding new content to a
    finalized session needs operator re-confirmation. No `force` flag.

  - **Guards:** target exists, target not archived, same job, source ≠
    target session. (Same-job is required because COPY targets a
    PlayerMembership-scoped team. Same-session is refused — operator's
    intent for in-session duplication isn't covered by the coach
    use case Phase B addresses.)

Kept SEPARATE from cluster_move.py per the locked design — copy and move
have different semantics (copy preserves source; move removes it) and
different guard models (move guards against accidentally losing locked
work; copy doesn't because the source stays put).
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session as DbSession

from app.db import get_db
from app.models.db_models import Cluster, Image, ImageRole, Session

logger = logging.getLogger(__name__)
router = APIRouter()


class CopyToSessionRequest(BaseModel):
    target_session_id: int


def copy_cluster_to_session(
    db: DbSession, source_cluster_id: int, target_session_id: int,
) -> dict:
    """Duplicate the source cluster (its row, ImageRole rows, and Image
    rows) onto the destination session. Faces are NOT duplicated.

    Returns a dict with the new cluster id and a flag indicating whether
    the target session was auto-unreviewed.

    Guard order (4xx before any DB writes):
      404 if source cluster missing, source session missing, target
          session missing.
      400 if cross-job, target archived, or source==target session.
    """
    source = db.query(Cluster).get(source_cluster_id)
    if source is None:
        raise HTTPException(404, "Source cluster not found")
    source_session = db.query(Session).get(source.session_id)
    if source_session is None:
        raise HTTPException(404, "Source session not found")
    target_session = db.query(Session).get(target_session_id)
    if target_session is None:
        raise HTTPException(404, "Target session not found")
    if source_session.job_id is None or source_session.job_id != target_session.job_id:
        raise HTTPException(400, "Cross-job copies are not allowed")
    if target_session.archived:
        raise HTTPException(400, "Target session is archived")
    if source.session_id == target_session.id:
        # Same-session copy refused for Phase B — coach use case is always
        # cross-team. In-session duplication can be revisited if a real need
        # appears.
        raise HTTPException(400, "Target is the same as source session")

    # ── Duplicate the Cluster row ────────────────────────────────────────
    # Copy: auto_label/manual_label (None for unlabeled), is_likely_coach,
    # manual_coach_override, image_count, label (deprecated but harmless),
    # needs_review, review_reason.
    # Skip: matched_player_id / match_confidence / match_tier / match_scope
    # / auto_label_source — these are face-match-derived. The destination
    # has no Faces (locked design), so leaving these null is honest.
    # Reset: accepted_cross_team to 0 (operator's source-side dismiss
    # shouldn't carry into a fresh copy).
    new_cluster = Cluster(
        session_id=target_session.id,
        label=source.label,
        auto_label=source.auto_label,
        manual_label=source.manual_label,
        is_likely_coach=source.is_likely_coach,
        manual_coach_override=source.manual_coach_override,
        image_count=source.image_count,
        needs_review=source.needs_review,
        review_reason=source.review_reason,
        # Skip fields → None / 0 (defaults handle it; explicit for clarity)
        matched_player_id=None,
        match_confidence=None,
        match_tier=None,
        match_scope=None,
        auto_label_source=None,
        accepted_cross_team=0,
    )
    db.add(new_cluster)
    db.flush()  # assign new_cluster.id

    # ── Duplicate Image rows + ImageRole rows ────────────────────────────
    # For each source ImageRole row: create a new Image row (option β —
    # new image_id, same path) on the destination session, then create a
    # new ImageRole row pointing at (new_image_id, new_cluster_id) with
    # the same role + manual_override.
    source_roles = (
        db.query(ImageRole).filter_by(cluster_id=source.id).all()
    )
    for src_role in source_roles:
        src_img = db.query(Image).get(src_role.image_id)
        if src_img is None:
            # Defensive — shouldn't happen with FK integrity. Log + skip.
            logger.warning(
                "[copy] source ImageRole %s references missing Image %s; "
                "skipping.", src_role.image_id, src_role.image_id,
            )
            continue
        new_img = Image(
            session_id=target_session.id,
            path=src_img.path,
            filename=src_img.filename,
            capture_time=src_img.capture_time,
            copyright_tag=src_img.copyright_tag,
        )
        db.add(new_img)
        db.flush()  # assign new_img.id
        db.add(ImageRole(
            image_id=new_img.id,
            cluster_id=new_cluster.id,
            role=src_role.role,
            manual_override=src_role.manual_override,
        ))

    # ── Auto-unreview destination session ────────────────────────────────
    target_unreviewed = False
    if target_session.reviewed:
        target_session.reviewed = 0
        target_session.reviewed_at = None
        target_unreviewed = True

    db.commit()
    db.refresh(new_cluster)

    logger.info(
        "[copy] cluster %s → session %s (new cluster %s, %s ImageRole rows, "
        "target_unreviewed=%s)",
        source_cluster_id, target_session_id, new_cluster.id,
        len(source_roles), target_unreviewed,
    )

    return {
        "status": "copied",
        "source_cluster_id": source_cluster_id,
        "new_cluster_id": new_cluster.id,
        "target_session_id": target_session.id,
        "target_unreviewed": target_unreviewed,
    }


@router.post("/{cluster_id}/copy-to-session")
def copy_to_session(
    cluster_id: int,
    payload: CopyToSessionRequest,
    db: DbSession = Depends(get_db),
):
    """Copy an unlabeled (or labeled) cluster to another team's session
    in the same job. See `copy_cluster_to_session` for the full design.

    Operator use case: a coach belongs to multiple teams; copy puts the
    coach photo on each team so it exports under all of them.
    """
    return copy_cluster_to_session(db, cluster_id, payload.target_session_id)
