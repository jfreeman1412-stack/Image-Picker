"""Phase A.2 — reference photo endpoints (per-player).

    POST   /api/players/{player_id}/references                   upload (multipart) — append
    GET    /api/players/{player_id}/references                   list this player's references
    PUT    /api/players/{player_id}/references                   replace ALL with one
    DELETE /api/players/{player_id}/references/{ref_id}          delete one
    GET    /api/players/{player_id}/references/{ref_id}/image    serve the stored photo
    PUT    /api/players/{player_id}/references/shoot/{job_id}    replace this player's ref FOR one shoot (B.2)
    DELETE /api/players/{player_id}/references/shoot/{job_id}    delete this player's ref(s) FOR one shoot (B.2)

All routes nest under `/{player_id}/references...`, so none collide with the
A.1 `/{player_id}` route (different segment counts). The real logic lives in
`services/references.py`; these are thin multipart wrappers. See
PHASE_A2_REFERENCE_UPLOAD.md.
"""
import logging
from pathlib import Path

import json

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session as DbSession

from app.db import get_db
from app.models.db_models import ReferenceFace
from app.services.references import (
    ReferenceQualityError, add_reference, delete_reference,
    delete_shoot_references, detect_reference_faces, list_references,
    replace_player_references, replace_shoot_reference, resolve_shoot_reference,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _quality_400(exc: ReferenceQualityError) -> HTTPException:
    """Translate a failed quality gate into the HTTP 400 the client expects."""
    return HTTPException(400, detail={"error": exc.code, "message": str(exc)})


@router.post("/{player_id}/references")
async def upload_reference(
    player_id: int,
    file: UploadFile = File(...),
    job_id: int | None = None,
    db: DbSession = Depends(get_db),
):
    """Upload a reference photo for a player (appends). `?job_id=` is the
    optional shoot the photo was captured in (provenance)."""
    data = await file.read()
    try:
        return add_reference(
            db, player_id, data,
            captured_job_id=job_id, original_filename=file.filename)
    except ReferenceQualityError as exc:
        raise _quality_400(exc)


@router.get("/{player_id}/references")
def get_references(player_id: int, db: DbSession = Depends(get_db)):
    return list_references(db, player_id)


@router.put("/{player_id}/references")
async def replace_references(
    player_id: int,
    file: UploadFile = File(...),
    job_id: int | None = None,
    db: DbSession = Depends(get_db),
):
    """Replace ALL of this player's references with one fresh photo. A failed
    quality gate leaves the existing references intact."""
    data = await file.read()
    try:
        return replace_player_references(
            db, player_id, data,
            captured_job_id=job_id, original_filename=file.filename)
    except ReferenceQualityError as exc:
        raise _quality_400(exc)


@router.delete("/{player_id}/references/{ref_id}")
def remove_reference(player_id: int, ref_id: int, db: DbSession = Depends(get_db)):
    return delete_reference(db, player_id, ref_id)


@router.get("/{player_id}/references/{ref_id}/image")
def reference_image(player_id: int, ref_id: int, db: DbSession = Depends(get_db)):
    """Serve the stored reference photo. 404 if it isn't this player's or the
    file is missing on disk."""
    ref = (
        db.query(ReferenceFace)
        .filter_by(id=ref_id, player_id=player_id)
        .one_or_none()
    )
    if ref is None:
        raise HTTPException(404, "Reference not found")
    path = Path(ref.image_path)
    if not path.exists():
        raise HTTPException(404, "File missing on disk")
    return FileResponse(path)


# ── shoot-scoped replace / delete (Phase B.2) ─────────────────────────────
# Distinct 3-segment paths (`references/shoot/{job_id}`) — no collision with the
# typed-int `references/{ref_id}` routes above.

@router.put("/{player_id}/references/shoot/{job_id}")
async def replace_shoot_reference_endpoint(
    player_id: int,
    job_id: int,
    file: UploadFile = File(...),
    db: DbSession = Depends(get_db),
):
    """Set this player's single reference FOR THIS shoot (capture / retake).
    Validates, then wipes only this shoot's refs and stores the new one tagged
    with job_id; other shoots are untouched. A failed quality gate leaves this
    shoot's existing reference intact."""
    data = await file.read()
    try:
        return replace_shoot_reference(
            db, player_id, job_id, data, original_filename=file.filename)
    except ReferenceQualityError as exc:
        raise _quality_400(exc)


@router.delete("/{player_id}/references/shoot/{job_id}")
def delete_shoot_reference_endpoint(
    player_id: int, job_id: int, db: DbSession = Depends(get_db),
):
    """Remove this player's reference(s) FOR THIS shoot only. Idempotent;
    other shoots untouched."""
    return delete_shoot_references(db, player_id, job_id)


# ── Phase B.6 — salvage routes (additive; B.2 PUT above stays frozen) ────


@router.post("/{player_id}/references/shoot/{job_id}/detect")
async def detect_route(
    player_id: int,
    job_id: int,
    file: UploadFile = File(...),
    db: DbSession = Depends(get_db),
):
    """Phase B.6 — read-only face detection over the uploaded bytes.
    Returns {width, height, faces:[{index, bbox, det_score,
    face_area_ratio}]}. Writes nothing. Powers the operator's tap-target
    overlay in the capture app's Needs-attention salvage flow.

    Same 404s as the family (player or job missing). Same multipart shape
    as the B.2 PUT so the capture app reuses the upload helper."""
    data = await file.read()
    return detect_reference_faces(
        db, player_id, job_id, data, original_filename=file.filename)


@router.post("/{player_id}/references/shoot/{job_id}/resolve")
async def resolve_route(
    player_id: int,
    job_id: int,
    file: UploadFile = File(...),
    selected_bbox: str | None = Form(None),     # JSON-encoded [x, y, w, h]
    allow_low_confidence: bool = Form(False),
    db: DbSession = Depends(get_db),
):
    """Phase B.6 — operator-driven salvage. Validates with the extended
    gate (selected_bbox + allow_low_confidence), then scoped-replaces
    this player's references for this shoot (same wipe-then-store
    semantics as the B.2 PUT — which stays byte-for-byte unchanged).

    Form `selected_bbox` is a JSON string `[x,y,w,h]` or omitted.
    Form `allow_low_confidence` is bool (default False).
    Both omitted → equivalent to the B.2 PUT path but with
    `accepted_via='normal'` stamped on the row."""
    parsed_bbox: list[int] | None = None
    if selected_bbox is not None:
        try:
            parsed_bbox = json.loads(selected_bbox)
            if (not isinstance(parsed_bbox, list) or len(parsed_bbox) != 4
                    or not all(isinstance(v, (int, float)) for v in parsed_bbox)):
                raise ValueError("selected_bbox must be [x, y, w, h]")
            parsed_bbox = [int(v) for v in parsed_bbox]
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, detail={
                "error": "bad_selected_bbox", "message": str(exc),
            })
    data = await file.read()
    try:
        return resolve_shoot_reference(
            db, player_id, job_id, data,
            selected_bbox=parsed_bbox,
            allow_low_confidence=allow_low_confidence,
            original_filename=file.filename,
        )
    except ReferenceQualityError as exc:
        raise _quality_400(exc)
