"""Per-installation settings (single-user studio — global, not per-user).

Currently just flag visibility: which cluster review-reason flags the UI
should surface. Computation stays in the pipeline; this only controls display.
"""
import json

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session as DbSession

from app.db import get_db
from app.models.db_models import Setting

router = APIRouter()

# Keep in sync with everything cluster.review_reason can contain.
KNOWN_FLAGS = [
    "team_pick_not_smiling",
    "pano_pick_smiling",
    "no_clean_pano_pose",
    "no_pano_candidate",
    "no_single_face_images",
    "ambiguous_copyright",
    "outlier_high",
    "outlier_low",
    "no_team_pick",
    "no_pano_pick",
    "coach_no_solo_image",
    "empty_cluster",
]

_FLAG_VIS_KEY = "flag_visibility"


def get_flag_visibility_map(db: DbSession) -> dict[str, bool]:
    """Resolve effective visibility for every known flag (default True)."""
    row = db.query(Setting).get(_FLAG_VIS_KEY)
    stored = {}
    if row and row.value:
        try:
            stored = json.loads(row.value)
        except (ValueError, TypeError):
            stored = {}
    return {flag: bool(stored.get(flag, True)) for flag in KNOWN_FLAGS}


def filter_visible_reasons(review_reason: str | None, vis: dict[str, bool]) -> list[str]:
    """Split a comma-joined review_reason and drop hidden flags."""
    if not review_reason:
        return []
    return [
        r for r in (x.strip() for x in review_reason.split(","))
        if r and vis.get(r, True)
    ]


@router.get("/flag-visibility")
def get_flag_visibility(db: DbSession = Depends(get_db)):
    return get_flag_visibility_map(db)


@router.post("/flag-visibility")
def set_flag_visibility(payload: dict[str, bool], db: DbSession = Depends(get_db)):
    """Merge the given {flag: bool} overrides into stored settings."""
    row = db.query(Setting).get(_FLAG_VIS_KEY)
    current = {}
    if row and row.value:
        try:
            current = json.loads(row.value)
        except (ValueError, TypeError):
            current = {}

    for flag, visible in payload.items():
        if flag in KNOWN_FLAGS:
            current[flag] = bool(visible)

    if row is None:
        row = Setting(key=_FLAG_VIS_KEY, value=json.dumps(current))
        db.add(row)
    else:
        row.value = json.dumps(current)
    db.commit()
    return get_flag_visibility_map(db)
