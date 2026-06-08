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
from app.models.db_models import Cluster, Face, Image, ImageRole, Player, Session
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


# ── Move-card Phase 1 (2026-06-03) ───────────────────────────────────────────
#
# New endpoints layered on top of move_cluster's existing machinery:
#   POST /clusters/{id}/move-with-guards    — wraps `move_cluster` (mode=create)
#                                             with safety guards against
#                                             accidentally moving manually-
#                                             reviewed work. Clears
#                                             accepted_cross_team after move.
#   POST /clusters/{id}/dismiss-cross-team  — operator confirms intentional
#                                             cross-team appearance.
#                                             Sets accepted_cross_team=1.
#                                             Read-time match_team_mismatch
#                                             suppressed for this cluster.
#   POST /clusters/{id}/undismiss-cross-team — reverses the dismiss.
#
# Existing /move endpoint is intentionally untouched — RosterModal's Phase 6
# mismatch flow continues to work bit-for-bit.


class DismissCrossTeamRequest(BaseModel):
    """Empty body — endpoint just flips a flag. Kept as a class so the
    FE pattern matches other POST endpoints with payloads."""
    pass


class MoveWithGuardsRequest(BaseModel):
    target_session_id: int
    # When False (default), refuses to move if any of the cluster's images
    # has a manual_override=1 ImageRole OR the source session is reviewed=1.
    # When True, bypasses both guards explicitly — for the operator who
    # confirmed they really do want to move despite the work-loss risk.
    force: bool = False


def _check_move_safety(
    db: DbSession, source: Cluster, force: bool,
) -> dict | None:
    """Return an impact dict if the move is blocked by a safety guard,
    otherwise None. force=True returns None unconditionally.

    Blockers:
      - manual_role_overrides: count of ImageRole rows with
        manual_override=1 on any of the cluster's images. These are
        operator-locked role decisions that the move would carry forward
        (in create mode) — surfaced so the operator knows the move will
        bring locked picks into the target session.
      - source_session_reviewed: True if source session.reviewed=1. Moves
        from finalized sessions should not be casual.
    """
    if force:
        return None
    # Collect image_ids in the source cluster via Face rows.
    image_ids = {
        f.image_id
        for f in db.query(Face).filter_by(cluster_id=source.id).all()
    }
    manual_role_overrides = 0
    if image_ids:
        manual_role_overrides = (
            db.query(ImageRole)
            .filter(ImageRole.image_id.in_(image_ids),
                    ImageRole.manual_override == 1)
            .count()
        )
    source_session = db.query(Session).get(source.session_id)
    source_session_reviewed = bool(source_session and source_session.reviewed)

    if manual_role_overrides == 0 and not source_session_reviewed:
        return None
    return {
        "manual_role_overrides": manual_role_overrides,
        "source_session_reviewed": source_session_reviewed,
    }


@router.post("/{cluster_id}/move-with-guards")
def move_cluster_with_guards(
    cluster_id: int,
    payload: MoveWithGuardsRequest,
    db: DbSession = Depends(get_db),
):
    """Move-card workflow's move endpoint: existing /move machinery + the
    two mandatory safety guards. force=True bypasses both.

    Uses mode='create' under the hood (relocates the cluster intact;
    manual_label / manual_coach_override / ImageRoles preserved). Merge-
    mode is not exposed via this endpoint — the smart-suggestion flow
    explicitly creates a new cluster in the target session rather than
    merging into a name match (avoids surprise label collisions).

    On successful move: clears accepted_cross_team on the cluster (it's
    now in a session that should match its matched_player's team — the
    cross-team case is resolved by the move, not just acknowledged).
    """
    source = db.query(Cluster).get(cluster_id)
    if source is None:
        raise HTTPException(404, "Source cluster not found")

    impact = _check_move_safety(db, source, force=payload.force)
    if impact is not None:
        raise HTTPException(409, detail={
            "error": "move_blocked",
            "message": (
                "This cluster has operator-locked work that would be "
                "carried by the move (manual role picks) or comes from a "
                "reviewed session. Pass force=true to proceed."
            ),
            "impact": impact,
            "cluster_id": cluster_id,
        })

    # Delegate to the existing /move endpoint's body via direct call.
    # mode='create' relocates the source cluster intact.
    result = move_cluster(
        cluster_id,
        MoveRequest(target_session_id=payload.target_session_id,
                    mode="create"),
        db,
    )

    # After a successful move, clear accepted_cross_team. The cluster
    # has been moved to (presumably) the right team — the cross-team
    # case is resolved by the relocation, not just dismissed.
    db.expire_all()
    moved = db.query(Cluster).get(cluster_id)
    if moved is not None and moved.accepted_cross_team:
        moved.accepted_cross_team = 0
        db.commit()

    return result


