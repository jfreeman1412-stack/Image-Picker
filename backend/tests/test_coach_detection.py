"""Tests for coach detection — combination of age + photo count."""
from app.services.coach_detection import detect_coaches


def test_coach_flagged():
    """Median age 45, 2 photos, session median 8 → flagged."""
    data = [{"cluster_id": 1, "ages": [44.0, 46.0, 45.0], "image_count": 2}]
    flags = detect_coaches(data, session_median_count=8)
    assert flags[1] is True


def test_player_not_flagged():
    """Median age 12, 8 photos → not flagged (age fails)."""
    data = [{"cluster_id": 2, "ages": [11.5, 12.3, 12.0], "image_count": 8}]
    flags = detect_coaches(data, session_median_count=8)
    assert flags[2] is False


def test_young_assistant_not_flagged_age_fails():
    """Median age 18, 2 photos → not flagged (age below 25)."""
    data = [{"cluster_id": 3, "ages": [17.8, 18.2], "image_count": 2}]
    flags = detect_coaches(data, session_median_count=8)
    assert flags[3] is False


def test_player_short_session_not_flagged_age_fails():
    """Median age 12, 2 photos → not flagged (age fails, count would have passed)."""
    data = [{"cluster_id": 4, "ages": [11.0, 12.5, 12.5], "image_count": 2}]
    flags = detect_coaches(data, session_median_count=8)
    assert flags[4] is False


def test_coach_with_many_shots_not_flagged_count_fails():
    """Median age 45, 6 photos, session median 8 → not flagged.

    Known trade-off documented in coach_detection.py: a coach who got more
    shots than expected slips through. Reviewer catches it manually.
    """
    data = [{"cluster_id": 5, "ages": [44.0, 45.0, 46.0], "image_count": 6}]
    flags = detect_coaches(data, session_median_count=8)
    assert flags[5] is False


def test_multiple_clusters_independent():
    """Each cluster is judged on its own ages and count."""
    data = [
        {"cluster_id": 1, "ages": [45.0, 45.0], "image_count": 2},   # coach
        {"cluster_id": 2, "ages": [12.0, 12.0], "image_count": 8},   # player
        {"cluster_id": 3, "ages": [40.0, 42.0], "image_count": 7},   # coach-age but too many shots
    ]
    flags = detect_coaches(data, session_median_count=8)
    assert flags == {1: True, 2: False, 3: False}


def test_missing_ages_safe():
    """All ages None → not flagged (no signal)."""
    data = [{"cluster_id": 1, "ages": [None, None], "image_count": 2}]
    flags = detect_coaches(data, session_median_count=8)
    assert flags[1] is False


# ── Composition rule (rule B). Phase 4.6: total-count gate removed. ──────────


def _comp(cid, single, multi, ages=None):
    return {
        "cluster_id": cid,
        "ages": ages or [],
        "image_count": single + multi,
        "single_face_count": single,
        "multi_face_count": multi,
    }


def test_nash_hauer_real_case_now_flagged():
    """The Phase 4.6 motivating bug: real coach cluster, 2 single + 5 buddy =
    7 images, session median ~9-10. The old `total < median*0.7` gate
    (7 < 6.3 → false) wrongly missed this coach. Now flagged."""
    flags = detect_coaches([_comp(1, 2, 5)], session_median_count=10)
    assert flags[1] is True


def test_composition_zero_singles_flagged():
    """0 single + 4 buddy → flagged (single<=3 includes 0)."""
    flags = detect_coaches([_comp(1, 0, 4)], session_median_count=10)
    assert flags[1] is True


def test_composition_boundary_3_single_2_buddy_flagged():
    """3 single + 2 buddy → flagged (both at the inclusive boundary)."""
    flags = detect_coaches([_comp(1, 3, 2)], session_median_count=10)
    assert flags[1] is True


def test_composition_too_many_singles_not_flagged():
    """6 single + 2 buddy → NOT flagged (single > 3 = real portrait sequence)."""
    flags = detect_coaches([_comp(1, 6, 2)], session_median_count=10)
    assert flags[1] is False


def test_composition_not_enough_buddies_not_flagged():
    """2 single + 1 buddy → NOT flagged (multi < 2)."""
    flags = detect_coaches([_comp(1, 2, 1)], session_median_count=10)
    assert flags[1] is False


def test_acknowledged_false_positive_is_flagged():
    """2 single + 2 buddy, total=4 — a player who only got a few shots now
    trips rule B. This is the documented Phase 4.6 trade-off: better to
    over-flag and let the user un-coach via the dropdown than to miss real
    coaches. Was NOT flagged under the old total-count gate (4 < median*0.7)."""
    flags = detect_coaches([_comp(1, 2, 2)], session_median_count=10)
    assert flags[1] is True


def test_large_session_no_longer_blocks_composition():
    """Old gate would have failed 3 single + 5 buddy at small medians;
    without the gate it's flagged regardless of session median."""
    assert detect_coaches([_comp(1, 3, 5)], session_median_count=4)[1] is True
    assert detect_coaches([_comp(1, 3, 5)], session_median_count=100)[1] is True


def test_both_rules_fire_independently():
    """Age 45, single=1, multi=2 — either rule alone catches it."""
    flags = detect_coaches(
        [_comp(1, 1, 2, ages=[44.0, 45.0, 46.0])], session_median_count=10,
    )
    assert flags[1] is True


def test_composition_absent_keeps_old_behavior():
    """Old callers that don't pass single/multi counts only get rule A."""
    data = [{"cluster_id": 1, "ages": [], "image_count": 1}]  # no comp keys
    flags = detect_coaches(data, session_median_count=10)
    assert flags[1] is False
