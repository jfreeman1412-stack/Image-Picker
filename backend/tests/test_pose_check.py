"""Tests for the head-pose quality filter used by pano selection.

Phase 4.3: thresholds loosened to ±30° / 0.5 / 0.02. Tests that used to
verify the old strict cutoffs were moved into the "passes" set; new tests
cover the looser boundary.

Phase 4.4: floor dropped to 0.0015 (real full-body sports faces are ~0.007),
plus an end-to-end test through the Face ORM → ImageRecord → pose_metadata
chain — the dict-only tests couldn't catch a key-name mismatch in that path.
"""
from app.models.db_models import Face
from app.services.pose_check import is_acceptable_pose
from app.services.sort_rules import ImageRecord


def _meta(yaw=5.0, pitch=3.0, det_score=0.95, face_area_ratio=0.15):
    return {
        "yaw": yaw,
        "pitch": pitch,
        "det_score": det_score,
        "face_area_ratio": face_area_ratio,
    }


# ── Passes ────────────────────────────────────────────────────────────────────


def test_good_pose():
    assert is_acceptable_pose(_meta()) is True


def test_yaw_20_now_passes():
    """Previously failed at ±15°; passes under Phase 4.3 ±30°."""
    assert is_acceptable_pose(_meta(yaw=20.0)) is True


def test_pitch_20_now_passes():
    assert is_acceptable_pose(_meta(pitch=20.0)) is True


def test_det_score_06_now_passes():
    """Previously failed at floor 0.7; passes under Phase 4.3 floor 0.5."""
    assert is_acceptable_pose(_meta(det_score=0.6)) is True


def test_face_area_003_now_passes():
    """Previously failed at floor 0.05; passes under Phase 4.3 floor 0.02."""
    assert is_acceptable_pose(_meta(face_area_ratio=0.03)) is True


def test_new_boundary_pose_passes():
    """Right at the loosened limits — should still pass (≤ and ≥, not strict)."""
    assert is_acceptable_pose(_meta(
        yaw=30.0, pitch=30.0, det_score=0.5, face_area_ratio=0.02,
    )) is True


# ── Fails ─────────────────────────────────────────────────────────────────────


def test_yaw_35_still_fails():
    assert is_acceptable_pose(_meta(yaw=35.0)) is False


def test_yaw_negative_35_still_fails():
    """Symmetric: ±30° limit, so -35° also fails."""
    assert is_acceptable_pose(_meta(yaw=-35.0)) is False


def test_pitch_35_still_fails():
    assert is_acceptable_pose(_meta(pitch=35.0)) is False


def test_det_score_04_still_fails():
    assert is_acceptable_pose(_meta(det_score=0.4)) is False


def test_typical_fullbody_face_area_now_passes():
    """Phase 4.4: real ~20MP full-body sports portraits have the subject's
    face at ~0.007 of the frame. That must pass now (it was 0/3160 before)."""
    assert is_acceptable_pose(_meta(face_area_ratio=0.007)) is True
    assert is_acceptable_pose(_meta(face_area_ratio=0.0028)) is True  # smallest seen in real data


def test_microscopic_face_area_still_fails():
    """Background/crowd faces (~0.0005 on these shots) stay rejected."""
    assert is_acceptable_pose(_meta(face_area_ratio=0.0005)) is False
    assert is_acceptable_pose(_meta(face_area_ratio=0.001)) is False


# ── Missing data is always a fail ─────────────────────────────────────────────


def test_missing_yaw_fails():
    assert is_acceptable_pose(_meta(yaw=None)) is False


def test_missing_pitch_fails():
    assert is_acceptable_pose(_meta(pitch=None)) is False


def test_missing_det_score_fails():
    assert is_acceptable_pose(_meta(det_score=None)) is False


def test_missing_area_fails():
    assert is_acceptable_pose(_meta(face_area_ratio=None)) is False


# ── End-to-end: Face ORM → ImageRecord → pose_metadata → is_acceptable_pose ──


def test_face_orm_pose_flows_through_to_pose_check():
    """Catches key-name mismatches anywhere in the model→record→dict chain.

    Mirrors what _build_image_records_for_cluster does in face_pipeline.py:
    construct an ImageRecord straight off Face attributes, then feed
    record.pose_metadata() to is_acceptable_pose. Uses realistic values from
    the actual Joey DB (yaw≈-5, pitch≈-3, det≈0.84, area≈0.0073)."""
    face = Face(
        image_id=1, bbox="[0,0,330,443]", det_score=0.839,
        yaw=-4.77, pitch=-3.50, face_area_ratio=0.00732,
    )
    rec = ImageRecord(
        image_id=face.image_id,
        capture_time=0.0,
        face_count=1,
        expression="serious",
        yaw=face.yaw,
        pitch=face.pitch,
        det_score=face.det_score,
        face_area_ratio=face.face_area_ratio,
    )
    meta = rec.pose_metadata()
    assert set(meta) == {"yaw", "pitch", "det_score", "face_area_ratio"}
    assert is_acceptable_pose(meta) is True


def test_face_orm_with_null_pose_fails_closed():
    """Pre-Phase-3 Face rows have NULL yaw/pitch/area → must reject, not crash."""
    face = Face(image_id=1, bbox="[0,0,10,10]", det_score=0.84,
                yaw=None, pitch=None, face_area_ratio=None)
    rec = ImageRecord(
        image_id=1, capture_time=0.0, face_count=1, expression="serious",
        yaw=face.yaw, pitch=face.pitch, det_score=face.det_score,
        face_area_ratio=face.face_area_ratio,
    )
    assert is_acceptable_pose(rec.pose_metadata()) is False
