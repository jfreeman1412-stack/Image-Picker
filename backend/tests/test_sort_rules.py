"""Unit tests for sort_rules. These ARE the spec — keep them passing.

Phase 3 update: pano picking is order+pose based; smile is a sanity-check
signal only. _rec defaults to GOOD pose data so existing tests don't all hit
the no-pose-data fallback path; tests that want to exercise the pose filter
pass bad poses explicitly via yaw/pitch/det_score/area.
"""
from app.services.sort_rules import (
    ImageRecord, assign_roles, assign_roles_coach,
)


def _rec(
    image_id, t, face_count, expression="unknown",
    yaw=0.0, pitch=0.0, det_score=0.95, face_area_ratio=0.15,
):
    return ImageRecord(
        image_id=image_id, capture_time=t,
        face_count=face_count, expression=expression,
        yaw=yaw, pitch=pitch, det_score=det_score, face_area_ratio=face_area_ratio,
    )


# ── Phase 1 happy paths (still valid under the new rule) ──────────────────────


def test_happy_path_typical_sequence():
    """Typical shoot: 6 individuals, then pano, then team. Order-based pick."""
    images = [
        _rec(1, 1.0, 1, "smiling"),
        _rec(2, 2.0, 1, "smiling"),
        _rec(3, 3.0, 1, "smiling"),
        _rec(4, 4.0, 1, "smiling"),
        _rec(5, 5.0, 1, "smiling"),
        _rec(6, 6.0, 1, "smiling"),
        _rec(7, 7.0, 1, "serious"),   # pano (walks back from team, good pose)
        _rec(8, 8.0, 1, "smiling"),   # team (last single-face)
    ]
    r = assign_roles(images)
    assert r.team_image_id == 8
    assert r.panoramic_image_id == 7
    assert r.roles[1] == "individual"
    assert r.roles[7] == "panoramic"
    assert r.roles[8] == "team"
    assert not r.needs_review


def test_buddy_photo_after_team():
    """Buddy photo at the end stays 'buddy' (face_count>1), team = last single-face."""
    images = [
        _rec(1, 1.0, 1, "smiling"),
        _rec(2, 2.0, 1, "smiling"),
        _rec(3, 3.0, 1, "serious"),   # pano
        _rec(4, 4.0, 1, "smiling"),   # team — last single-face
        _rec(5, 5.0, 2, "smiling"),   # buddy
    ]
    r = assign_roles(images)
    assert r.team_image_id == 4
    assert r.panoramic_image_id == 3
    assert r.roles[5] == "buddy"


def test_capture_order_independent_of_input_order():
    """Roles depend on capture_time, not list order."""
    images = [
        _rec(8, 8.0, 1, "smiling"),
        _rec(1, 1.0, 1, "smiling"),
        _rec(7, 7.0, 1, "serious"),
    ]
    r = assign_roles(images)
    assert r.team_image_id == 8
    assert r.panoramic_image_id == 7


def test_empty_cluster():
    r = assign_roles([])
    assert r.needs_review
    assert r.review_reasons == ["empty_cluster"]


# ── Phase 3 new rule: order-based team, pose-filtered pano ─────────────────────


def test_silly_shot_intrusion_pose_filter_skips_it():
    """Critical: bad-pose shot between team and pano should be skipped.

    Capture order: 5 individuals, then a silly shot (yaw=40°), then team.
    Old rule would have picked the silly shot as pano (it's classified
    serious). New rule walks back past it to the next good-pose image.
    """
    images = [
        _rec(1, 1.0, 1, "smiling"),
        _rec(2, 2.0, 1, "smiling"),
        _rec(3, 3.0, 1, "smiling"),
        _rec(4, 4.0, 1, "serious"),                       # good pose pano
        _rec(5, 5.0, 1, "serious", yaw=40.0),             # silly: skipped
        _rec(6, 6.0, 1, "smiling"),                       # team
    ]
    r = assign_roles(images)
    assert r.team_image_id == 6
    assert r.panoramic_image_id == 4
    assert r.roles[5] == "individual"


