"""Cluster endpoints — viewing and reassigning.

GET    /api/sessions/{id}/clusters         list clusters with image refs + roles
POST   /api/sessions/{id}/reassign         move images between clusters
POST   /api/sessions/{id}/merge-clusters   merge two clusters
POST   /api/sessions/{id}/new-cluster      create an empty cluster
POST   /api/sessions/{id}/clusters/{cid}/rename
"""
from typing import List
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session as DbSession

from app.db import get_db
from app.models.db_models import Session, Cluster, Face, Image, ImageRole
from app.services.face_pipeline import _sort_cluster
from app.services.outliers import flag_outliers
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

    out = []
    for c in s.clusters:
        image_ids = {f.image_id for f in c.faces}
        image_rows = db.query(Image).filter(Image.id.in_(image_ids)).all()
        role_rows = db.query(ImageRole).filter(
            ImageRole.cluster_id == c.id,
            ImageRole.image_id.in_(image_ids),
        ).all()
        role_by_image = {r.image_id: (r.role, bool(r.manual_override)) for r in role_rows}

        images = []
        for img in sorted(image_rows, key=lambda i: i.capture_time or i.filename):
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

        visible_reasons = filter_visible_reasons(c.review_reason, flag_vis)
        out.append({
            "cluster_id": c.id,
            "label": c.display_label(),
            "image_count": c.image_count,
            # needs_review is the raw pipeline truth; the UI should key its
            # yellow badge off visible_review_reasons so hiding all of a
            # cluster's flags visually clears it.
            "needs_review": bool(c.needs_review),
            "review_reason": c.review_reason,
            "visible_review_reasons": visible_reasons,
            "is_likely_coach": bool(c.is_likely_coach),
            "is_coach_for_sort": c.is_coach_for_sort(),
            "manual_coach_override": c.manual_coach_override or 0,
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

    cluster = db.query(Cluster).filter_by(
        id=payload.cluster_id, session_id=session_id,
    ).first()
    if cluster is None:
        raise HTTPException(404, "Cluster not found")

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
