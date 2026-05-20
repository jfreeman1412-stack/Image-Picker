"""Sessions endpoints.

POST   /api/sessions                       create a session from a folder path
POST   /api/sessions/{id}/run              kick off pipeline (background task)
GET    /api/sessions                       list sessions
GET    /api/sessions/{id}                  session detail + status
POST   /api/sessions/{id}/reviewed         toggle the reviewed flag (gated; force=true to override)
GET    /api/sessions/{id}/review-readiness  per-cluster team/pano completeness
GET    /api/sessions/{id}/next-unreviewed  next unreviewed team in same job
GET    /api/sessions/{id}/siblings         previous/next teams in same job
POST   /api/sessions/{id}/archive          soft-hide a team
POST   /api/sessions/{id}/unarchive        restore an archived team
DELETE /api/sessions/{id}                  hard-delete team + cascade (incl. thumbs)
"""
import logging
from datetime import datetime
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel
from sqlalchemy.orm import Session as DbSession

from app.db import get_db, SessionLocal
from app.models.db_models import Cluster, ImageRole, RosterEntry, Session
from app.services.eta import eta_seconds
from app.services.ingest import ingest_folder
from app.services.face_pipeline import run_pipeline
from app.services.roster import normalize_name
from app.services.roster_check import session_norm_team

logger = logging.getLogger(__name__)

router = APIRouter()


def _compute_review_readiness(db: DbSession, session: Session) -> dict:
    """Per cluster: a coach needs a 'team' role; a player needs both 'team'
    and 'panoramic'. Returns {ready, incomplete_clusters:[{cluster_id,label,
    missing:[...]}]}."""
    incomplete: list[dict] = []
    for c in session.clusters:
        roles = {
            r.role for r in db.query(ImageRole).filter_by(cluster_id=c.id).all()
        }
        if c.is_coach_for_sort():
            required = ["team"]
        else:
            required = ["team", "pano"]
        missing = []
        if "team" not in roles:
            missing.append("team")
        if "pano" in required and "panoramic" not in roles:
            missing.append("pano")
        if missing:
            incomplete.append({
                "cluster_id": c.id,
                "label": c.display_label(),
                "missing": missing,
            })
    return {"ready": not incomplete, "incomplete_clusters": incomplete}


class CreateSessionRequest(BaseModel):
    name: str
    folder: str


@router.post("")
def create_session(payload: CreateSessionRequest, db: DbSession = Depends(get_db)):
    folder = Path(payload.folder)
    if not folder.exists() or not folder.is_dir():
        raise HTTPException(400, f"Folder not found: {payload.folder}")
    s = Session(name=payload.name, source_path=str(folder.resolve()), status="pending")
    db.add(s)
    db.commit()
    db.refresh(s)
    count = ingest_folder(db, s.id, folder)
    return {"id": s.id, "name": s.name, "image_count": count}


@router.post("/{session_id}/run")
def run(session_id: int, background: BackgroundTasks, db: DbSession = Depends(get_db)):
    s = db.query(Session).get(session_id)
    if s is None:
        raise HTTPException(404, "Session not found")
    if s.status == "running":
        raise HTTPException(409, "Pipeline already running")

    def _run():
        with SessionLocal() as bg_db:
            run_pipeline(bg_db, session_id)

    background.add_task(_run)
    s.status = "running"
    db.commit()
    return {"status": "running"}


@router.get("")
def list_sessions(db: DbSession = Depends(get_db), legacy_only: bool = False):
    """List sessions. `legacy_only=True` returns only jobless (Phase 1–3) sessions."""
    q = db.query(Session).order_by(Session.created_at.desc())
    if legacy_only:
        q = q.filter(Session.job_id.is_(None))
    sessions = q.all()
    return [
        {
            "id": s.id, "name": s.name, "status": s.status,
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "image_count": len(s.images),
            "job_id": s.job_id,
        }
        for s in sessions
    ]


