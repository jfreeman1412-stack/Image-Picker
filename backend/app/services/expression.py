"""Smile vs serious expression classification.

Phase 4.3: replaced the Phase 1 OpenCV Haar smile cascade with a real FER
classifier. The Haar approach fired false-positive `pano_pick_smiling`
warnings on open mouths, mouthguards, shadows, and intense serious
expressions — known weak spots for Haar cascades.

Implementation path:
  - We investigated InsightFace's `buffalo_l` model pack for an emotion head.
    The standard pack only ships detection + recognition + genderage +
    landmarks — no emotion module. So we use the `fer` PyPI package, a small
    pretrained CNN that ships its own weights (~1MB).
  - `fer` is imported lazily so the rest of the test suite loads cleanly even
    when its TensorFlow dependency isn't installed. At runtime, if `fer`
    can't be imported we return ('unknown', 0.0) and log a warning rather
    than crashing the pipeline.

Phase 4.4: switched from an absolute `happy >= 0.5` threshold to ARGMAX.
A threshold was tried first and abandoned: FER is trained on adult faces and
gives kids systematically low `happy` scores (especially closed-mouth smiles),
so genuinely smiling children were landing at happy≈0.35 and getting labeled
serious — firing false `team_pick_not_smiling` warnings on obviously-smiling
team photos. FER's softmax outputs are not calibrated probabilities, so an
absolute cutoff is the wrong tool. Argmax (is `happy` the single
strongest emotion?) is robust to the per-population score shift.

Sort-rules contract: returns (label, confidence) where label is one of
'smiling' | 'serious' | 'unknown'. 'unknown' is treated as 'serious' for
pano picking, so the conservative degraded mode is safe.
"""
import logging
from functools import lru_cache
from pathlib import Path
from typing import Tuple

import cv2

from app.services.image_io import read_bgr

logger = logging.getLogger(__name__)

CROP_MARGIN_FRAC = 0.10
MIN_FACE_PX = 32


@lru_cache(maxsize=1)
def _get_fer_detector():
    """Lazy import + cache. Returns None if fer isn't installed."""
    try:
        from fer import FER  # type: ignore
    except ImportError:
        logger.warning(
            "fer package not installed — expression classification disabled. "
            "Install with: pip install fer"
        )
        return None
    # mtcnn=False: we already have the bbox so we don't need the built-in
    # face detector. Cuts ~150ms per call.
    return FER(mtcnn=False)


def classify_expression(image_path: Path, bbox: list) -> Tuple[str, float]:
    """Classify a single face crop as 'smiling' or 'serious'.

    Args:
        image_path: path to the full image.
        bbox: [x, y, w, h] of the face within the image.

    Returns:
        (label, confidence) — label is 'smiling' | 'serious' | 'unknown';
        confidence is in [0, 1]. 'unknown' on read failure, missing
        classifier, or too-small crop.
    """
    img = read_bgr(image_path)
    if img is None:
        logger.warning("Could not load image %s for expression classify", image_path)
        return "unknown", 0.0

    x, y, w, h = bbox
    h_img, w_img = img.shape[:2]
    mx = int(w * CROP_MARGIN_FRAC)
    my = int(h * CROP_MARGIN_FRAC)
    x0 = max(0, x - mx)
    y0 = max(0, y - my)
    x1 = min(w_img, x + w + mx)
    y1 = min(h_img, y + h + my)
    if x1 - x0 < MIN_FACE_PX or y1 - y0 < MIN_FACE_PX:
        return "unknown", 0.0

    face = img[y0:y1, x0:x1]

    detector = _get_fer_detector()
    if detector is None:
        return "unknown", 0.0

    try:
        results = detector.detect_emotions(face)
    except Exception as exc:
        logger.warning("FER classify failed for %s: %s", image_path, exc)
        return "unknown", 0.0

    if not results:
        return "unknown", 0.0

    scores = results[0].get("emotions") or {}
    if not scores:
        return "unknown", 0.0

    # Argmax: whichever of the 7 FER emotions scores highest wins. If that's
    # 'happy', it's a smile — regardless of the absolute value, which FER
    # systematically deflates for kids.
    top_emotion, top_score = max(scores.items(), key=lambda kv: kv[1])
    if top_emotion == "happy":
        return "smiling", float(top_score)
    return "serious", float(top_score)
