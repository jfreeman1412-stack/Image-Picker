"""Cluster endpoints — viewing and reassigning.

GET    /api/sessions/{id}/clusters         list clusters with image refs + roles
POST   /api/sessions/{id}/reassign         move images between clusters
POST   /api/sessions/{id}/merge-clusters   merge two clusters
POST   /api/sessions/{id}/new-cluster      create an empty cluster
POST   /api/sessions/{id}/clusters/{cid}/rename
"""
from datetime import datetime
from typing import List
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session as DbSession

from app.db import get_db
from app.models.db_models import (
    Session, Cluster, Face, Image, ImageRole, Player, PlayerMembership,
)
from app.services.face_pipeline import _sort_cluster
from app.services.outliers import flag_outliers
from app.services.roster import build_lookup
from app.services.roster_check import (
    add_duplicate_label_flag, add_match_team_mismatch_flag, add_roster_flag,
    cluster_is_mismatched, cluster_match_team_mismatch,
    find_duplicate_label_cluster_ids, session_norm_team,
)
from app.api.settings import get_flag_visibility_map, filter_visible_reasons

router = APIRouter()


def _resort_session_outliers(db: DbSession, session_id: int) -> None:
    """Re-run outlier flagging across a session's clusters after mutations.

    Sort-rule-driven needs_review (no_team_pick / no_pano_pick / empty_cluster)
    is already set by _sort_cluster — only promote outlier_high/low for
    clusters that aren't already flagged by a rule.
    """
    clusters = db.query(Cluster).filter_by(session_id=session_id).all()
    sizes = {c.id: (c.image_count or 0) for c in clusters}
    flags_by_cluster = {f.cluster_id: f for f in flag_outliers(sizes)}
    for c in clusters:
        f = flags_by_cluster.get(c.id)
        if f and f.flagged and not c.needs_review:
            c.needs_review = 1
            c.review_reason = f.reason


