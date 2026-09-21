"""Tests for coach detection — combination of age + photo count.

2026-09-21 (F-A): Rule A now requires multi_face_count >= 1 in addition
to age+count. Tests that used to exercise Rule A without comp keys now
supply them so the intent is explicit; the "no comp keys" back-compat
branch of Rule A can no longer fire (that's the point of F-A). New
tests below assert the tighter shape.
"""
from app.services.coach_detection import detect_coaches


def test_coach_flagged():
    """Median age 45, 2 photos with a buddy shot, session median 8 →
    flagged by Rule A (F-A: buddy presence required)."""
    data = [{
        "cluster_id": 1, "ages": [44.0, 46.0, 45.0], "image_count": 2,
        "single_face_count": 1, "multi_face_count": 1,
    }]
    flags = detect_coaches(data, session_median_count=8)
    assert flags[1] is True


def test_player_not_flagged():
    """Median age 12, 8 photos → not flagged (age fails)."""
    data = [{
        "cluster_id": 2, "ages": [11.5, 12.3, 12.0], "image_count": 8,
        "single_face_count": 8, "multi_face_count": 0,
    }]
    flags = detect_coaches(data, session_median_count=8)
    assert flags[2] is False


def test_young_assistant_not_flagged_age_fails():
    """Median age 18, 2 photos → not flagged (age below 25)."""
    data = [{
        "cluster_id": 3, "ages": [17.8, 18.2], "image_count": 2,
        "single_face_count": 1, "multi_face_count": 1,
    }]
    flags = detect_coaches(data, session_median_count=8)
    assert flags[3] is False


def test_player_short_session_not_flagged_age_fails():
    """Median age 12, 2 photos → not flagged (age fails, count would have passed)."""
    data = [{
        "cluster_id": 4, "ages": [11.0, 12.5, 12.5], "image_count": 2,
        "single_face_count": 2, "multi_face_count": 0,
    }]
    flags = detect_coaches(data, session_median_count=8)
    assert flags[4] is False


def test_coach_with_many_shots_not_flagged_count_fails():
    """Median age 45, 6 photos, session median 8 → not flagged.

    Known trade-off documented in coach_detection.py: a coach who got more
    shots than expected slips through. Reviewer catches it manually.
    """
    data = [{
        "cluster_id": 5, "ages": [44.0, 45.0, 46.0], "image_count": 6,
        "single_face_count": 4, "multi_face_count": 2,
    }]
    flags = detect_coaches(data, session_median_count=8)
    assert flags[5] is False


def test_multiple_clusters_independent():
    """Each cluster is judged on its own ages and count."""
    data = [
        # coach: age 45, 2 imgs, has buddy → Rule A fires
        {"cluster_id": 1, "ages": [45.0, 45.0], "image_count": 2,
         "single_face_count": 1, "multi_face_count": 1},
        # player: age 12, 8 imgs → no rule fires
        {"cluster_id": 2, "ages": [12.0, 12.0], "image_count": 8,
         "single_face_count": 8, "multi_face_count": 0},
        # coach-age but too many shots → Rule A count guard fails
        {"cluster_id": 3, "ages": [40.0, 42.0], "image_count": 7,
         "single_face_count": 5, "multi_face_count": 2},
    ]
    flags = detect_coaches(data, session_median_count=8)
    assert flags == {1: True, 2: False, 3: False}


def test_missing_ages_safe():
    """All ages None → not flagged (no signal)."""
    data = [{
        "cluster_id": 1, "ages": [None, None], "image_count": 2,
        "single_face_count": 1, "multi_face_count": 1,
    }]
    flags = detect_coaches(data, session_median_count=8)
    assert flags[1] is False


# ── Rule A — F-A buddy-presence guard (2026-09-21) ────────────────────────────
#
# Session 1123 mislabel: cluster 14706, a real player, had 3 solo photos,
# 0 buddy, and per-face ages [24, 26, 26] (median 26.0). Rule A pre-fix
# fired because age >= 25 AND count <= max(3, 5*0.5) = 3. F-A adds a
# `multi_face_count >= 1` guard: age+count alone isn't enough when the
# cluster has zero buddy shots. Real coaches worth catching via age+count
# also show up in group photos. Kids with a few solo shots and no buddy
# shots stay clean.


def test_rule_a_no_buddy_shots_not_flagged():
    """The load-bearing F-A test — session 1123 cluster 14706 reproduction.
    Age median 26, 3 solo photos, 0 buddy → NOT flagged.

    Pre-F-A this fired Rule A (age 26 >= 25 AND count 3 <= 3 → coach).
    Post-F-A the buddy-presence guard blocks it. Rule B is not applicable
    (multi=0 < 2), Rule C is not applicable (count=3 != 1).
    """
    data = [{
        "cluster_id": 14706, "ages": [24.0, 26.0, 26.0], "image_count": 3,
        "single_face_count": 3, "multi_face_count": 0,
    }]
    flags = detect_coaches(data, session_median_count=5)
    assert flags[14706] is False, (
        "F-A regressed — Rule A fired for a kid with age median in the "
        "InsightFace-noise band and zero buddy shots. This is exactly the "
        "1123 bug that F-A was built to fix."
    )


def test_rule_a_with_buddy_shot_still_flagged():
    """A real coach with 3 solo + 1 buddy at age 45 still trips Rule A.
    The F-A guard just filters out zero-buddy borderline-age cases; it
    doesn't disable Rule A."""
    data = [{
        "cluster_id": 1, "ages": [43.0, 45.0, 47.0], "image_count": 4,
        "single_face_count": 3, "multi_face_count": 1,
    }]
    flags = detect_coaches(data, session_median_count=8)
    assert flags[1] is True


