"""Unit tests for the shared ETA estimator."""
from app.services.eta import eta_seconds


def test_none_until_signal():
    assert eta_seconds(0, 100, 10) is None     # nothing done yet
    assert eta_seconds(10, 100, 0) is None     # no elapsed time
    assert eta_seconds(10, 0, 10) is None      # no denominator
    assert eta_seconds(0, 0, 0) is None


def test_zero_when_complete():
    assert eta_seconds(100, 100, 50) == 0
    assert eta_seconds(120, 100, 50) == 0      # over-count clamps to done


def test_linear_projection():
    # 25 of 100 in 10s → 2.5/s → 75 remaining → 30s
    assert eta_seconds(25, 100, 10) == 30
    # halfway in 30s → 30s left
    assert eta_seconds(50, 100, 30) == 30


def test_rounds_to_int():
    v = eta_seconds(3, 10, 7)  # rate 3/7, remaining 7 / (3/7) ≈ 16.33
    assert isinstance(v, int)
    assert v == 16