@router.get("/{session_id}/clusters")
def list_clusters(session_id: int, db: DbSession = Depends(get_db)):
    s = db.query(Session).get(session_id)
    if s is None:
        raise HTTPException(404, "Session not found")

    flag_vis = get_flag_visibility_map(db)
    # Roster lookup is per-job. Empty dict → no roster uploaded → mismatch
    # check is a no-op (see roster_check.cluster_is_mismatched).
    roster_lookup = build_lookup(db, s.job_id) if s.job_id else {}
    sess_norm = session_norm_team(s)
    # Phase 9: cross-cluster duplicate-auto-label set. Read-time only.
    dup_cluster_ids = find_duplicate_label_cluster_ids(s.clusters)
    # Phase 11: cross-team guest clusters (phantom siblings). Cheap unless
    # this session actually has structural candidates (then it loads other
    # teams' centroids once). {cluster_id: {name, team, session_id, distance}}
    from app.services.guest_clusters import guest_map_for_session
    guest_map = guest_map_for_session(db, s)

    # Phase A.4: per-job roster team lookup (for the read-time match team
    # mismatch) + matched-player display names. One query each, reused below.
    membership_teams_by_player: dict[int, set[str]] = {}
    raw_team_by_player: dict[int, set[str]] = {}
    if s.job_id is not None:
        for m in db.query(PlayerMembership).filter_by(job_id=s.job_id).all():
            membership_teams_by_player.setdefault(m.player_id, set()).add(m.norm_team)
            raw_team_by_player.setdefault(m.player_id, set()).add(m.team_name)
    matched_ids = {c.matched_player_id for c in s.clusters if c.matched_player_id}
    matched_name_by_id: dict[int, str] = {}
    if matched_ids:
        matched_name_by_id = dict(
            db.query(Player.id, Player.display_name)
            .filter(Player.id.in_(matched_ids)).all()
        )

    out = []
    for c in s.clusters:
        # 2026-06-29: derive cluster image membership for DISPLAY from
        # ImageRole UNION Face — three cluster classes coexist in production
        # and they each populate the two relations differently:
        #
        #   - Normal pipeline cluster: every image has BOTH a Face row
        #     (clustering primitive) AND an ImageRole row (assigned by
        #     _sort_cluster). Union = either set; no double-count, no
        #     change vs the pre-Phase-B c.faces-only read.
        #   - Copy-to-session (Phase B, 2026-06-25): Image+ImageRole rows
        #     duplicated, Face rows NOT duplicated. ImageRole is the only
        #     populated relation. Union = ImageRole — matches 9dbf889.
        #   - Buddy-only coach: faces only appear in multi-face shots that
        #     "belong to" another cluster's solo subject. _sort_cluster
        #     intentionally writes NO ImageRole rows for the coach (the
        #     buddy shot's ImageRole goes to the kid). Faces only. Union
        #     = Face — restores the pre-9dbf889 ability to view + act on
        #     these coach cards. review_reason='coach_no_solo_image'.
        #
        # The 2026-06-25 fix was correct that the card's source of truth
        # should align with export's (ImageRole), but missed that the
        # buddy-only-coach class exists outside ImageRole's scope. UNION
        # is the reconciliation — display covers all three; export still
        # consults ImageRole alone (correct — coach buddy shots already
        # export via the kid's ImageRole). See [[face-vs-imagerole-membership]].
        #
        # role_by_image stays ImageRole-keyed. For buddy-only-coach buddy
        # shots, the lookup returns no entry → role=None displayed on the
        # thumbnail (matches pre-9dbf889 behavior — operator sees the
        # photo but no role chip on it from the coach's vantage).
        role_rows = db.query(ImageRole).filter(ImageRole.cluster_id == c.id).all()
        face_image_ids = {f.image_id for f in c.faces}
        image_ids = {r.image_id for r in role_rows} | face_image_ids
        image_rows = db.query(Image).filter(Image.id.in_(image_ids)).all()
        role_by_image = {r.image_id: (r.role, bool(r.manual_override)) for r in role_rows}

        images = []
        # Tuple sort-key: untimed images (capture_time=None) sort to the front
        # via the datetime.min sentinel; among each group, filename is the
        # tiebreaker. The earlier `i.capture_time or i.filename` form returned
        # a mix of datetime and str across a cluster's images and tripped a
        # str-vs-datetime TypeError whenever a cluster contained both
        # populations — which surfaces in any merged session whose source
        # folders disagree on whether EXIF DateTimeOriginal is present
        # (e.g., outdoor-shoot PNGs converted from RAW keep capture_time;
        # camera JPGs from the naturals line drop it).
        for img in sorted(image_rows,
                          key=lambda i: (i.capture_time or datetime.min, i.filename)):
            role, manual = role_by_image.get(img.id, (None, False))
            images.append({
                "image_id": img.id,
                "filename": img.filename,
                "capture_time": img.capture_time.isoformat() if img.capture_time else None,
                "thumb_url": f"/api/images/{img.id}/thumb",
                "full_url": f"/api/images/{img.id}/full",
                "role": role,
                "manual_override": manual,
            })

        # Read-time roster_mismatch splice. Never persisted on
        # Cluster.review_reason — roster can be uploaded/cleared without
        # re-running the pipeline.
        mismatched = cluster_is_mismatched(c, sess_norm, roster_lookup)
        combined_reason = add_roster_flag(c.review_reason, mismatched)
        # Phase 9: cross-cluster duplicate_auto_label splice (also read-time).
        combined_reason = add_duplicate_label_flag(
            combined_reason, c.id in dup_cluster_ids,
        )
        # Phase A.4: read-time match team mismatch splice.
        team_mismatch = cluster_match_team_mismatch(
            c, sess_norm, membership_teams_by_player,
        )
        # Move-card Phase 1 (2026-06-03): operator-dismissed cross-team
        # cases suppress the flag at read time. The match data still
        # surfaces in the `match` block below so the UI can render the
        # "dismissed" state explicitly if needed.
        if c.accepted_cross_team:
            team_mismatch = False
        combined_reason = add_match_team_mismatch_flag(combined_reason, team_mismatch)
        visible_reasons = filter_visible_reasons(combined_reason, flag_vis)
        out.append({
            "cluster_id": c.id,
            "label": c.display_label(),
            "image_count": c.image_count,
            # needs_review is the raw pipeline truth; the UI should key its
            # yellow badge off visible_review_reasons so hiding all of a
            # cluster's flags visually clears it.
            "needs_review": bool(c.needs_review),
            "review_reason": combined_reason,
            "visible_review_reasons": visible_reasons,
            "is_likely_coach": bool(c.is_likely_coach),
            "is_coach_for_sort": c.is_coach_for_sort(),
            "manual_coach_override": c.manual_coach_override or 0,
            "accepted_cross_team": bool(c.accepted_cross_team),
            # Phase B (2026-06-25): has_faces=False indicates a copied cluster
            # (no Face rows, won't survive a pipeline re-run). FE renders a
            # "📋 copy" badge for these. Normal pipeline-derived clusters
            # always have Face rows (one per detected face); a copy created
            # via /api/clusters/{id}/copy-to-session has none by design.
            "has_faces": bool(c.faces),
            "guest_of": guest_map.get(c.id),   # None unless a confirmed cross-team guest
            # Phase A.4: reference-match data (additive — existing UI ignores it;
            # A.5 will render it). roster_team is the matched player's team(s)
            # for this job, None when they aren't rostered here (e.g. fallback).
            "match": {
                "player_id": c.matched_player_id,
                "player_name": matched_name_by_id.get(c.matched_player_id),
                "confidence": c.match_confidence,
                "tier": c.match_tier,
                "scope": c.match_scope,
                "roster_team": ", ".join(
                    sorted(raw_team_by_player.get(c.matched_player_id, set()))
                ) or None,
                "team_mismatch": team_mismatch,
            },
            "images": images,
        })
    return out


