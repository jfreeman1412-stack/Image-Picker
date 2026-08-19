"""Sorting rules — the source of truth for what counts as team/pano/individual.

This module is intentionally pure (no I/O, no DB). Hand it a list of image
records for one cluster and it returns a role assignment.

Rules (order + pose, then NON-SMILING preference for the pano):
  1. Sort cluster images by capture_time ascending.
  2. Single-face images (face_count == 1) are candidates for team and pano.
  3. team = the last single-face image. No expression condition.
  4. pano is chosen among the preceding single-face images that pass
     is_acceptable_pose(), taken in "walk back from team" order (closest-to-
     team first = the position prior for the pano pose). Among those
     already-acceptable candidates a NON-smiling frame is PREFERRED (landmark
     smile_score below threshold; see services/smile.py); the position prior
     breaks ties. Non-smiling is a preference among acceptable poses only — it
     never lets a badly-posed neutral shot beat a well-posed one. If EVERY
     acceptable candidate is smiling, the best-positioned one is still picked
     and 'pano_smiling_fallback' is flagged — pano is never left empty over
     expression. If NO preceding single passes the pose check, pano stays None
     with 'no_clean_pano_pose' (better to surface "pick one manually" than to
     silently guess a bad pose).
  5. Multi-face images are always 'buddy'.
  6. Everything else in the cluster is 'individual'.
  7. If the cluster has no single-face images at all → review reason
     'no_single_face_images', no picks.
  8. If the cluster has exactly one single-face image (= team only) → pano
     is None, review reason 'no_pano_candidate'.

Sanity-check flag added after the picks (does not affect selection):
  - team pick is not classified 'smiling' (FER expression) →
    'team_pick_not_smiling'. The pano's smile handling lives in rule 4 and
    uses the landmark smile_score, not FER (FER under-scores kids' smiles —
    see services/smile.py and expression.py Phase 4.4).

Valid role values: 'team' | 'panoramic' | 'individual' | 'buddy' | 'rejected'.
'rejected' is set only by manual override; the auto-sort never produces it.
Rejected images count as members of the cluster but are excluded from the
team/pano candidate pool.

review_reasons is a list; the caller may join with ',' for storage.
"""
from dataclasses import dataclass, field
from typing import Optional

from app.services.pose_check import is_acceptable_pose
from app.services.smile import is_smiling


@dataclass
class ImageRecord:
    image_id: int
    capture_time: float                       # unix ts; sortable
    face_count: int                           # from face detection
    expression: str = "unknown"               # FER label; team_pick_not_smiling only
    yaw: Optional[float] = None               # head pose degrees
    pitch: Optional[float] = None
    det_score: Optional[float] = None
    face_area_ratio: Optional[float] = None
    smile_score: Optional[float] = None       # landmark smile [0,1]; drives pano preference

    def pose_metadata(self) -> dict:
        return {
            "yaw": self.yaw,
            "pitch": self.pitch,
            "det_score": self.det_score,
            "face_area_ratio": self.face_area_ratio,
        }


@dataclass
class SortResult:
    roles: dict                          # image_id -> role
    team_image_id: Optional[int]
    panoramic_image_id: Optional[int]
    needs_review: bool
    review_reasons: list[str] = field(default_factory=list)

    @property
    def review_reason(self) -> Optional[str]:
        """Comma-joined view for back-compat with one-string consumers."""
        return ",".join(self.review_reasons) if self.review_reasons else None


