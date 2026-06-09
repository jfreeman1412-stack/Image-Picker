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


def _iou(box_a: list[int], box_b: list[int]) -> float:
    """Intersection-over-Union for two [x, y, w, h] boxes. Phase B.6 helper
    for selected_bbox face picking. Returns 0.0 for disjoint boxes."""
    ax, ay, aw, ah = box_a
    bx, by, bw, bh = box_b
    ix1 = max(ax, bx)
    iy1 = max(ay, by)
    ix2 = min(ax + aw, bx + bw)
    iy2 = min(ay + ah, by + bh)
    iw = max(0, ix2 - ix1)
    ih = max(0, iy2 - iy1)
    inter = iw * ih
    if inter == 0:
        return 0.0
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def evaluate_reference_quality(
    detections: list[dict],
    *,
    selected_bbox: list[int] | None = None,
    allow_low_confidence: bool = False,
) -> dict:
    """Pick the single reference face from a detection list, or raise
    ReferenceQualityError with a clear code+message. Pure — no I/O, no model —
    so it's unit-tested directly with hand-built dicts.

    Default order (no kwargs — A.2 behavior, byte-for-byte unchanged):
      no_face        — zero faces (also covers an unreadable image, which
                       detect_faces returns as [])
      multiple_faces — 2+ faces at the DET_SCORE_THRESHOLD (0.5) floor (ANY
                       second real face → alert; the user wants bystanders
                       over-caught rather than risk a poisoned reference)
      low_confidence — the single face is below REF_MIN_DET_SCORE
      face_too_small — the single face's area ratio is below REF_MIN_AREA_RATIO

    Phase B.6 salvage kwargs:
      selected_bbox=[x, y, w, h] — operator tapped this face in a
        multiple_faces situation. Skips the multiplicity check and picks
        the face with highest IoU against the box. Raises
        selected_face_not_found if no face overlaps (defensive — identical
        bytes detect identically, so this is rare).
      allow_low_confidence=True — operator's deliberate "use anyway" on a
        low_confidence single face. Bypasses the low_confidence raise.
        face_too_small is ALWAYS enforced (would store an unusable
        embedding) regardless of this flag.

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

    if selected_bbox is not None:
        # Pick the face with highest IoU; an all-zero IoU run means the
        # operator tapped somewhere no face was detected.
        scored = sorted(
            ((_iou(selected_bbox, f["bbox"]), f) for f in faces),
            key=lambda t: t[0], reverse=True,
        )
        best_iou, best_face = scored[0]
        if best_iou <= 0.0:
            raise ReferenceQualityError(
                "selected_face_not_found",
                "The face you selected doesn't match any detected face in "
                "the photo. Tap a face box and try again.")
        face = best_face
    else:
        if len(faces) > 1:
            raise ReferenceQualityError(
                "multiple_faces",
                f"{len(faces)} faces detected. A reference photo must show "
                "exactly one person — make sure no one else is in frame.")
        face = faces[0]

    if face["det_score"] < REF_MIN_DET_SCORE and not allow_low_confidence:
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
    *, accepted_via: str | None = None,
) -> dict:
    """Persist a validated face: create the row (flush for id), move the temp
    file to references/{player_id}/{id}{ext}, commit, return the summary.

    Phase B.6: accepted_via stamps how the row was accepted (NULL keeps
    today's A.2/B.2 behavior — pre-B.6 rows and the unchanged B.2 PUT)."""
    ref = ReferenceFace(
        player_id=player_id,
        captured_job_id=captured_job_id,
        image_path="",  # filled in once we know the row id
        original_filename=original_filename,
        embedding=face["embedding"].astype(np.float32).tobytes(),
        det_score=float(face["det_score"]),
        bbox=json.dumps(face["bbox"]),
        face_area_ratio=face.get("face_area_ratio"),
        accepted_via=accepted_via,
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
        "accepted_via": ref.accepted_via,
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


def _wipe_player_references_for_job(
    db: DbSession, player_id: int, job_id: int,
) -> int:
    """Phase B.2 — shoot-scoped sibling of _wipe_player_references: delete only
    this player's reference rows + files whose `captured_job_id == job_id`,
    leaving every other shoot's references intact. Caller commits."""
    refs = (
        db.query(ReferenceFace)
        .filter_by(player_id=player_id, captured_job_id=job_id)
        .all()
    )
    for r in refs:
        _unlink_quietly(r.image_path)
    n = (
        db.query(ReferenceFace)
        .filter_by(player_id=player_id, captured_job_id=job_id)
        .delete(synchronize_session=False)
    )
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


# ── shoot-scoped replace / delete (Phase B.2) ─────────────────────────────

def replace_shoot_reference(
    db: DbSession, player_id: int, job_id: int, data: bytes, *,
    original_filename: str | None = None,
) -> dict:
    """Phase B.2 — set this player's single reference FOR ONE shoot (capture /
    retake). Validate the new photo (detect + gate), then wipe ONLY this
    player's refs whose `captured_job_id == job_id`, then store the new one
    tagged with `job_id`. Every other shoot's references are untouched — unlike
    `replace_player_references`, which wipes the player globally. The new photo
    is validated BEFORE the wipe, so a failed gate leaves this shoot's existing
    reference intact. 404 if the player or the job is missing."""
    _require_player(db, player_id)
    _require_job_if_given(db, job_id)  # job_id is required here → 404 if missing
    ext = _safe_ext(original_filename)
    tmp = _write_temp(data, ext)
    try:
        face = evaluate_reference_quality(face_detector.detect_faces(tmp))
        _wipe_player_references_for_job(db, player_id, job_id)
        return _store_reference(
            db, player_id, job_id, face, tmp, ext, original_filename)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def delete_shoot_references(db: DbSession, player_id: int, job_id: int) -> dict:
    """Phase B.2 — remove this player's reference(s) FOR ONE shoot only
    (`captured_job_id == job_id`); other shoots are untouched. Idempotent:
    returns {"deleted": 0} when there's nothing for this shoot. 404 if the
    player is missing."""
    _require_player(db, player_id)
    n = _wipe_player_references_for_job(db, player_id, job_id)
    db.commit()
    return {"deleted": n}


# ── Phase B.6 — salvage paths ────────────────────────────────────────────


def _read_image_dims(path: Path) -> tuple[int, int]:
    """Return (width, height) of an image without loading the full data.
    Pillow's open is lazy, so this is cheap. Wrapped in its own function
    so tests can monkeypatch it without needing real image bytes."""
    from PIL import Image as PILImage
    with PILImage.open(path) as img:
        return img.size  # (width, height)


def detect_reference_faces(
    db: DbSession, player_id: int, job_id: int, data: bytes, *,
    original_filename: str | None = None,
) -> dict:
    """Phase B.6 — read-only face detection over the uploaded bytes for
    the salvage UI. Returns image dimensions + every detected face's
    bbox/det_score/face_area_ratio plus a stable index. Writes nothing.

    Defensively re-filters by DET_SCORE_THRESHOLD so the UI's tap-targets
    can never include a sub-floor face the gate would later reject as
    not-found. 404 if the player or the job is missing."""
    _require_player(db, player_id)
    _require_job_if_given(db, job_id)
    ext = _safe_ext(original_filename)
    tmp = _write_temp(data, ext)
    try:
        width, height = _read_image_dims(tmp)
        raw = face_detector.detect_faces(tmp)
        faces = []
        for i, f in enumerate([d for d in raw if d["det_score"] >= DET_SCORE_THRESHOLD]):
            faces.append({
                "index": i,
                "bbox": list(f["bbox"]),
                "det_score": float(f["det_score"]),
                "face_area_ratio": f.get("face_area_ratio"),
            })
        return {"width": width, "height": height, "faces": faces}
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def resolve_shoot_reference(
    db: DbSession, player_id: int, job_id: int, data: bytes, *,
    selected_bbox: list[int] | None = None,
    allow_low_confidence: bool = False,
    original_filename: str | None = None,
) -> dict:
    """Phase B.6 — operator-driven salvage of a previously rejected
    capture. Same scoped-replace semantics as `replace_shoot_reference`
    (validate → wipe this shoot's refs for this player → store the new
    one tagged with `captured_job_id == job_id`), but the gate accepts
    salvage kwargs:

      selected_bbox=[x,y,w,h] → multiple_faces salvage (pick the operator-
        tapped face). On no overlap, raises selected_face_not_found.
      allow_low_confidence=True → low-confidence override. face_too_small
        is ALWAYS enforced regardless.

    accepted_via on the stored row records which path was taken:
      both unset → 'normal'   (resolve called without salvage kwargs)
      selected_bbox set → 'face_select'
      allow_low_confidence only → 'low_conf_override'
      both → 'face_select' (the face-pick is the dominant operator action;
        the low-conf override is the consequential one)

    Default-arg call is byte-for-byte equivalent to replace_shoot_reference
    except for `accepted_via='normal'` on the stored row. 404 if player or
    job missing; 400 (via ReferenceQualityError) on a gate rejection."""
    _require_player(db, player_id)
    _require_job_if_given(db, job_id)
    ext = _safe_ext(original_filename)
    tmp = _write_temp(data, ext)
    try:
        face = evaluate_reference_quality(
            face_detector.detect_faces(tmp),
            selected_bbox=selected_bbox,
            allow_low_confidence=allow_low_confidence,
        )
        accepted_via = (
            "face_select" if selected_bbox is not None
            else "low_conf_override" if allow_low_confidence
            else "normal"
        )
        _wipe_player_references_for_job(db, player_id, job_id)
        return _store_reference(
            db, player_id, job_id, face, tmp, ext, original_filename,
            accepted_via=accepted_via,
        )
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
