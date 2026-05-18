"""Image endpoints — serve thumbnails and full images.

GET /api/images/{id}/thumb   small JPEG (256px max edge), cached on disk
GET /api/images/{id}/full    full-res JPEG
"""
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from PIL import Image as PILImage
from sqlalchemy.orm import Session as DbSession

from app.db import DATA_DIR, get_db
from app.models.db_models import Image

logger = logging.getLogger(__name__)

router = APIRouter()

THUMB_MAX_EDGE = 256
THUMB_DIR = DATA_DIR / "thumbs"
THUMB_DIR.mkdir(parents=True, exist_ok=True)


@router.get("/{image_id}/thumb")
def thumb(image_id: int, db: DbSession = Depends(get_db)):
    img = db.query(Image).get(image_id)
    if img is None:
        raise HTTPException(404, "Image not found")
    path = Path(img.path)
    if not path.exists():
        raise HTTPException(404, "File missing on disk")

    thumb_path = THUMB_DIR / f"{image_id}.jpg"
    src_mtime = path.stat().st_mtime
    if not thumb_path.exists() or thumb_path.stat().st_mtime < src_mtime:
        try:
            with PILImage.open(path) as im:
                im = im.convert("RGB")
                im.thumbnail((THUMB_MAX_EDGE, THUMB_MAX_EDGE))
                im.save(thumb_path, "JPEG", quality=85, optimize=True)
        except (OSError, PILImage.UnidentifiedImageError) as exc:
            logger.warning("Could not generate thumb for %s: %s", path, exc)
            return FileResponse(path)

    return FileResponse(thumb_path, media_type="image/jpeg")


@router.get("/{image_id}/full")
def full(image_id: int, db: DbSession = Depends(get_db)):
    img = db.query(Image).get(image_id)
    if img is None:
        raise HTTPException(404, "Image not found")
    path = Path(img.path)
    if not path.exists():
        raise HTTPException(404, "File missing on disk")
    return FileResponse(path)
