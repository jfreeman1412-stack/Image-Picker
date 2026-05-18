"""Smoke tests for the FER-backed expression classifier.

These don't validate accuracy — that's the model's job. They verify the
wiring: crop extraction, dispatch to the detector, threshold logic, and the
'unknown' degradation path for I/O errors and unreadable files.

We monkeypatch the lazy `_get_fer_detector` so the suite runs without `fer`
actually installed.
"""
import cv2
import numpy as np
import pytest

from app.services import expression
from app.services.expression import classify_expression


class _FakeFER:
    def __init__(self, emotions):
        self.emotions = emotions

    def detect_emotions(self, crop):
        return [{"box": [0, 0, crop.shape[1], crop.shape[0]],
                 "emotions": self.emotions}]


class _NoFaceFER:
    def detect_emotions(self, crop):
        return []


class _RaisingFER:
    def detect_emotions(self, crop):
        raise RuntimeError("boom")


def _write_face_image(path, size=200):
    """Solid mid-gray image — content doesn't matter, FER is mocked."""
    img = np.full((size, size, 3), 128, dtype=np.uint8)
    cv2.imwrite(str(path), img)


@pytest.fixture(autouse=True)
def clear_detector_cache():
    """Reset the lru_cache around _get_fer_detector between tests."""
    expression._get_fer_detector.cache_clear()
    yield
    expression._get_fer_detector.cache_clear()


def test_smiling_crop_returns_smiling(monkeypatch, tmp_path):
    path = tmp_path / "smile.jpg"
    _write_face_image(path)

    monkeypatch.setattr(
        expression, "_get_fer_detector",
        lambda: _FakeFER({"happy": 0.85, "neutral": 0.1, "sad": 0.05}),
    )
    label, conf = classify_expression(path, [0, 0, 200, 200])
    assert label == "smiling"
    assert conf >= 0.5


def test_serious_crop_returns_serious(monkeypatch, tmp_path):
    path = tmp_path / "serious.jpg"
    _write_face_image(path)

    monkeypatch.setattr(
        expression, "_get_fer_detector",
        lambda: _FakeFER({"happy": 0.1, "neutral": 0.7, "sad": 0.2}),
    )
    label, _ = classify_expression(path, [0, 0, 200, 200])
    assert label == "serious"


def test_low_happy_but_argmax_is_smiling(monkeypatch, tmp_path):
    """Phase 4.4: kid smiling, FER deflates happy to 0.4 but it's still the
    single strongest emotion → smiling. The old happy>=0.5 threshold would
    have wrongly called this serious and fired team_pick_not_smiling."""
    path = tmp_path / "kidsmile.jpg"
    _write_face_image(path)

    monkeypatch.setattr(
        expression, "_get_fer_detector",
        lambda: _FakeFER({"happy": 0.4, "neutral": 0.3, "sad": 0.1,
                          "surprise": 0.1, "angry": 0.1}),
    )
    label, conf = classify_expression(path, [0, 0, 200, 200])
    assert label == "smiling"
    assert conf == 0.4


def test_neutral_dominant_is_serious(monkeypatch, tmp_path):
    """happy is low AND not the top emotion → serious."""
    path = tmp_path / "serious2.jpg"
    _write_face_image(path)

    monkeypatch.setattr(
        expression, "_get_fer_detector",
        lambda: _FakeFER({"happy": 0.1, "neutral": 0.6, "sad": 0.2, "angry": 0.1}),
    )
    label, conf = classify_expression(path, [0, 0, 200, 200])
    assert label == "serious"
    assert conf == 0.6


def test_unreadable_file_returns_unknown(tmp_path):
    """No image at the path → 'unknown', no crash."""
    label, conf = classify_expression(tmp_path / "missing.jpg", [0, 0, 100, 100])
    assert label == "unknown"
    assert conf == 0.0


def test_corrupt_file_returns_unknown(tmp_path):
    path = tmp_path / "broken.jpg"
    path.write_bytes(b"not a jpeg")
    label, _ = classify_expression(path, [0, 0, 100, 100])
    assert label == "unknown"


def test_tiny_crop_returns_unknown(monkeypatch, tmp_path):
    """Bbox below the min face size short-circuits to 'unknown' before the
    classifier is even consulted."""
    path = tmp_path / "small.jpg"
    _write_face_image(path)

    called = {"n": 0}
    def _detector_should_not_be_used():
        called["n"] += 1
        return _FakeFER({"happy": 0.99})
    monkeypatch.setattr(
        expression, "_get_fer_detector", _detector_should_not_be_used
    )

    label, _ = classify_expression(path, [0, 0, 10, 10])
    assert label == "unknown"
    assert called["n"] == 0


def test_no_face_detected_in_crop_returns_unknown(monkeypatch, tmp_path):
    path = tmp_path / "noface.jpg"
    _write_face_image(path)
    monkeypatch.setattr(expression, "_get_fer_detector", lambda: _NoFaceFER())
    label, _ = classify_expression(path, [0, 0, 200, 200])
    assert label == "unknown"


def test_detector_raises_returns_unknown(monkeypatch, tmp_path):
    """FER blowing up shouldn't bring down the whole pipeline."""
    path = tmp_path / "boom.jpg"
    _write_face_image(path)
    monkeypatch.setattr(expression, "_get_fer_detector", lambda: _RaisingFER())
    label, _ = classify_expression(path, [0, 0, 200, 200])
    assert label == "unknown"


def test_no_classifier_installed_returns_unknown(monkeypatch, tmp_path):
    """When fer isn't installed, _get_fer_detector returns None → 'unknown'."""
    path = tmp_path / "noimport.jpg"
    _write_face_image(path)
    monkeypatch.setattr(expression, "_get_fer_detector", lambda: None)
    label, conf = classify_expression(path, [0, 0, 200, 200])
    assert label == "unknown"
    assert conf == 0.0