def test_two_silly_shots_before_team():
    """Walk-back skips multiple consecutive bad poses.

    Phase 4.3: pose limits loosened to ±30°; bumped silly-shot values to ±40°
    so the test still exercises the walk-back skipping behavior.
    """
    images = [
        _rec(1, 1.0, 1, "smiling"),
        _rec(2, 2.0, 1, "serious"),                       # good pose pano (4th from end)
        _rec(3, 3.0, 1, "serious", yaw=40.0),             # silly (yaw past loosened ±30°)
        _rec(4, 4.0, 1, "serious", pitch=40.0),           # silly (pitch past loosened ±30°)
        _rec(5, 5.0, 1, "smiling"),                       # team
    ]
    r = assign_roles(images)
    assert r.team_image_id == 5
    assert r.panoramic_image_id == 2


def test_no_acceptable_pano_pose_leaves_pano_unset_and_flags():
    """All preceding singles have bad pose → pano=None, cluster flagged for review.

    Joey'd rather have an empty pano slot to fill in manually than a guess.
    """
    images = [
        _rec(1, 1.0, 1, "serious", yaw=40.0),               # bad pose (yaw > 30)
        _rec(2, 2.0, 1, "serious", det_score=0.3),          # bad detect (< 0.5)
        _rec(3, 3.0, 1, "serious", face_area_ratio=0.0005), # microscopic face (< 0.0015)
        _rec(4, 4.0, 1, "smiling"),                         # team
    ]
    r = assign_roles(images)
    assert r.team_image_id == 4
    assert r.panoramic_image_id is None
    assert r.needs_review
    assert "no_clean_pano_pose" in r.review_reasons
    # The bad-pose images stay 'individual' — none of them become a pano fallback.
    assert r.roles[1] == "individual"
    assert r.roles[2] == "individual"
    assert r.roles[3] == "individual"


def test_single_image_cluster_no_pano_candidate():
    """One single-face image total → team is it, pano is None."""
    images = [_rec(1, 1.0, 1, "smiling")]
    r = assign_roles(images)
    assert r.team_image_id == 1
    assert r.panoramic_image_id is None
    assert r.needs_review
    assert "no_pano_candidate" in r.review_reasons


def test_all_buddy_cluster_no_single_face_images():
    """Cluster contains only multi-face buddy shots → no picks, flagged."""
    images = [
        _rec(1, 1.0, 2, "smiling"),
        _rec(2, 2.0, 3, "smiling"),
    ]
    r = assign_roles(images)
    assert r.team_image_id is None
    assert r.panoramic_image_id is None
    assert r.roles[1] == "buddy"
    assert r.roles[2] == "buddy"
    assert r.needs_review
    assert "no_single_face_images" in r.review_reasons


# ── Sanity-check flags (smile classifier as advisory signal) ───────────────────


def test_team_pick_serious_flags_review():
    """Team is picked by order, but if it's not smiling, surface the warning."""
    images = [
        _rec(1, 1.0, 1, "smiling"),
        _rec(2, 2.0, 1, "serious"),  # team, not smiling
    ]
    r = assign_roles(images)
    assert r.team_image_id == 2
    assert "team_pick_not_smiling" in r.review_reasons


def test_pano_pick_smiling_flags_review():
    """Pano is picked by order, but if it's smiling, surface the warning."""
    images = [
        _rec(1, 1.0, 1, "smiling"),  # pano fallback / walk-back, smiling
        _rec(2, 2.0, 1, "smiling"),  # team
    ]
    r = assign_roles(images)
    assert r.panoramic_image_id == 1
    assert "pano_pick_smiling" in r.review_reasons


def test_both_sanity_flags_coexist():
    """A single cluster can carry both team_pick_not_smiling and pano_pick_smiling."""
    images = [
        _rec(1, 1.0, 1, "smiling"),  # pano → smiling triggers pano_pick_smiling
        _rec(2, 2.0, 1, "serious"),  # team → not smiling triggers team_pick_not_smiling
    ]
    r = assign_roles(images)
    assert "team_pick_not_smiling" in r.review_reasons
    assert "pano_pick_smiling" in r.review_reasons


