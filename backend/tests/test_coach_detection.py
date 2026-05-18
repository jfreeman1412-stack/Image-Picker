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


# ── Phase 4.5: composition rule (rule B) ─────────────────────────────────────


def _comp(cid, single, multi, ages=None):
    return {
        "cluster_id": cid,
        "ages": ages or [],
        "image_count": single + multi,
        "single_face_count": single,
        "multi_face_count": multi,
    }


def test_composition_rule_fires_1_single_3_buddy():
    """1 single + 3 buddy, total=4, median=10 → flagged (4 < 7)."""
    flags = detect_coaches([_comp(1, 1, 3)], session_median_count=10)
    assert flags[1] is True


def test_composition_rule_fires_3_single_5_buddy():
    """3 single + 5 buddy, total=8, median=12 → flagged (8 < 8.4)."""
    flags = detect_coaches([_comp(1, 3, 5)], session_median_count=12)
    assert flags[1] is True


def test_composition_rule_fails_too_many_singles():
    """4 single + 3 buddy → fails single<=3."""
    flags = detect_coaches([_comp(1, 4, 3)], session_median_count=10)
    assert flags[1] is False


def test_composition_rule_fails_not_enough_buddies():
    """2 single + 1 buddy → fails multi>=2."""
    flags = detect_coaches([_comp(1, 2, 1)], session_median_count=10)
    assert flags[1] is False


def test_composition_rule_fails_total_not_below_threshold():
    """2 single + 3 buddy, total=5, median=6 → fails 5 < 4.2."""
    flags = detect_coaches([_comp(1, 2, 3)], session_median_count=6)
    assert flags[1] is False


def test_composition_rule_fires_zero_singles():
    """0 single + 4 buddy, total=4, median=10 → flagged (single<=3 includes 0)."""
    flags = detect_coaches([_comp(1, 0, 4)], session_median_count=10)
    assert flags[1] is True


def test_both_rules_fire_independently():
    """Age 45, total 2, single=1, multi=2, median=10 — either rule alone catches it."""
    flags = detect_coaches(
        [_comp(1, 1, 2, ages=[44.0, 45.0, 46.0])], session_median_count=10,
    )
    assert flags[1] is True


def test_composition_absent_keeps_old_behavior():
    """Old callers that don't pass single/multi counts only get rule A."""
    data = [{"cluster_id": 1, "ages": [], "image_count": 1}]  # no comp keys
    flags = detect_coaches(data, session_median_count=10)
    assert flags[1] is False
