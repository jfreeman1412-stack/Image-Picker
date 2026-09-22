"""Unit tests for sort_rules. These ARE the spec — keep them passing.

Pano picking is order + pose gated, THEN prefers a non-smiling frame among the
acceptable candidates (2026-08). Pano "smiling" is the landmark smile_score
(>= smile.SMILE_THRESHOLD), NOT the FER `expression` field — expression now
only drives the team_pick_not_smiling sanity flag. _rec defaults to GOOD pose
data and smile_score=None (un-scorable → treated as non-smiling) so existing
order/pose tests are unaffected; smile tests set smile_score explicitly.
"""
from app.services.smile import SMILE_THRESHOLD
from app.services.sort_rules import (
    ImageRecord, assign_roles, assign_roles_coach,
)

_SMILING = SMILE_THRESHOLD + 0.2      # unambiguously smiling
_NEUTRAL = SMILE_THRESHOLD - 0.2      # unambiguously non-smiling


def _rec(
    image_id, t, face_count, expression="unknown",
    yaw=0.0, pitch=0.0, det_score=0.95, face_area_ratio=0.15,
    smile_score=None,
):
    return ImageRecord(
        image_id=image_id, capture_time=t,
        face_count=face_count, expression=expression,
        yaw=yaw, pitch=pitch, det_score=det_score, face_area_ratio=face_area_ratio,
        smile_score=smile_score,
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


def test_pano_all_smiling_falls_back_and_flags():
    """Every acceptable pano candidate smiling → still pick, flag the fallback."""
    images = [
        _rec(1, 1.0, 1, smile_score=_SMILING),  # only pano candidate, smiling
        _rec(2, 2.0, 1, smile_score=_SMILING),  # team
    ]
    r = assign_roles(images)
    assert r.panoramic_image_id == 1                      # never left empty
    assert "pano_smiling_fallback" in r.review_reasons


def test_team_not_smiling_and_pano_fallback_coexist():
    """team_pick_not_smiling (FER expression) and pano_smiling_fallback
    (landmark smile_score) are independent signals that can co-occur."""
    images = [
        _rec(1, 1.0, 1, smile_score=_SMILING),           # pano → all-smiling fallback
        _rec(2, 2.0, 1, expression="serious"),           # team → not smiling (FER)
    ]
    r = assign_roles(images)
    assert "team_pick_not_smiling" in r.review_reasons
    assert "pano_smiling_fallback" in r.review_reasons


# ── Non-smiling pano preference (the 2026-08 feature) ──────────────────────────


def test_pano_prefers_nonsmiling_over_closer_smiling():
    """A neutral frame wins even when a smiling frame sits closer to the team.

    Walk-back-closest would pick 3 (smiling, adjacent to team). The non-smiling
    preference picks 2 instead — and does NOT flag a fallback, because a neutral
    frame was available.
    """
    images = [
        _rec(1, 1.0, 1, smile_score=_SMILING),   # smiling
        _rec(2, 2.0, 1, smile_score=_NEUTRAL),   # neutral — should win
        _rec(3, 3.0, 1, smile_score=_SMILING),   # smiling, closest to team
        _rec(4, 4.0, 1, smile_score=_SMILING),   # team
    ]
    r = assign_roles(images)
    assert r.panoramic_image_id == 2
    assert "pano_smiling_fallback" not in r.review_reasons


def test_pano_nonsmiling_closest_to_team_tiebreak():
    """Among multiple non-smiling candidates, the one closest to team wins."""
    images = [
        _rec(1, 1.0, 1, smile_score=_NEUTRAL),   # neutral, earlier
        _rec(2, 2.0, 1, smile_score=_NEUTRAL),   # neutral, closest to team → wins
        _rec(3, 3.0, 1, smile_score=_SMILING),   # team
    ]
    r = assign_roles(images)
    assert r.panoramic_image_id == 2


def test_pano_pose_gate_dominates_smile_preference():
    """A non-smiling but badly-posed frame must NOT beat a well-posed smiling one.

    Frame 2 is neutral but yaw=40° (fails the pose gate), so it's not a
    candidate at all; the well-posed smiling frame 1 is picked and flagged as a
    smiling fallback. Non-smiling is a preference among acceptable poses only.
    """
    images = [
        _rec(1, 1.0, 1, smile_score=_SMILING),               # well-posed, smiling
        _rec(2, 2.0, 1, smile_score=_NEUTRAL, yaw=40.0),     # neutral but bad pose
        _rec(3, 3.0, 1, smile_score=_SMILING),               # team
    ]
    r = assign_roles(images)
    assert r.panoramic_image_id == 1
    assert r.roles[2] == "individual"                        # bad-pose neutral not used
    assert "pano_smiling_fallback" in r.review_reasons


def test_pano_unknown_smile_treated_as_nonsmiling():
    """smile_score=None (un-scorable) is treated as non-smiling → eligible pano,
    no fallback flag (conservative degraded mode)."""
    images = [
        _rec(1, 1.0, 1, smile_score=None),   # un-scorable
        _rec(2, 2.0, 1, smile_score=_SMILING),  # team
    ]
    r = assign_roles(images)
    assert r.panoramic_image_id == 1
    assert "pano_smiling_fallback" not in r.review_reasons


def test_unknown_expression_does_not_trigger_team_not_smiling_flag():
    """'unknown' = "we couldn't classify" (fer import failed, crop too
    small, classify raised) → NO signal, don't fire the flag.

    2026-09-14: flipped from the prior "unknown fires the flag" assertion.
    That behavior meant every team got team_pick_not_smiling whenever fer's
    lazy import failed (which happens today with moviepy v2 installed),
    cluttering every review page with false positives. Only a confidently-
    'serious' team pick should fire the nag now. See sort_rules.py:188.
    """
    images = [
        _rec(1, 1.0, 1, "smiling"),
        _rec(2, 2.0, 1, "unknown"),  # team — un-classifiable, not a nag
    ]
    r = assign_roles(images)
    assert r.team_image_id == 2
    assert "team_pick_not_smiling" not in r.review_reasons


# ── 2026-09-21 pano window refinement: before FIRST group shot ────────────────
#
# Old rule: pano candidates were single-face images with index < team_idx.
# New rule: candidates are single-face images before the FIRST multi-face
# (group / "team photo") in capture order. Falls back to the old window
# only when no multi-face image exists in the cluster.


def test_pano_alt_structure_group_first_no_pano_candidate():
    """Alt shoot structure: group photo captured FIRST, individual portraits
    after. Old rule would have picked a "pano" from the late-shot
    individuals (walking back from team_idx). New rule: no candidate
    before the group shot → no_pano_candidate flagged.

    Better to surface "pick manually" than silently guess a shot taken
    AFTER the group photo when the operator's intent is "pano is the
    neutral portrait BEFORE the group shot"."""
    images = [
        _rec(1, 1.0, 3, smile_score=_SMILING),   # group first (t=1)
        _rec(2, 2.0, 1, smile_score=_NEUTRAL),   # solo after group
        _rec(3, 3.0, 1, smile_score=_NEUTRAL),   # solo
        _rec(4, 4.0, 1, smile_score=_SMILING),   # team pick (last solo)
    ]
    r = assign_roles(images)
    assert r.team_image_id == 4
    assert r.panoramic_image_id is None, (
        "New pano rule: no candidates BEFORE the first group shot → no pano. "
        "Old rule wrongly picked from post-group solos."
    )
    assert "no_pano_candidate" in r.review_reasons


def test_pano_blink_reshoot_group1_defines_the_window():
    """Blink-reshoot: solos → group1 → group2 (reshoot). Pano candidates
    are drawn from solos BEFORE group1 (not from between the reshoots
    or after them). team_idx is still the last single-face image
    (before either group shot); pano is the latest solo before group1."""
    images = [
        _rec(1, 1.0, 1, smile_score=_SMILING),
        _rec(2, 2.0, 1, smile_score=_NEUTRAL),   # neutral pano candidate
        _rec(3, 3.0, 1, smile_score=_SMILING),   # team pick (last single-face)
        _rec(4, 4.0, 2, smile_score=_SMILING),   # group1
        _rec(5, 5.0, 2, smile_score=_SMILING),   # group2 reshoot
    ]
    r = assign_roles(images)
    assert r.team_image_id == 3
    # Preceding-singles window (before first group at index 3, excluding
    # team_idx=2): [0, 1]. Walk back: [1, 0]. Non-smiling: img2 (index 1).
    assert r.panoramic_image_id == 2
    assert "pano_smiling_fallback" not in r.review_reasons


def test_pano_late_reshoot_solo_after_group_excluded():
    """Mixed structure: solos → group → LATE reshoot solo. Old rule made
    the late solo eligible as pano (i < team_idx). New rule excludes it
    (i >= first_group_idx). Pano must come from solos BEFORE the group.

    This is the load-bearing case for the "handles alt shoot structure"
    part of the spec — a solo captured AFTER a group shot doesn't fit
    the pano-pose position prior (it's not immediately-before-team;
    it's a late-catchup shot)."""
    images = [
        _rec(1, 1.0, 1, smile_score=_NEUTRAL),   # solo A
        _rec(2, 2.0, 1, smile_score=_NEUTRAL),   # solo B — should win pano
        _rec(3, 3.0, 2, smile_score=_SMILING),   # group at t=3
        _rec(4, 4.0, 1, smile_score=_NEUTRAL),   # late-reshoot solo
        _rec(5, 5.0, 1, smile_score=_SMILING),   # ANOTHER late solo (team pick)
    ]
    r = assign_roles(images)
    assert r.team_image_id == 5     # still last single-face
    # Window: singles with i < first_group_idx (2), i != team_idx (4).
    # That's [0, 1]. Walk back: [1, 0]. img2 (index 1) is neutral → pano.
    assert r.panoramic_image_id == 2
    # img4 (the late-reshoot solo) is 'individual', NOT pano.
    assert r.roles[4] == "individual"


def test_pano_fallback_window_when_no_group_shot():
    """Regression: cluster of pure solos (no multi-face image anywhere)
    falls back to the classic "before team pick" window. Same as
    test_pano_prefers_nonsmiling_over_closer_smiling structure."""
    images = [
        _rec(1, 1.0, 1, smile_score=_SMILING),
        _rec(2, 2.0, 1, smile_score=_NEUTRAL),   # neutral pano
        _rec(3, 3.0, 1, smile_score=_SMILING),
        _rec(4, 4.0, 1, smile_score=_SMILING),   # team (last single-face)
    ]
    r = assign_roles(images)
    assert r.team_image_id == 4
    # No multi-face → fallback to old window: preceding_singles<4 excluding
    # team = [0, 1, 2]. Non-smiling: img2. Same as before the refinement.
    assert r.panoramic_image_id == 2
    assert "pano_smiling_fallback" not in r.review_reasons


def test_pano_pure_alt_no_solos_before_group_no_candidate():
    """Even edgier alt structure: no solos before group at all — only
    solos after. Confirms `no_pano_candidate` fires cleanly."""
    images = [
        _rec(1, 1.0, 2, smile_score=_SMILING),   # group at t=1 (index 0)
        _rec(2, 2.0, 1, smile_score=_NEUTRAL),
        _rec(3, 3.0, 1, smile_score=_SMILING),   # team
    ]
    r = assign_roles(images)
    assert r.team_image_id == 3
    assert r.panoramic_image_id is None
    assert "no_pano_candidate" in r.review_reasons


def test_pano_group_after_team_pick_same_as_classic():
    """Standard shoot: solos → team pick (last single-face) → group
    shot. first_group_idx sits AFTER team_idx. The window excludes
    team_idx and everything from first_group_idx onward, matching the
    classic "before team pick" behavior exactly (both give [0..team_idx-1]).
    This is a regression guard for the most common shoot shape."""
    images = [
        _rec(1, 1.0, 1, smile_score=_SMILING),
        _rec(2, 2.0, 1, smile_score=_SMILING),
        _rec(3, 3.0, 1, smile_score=_NEUTRAL),   # pano candidate
        _rec(4, 4.0, 1, smile_score=_SMILING),   # team (last single-face)
        _rec(5, 5.0, 2, smile_score=_SMILING),   # group at end
    ]
    r = assign_roles(images)
    assert r.team_image_id == 4
    assert r.panoramic_image_id == 3


# ── Coach sort variant ────────────────────────────────────────────────────────


def test_coach_last_single_face_is_team_buddy_at_end_stays_buddy():
    """2026-09-21 Bug 2 export fix: a buddy shot at the end of a coach
    cluster's capture sequence is NOT picked as team anymore — team is
    the last SINGLE-FACE image. The buddy shot stays 'buddy'.

    Pre-fix, image 3 (face_count=2) was tagged 'team', which leaked
    into the Team Images export folder for team-composite production.
    Post-fix, image 2 (last single-face) is team; image 3 is buddy.
    """
    images = [
        _rec(1, 1.0, 1, "smiling"),
        _rec(2, 2.0, 1, "serious"),   # last single-face → team
        _rec(3, 3.0, 2, "smiling"),   # buddy at end → stays buddy, NOT team
    ]
    r = assign_roles_coach(images)
    assert r.team_image_id == 2, "team pick should be the last single-face, not the buddy"
    assert r.roles[3] == "buddy", (
        "buddy shot in coach cluster stayed 'individual' (or worse, 'team') — "
        "the export leak into Team Images is back"
    )
    assert r.roles[2] == "team"
    assert r.roles[1] == "individual"
    assert r.panoramic_image_id is None
    assert not r.needs_review


def test_coach_buddy_only_cluster_flags_no_solo():
    """Coach cluster with ONLY buddy shots (no single-face after coach-
    exclusion filter): no team pick, needs_review with coach_no_solo_image.
    Operator resolves via manual set-role. Was previously
    'last-buddy-becomes-team', which polluted Team Images."""
    images = [
        _rec(1, 1.0, 2, "smiling"),   # buddy
        _rec(2, 2.0, 3, "smiling"),   # buddy
        _rec(3, 3.0, 2, "smiling"),   # buddy at end — NO LONGER auto-tagged team
    ]
    r = assign_roles_coach(images)
    assert r.team_image_id is None, (
        "buddy-only coach cluster wrongly auto-picked a team — export "
        "will leak this buddy shot into Team Images"
    )
    for i in (1, 2, 3):
        assert r.roles[i] == "buddy"
    assert r.needs_review
    assert "coach_no_solo_image" in r.review_reasons


def test_coach_no_images_needs_review():
    """Coach cluster empty after exclusion filter → coach_no_solo_image."""
    r = assign_roles_coach([])
    assert r.team_image_id is None
    assert r.needs_review
    assert r.review_reasons == ["coach_no_solo_image"]


def test_coach_picks_in_capture_order_not_input_order():
    """Coach team-pick is last SINGLE-FACE by capture_time, not last in
    input list."""
    images = [
        _rec(99, 5.0, 1, "smiling"),   # latest single-face → team
        _rec(1, 1.0, 1, "smiling"),
        _rec(50, 3.0, 1, "serious"),
    ]
    r = assign_roles_coach(images)
    assert r.team_image_id == 99


def test_coach_manual_team_on_buddy_shot_still_wins():
    """Operator's explicit choice: manually pinning a buddy shot as
    'team' still applies (operator's intent > auto-safety heuristic).
    The auto-picker's face_count==1 filter doesn't apply to manual
    picks — that's a set-role decision the operator owns."""
    images = [
        _rec(1, 1.0, 1, "smiling"),
        _rec(2, 2.0, 2, "smiling"),   # buddy manually pinned as team
        _rec(3, 3.0, 1, "smiling"),
    ]
    r = assign_roles_coach(images, manual_roles={2: "team"})
    assert r.team_image_id == 2
    assert r.roles[2] == "team"


def test_coach_mixed_buddies_between_solos_solos_still_win_team():
    """Coach cluster: solo, buddy, solo, buddy, solo. Team = last solo
    (index 4). Both buddies stay 'buddy'. Regression guard for the
    common shape where buddy shots interleave with solos."""
    images = [
        _rec(1, 1.0, 1, "smiling"),
        _rec(2, 2.0, 2, "smiling"),   # buddy
        _rec(3, 3.0, 1, "smiling"),
        _rec(4, 4.0, 2, "smiling"),   # buddy
        _rec(5, 5.0, 1, "smiling"),   # last single-face → team
    ]
    r = assign_roles_coach(images)
    assert r.team_image_id == 5
    assert r.roles[5] == "team"
    assert r.roles[2] == "buddy"
    assert r.roles[4] == "buddy"
    assert r.roles[1] == "individual"
    assert r.roles[3] == "individual"


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
    """Coach team default is last single-face shot; manual override
    repins to a different image. 2026-09-21 Bug 2 fix: the buddy at
    index 3 stays 'buddy' (was 'individual' pre-fix — old assertion
    codified the bug where multi-face images were miscategorised as
    individual in the coach variant)."""
    images = [
        _rec(1, 1.0, 1, "smiling"),
        _rec(2, 2.0, 1, "serious"),
        _rec(3, 3.0, 2, "smiling"),   # buddy — post-fix stays 'buddy'
    ]
    r = assign_roles_coach(images, manual_roles={1: "team"})
    assert r.team_image_id == 1
    assert r.roles[1] == "team"
    assert r.roles[3] == "buddy"