def test_unknown_expression_triggers_team_not_smiling_flag():
    """'unknown' isn't 'smiling' → counts as not-smiling for the sanity flag."""
    images = [
        _rec(1, 1.0, 1, "smiling"),
        _rec(2, 2.0, 1, "unknown"),  # team
    ]
    r = assign_roles(images)
    assert r.team_image_id == 2
    assert "team_pick_not_smiling" in r.review_reasons


# ── Coach sort variant ────────────────────────────────────────────────────────


def test_coach_last_image_is_team_even_buddy():
    """Coach's last image is team, regardless of face_count or expression."""
    images = [
        _rec(1, 1.0, 1, "smiling"),
        _rec(2, 2.0, 1, "serious"),
        _rec(3, 3.0, 2, "smiling"),   # buddy at end — still team for coach
    ]
    r = assign_roles_coach(images)
    assert r.team_image_id == 3
    assert r.panoramic_image_id is None
    assert r.roles[3] == "team"
    assert r.roles[1] == "individual"
    assert not r.needs_review


def test_coach_no_images_needs_review():
    """Coach cluster empty after exclusion filter → coach_no_solo_image."""
    r = assign_roles_coach([])
    assert r.team_image_id is None
    assert r.needs_review
    assert r.review_reasons == ["coach_no_solo_image"]


def test_coach_picks_in_capture_order_not_input_order():
    """Coach team-pick is last by capture_time, not last in list."""
    images = [
        _rec(99, 5.0, 1, "smiling"),
        _rec(1, 1.0, 1, "smiling"),
        _rec(50, 3.0, 1, "serious"),
    ]
    r = assign_roles_coach(images)
    assert r.team_image_id == 99


# ── Manual role override ──────────────────────────────────────────────────────


def test_manual_reject_removes_from_candidate_pool():
    """If image 5 is rejected, it's not picked even though it would have been team."""
    images = [
        _rec(1, 1.0, 1, "smiling"),
        _rec(2, 2.0, 1, "serious"),
        _rec(3, 3.0, 1, "smiling"),
        _rec(4, 4.0, 1, "serious"),
        _rec(5, 5.0, 1, "smiling"),   # would auto-be team; manually rejected
    ]
    r = assign_roles(images, manual_roles={5: "rejected"})
    # New rule: team = last single-face from the AUTO pool = image 4.
    assert r.team_image_id == 4
    assert r.roles[5] == "rejected"


def test_manual_team_pins_image_as_team():
    """Manually marking 5 as team makes it team even if 8 would have been auto-pick."""
    images = [
        _rec(1, 1.0, 1, "smiling"),
        _rec(5, 5.0, 1, "smiling"),
        _rec(7, 7.0, 1, "serious"),
        _rec(8, 8.0, 1, "smiling"),   # would auto-be team
    ]
    r = assign_roles(images, manual_roles={5: "team"})
    assert r.team_image_id == 5
    assert r.roles[5] == "team"
    # Image 8 falls out of the team slot — should be 'individual', not 'team'.
    assert r.roles[8] == "individual"


def test_manual_panoramic_pins_pano_pick():
    images = [
        _rec(1, 1.0, 1, "serious"),
        _rec(2, 2.0, 1, "smiling"),
        _rec(3, 3.0, 1, "serious"),
        _rec(4, 4.0, 1, "smiling"),   # team
    ]
    r = assign_roles(images, manual_roles={1: "panoramic"})
    assert r.panoramic_image_id == 1
    assert r.team_image_id == 4
    # Image 3 no longer pano.
    assert r.roles[3] == "individual"


def test_manual_override_in_coach_cluster():
    """Coach team default is last shot; manual override repins to a different image."""
    images = [
        _rec(1, 1.0, 1, "smiling"),
        _rec(2, 2.0, 1, "serious"),
        _rec(3, 3.0, 2, "smiling"),   # last by capture, default coach team
    ]
    r = assign_roles_coach(images, manual_roles={1: "team"})
    assert r.team_image_id == 1
    assert r.roles[1] == "team"
    assert r.roles[3] == "individual"
