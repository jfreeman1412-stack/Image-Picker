"""Head-pose quality filter for portrait pano selection.

Pure function — given a face's pose + detection metadata, return True if the
face looks like a clean portrait pose. Used by the pano walk-back in
sort_rules.assign_roles to skip silly shots between team and pano.

The thresholds below are tuned for the Sportsline volume-sports use case
(youth/HS straight-on portraits). Adjust if shooting style changes.

Phase 4.3: loosened from ±15°/0.7/0.05 to ±30°/0.5/0.02 after real-world
feedback that legitimate serious portraits were being rejected.

Phase 4.4: MIN_FACE_AREA_RATIO dropped 0.02 → 0.0015. Diagnosis on a real
Joey job showed EVERY clustered face had face_area_ratio in 0.003–0.012
(avg 0.007) and 0/3160 passed the 0.02 floor — so every cluster fell back to
`no_clean_pano_pose`. Root cause: these are ~20MP full-body sports portraits
(3648×5472) where the subject's own face is legitimately <1% of the frame.
The computation is correct; the threshold was calibrated for tight headshots,
not full-body shots. det_score>=0.5 already filters non-faces; this floor now
only guards against truly microscopic background/crowd detections
(a background person on these shots is ~0.0005, well under 0.0015).

Future tuning note (not implemented): if these thresholds still don't catch
enough legitimate poses, a smarter approach is to *score* each candidate
(yaw + pitch deviation, area, det_score combined) and pick the best-scoring
candidate near the end of the cluster rather than the first one to pass a
binary filter. A small "earlier in sequence" penalty would bias toward the
second-to-last shot on ties. See WISHLIST 3.1.
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)


YAW_LIMIT_DEG = 30.0
PITCH_LIMIT_DEG = 30.0
MIN_DET_SCORE = 0.5
MIN_FACE_AREA_RATIO = 0.0015


def is_acceptable_pose(face_metadata: dict) -> bool:
    """Return True if this face looks like a clean portrait pose.

    Signals (ALL must pass):
      - yaw within ±30° (camera-facing, not turned)
      - pitch within ±30° (head not tilted up/down)
      - det_score >= 0.5 (decent-confidence detection)
      - face_area_ratio >= 0.0015 (filters microscopic background faces only)

    Missing data (None) on any required signal → False (conservative).
    Logs the actual values + which check failed at DEBUG so a recurrence of
    the Phase 4.4 "everything fails one signal" bug is immediately visible.
    """
    yaw: Optional[float] = face_metadata.get("yaw")
    pitch: Optional[float] = face_metadata.get("pitch")
    det_score: Optional[float] = face_metadata.get("det_score")
    area: Optional[float] = face_metadata.get("face_area_ratio")

    if yaw is None or pitch is None or det_score is None or area is None:
        logger.debug(
            "pose reject (missing data): yaw=%s pitch=%s det=%s area=%s",
            yaw, pitch, det_score, area,
        )
        return False

    checks = {
        "yaw": abs(yaw) <= YAW_LIMIT_DEG,
        "pitch": abs(pitch) <= PITCH_LIMIT_DEG,
        "det_score": det_score >= MIN_DET_SCORE,
        "face_area_ratio": area >= MIN_FACE_AREA_RATIO,
    }
    if not all(checks.values()):
        failed = [k for k, ok in checks.items() if not ok]
        logger.debug(
            "pose reject (%s): yaw=%.1f pitch=%.1f det=%.3f area=%.5f",
            ",".join(failed), yaw, pitch, det_score, area,
        )
        return False
    return True
