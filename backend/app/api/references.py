"""Phase A.2 — reference photo endpoints (per-player).

    POST   /api/players/{player_id}/references                   upload (multipart) — append
    GET    /api/players/{player_id}/references                   list this player's references
    PUT    /api/players/{player_id}/references                   replace ALL with one
    DELETE /api/players/{player_id}/references/{ref_id}          delete one
    GET    /api/players/{player_id}/references/{ref_id}/image    serve the stored photo

All routes nest under `/{player_id}/references...`, so none collide with the
A.1 `/{player_id}` route (different segment counts). The real logic lives in
`services/references.py`; these are thin multipart wrappers. See
PHASE_A2_REFERENCE_UPLOAD.md.
"""
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session as DbSession

from app.db import get_db
from app.models.db_models import ReferenceFace
from app.services.references import (
    ReferenceQualityError, add_reference, delete_reference, list_references,
    replace_player_references,
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