def pipeline_progress_fields(s: Session) -> dict:
    """Progress block shared by session detail + job detail. elapsed is
    whole-pipeline; eta is for the *current* stage (stages run at very
    different speeds, so a per-stage estimate is the honest one)."""
    now = datetime.utcnow()
    elapsed = (
        int((now - s.progress_started_at).total_seconds())
        if s.progress_started_at else 0
    )
    stage_eta = None
    if s.progress_stage_started_at and (s.progress_total or 0) > 0:
        stage_elapsed = (now - s.progress_stage_started_at).total_seconds()
        stage_eta = eta_seconds(
            s.progress_current or 0, s.progress_total or 0, stage_elapsed
        )
    return {
        "progress_stage": s.progress_stage,
        "progress_current": s.progress_current or 0,
        "progress_total": s.progress_total or 0,
        "progress_elapsed_seconds": elapsed,
        "progress_stage_eta_seconds": stage_eta,
    }


@router.get("/{session_id}")
def get_session(session_id: int, db: DbSession = Depends(get_db)):
    s = db.query(Session).get(session_id)
    if s is None:
        raise HTTPException(404, "Session not found")
    return {
        "id": s.id,
        "name": s.name,
        "source_path": s.source_path,
        "status": s.status,
        "image_count": len(s.images),
        "cluster_count": len(s.clusters),
        "needs_review_count": sum(1 for c in s.clusters if c.needs_review),
        "job_id": s.job_id,
        "job_name": s.job.name if s.job is not None else None,
        "reviewed": bool(s.reviewed),
        "reviewed_at": s.reviewed_at.isoformat() if s.reviewed_at else None,
        "archived": bool(s.archived),
        "archived_at": s.archived_at.isoformat() if s.archived_at else None,
        **pipeline_progress_fields(s),
    }


# ── Review state + navigation ─────────────────────────────────────────────────


class SetReviewedRequest(BaseModel):
    reviewed: bool
    force: bool = False


@router.get("/{session_id}/review-readiness")
def review_readiness(session_id: int, db: DbSession = Depends(get_db)):
    """Whether every cluster has its required role(s): coach → team;
    player → team + panoramic. ready=true iff nothing is incomplete."""
    s = db.query(Session).get(session_id)
    if s is None:
        raise HTTPException(404, "Session not found")
    return _compute_review_readiness(db, s)


@router.post("/{session_id}/reviewed")
def set_reviewed(
    session_id: int, payload: SetReviewedRequest,
    db: DbSession = Depends(get_db),
):
    """Flip the reviewed flag. Marking reviewed=True is gated by
    review-readiness unless force=True (logged for audit). Unmarking is
    never gated."""
    s = db.query(Session).get(session_id)
    if s is None:
        raise HTTPException(404, "Session not found")

    if payload.reviewed:
        readiness = _compute_review_readiness(db, s)
        if not readiness["ready"] and not payload.force:
            raise HTTPException(400, detail={
                "error": "session_not_ready",
                "incomplete_clusters": readiness["incomplete_clusters"],
            })
        if not readiness["ready"] and payload.force:
            summary = "; ".join(
                f"'{c['label']}' (missing {', '.join(c['missing'])})"
                for c in readiness["incomplete_clusters"]
            )
            logger.warning(
                "[review] Session %s marked reviewed with force=true. "
                "%d incomplete cluster(s): %s",
                session_id, len(readiness["incomplete_clusters"]), summary,
            )
        s.reviewed = 1
        s.reviewed_at = datetime.utcnow()
    else:
        s.reviewed = 0
        s.reviewed_at = None
    db.commit()
    return {
        "reviewed": bool(s.reviewed),
        "reviewed_at": s.reviewed_at.isoformat() if s.reviewed_at else None,
    }


def _sessions_in_job_ordered(db: DbSession, job_id: int | None) -> list[Session]:
    """All sessions in a job, ordered by id (creation order from the wizard)."""
    if job_id is None:
        return []
    return (
        db.query(Session)
        .filter(Session.job_id == job_id)
        .order_by(Session.id.asc())
        .all()
    )


