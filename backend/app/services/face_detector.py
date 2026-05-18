"""Face detection + embedding wrapper around InsightFace.

The model is loaded once and reused. InsightFace's `buffalo_l` pack gives us
detection, alignment, and a 512-d ArcFace embedding in one shot — which is what
we want for downstream clustering.
"""
import logging
from functools import lru_cache
from pathlib import Path
from typing import List

import numpy as np

logger = logging.getLogger(__name__)

DET_SCORE_THRESHOLD = 0.5


@lru_cache(maxsize=1)
def get_detector():
    """Load the InsightFace model once per process.

    First load downloads ~300MB to ~/.insightface/ which is expected.
    """
    from insightface.app import FaceAnalysis

    app = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"])
    app.prepare(ctx_id=0, det_size=(640, 640))
    return app


def detect_faces(image_path: Path) -> List[dict]:
    """Detect every face in `image_path`.

    Returns a list of dicts:
        {
            "bbox": [x, y, w, h],   # ints
            "embedding": np.ndarray (float32, shape (512,)),  # L2-normalized
            "det_score": float,
            "age": float | None,            # InsightFace genderage estimate
            "yaw": float | None,            # head pose yaw, degrees
            "pitch": float | None,          # head pose pitch, degrees
            "face_area_ratio": float | None, # bbox area / image area
        }
    Returns [] if the image cannot be loaded.
    """
    from app.services.image_io import read_bgr

    img = read_bgr(image_path)
    if img is None:
        logger.warning("Could not load image %s", image_path)
        return []

    img_h, img_w = img.shape[:2]
    img_area = float(img_h * img_w) if img_h and img_w else 0.0

    app = get_detector()
    faces = app.get(img)
    out: List[dict] = []
    for f in faces:
        if float(f.det_score) < DET_SCORE_THRESHOLD:
            continue
        x1, y1, x2, y2 = f.bbox.astype(int)
        bbox_w, bbox_h = int(x2 - x1), int(y2 - y1)
        age = getattr(f, "age", None)

        # InsightFace buffalo_l exposes .pose as [pitch, yaw, roll] in degrees.
        # Older versions / edge cases: missing → None and downstream pose check
        # conservatively treats it as a fail.
        pose = getattr(f, "pose", None)
        pitch_val = yaw_val = None
        if pose is not None:
            try:
                pitch_val = float(pose[0])
                yaw_val = float(pose[1])
            except (TypeError, ValueError, IndexError):
                pitch_val = yaw_val = None

        area_ratio = (bbox_w * bbox_h) / img_area if img_area else None

        out.append({
            "bbox": [int(x1), int(y1), bbox_w, bbox_h],
            "embedding": np.asarray(f.normed_embedding, dtype=np.float32),
            "det_score": float(f.det_score),
            "age": float(age) if age is not None else None,
            "yaw": yaw_val,
            "pitch": pitch_val,
            "face_area_ratio": float(area_ratio) if area_ratio is not None else None,
        })
    return out