class ReassignRequest(BaseModel):
    image_id: int
    from_cluster_id: int
    to_cluster_id: int


@router.post("/{session_id}/reassign")
def reassign(session_id: int, payload: ReassignRequest, db: DbSession = Depends(get_db)):
    """Move the Face row belonging to image_id+from_cluster_id over to to_cluster_id.

    NOTE: If the image has multiple faces (buddy photo), only the face that
    currently belongs to from_cluster_id is reassigned — other faces in the
    image stay with their own clusters.
    """
    face = db.query(Face).filter_by(
        image_id=payload.image_id, cluster_id=payload.from_cluster_id
    ).first()
    if face is None:
        raise HTTPException(404, "No face in that image belongs to from_cluster_id")
    face.cluster_id = payload.to_cluster_id
    db.flush()

    for cid in (payload.from_cluster_id, payload.to_cluster_id):
        c = db.query(Cluster).get(cid)
        if c is not None:
            _sort_cluster(db, c)
    _resort_session_outliers(db, session_id)
    db.commit()
    return {"status": "reassigned"}


class MergeRequest(BaseModel):
    source_cluster_id: int
    target_cluster_id: int


@router.post("/{session_id}/merge-clusters")
def merge_clusters(session_id: int, payload: MergeRequest, db: DbSession = Depends(get_db)):
    src = db.query(Cluster).get(payload.source_cluster_id)
    tgt = db.query(Cluster).get(payload.target_cluster_id)
    if src is None or tgt is None:
        raise HTTPException(404, "Cluster not found")
    db.query(Face).filter_by(cluster_id=src.id).update({"cluster_id": tgt.id})
    db.query(ImageRole).filter_by(cluster_id=src.id).delete(synchronize_session=False)
    db.query(Cluster).filter_by(id=src.id).delete()
    db.flush()

    _sort_cluster(db, tgt)
    _resort_session_outliers(db, session_id)
    db.commit()
    return {"status": "merged", "into": tgt.id}


class NewClusterRequest(BaseModel):
    label: str = "New player"


@router.post("/{session_id}/new-cluster")
def new_cluster(session_id: int, payload: NewClusterRequest, db: DbSession = Depends(get_db)):
    c = Cluster(session_id=session_id, manual_label=payload.label, image_count=0)
    db.add(c)
    db.commit()
    db.refresh(c)
    return {"cluster_id": c.id, "label": c.display_label()}


class SetCoachOverrideRequest(BaseModel):
    cluster_id: int
    override: int  # 0 = auto, 1 = forced coach, -1 = forced player


@router.post("/{session_id}/set-coach-override")
def set_coach_override(
    session_id: int, payload: SetCoachOverrideRequest,
    db: DbSession = Depends(get_db),
):
    """Force a cluster's coach/player state and immediately re-sort it.

    The override is read by Cluster.is_coach_for_sort(), which _sort_cluster
    dispatches on — so this survives a full pipeline re-run too.
    """
    if payload.override not in (0, 1, -1):
        raise HTTPException(400, "override must be 0, 1, or -1")
    c = db.query(Cluster).filter_by(
        id=payload.cluster_id, session_id=session_id,
    ).first()
    if c is None:
        raise HTTPException(404, "Cluster not found")

    c.manual_coach_override = payload.override
    db.flush()
    _sort_cluster(db, c)
    _resort_session_outliers(db, session_id)
    db.commit()

    flag_vis = get_flag_visibility_map(db)
    return {
        "cluster_id": c.id,
        "manual_coach_override": c.manual_coach_override,
        "is_coach_for_sort": c.is_coach_for_sort(),
        "is_likely_coach": bool(c.is_likely_coach),
        "needs_review": bool(c.needs_review),
        "review_reason": c.review_reason,
        "visible_review_reasons": filter_visible_reasons(c.review_reason, flag_vis),
    }


