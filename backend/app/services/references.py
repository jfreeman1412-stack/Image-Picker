"""Phase A.2 — reference photo quality gate + storage service.

Hangs the first real face data off the A.1 `Player` spine: a reference photo,
its 512-d InsightFace embedding, and automatic quality gates. Reuses the locked
InsightFace integration via `face_detector.detect_faces` (never constructs the
model itself). See PHASE_A2_REFERENCE_UPLOAD.md.

The detection+gate logic is split into a pure `evaluate_reference_quality` over
a detection list (unit-tested with hand-built dicts, no model) and the I/O
cores (`add_reference` / `replace_player_references` / `list_references` /
`delete_reference`) that tests drive with a monkeypatched detector.
"""
from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from uuid import uuid4

import numpy as np
from fastapi import HTTPException
from sqlalchemy.orm import Session as DbSession

from app.db import DATA_DIR
from app.models.db_models import Job, Player, ReferenceFace
from app.services import face_detector
from app.services.face_detector import DET_SCORE_THRESHOLD  # 0.5 — multiplicity floor

logger = logging.getLogger(__name__)

REF_MIN_DET_SCORE = 0.65     # single-face confidence gate (≥ DET_SCORE_THRESHOLD)
REF_MIN_AREA_RATIO = 0.02    # face bbox must be ≥ 2% of the frame
REFERENCES_DIR = DATA_DIR / "references"
REFERENCES_DIR.mkdir(parents=True, exist_ok=True)

_ALLOWED_EXTS = (".jpg", ".jpeg", ".png")


class ReferenceQualityError(ValueError):
    """A reference photo failed an automatic quality gate. Carries a stable
    `code` (for the client) plus a human message. The HTTP layer maps this to
    400 with detail={"error": code, "message": str}."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# ── the quality gate (pure — the heart of the phase) ─────────────────────

def evaluate_reference_quality(detections: list[dict]) -> dict:
    """Pick the single reference face from a detection list, or raise
    ReferenceQualityError with a clear code+message. Pure — no I/O, no model —
    so it's unit-tested directly with hand-built dicts.

    Order matters; first failure wins:
      no_face        — zero faces (also covers an unreadable image, which
                       detect_faces returns as [])
      multiple_faces — 2+ faces at the DET_SCORE_THRESHOLD (0.5) floor (ANY
                       second real face → alert; the user wants bystanders
                       over-caught rather than risk a poisoned reference)
      low_confidence — the single face is below REF_MIN_DET_SCORE
      face_too_small — the single face's area ratio is below REF_MIN_AREA_RATIO
    Returns the chosen detection dict on success.
    """
    # detect_faces() already drops anything below DET_SCORE_THRESHOLD, but be
    # explicit about the multiplicity floor so intent survives any future
    # change to the detector's filtering.
    faces = [d for d in detections if d["det_score"] >= DET_SCORE_THRESHOLD]
    if not faces:
        raise ReferenceQualityError(
            "no_face",
            "No face was detected in the photo (or the file isn't a readable "
            "image). Retake with the player's face clearly in frame.")
    if len(faces) > 1:
        raise ReferenceQualityError(
            "multiple_faces",
            f"{len(faces)} faces detected. A reference photo must show exactly "
            "one person — make sure no one else is in frame.")
    face = faces[0]
    if face["det_score"] < REF_MIN_DET_SCORE:
        raise ReferenceQualityError(
            "low_confidence",
            "Face detection confidence is too low. Retake in better lighting, "
            "facing the camera.")
    if (face.get("face_area_ratio") or 0.0) < REF_MIN_AREA_RATIO:
        raise ReferenceQualityError(
            "face_too_small",
            "The face is too small in the frame. Move closer and retake.")
    return face


# ── helpers ──────────────────────────────────────────────────────────────

def _require_player(db: DbSession, player_id: int) -> Player:
    player = db.query(Player).get(player_id)
    if player is None:
        raise HTTPException(404, "Player not found")
    return player


def _require_job_if_given(db: DbSession, job_id: int | None) -> None:
    if job_id is not None and db.query(Job).get(job_id) is None:
        raise HTTPException(404, "Job not found")


def _safe_ext(original_filename: str | None) -> str:
    """Sanitized extension limited to jpg/jpeg/png; default .jpg. The temp
    file keeps this suffix so detect_faces' PNG-vs-cv2 dispatch works."""
    if original_filename:
        suffix = Path(original_filename).suffix.lower()
        if suffix in _ALLOWED_EXTS:
            return suffix
    return ".jpg"


def _write_temp(data: bytes, ext: str) -> Path:
    tmp = REFERENCES_DIR / f".tmp-{uuid4().hex}{ext}"
    tmp.write_bytes(data)
    return tmp