@router.post("/{cluster_id}/dismiss-cross-team")
def dismiss_cross_team(
    cluster_id: int,
    payload: DismissCrossTeamRequest,
    db: DbSession = Depends(get_db),
):
    """Operator confirms this cluster's cross-team appearance is
    intentional (a guest player, sibling in a buddy shot, or a kid who
    actually plays on multiple teams). Sets accepted_cross_team=1; the
    read-time match_team_mismatch flag is suppressed for this cluster
    until the operator undismisses it OR the cluster is moved.

    Idempotent: dismissing an already-dismissed cluster returns the
    same response without error."""
    c = db.query(Cluster).get(cluster_id)
    if c is None:
        raise HTTPException(404, "Cluster not found")
    if not c.accepted_cross_team:
        c.accepted_cross_team = 1
        db.commit()
    return {
        "cluster_id": c.id,
        "accepted_cross_team": True,
    }


@router.post("/{cluster_id}/undismiss-cross-team")
def undismiss_cross_team(
    cluster_id: int,
    db: DbSession = Depends(get_db),
):
    """Operator reverses a prior dismiss. accepted_cross_team flips back
    to 0; the match_team_mismatch flag re-surfaces on the next read."""
    c = db.query(Cluster).get(cluster_id)
    if c is None:
        raise HTTPException(404, "Cluster not found")
    if c.accepted_cross_team:
        c.accepted_cross_team = 0
        db.commit()
    return {
        "cluster_id": c.id,
        "accepted_cross_team": False,
    }


# ── Move-card Phase 2 (2026-06-08) ───────────────────────────────────────────
#
# Two new endpoints that extend Phase 1's safety-guarded move to two cases the
# operator runs into when the matched player's roster team has no session yet:
#
#   POST /{cluster_id}/move-to-new-session  (Case 2)
#       team_name is a known roster team (in PlayerMembership for this job)
#       that has no session yet. We auto-create an empty session and move.
#
#   POST /{cluster_id}/move-to-add-team  (Case 3)
#       team_name comes from the "+ Add new team…" modal. It might (a) match
#       an existing session, (b) match an existing roster team without a
#       session, or (c) be brand new. The endpoint figures out which sub-case
#       it actually is and delegates internally; for (c) it also inserts a
#       PlayerMembership row for the cluster's matched player (silently
#       skipped when the cluster has no matched player — per spec the new
#       team exists as a session only and player ID happens later).
#
# Both endpoints reuse Phase 1's _check_move_safety and the existing
# move_cluster(mode="create") machinery. The auto-created session has
# status="done" + source_path=NULL so the export pipeline (which keys off
# status=="done") sees it as soon as a cluster lands in it.


class MoveToNewSessionRequest(BaseModel):
    team_name: str
    # Same semantics as MoveWithGuardsRequest.force: bypass _check_move_safety
    # for an operator who confirmed the work-loss risk.
    force: bool = False


class MoveToAddTeamRequest(BaseModel):
    team_name: str
    force: bool = False


def _norm_team(s: str) -> str:
    """Match Phase A.1's norm_team rule for case-insensitive team-name
    comparisons (replace_shoot_memberships writes the same key)."""
    from app.services.roster import normalize_name
    return normalize_name(s)


def _find_session_in_job_by_name(
    db: DbSession, job_id: int, team_name: str,
) -> Session | None:
    """Case-insensitive lookup for a non-archived session in this job whose
    name matches team_name. None if not found."""
    target_norm = _norm_team(team_name)
    if not target_norm:
        return None
    for s in db.query(Session).filter_by(job_id=job_id, archived=0).all():
        if _norm_team(s.name) == target_norm:
            return s
    return None


def _roster_team_exists(db: DbSession, job_id: int, team_name: str) -> bool:
    """True iff any PlayerMembership row in this job has norm_team matching
    the normalized team_name. Drives Case 3's existing-team-match branch."""
    from app.models.db_models import PlayerMembership
    target_norm = _norm_team(team_name)
    if not target_norm:
        return False
    return db.query(PlayerMembership).filter_by(
        job_id=job_id, norm_team=target_norm,
    ).first() is not None


def _create_empty_session(
    db: DbSession, job_id: int, team_name: str,
) -> Session:
    """Create an empty session for Phase 2 auto-routing. status='done' so the
    export pipeline includes it once a cluster moves in; source_path=NULL
    since there is no folder to ingest. Caller commits."""
    from datetime import datetime
    s = Session(
        job_id=job_id,
        name=team_name,
        source_path=None,
        status="done",
        created_at=datetime.utcnow(),
    )
    db.add(s)
    db.flush()
    return s