class RenameRequest(BaseModel):
    label: str


@router.post("/{session_id}/clusters/{cluster_id}/rename")
def rename_cluster(
    session_id: int, cluster_id: int, payload: RenameRequest,
    db: DbSession = Depends(get_db),
):
    """Rename a cluster. Writes manual_label (wins over auto_label)."""
    c = db.query(Cluster).filter_by(id=cluster_id, session_id=session_id).first()
    if c is None:
        raise HTTPException(404, "Cluster not found")
    c.manual_label = payload.label
    db.commit()
    return {"status": "renamed", "label": c.manual_label}


VALID_ROLES = {"team", "panoramic", "individual", "buddy", "rejected"}
SOLO_ROLES = {"team", "panoramic"}


class SetRoleRequest(BaseModel):
    image_id: int
    cluster_id: int
    role: str


@router.post("/{session_id}/set-role")
def set_role(session_id: int, payload: SetRoleRequest, db: DbSession = Depends(get_db)):
    """Manually assign a role to an image within a cluster (locks it).

    Only one team and one panoramic per cluster. Setting role=team or pano
    clears any other manually-set or auto-assigned image with that role in
    the same cluster, downgrading them to 'individual'.
    """
    if payload.role not in VALID_ROLES:
        raise HTTPException(400, f"Invalid role: {payload.role}")

    # 2026-09-14 F1: resolve cluster by id alone, then explicitly disambiguate
    # "genuinely missing" (404) from "cluster was moved out of this session
    # since the frontend last rendered" (409). Pre-fix this returned a bare
    # 404 for BOTH cases; after a move-with-guards mutates cluster.session_id,
    # any set-role fired from a stale source-page render would 404 with no
    # actionable message and the frontend's set-role-defensive alert would
    # say "That change didn't save (HTTP 404)" — which is technically true
    # but doesn't tell the operator the cluster is fine, just elsewhere.
    # The 409 gives the frontend a specific reason to auto-refresh and
    # surface a "cluster moved — refreshed" message instead. See F2 for
    # the frontend optimistic-clear that closes the race window in the
    # common single-user case; this backend guard catches the residual
    # multi-tab / another-user-moved-it / RosterModal cases.
    cluster = db.query(Cluster).get(payload.cluster_id)
    if cluster is None:
        raise HTTPException(404, "Cluster not found")
    if cluster.session_id != session_id:
        raise HTTPException(409, detail={
            "error": "cluster_moved",
            "message": (
                "This cluster is no longer in this session — it was moved "
                "to another team. Refresh to see the current cluster list."
            ),
            "cluster_id": cluster.id,
            "current_session_id": cluster.session_id,
        })

    if payload.role in SOLO_ROLES:
        existing_solos = db.query(ImageRole).filter(
            ImageRole.cluster_id == payload.cluster_id,
            ImageRole.role == payload.role,
            ImageRole.image_id != payload.image_id,
        ).all()
        for er in existing_solos:
            er.role = "individual"

    role_row = db.query(ImageRole).filter_by(
        image_id=payload.image_id, cluster_id=payload.cluster_id,
    ).first()
    if role_row is None:
        role_row = ImageRole(
            image_id=payload.image_id,
            cluster_id=payload.cluster_id,
            role=payload.role,
            manual_override=1,
        )
        db.add(role_row)
    else:
        role_row.role = payload.role
        role_row.manual_override = 1

    db.flush()
    _sort_cluster(db, cluster)
    _resort_session_outliers(db, session_id)
    db.commit()
    return {"status": "set", "role": payload.role}


class ClearRoleOverrideRequest(BaseModel):
    image_id: int
    cluster_id: int


@router.post("/{session_id}/clear-role-override")
def clear_role_override(
    session_id: int, payload: ClearRoleOverrideRequest,
    db: DbSession = Depends(get_db),
):
    """Unlock a manually-overridden role and let the auto-sort decide again."""
    cluster = db.query(Cluster).filter_by(
        id=payload.cluster_id, session_id=session_id,
    ).first()
    if cluster is None:
        raise HTTPException(404, "Cluster not found")

    role_row = db.query(ImageRole).filter_by(
        image_id=payload.image_id, cluster_id=payload.cluster_id,
    ).first()
    if role_row is not None:
        role_row.manual_override = 0

    db.flush()
    _sort_cluster(db, cluster)
    _resort_session_outliers(db, session_id)
    db.commit()
    return {"status": "cleared"}