def test_rule_c_singleton_kid_still_flagged_needs_manual_override():
    """Session 1123 cluster 14709: singleton (image_count=1) with a
    buddy-only shot and an InsightFace age estimate of 48 on what the
    operator says is actually a kid. F-A does NOT (and cannot) address
    this — Rule C flags every singleton as coach by workflow assumption
    ('every player gets multiple shots'). The kid-arrived-late case is
    algorithmically indistinguishable from a real one-shot coach and
    must be resolved via manual_coach_override=-1. Regression guard so
    a future softening of Rule C doesn't silently re-open Issue 5
    (see [[clustering-singleton-edge]])."""
    data = [{
        "cluster_id": 14709, "ages": [48.0], "image_count": 1,
        "single_face_count": 0, "multi_face_count": 1,
    }]
    flags = detect_coaches(data, session_median_count=5)
    assert flags[14709] is True, (
        "Rule C stopped firing on singletons. If this was intentional, "
        "[[clustering-singleton-edge]] Issue 5 needs re-examining — real "
        "one-shot coaches used to silently vanish from the cluster grid."
    )


def test_rule_a_absent_comp_keys_cannot_fire():
    """Old-style caller (no single_face_count / multi_face_count) —
    Rule A can no longer fire because the buddy-presence guard needs
    the comp key. Rule B already couldn't fire without comp keys. Only
    Rule C is available in this back-compat shape.

    In production, face_pipeline.py always supplies both comp keys, so
    this only matters for callers/tests written before F-A. Documented
    tighter contract."""
    data = [{"cluster_id": 1, "ages": [45.0, 45.0], "image_count": 2}]
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
    """Old callers that don't pass single/multi counts only get rules A and C
    (B can't fire without comp keys). Issue 5 turned image_count==1 into Rule C,
    so the prior expectation flipped — a comp-less image_count=1 is now a coach
    by Rule C alone."""
    # image_count=2 → no rule fires; was the actual back-compat shape.
    data = [{"cluster_id": 1, "ages": [], "image_count": 2}]  # no comp keys
    flags = detect_coaches(data, session_median_count=10)
    assert flags[1] is False


# ── Rule C — singleton heuristic (Issue 5) ───────────────────────────────────
#
# In our workflow, every player gets multiple shots by procedure (an individual,
# a team, and a pano). The only person who ever gets a single shot is a coach.
# So a singleton cluster (image_count == 1) is presumed coach. It's effectively
# a rule, not a probabilistic heuristic — but the operator can still override
# via manual_coach_override=-1 for the off-process kid-arrived-late case.


def test_singleton_flagged_as_coach():
    """image_count == 1 → coach by Rule C (regardless of ages/composition)."""
    data = [{"cluster_id": 1, "ages": [], "image_count": 1,
             "single_face_count": 1, "multi_face_count": 0}]
    flags = detect_coaches(data, session_median_count=10)
    assert flags[1] is True


def test_singleton_flagged_even_with_kid_age():
    """Even a 'kid'-aged singleton trips Rule C — the operator overrides if
    it's actually a 1-shot kid (off-process recovery hatch)."""
    data = [{"cluster_id": 1, "ages": [11.0, 12.0], "image_count": 1,
             "single_face_count": 1, "multi_face_count": 0}]
    flags = detect_coaches(data, session_median_count=10)
    assert flags[1] is True


def test_singleton_flagged_without_comp_keys():
    """Rule C fires on image_count==1 alone — even old callers without comp
    keys see it."""
    data = [{"cluster_id": 1, "ages": [], "image_count": 1}]
    flags = detect_coaches(data, session_median_count=10)
    assert flags[1] is True


def test_two_image_cluster_no_rule_c():
    """Rule C is image_count == 1 strict; a 2-image cluster needs other rules
    to fire (Rule A age or Rule B composition)."""
    data = [{"cluster_id": 1, "ages": [], "image_count": 2,
             "single_face_count": 2, "multi_face_count": 0}]
    flags = detect_coaches(data, session_median_count=10)
    assert flags[1] is False


def test_singleton_alongside_normal_clusters():
    """Mixed session: 1 singleton coach + 1 normal player cluster + 1 normal
    coach cluster. Each judged independently."""
    data = [
        # singleton — Rule C
        {"cluster_id": 1, "ages": [], "image_count": 1,
         "single_face_count": 1, "multi_face_count": 0},
        # normal player — no rule fires
        {"cluster_id": 2, "ages": [12.0, 12.0], "image_count": 8,
         "single_face_count": 6, "multi_face_count": 2},
        # normal coach by composition (Rule B)
        {"cluster_id": 3, "ages": [], "image_count": 7,
         "single_face_count": 2, "multi_face_count": 5},
    ]
    flags = detect_coaches(data, session_median_count=8)
    assert flags == {1: True, 2: False, 3: True}


def test_rule_c_aligns_with_issue_2_single_face_requirement():
    """A singleton trivially has single_face_count == 1, so Rule C does not
    conflict with Issue 2's planned 'coach requires >=1 single-face image'
    gate. When Issue 2 lands, this expectation stays."""
    data = [{"cluster_id": 1, "ages": [], "image_count": 1,
             "single_face_count": 1, "multi_face_count": 0}]
    flags = detect_coaches(data, session_median_count=10)
    assert flags[1] is True
    # The trivial property:
    assert data[0]["single_face_count"] >= 1