@router.post("/{cluster_id}/move-to-new-session")
def move_to_new_session(
    cluster_id: int,
    payload: MoveToNewSessionRequest,
    db: DbSession = Depends(get_db),
):
    """Case 2: matched player's roster team has no session yet. Validate
    that team_name IS in PlayerMembership for the job (Case 2's precondition
    — Case 3 handles brand-new teams), reject if a non-archived session of
    the same name already exists, check Phase 1's safety guards, create
    the empty session, then delegate to move_cluster(mode='create').

    On guard block we 409 BEFORE creating the session so guard-blocked
    attempts don't leak orphan empty sessions."""
    source = db.query(Cluster).get(cluster_id)
    if source is None:
        raise HTTPException(404, "Source cluster not found")
    source_session = db.query(Session).get(source.session_id)
    if source_session is None or source_session.job_id is None:
        raise HTTPException(400, "Source session has no job")
    job_id = source_session.job_id

    team_name = (payload.team_name or "").strip()
    if not team_name:
        raise HTTPException(400, detail={
            "error": "missing_team_name",
            "message": "team_name is required.",
        })
    if not _roster_team_exists(db, job_id, team_name):
        raise HTTPException(400, detail={
            "error": "team_not_in_roster",
            "message": f"'{team_name}' is not in this job's roster. Use "
                       "the 'Add new team' flow for brand-new teams.",
        })
    if _find_session_in_job_by_name(db, job_id, team_name) is not None:
        raise HTTPException(409, detail={
            "error": "session_already_exists",
            "message": f"A session named '{team_name}' already exists in "
                       "this job. Pick it from the team dropdown instead.",
        })

    # Phase 1 guards BEFORE session creation — guard-blocked attempts must
    # not leak orphan empty sessions.
    impact = _check_move_safety(db, source, force=payload.force)
    if impact is not None:
        raise HTTPException(409, detail={
            "error": "move_blocked",
            "message": (
                "This cluster has operator-locked work that would be "
                "carried by the move (manual role picks) or comes from a "
                "reviewed session. Pass force=true to proceed."
            ),
            "impact": impact,
            "cluster_id": cluster_id,
        })

    new_sess = _create_empty_session(db, job_id, team_name)
    db.commit()

    result = move_cluster(
        cluster_id,
        MoveRequest(target_session_id=new_sess.id, mode="create"),
        db,
    )
    # Same post-move bookkeeping as /move-with-guards.
    db.expire_all()
    moved = db.query(Cluster).get(cluster_id)
    if moved is not None and moved.accepted_cross_team:
        moved.accepted_cross_team = 0
        db.commit()
    return result


@router.post("/{cluster_id}/move-to-add-team")
def move_to_add_team(
    cluster_id: int,
    payload: MoveToAddTeamRequest,
    db: DbSession = Depends(get_db),
):
    """Case 3: operator typed a team name in the "+ Add new team…" modal.
    Three internal branches:

      (a) Name normalizes to an existing non-archived session → Case 1: just
          /move-with-guards into that session. No new session, no new
          membership.
      (b) Name normalizes to a roster team without a session → Case 2 path:
          create empty session, move. No new membership (already in roster).
      (c) Truly new team → add_walkup_player IFF the cluster has a
          matched_player_id (silently skip the membership otherwise per
          spec); create empty session; move.

    Phase 1 guards apply uniformly — checked BEFORE any session/membership
    creation so a guard-blocked Case 3 leaks neither artifact."""
    from app.models.db_models import PlayerMembership
    from app.services.players import add_walkup_player

    source = db.query(Cluster).get(cluster_id)
    if source is None:
        raise HTTPException(404, "Source cluster not found")
    source_session = db.query(Session).get(source.session_id)
    if source_session is None or source_session.job_id is None:
        raise HTTPException(400, "Source session has no job")
    job_id = source_session.job_id

    team_name = (payload.team_name or "").strip()
    if not team_name:
        raise HTTPException(400, detail={
            "error": "missing_team_name",
            "message": "team_name is required.",
        })

    # Guard check FIRST so a block leaks neither a session nor a membership.
    impact = _check_move_safety(db, source, force=payload.force)
    if impact is not None:
        raise HTTPException(409, detail={
            "error": "move_blocked",
            "message": (
                "This cluster has operator-locked work that would be "
                "carried by the move (manual role picks) or comes from a "
                "reviewed session. Pass force=true to proceed."
            ),
            "impact": impact,
            "cluster_id": cluster_id,
        })

    # (a) Name matches an existing session → Case 1 fall-through.
    existing = _find_session_in_job_by_name(db, job_id, team_name)
    if existing is not None:
        return move_cluster(
            cluster_id,
            MoveRequest(target_session_id=existing.id, mode="create"),
            db,
        )

    # (b) Name matches a roster team without a session → Case 2 fall-through.
    # No add_walkup_player needed — the player(s) are already in the roster
    # for this team.
    is_roster_team = _roster_team_exists(db, job_id, team_name)
    new_sess = _create_empty_session(db, job_id, team_name)
    db.commit()

    if not is_roster_team:
        # (c) Truly new team. Add the cluster's matched player to it via the
        # Phase B.5 walkup path — but only when the cluster IS matched. An
        # unmatched cluster yields a session with no members (operator will
        # rename + add roster row later).
        if source.matched_player_id is not None:
            player = db.query(Player).get(source.matched_player_id)
            if player is not None:
                add_walkup_player(db, job_id, player.display_name, team_name)

    result = move_cluster(
        cluster_id,
        MoveRequest(target_session_id=new_sess.id, mode="create"),
        db,
    )
    db.expire_all()
    moved = db.query(Cluster).get(cluster_id)
    if moved is not None and moved.accepted_cross_team:
        moved.accepted_cross_team = 0
        db.commit()
    return result


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
