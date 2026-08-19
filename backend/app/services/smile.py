"""Landmark-based smile signal for pano selection (2026-08).

Why this exists: pano selection should PREFER a non-smiling (neutral) frame.
The pipeline already has an FER expression classifier (expression.py), but FER
systematically under-scores children's smiles (see expression.py Phase 4.4),
so pano selection uses THIS geometric signal instead — computed directly from
the InsightFace 2d106det landmarks that buffalo_l already produces (no new
model, no new dependency).

── Landmark indices (InsightFace buffalo_l `2d106det`, verified empirically) ──
The 106-point layout is FIXED per the model. The mouth is the contiguous block
52-71, verified on real detections (scratchpad/probe_landmarks.py):
  * OUTER lip ring: indices 52-63 (12 points); outer CORNERS are 52 (image
    left) and 61 (image right). Confirmed symmetric about the nose on a
    frontal face (corner-midpoint x within 0.02*IOD of the nose keypoint).
  * INNER lip ring: indices 64-71 (8 points); used for the open-mouth gap.
Do NOT identify corners by nearest-neighbour to the 5-pt keypoints — the 5-pt
mouth corner sits ambiguously between the inner (65) and outer (52) corner and
the nearest index flips between faces. The anatomical indices above are stable.

── The signal ──
Two geometric features, both normalized by interocular distance (IOD) so they
are scale- and distance-invariant:
  * corner LIFT  = (mean mouth y) - (mean corner y), /IOD. A smile pulls the
    corners UP; in image coords (y grows downward) that means the corners sit
    ABOVE the mouth's vertical center, so lift > 0. Neutral ≈ 0.
  * inner OPEN   = (inner-lip vertical span)/IOD. Teeth/open smile widens it.
score = clip(W_LIFT*lift + W_OPEN*max(0, open - OPEN_BASELINE), 0, 1)
A face is "smiling" iff score >= SMILE_THRESHOLD.

Pose note: lift/openness are 2D image-space measurements, so they are only
trustworthy on near-frontal faces. That is fine here — pano candidates are
already gated by is_acceptable_pose() (yaw/pitch within ±30°) before the smile
preference is consulted, so badly-posed faces never reach this signal.

── CALIBRATION ──
The weights + threshold below are PROVISIONAL starting points, grounded in the
t1.jpg probe ranges (lift 0.05-0.21, open 0.12-0.37) but NOT yet tuned on real
shop images. Face.smile_score persists the continuous value per face, and the
"smiling" decision applies SMILE_THRESHOLD at sort time — so the threshold can
be retuned WITHOUT re-running detection. Use scratchpad/calibrate_smile.py to
compare scores against FER labels on a real session before trusting these.

Pure module: numpy only, no InsightFace import — unit-testable with synthetic
landmark arrays. See test_smile.py.
"""
from __future__ import annotations

import logging
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# ── Landmark indices (fixed 2d106det layout; see module docstring) ──────────
LEFT_MOUTH_CORNER = 52
RIGHT_MOUTH_CORNER = 61
OUTER_MOUTH = slice(52, 64)   # 52..63 inclusive
INNER_MOUTH = slice(64, 72)   # 64..71 inclusive
LEFT_EYE_KP = 0               # 5-pt keypoint order: [Leye, Reye, nose, Lmouth, Rmouth]
RIGHT_EYE_KP = 1
_EXPECTED_LANDMARKS = 106

# ── Tunable weights + threshold ─────────────────────────────────────────────
# Weights are provisional (grounded in the t1.jpg probe ranges). The THRESHOLD
# was calibrated 2026-08 by eye on real crops from session 801 (8th-grade team,
# 41 pose-acceptable faces via calibrate_smile.py): neutral/very-slight faces
# scored 0.00-0.19, clear open-mouth smiles 0.33-0.60, so the neutral→smile
# line sits ≈0.30 — NOT 0.50, where obvious smiles (0.33-0.36) leaked through as
# "neutral". Re-run calibrate_smile.py on more teams (younger kids, other
# lighting) to refine; adjust here without re-running detection (score persists).
W_LIFT = 4.0            # weight on corner-lift (the primary, robust smile cue)
W_OPEN = 1.0            # weight on inner-lip opening (secondary; noisier)
OPEN_BASELINE = 0.22    # inner-open below this (closed mouth) contributes nothing
SMILE_THRESHOLD = 0.30  # score >= this ⇒ "smiling" (calibrated on session 801)

_MIN_IOD_PX = 1.0       # guard against degenerate (near-zero) interocular distance


def smile_score_from_landmarks(
    landmark_2d_106: Optional[np.ndarray],
    kps: Optional[np.ndarray],
) -> Optional[float]:
    """Continuous smile score in [0, 1] from 2d106 landmarks + 5-pt keypoints.

    Returns None when the inputs are missing/degenerate (wrong shape, no
    landmarks, near-zero interocular distance). None is the "unknown" signal:
    callers treat unknown as NON-smiling so an un-scorable frame stays a valid
    pano candidate (matches the pipeline's conservative degraded mode).
    """
    if landmark_2d_106 is None or kps is None:
        return None
    lmk = np.asarray(landmark_2d_106, dtype=np.float64)
    k = np.asarray(kps, dtype=np.float64)
    if lmk.shape != (_EXPECTED_LANDMARKS, 2) or k.shape[0] < 2:
        logger.debug("smile: unexpected landmark/kps shape %s / %s", lmk.shape, k.shape)
        return None

    iod = float(np.linalg.norm(k[LEFT_EYE_KP] - k[RIGHT_EYE_KP]))
    if iod < _MIN_IOD_PX:
        return None

    left_corner = lmk[LEFT_MOUTH_CORNER]
    right_corner = lmk[RIGHT_MOUTH_CORNER]
    outer = lmk[OUTER_MOUTH]
    inner = lmk[INNER_MOUTH]

    corner_mean_y = (left_corner[1] + right_corner[1]) / 2.0
    outer_center_y = float(outer[:, 1].mean())
    lift = (outer_center_y - corner_mean_y) / iod          # >0 ⇒ smile
    inner_open = float(inner[:, 1].max() - inner[:, 1].min()) / iod

    raw = W_LIFT * lift + W_OPEN * max(0.0, inner_open - OPEN_BASELINE)
    score = float(np.clip(raw, 0.0, 1.0))
    logger.debug(
        "smile: lift=%.3f open=%.3f score=%.3f (iod=%.1f)", lift, inner_open, score, iod
    )
    return score


def is_smiling(smile_score: Optional[float]) -> bool:
    """Threshold the continuous score. Unknown (None) ⇒ NOT smiling, so an
    un-scorable frame remains eligible as the preferred (non-smiling) pano."""
    return smile_score is not None and smile_score >= SMILE_THRESHOLD