def assign_roles(
    images: list[ImageRecord],
    manual_roles: dict[int, str] | None = None,
) -> SortResult:
    """Assign roles to images in a cluster using the order+pose rule.

    Args:
        images: list of ImageRecords in any order.
        manual_roles: optional {image_id: role} of locked manual overrides.
            Overridden images are removed from the team/pano candidate pool
            and keep their override role in the output. If a manual override
            already pins team or panoramic, the auto-sort still picks the
            next eligible image for the *other* role.
    """
    if not images:
        return SortResult(
            roles={}, team_image_id=None, panoramic_image_id=None,
            needs_review=True, review_reasons=["empty_cluster"],
        )

    manual_roles = manual_roles or {}
    ordered = sorted(images, key=lambda i: i.capture_time)

    manual_team_idx = next(
        (i for i, img in enumerate(ordered)
         if manual_roles.get(img.image_id) == "team"),
        None,
    )
    manual_pano_idx = next(
        (i for i, img in enumerate(ordered)
         if manual_roles.get(img.image_id) == "panoramic"),
        None,
    )

    # Single-face indexes (in ordered/capture-time order), excluding manual overrides.
    auto_singles = [
        i for i, img in enumerate(ordered)
        if img.face_count == 1 and img.image_id not in manual_roles
    ]

    review_reasons: list[str] = []

    # ── Team pick (last single-face image, no condition) ──────────────────
    if manual_team_idx is not None:
        team_idx = manual_team_idx
    elif auto_singles:
        team_idx = auto_singles[-1]
    else:
        team_idx = None

    # ── Pano pick: acceptable pose + prefer non-smiling, position as tiebreak ─
    pano_idx: Optional[int] = None
    if manual_pano_idx is not None:
        pano_idx = manual_pano_idx
    elif team_idx is not None:
        preceding_singles = [
            i for i in auto_singles
            if i < team_idx and i != manual_team_idx
        ]
        # Pose-acceptable candidates in "walk back from team" order
        # (closest-to-team first) — this ordering IS the position prior.
        acceptable = [
            i for i in reversed(preceding_singles)
            if is_acceptable_pose(ordered[i].pose_metadata())
        ]
        if acceptable:
            # Prefer a NON-smiling frame; the walk-back order breaks ties so a
            # neutral frame closest to the team shot wins. If every acceptable
            # candidate is smiling, still pick the best-positioned one and flag
            # the fallback — pano is never left empty over expression.
            non_smiling = [i for i in acceptable if not is_smiling(ordered[i].smile_score)]
            if non_smiling:
                pano_idx = non_smiling[0]
            else:
                pano_idx = acceptable[0]
                review_reasons.append("pano_smiling_fallback")
        elif preceding_singles:
            review_reasons.append("no_clean_pano_pose")
        else:
            review_reasons.append("no_pano_candidate")

    # ── Cluster-level review reasons for missing structure ────────────────
    has_any_single_face = any(img.face_count == 1 for img in ordered)
    if not has_any_single_face:
        review_reasons.append("no_single_face_images")

    # ── Assign roles ──────────────────────────────────────────────────────
    roles: dict = {}
    for i, img in enumerate(ordered):
        if img.image_id in manual_roles:
            roles[img.image_id] = manual_roles[img.image_id]
        elif img.face_count > 1:
            roles[img.image_id] = "buddy"
        elif i == team_idx:
            roles[img.image_id] = "team"
        elif i == pano_idx:
            roles[img.image_id] = "panoramic"
        else:
            roles[img.image_id] = "individual"

    # ── Sanity-check flag: team photos should be smiling (FER expression) ──
    # The pano's smile handling is in the selection above (landmark
    # smile_score → prefer non-smiling, flag pano_smiling_fallback), so there
    # is no separate FER-based pano flag anymore.
    if team_idx is not None and ordered[team_idx].expression != "smiling":
        review_reasons.append("team_pick_not_smiling")

    return SortResult(
        roles=roles,
        team_image_id=ordered[team_idx].image_id if team_idx is not None else None,
        panoramic_image_id=ordered[pano_idx].image_id if pano_idx is not None else None,
        needs_review=bool(review_reasons),
        review_reasons=review_reasons,
    )


def assign_roles_coach(
    images: list[ImageRecord],
    manual_roles: dict[int, str] | None = None,
) -> SortResult:
    """Sort rule variant for coach clusters.

    Coach clusters often contain only buddy/team-pose shots after the
    coach-exclusion filter has removed images that belong to players' clusters.
    Rules:
      - team = last image in capture order, regardless of face_count or expression
      - panoramic = None (coaches don't get a panoramic)
      - everything else = individual
      - if cluster is empty after coach-exclusion filter, needs_review with
        reason='coach_no_solo_image'

    Manual overrides win the same way as in assign_roles: a manually 'team'
    image stays team, and overridden images are excluded from the auto pool.
    """
    if not images:
        return SortResult(
            roles={}, team_image_id=None, panoramic_image_id=None,
            needs_review=True, review_reasons=["coach_no_solo_image"],
        )

    manual_roles = manual_roles or {}
    ordered = sorted(images, key=lambda i: i.capture_time)

    manual_team_id = next(
        (img.image_id for img in ordered if manual_roles.get(img.image_id) == "team"),
        None,
    )
    if manual_team_id is not None:
        team_image_id = manual_team_id
    else:
        auto_pool = [img for img in ordered if img.image_id not in manual_roles]
        team_image_id = auto_pool[-1].image_id if auto_pool else None

    roles: dict = {}
    for img in ordered:
        if img.image_id in manual_roles:
            roles[img.image_id] = manual_roles[img.image_id]
        elif img.image_id == team_image_id:
            roles[img.image_id] = "team"
        else:
            roles[img.image_id] = "individual"

    return SortResult(
        roles=roles,
        team_image_id=team_image_id,
        panoramic_image_id=None,
        needs_review=False,
        review_reasons=[],
    )