def _store_reference(
    db: DbSession, player_id: int, captured_job_id: int | None,
    face: dict, tmp: Path, ext: str, original_filename: str | None,
) -> dict:
    """Persist a validated face: create the row (flush for id), move the temp
    file to references/{player_id}/{id}{ext}, commit, return the summary."""
    ref = ReferenceFace(
        player_id=player_id,
        captured_job_id=captured_job_id,
        image_path="",  # filled in once we know the row id
        original_filename=original_filename,
        embedding=face["embedding"].astype(np.float32).tobytes(),
        det_score=float(face["det_score"]),
        bbox=json.dumps(face["bbox"]),
        face_area_ratio=face.get("face_area_ratio"),
    )
    db.add(ref)
    db.flush()  # assign ref.id

    player_dir = REFERENCES_DIR / str(player_id)
    player_dir.mkdir(parents=True, exist_ok=True)
    final = player_dir / f"{ref.id}{ext}"
    shutil.move(str(tmp), str(final))
    ref.image_path = str(final)
    db.commit()
    return {
        "id": ref.id,
        "player_id": player_id,
        "captured_job_id": captured_job_id,
        "det_score": ref.det_score,
        "face_area_ratio": ref.face_area_ratio,
        "image_path": ref.image_path,
    }


def _wipe_player_references(db: DbSession, player_id: int) -> int:
    """Delete this player's reference rows AND their files (best-effort).
    Caller commits (via _store_reference)."""
    refs = db.query(ReferenceFace).filter_by(player_id=player_id).all()
    for r in refs:
        _unlink_quietly(r.image_path)
    n = db.query(ReferenceFace).filter_by(player_id=player_id).delete(
        synchronize_session=False)
    db.flush()
    return n


def _unlink_quietly(path: str | None) -> None:
    if not path:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except OSError as exc:  # noqa: BLE001 — file cleanup is best-effort
        logger.warning("Could not remove reference file %s: %s", path, exc)


# ── add / list / delete / replace (testable cores) ───────────────────────

def add_reference(
    db: DbSession, player_id: int, data: bytes, *,
    captured_job_id: int | None = None, original_filename: str | None = None,
) -> dict:
    """Validate the player (404) + optional captured job (404), run the
    detector, enforce evaluate_reference_quality (raises ReferenceQualityError
    on a failed gate — no row, no kept file), then persist the row + file.
    Appends — allows multiple references per player."""
    _require_player(db, player_id)
    _require_job_if_given(db, captured_job_id)
    ext = _safe_ext(original_filename)
    tmp = _write_temp(data, ext)
    try:
        face = evaluate_reference_quality(face_detector.detect_faces(tmp))
        return _store_reference(
            db, player_id, captured_job_id, face, tmp, ext, original_filename)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def list_references(db: DbSession, player_id: int) -> dict:
    """404 if player missing. Returns the player's references (no embedding
    bytes) ordered by id."""
    _require_player(db, player_id)
    refs = (
        db.query(ReferenceFace)
        .filter_by(player_id=player_id)
        .order_by(ReferenceFace.id.asc())
        .all()
    )
    return {
        "player_id": player_id,
        "items": [
            {
                "id": r.id,
                "captured_job_id": r.captured_job_id,
                "det_score": r.det_score,
                "face_area_ratio": r.face_area_ratio,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in refs
        ],
    }


def delete_reference(db: DbSession, player_id: int, ref_id: int) -> dict:
    """404 if the reference doesn't exist OR isn't this player's. Remove the
    file (best-effort) then the row."""
    ref = (
        db.query(ReferenceFace)
        .filter_by(id=ref_id, player_id=player_id)
        .one_or_none()
    )
    if ref is None:
        raise HTTPException(404, "Reference not found")
    _unlink_quietly(ref.image_path)
    db.delete(ref)
    db.commit()
    return {"deleted": 1}


def replace_player_references(
    db: DbSession, player_id: int, data: bytes, *,
    captured_job_id: int | None = None, original_filename: str | None = None,
) -> dict:
    """Wipe ALL of this player's references (rows + files), then add one fresh.
    The new photo is validated (detect + gate) BEFORE the wipe, so a failed
    upload leaves the existing references intact. 404 if player missing."""
    _require_player(db, player_id)
    _require_job_if_given(db, captured_job_id)
    ext = _safe_ext(original_filename)
    tmp = _write_temp(data, ext)
    try:
        # Validate the NEW photo first — only destroy existing refs once the
        # replacement is known-good.
        face = evaluate_reference_quality(face_detector.detect_faces(tmp))
        _wipe_player_references(db, player_id)
        return _store_reference(
            db, player_id, captured_job_id, face, tmp, ext, original_filename)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