@router.get("/{session_id}/next-unreviewed")
def next_unreviewed(session_id: int, db: DbSession = Depends(get_db)):
    """Return next session in the same job with reviewed=0, in id order.

    Search forward from current session_id first; if nothing found, wrap to the
    start of the job and search up to (but not including) the current session.
    Returns {session_id: int | null}.
    """
    s = db.query(Session).get(session_id)
    if s is None:
        raise HTTPException(404, "Session not found")

    siblings_list = _sessions_in_job_ordered(db, s.job_id)
    if not siblings_list:
        return {"session_id": None}

    # Archived sessions are out of the review rotation.
    forward = [t for t in siblings_list
               if t.id > session_id and not t.reviewed and not t.archived]
    if forward:
        return {"session_id": forward[0].id}

    wrapped = [t for t in siblings_list
               if t.id < session_id and not t.reviewed and not t.archived]
    if wrapped:
        return {"session_id": wrapped[0].id}

    return {"session_id": None}


@router.get("/{session_id}/siblings")
def siblings(session_id: int, db: DbSession = Depends(get_db)):
    """Return previous and next session ids in the same job (by id order)."""
    s = db.query(Session).get(session_id)
    if s is None:
        raise HTTPException(404, "Session not found")

    siblings_list = _sessions_in_job_ordered(db, s.job_id)
    ids = [t.id for t in siblings_list]
    try:
        idx = ids.index(session_id)
    except ValueError:
        return {"previous_session_id": None, "next_session_id": None}

    prev_id = ids[idx - 1] if idx > 0 else None
    next_id = ids[idx + 1] if idx + 1 < len(ids) else None
    return {"previous_session_id": prev_id, "next_session_id": next_id}


# ── Phase 6.1: roster-team alias (folder ↔ CSV-team mapping) ──────────────────


class RosterMappingRequest(BaseModel):
    roster_team_alias: str | None  # null clears the mapping


@router.post("/{session_id}/roster-mapping")
def set_roster_mapping(
    session_id: int, payload: RosterMappingRequest,
    db: DbSession = Depends(get_db),
):
    """Set or clear the CSV-team string this session's folder maps to.

    Stored raw (e.g. "10U-Black-Softball"); compared after normalize_name
    against the roster. Passing null clears the mapping so the folder name
    itself is used again. Survives roster re-uploads.
    """
    s = db.query(Session).get(session_id)
    if s is None:
        raise HTTPException(404, "Session not found")
    alias = (payload.roster_team_alias or "").strip() or None
    s.roster_team_alias = alias
    db.commit()
    return {"session_id": s.id, "roster_team_alias": s.roster_team_alias}


# ── Phase 9: per-team roster coverage report ──────────────────────────────────


