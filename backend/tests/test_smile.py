"""Unit tests for the landmark smile signal (services/smile.py).

Synthetic 106-pt landmark arrays with controlled mouth geometry — no
InsightFace needed. Exercises the two features (corner lift, inner openness),
the threshold, and the degraded-mode (None) contract.
"""
import numpy as np

from app.services.smile import (
    smile_score_from_landmarks, is_smiling, SMILE_THRESHOLD,
    LEFT_MOUTH_CORNER, RIGHT_MOUTH_CORNER, OUTER_MOUTH, INNER_MOUTH,
)

# Eyes 20px apart → interocular distance (IOD) = 20 for easy normalization.
KPS = np.array([[40., 50.], [60., 50.], [50., 65.], [45., 80.], [55., 80.]])


def _landmarks(corner_y, center_y, inner_gap, x_left=45.0, x_right=55.0):
    """Build a (106,2) array with a controllable mouth.

    corner_y  : y of both outer corners (52, 61). Lower than center_y => lift.
    center_y  : desired MEAN y of the 12 outer-mouth points (52..63).
    inner_gap : vertical span of the 8 inner-mouth points (64..71) => openness.
    """
    lmk = np.zeros((106, 2), dtype=float)
    lmk[LEFT_MOUTH_CORNER] = [x_left, corner_y]
    lmk[RIGHT_MOUTH_CORNER] = [x_right, corner_y]
    others = [i for i in range(OUTER_MOUTH.start, OUTER_MOUTH.stop)
              if i not in (LEFT_MOUTH_CORNER, RIGHT_MOUTH_CORNER)]
    # Solve so the 12-point outer mean equals center_y (corners fixed).
    other_y = (12 * center_y - 2 * corner_y) / len(others)
    for i in others:
        lmk[i] = [50.0, other_y]
    top, bot = center_y - inner_gap / 2, center_y + inner_gap / 2
    for j, i in enumerate(range(INNER_MOUTH.start, INNER_MOUTH.stop)):
        lmk[i] = [50.0, top if j % 2 == 0 else bot]
    return lmk


def test_neutral_closed_mouth_scores_low():
    s = smile_score_from_landmarks(_landmarks(corner_y=80, center_y=80, inner_gap=2), KPS)
    assert s is not None and s < SMILE_THRESHOLD
    assert not is_smiling(s)


def test_lifted_corners_score_high():
    # Corners well above the mouth center → strong lift → smiling.
    s = smile_score_from_landmarks(_landmarks(corner_y=74, center_y=82, inner_gap=2), KPS)
    assert s is not None and s >= SMILE_THRESHOLD
    assert is_smiling(s)


def test_openness_raises_score():
    closed = smile_score_from_landmarks(_landmarks(80, 81, inner_gap=2), KPS)
    opened = smile_score_from_landmarks(_landmarks(80, 81, inner_gap=12), KPS)
    assert opened > closed


def test_score_is_clamped_to_unit_interval():
    s = smile_score_from_landmarks(_landmarks(corner_y=60, center_y=90, inner_gap=20), KPS)
    assert 0.0 <= s <= 1.0


def test_none_inputs_return_none():
    assert smile_score_from_landmarks(None, KPS) is None
    assert smile_score_from_landmarks(_landmarks(80, 80, 2), None) is None


def test_wrong_landmark_shape_returns_none():
    assert smile_score_from_landmarks(np.zeros((68, 2)), KPS) is None


def test_degenerate_iod_returns_none():
    coincident_eyes = np.array([[50., 50.], [50., 50.], [50., 65.], [45., 80.], [55., 80.]])
    assert smile_score_from_landmarks(_landmarks(80, 80, 2), coincident_eyes) is None


def test_is_smiling_none_is_false():
    assert is_smiling(None) is False
    assert is_smiling(SMILE_THRESHOLD) is True
    assert is_smiling(SMILE_THRESHOLD - 0.01) is False


def test_corner_indices_pinned():
    # Guard the empirically-verified anatomical corner indices (probe_smile2.py).
    assert LEFT_MOUTH_CORNER == 52
    assert RIGHT_MOUTH_CORNER == 61
    assert (OUTER_MOUTH.start, OUTER_MOUTH.stop) == (52, 64)
    assert (INNER_MOUTH.start, INNER_MOUTH.stop) == (64, 72)