@router.get("/{session_id}/roster-coverage")
def roster_coverage(session_id: int, db: DbSession = Depends(get_db)):
    """Compare this session's clusters against the roster's expected players
    for the effective team (via Phase 6.1 alias OR folder name).

    Returns four buckets so the UI can show:
      - expected_players      : roster rows for this team
      - present_players       : roster players matched to a cluster's auto_label
      - missing_players       : expected − present  ("X didn't get photographed")
      - extra_clusters        : clusters whose auto_label maps to a different
                                team in the roster OR isn't in the roster at all
      - unidentified_clusters : clusters with no auto_label (blank copyright)

    No-roster case: all buckets empty, expected_team_raw = session.name. The
    UI is expected to hide the section when expected_players is empty.
    """
    s = db.query(Session).get(session_id)
    if s is None:
        raise HTTPException(404, "Session not found")

    effective_norm = session_norm_team(s)
    expected_raw = s.roster_team_alias or s.name

    # Roster for this team. If the session belongs to a jobless legacy row,
    # the join below returns no rows — graceful no-op.
    expected_rows: list[RosterEntry] = []
    if s.job_id is not None:
        expected_rows = (
            db.query(RosterEntry)
            .filter(RosterEntry.job_id == s.job_id,
                    RosterEntry.norm_team == effective_norm)
            .order_by(RosterEntry.raw_name.asc())
            .all()
        )

    expected_norms = {r.norm_name for r in expected_rows}

    # Build a job-wide lookup norm_name -> (norm_team, raw_team) for resolving
    # extras (clusters whose label maps to a different team in this job).
    job_roster: list[RosterEntry] = []
    if s.job_id is not None:
        job_roster = db.query(RosterEntry).filter_by(job_id=s.job_id).all()
    by_norm_name: dict[str, tuple[str, str]] = {}
    for r in job_roster:
        # If a name appears on multiple teams, leave it ambiguous (None) so
        # we don't claim a single team for it. Matches Phase 6 lookup
        # abstain rule.
        if r.norm_name in by_norm_name:
            by_norm_name[r.norm_name] = (None, None)
        else:
            by_norm_name[r.norm_name] = (r.norm_team, r.team_name)
    # Drop the ambiguous markers — they behave like "no match" for extras.
    by_norm_name = {k: v for k, v in by_norm_name.items() if v[0] is not None}

    present: list[dict] = []
    extra_clusters: list[dict] = []
    unidentified_clusters: list[dict] = []
    matched_norms: set[str] = set()

    for c in s.clusters:
        label = (c.auto_label or "").strip()
        if not label:
            unidentified_clusters.append({
                "cluster_id": c.id,
                "image_count": c.image_count or 0,
            })
            continue
        norm = normalize_name(label)
        if not norm:
            unidentified_clusters.append({
                "cluster_id": c.id,
                "image_count": c.image_count or 0,
            })
            continue
        if norm in expected_norms:
            # Find the roster row to pick a canonical raw_name for display.
            raw_name = next(
                (r.raw_name for r in expected_rows if r.norm_name == norm), label,
            )
            present.append({
                "raw_name": raw_name,
                "cluster_id": c.id,
                "image_count": c.image_count or 0,
            })
            matched_norms.add(norm)
        else:
            other = by_norm_name.get(norm)
            extra_clusters.append({
                "cluster_id": c.id,
                "label": c.display_label(),
                "image_count": c.image_count or 0,
                # raw team name if the player is on the roster for a
                # DIFFERENT team; None if the player isn't on the roster
                # at all.
                "roster_team_raw": other[1] if other else None,
            })

    missing_players = [
        {"raw_name": r.raw_name}
        for r in expected_rows
        if r.norm_name not in matched_norms
    ]

    return {
        "expected_team_norm": effective_norm,
        "expected_team_raw": expected_raw,
        "expected_players": [
            {"raw_name": r.raw_name, "norm_name": r.norm_name}
            for r in expected_rows
        ],
        "present_players": present,
        "missing_players": missing_players,
        "extra_clusters": extra_clusters,
        "unidentified_clusters": unidentified_clusters,
    }


# ── Archive / delete ──────────────────────────────────────────────────────────


@router.post("/{session_id}/archive")
def archive_session(session_id: int, db: DbSession = Depends(get_db)):
    """Soft-hide a team from the job detail grid. Idempotent."""
    s = db.query(Session).get(session_id)
    if s is None:
        raise HTTPException(404, "Session not found")
    if not s.archived:
        s.archived = 1
        s.archived_at = datetime.utcnow()
        db.commit()
    return {"status": "archived", "archived": True,
            "archived_at": s.archived_at.isoformat() if s.archived_at else None}


@router.post("/{session_id}/unarchive")
def unarchive_session(session_id: int, db: DbSession = Depends(get_db)):
    """Restore an archived team. Idempotent."""
    s = db.query(Session).get(session_id)
    if s is None:
        raise HTTPException(404, "Session not found")
    s.archived = 0
    s.archived_at = None
    db.commit()
    return {"status": "unarchived", "archived": False}


@router.delete("/{session_id}")
def delete_session(session_id: int, db: DbSession = Depends(get_db)):
    """Permanently delete one team: cascades to its images, faces, clusters,
    image_roles, and cached thumbnails. Parent job is left intact."""
    from app.api.jobs import _delete_thumbs  # local import avoids import cycle
    s = db.query(Session).get(session_id)
    if s is None:
        raise HTTPException(404, "Session not found")

    image_ids = [i.id for i in s.images]
    if image_ids:
        db.query(ImageRole).filter(
            ImageRole.image_id.in_(image_ids)
        ).delete(synchronize_session=False)
    _delete_thumbs(image_ids)
    db.delete(s)  # ORM cascade: images → faces, clusters
    db.commit()
    return {"status": "deleted"}
