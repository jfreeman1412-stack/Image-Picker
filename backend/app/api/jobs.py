"""Job endpoints — multi-team shoot management.

A Job groups multiple Sessions (one per team). The wizard flow uses /peek to
inspect folders and /jobs (POST) to commit the structure once the user has
answered the walkthrough questions.

POST   /api/jobs/peek                  inspect folder contents
POST   /api/jobs                       create a job + sessions from confirmed structure
GET    /api/jobs                       list jobs (default: non-archived; ?archived=true for archived)
GET    /api/jobs/{id}                  job detail + session summaries
POST   /api/jobs/{id}/run-all          kick off pipeline for every session in this job
POST   /api/jobs/{id}/export           export sorted files (see Section 4)
POST   /api/jobs/{id}/archived         soft-hide/restore from default listing
DELETE /api/jobs/{id}                  hard-delete job + cascade sessions
"""
import json
import logging
import shutil
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session as DbSession

from app.db import SessionLocal, get_db
from app.models.db_models import Cluster, ImageRole, Image, Job, Session
from app.services.face_pipeline import run_pipeline
from app.services.ingest import RAW_EXTS, SUPPORTED_EXTS, ingest_folder

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Peek (folder inspector for the wizard) ────────────────────────────────────


class PeekRequest(BaseModel):
    path: str


@router.post("/peek")
def peek_folder(payload: PeekRequest):
    """List subdirectories and count files in a given path. Non-recursive."""
    folder = Path(payload.path)
    if not folder.exists() or not folder.is_dir():
        raise HTTPException(400, f"Not a directory: {payload.path}")

    subfolders = []
    image_count = 0
    raw_count = 0

    for entry in sorted(folder.iterdir()):
        if entry.is_dir():
            sub_count = 0
            sub_images = 0
            sub_raws = 0
            try:
                for inner in entry.iterdir():
                    if inner.is_dir():
                        sub_count += 1
                    elif inner.is_file():
                        ext = inner.suffix.lower()
                        if ext in SUPPORTED_EXTS:
                            sub_images += 1
                        elif ext in RAW_EXTS:
                            sub_raws += 1
            except PermissionError:
                logger.warning("Permission denied reading %s", entry)
            subfolders.append({
                "name": entry.name,
                "subfolder_count": sub_count,
                "image_count": sub_images,
                "raw_count": sub_raws,
            })
        elif entry.is_file():
            ext = entry.suffix.lower()
            if ext in SUPPORTED_EXTS:
                image_count += 1
            elif ext in RAW_EXTS:
                raw_count += 1

    return {
        "path": str(folder.resolve()),
        "subfolders": subfolders,
        "image_count": image_count,
        "raw_count": raw_count,
    }


# ── Create job from confirmed structure ───────────────────────────────────────


class CreateJobRequest(BaseModel):
    name: str
    root_path: str
    has_lines: bool
    image_subfolder_name: Optional[str] = None


def _iter_team_folders(root: Path, has_lines: bool):
    """Yield team folders given the wizard's structural choice."""
    if has_lines:
        for line_dir in sorted(root.iterdir()):
            if not line_dir.is_dir():
                continue
            for team_dir in sorted(line_dir.iterdir()):
                if team_dir.is_dir():
                    yield team_dir
    else:
        for team_dir in sorted(root.iterdir()):
            if team_dir.is_dir():
                yield team_dir


def _ingest_job(job_id: int, root: Path, has_lines: bool, subfolder: Optional[str]) -> None:
    """Background task: walk team folders, ingest each, track progress on Job."""
    with SessionLocal() as db:
        job = db.query(Job).get(job_id)
        if job is None:
            return
        job.ingest_status = "ingesting"
        db.commit()
        skipped: list[dict] = []
        done = 0
        try:
            team_dirs = list(_iter_team_folders(root, has_lines))
            for team_dir in team_dirs:
                image_source = team_dir
                if subfolder:
                    image_source = team_dir / subfolder
                    if not image_source.is_dir():
                        skipped.append({
                            "name": team_dir.name,
                            "reason": f"missing subfolder '{subfolder}'",
                        })
                        logger.warning(
                            "[ingest] Job %s: %s skipped — missing subfolder '%s'",
                            job_id, team_dir.name, subfolder,
                        )
                        continue

                job.ingest_current_team = team_dir.name
                db.commit()

                session = Session(
                    job_id=job_id,
                    name=team_dir.name,
                    source_path=str(team_dir.resolve()),
                    status="pending",
                )
                db.add(session)
                db.commit()
                db.refresh(session)

                count = ingest_folder(db, session.id, image_source)
                done += 1
                job.ingest_progress = done
                db.commit()
                logger.info(
                    "[ingest] Job %s team %d/%d: %s — %d images found",
                    job_id, done, len(team_dirs), team_dir.name, count,
                )

            job.ingest_status = "done"
            job.ingest_current_team = None
            # ingest_error doubles as a structured note for non-fatal skips
            # when there was no hard error (kept simple — no extra column).
            job.ingest_error = (
                json.dumps({"skipped_teams": skipped}) if skipped else None
            )
            db.commit()
        except Exception as exc:  # noqa: BLE001 — surface any failure to the UI
            logger.exception("[ingest] Job %s failed", job_id)
            job.ingest_status = "error"
            job.ingest_error = str(exc)
            job.ingest_current_team = None
            db.commit()


@router.post("")
def create_job(
    payload: CreateJobRequest, background: BackgroundTasks,
    db: DbSession = Depends(get_db),
):
    """Create the Job row, return immediately, ingest in the background.

    Folder-walking + EXIF read on a real job takes 30–60s; doing it inline
    left the user staring at a greyed button. The wizard now polls
    /jobs/{id}/ingest-status.
    """
    root = Path(payload.root_path)
    if not root.exists() or not root.is_dir():
        raise HTTPException(400, f"Folder not found: {payload.root_path}")

    # Count teams up front so the progress bar has a denominator immediately.
    team_total = sum(1 for _ in _iter_team_folders(root, payload.has_lines))

    job = Job(
        name=payload.name,
        root_path=str(root.resolve()),
        has_lines=1 if payload.has_lines else 0,
        image_subfolder_name=payload.image_subfolder_name,
        ingest_status="pending",
        ingest_progress=0,
        ingest_total=team_total,
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    background.add_task(
        _ingest_job, job.id, root, payload.has_lines, payload.image_subfolder_name,
    )

    return {"job_id": job.id, "ingest_total": team_total}


@router.get("/{job_id}/ingest-status")
def ingest_status(job_id: int, db: DbSession = Depends(get_db)):
    """Poll target for the wizard's creation progress UI."""
    job = db.query(Job).get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")

    skipped_teams: list[dict] = []
    error_msg = job.ingest_error
    if job.ingest_error:
        try:
            parsed = json.loads(job.ingest_error)
            if isinstance(parsed, dict) and "skipped_teams" in parsed:
                skipped_teams = parsed["skipped_teams"]
                error_msg = None
        except (ValueError, TypeError):
            pass

    return {
        "status": job.ingest_status,
        "progress": job.ingest_progress or 0,
        "total": job.ingest_total or 0,
        "current_team": job.ingest_current_team,
        "error": error_msg,
        "skipped_teams": skipped_teams,
    }


# ── List / detail / delete ────────────────────────────────────────────────────


@router.get("")
def list_jobs(db: DbSession = Depends(get_db), archived: bool = False):
    """List jobs. By default returns non-archived; pass `?archived=true` to
    get the archived ones instead."""
    q = db.query(Job)
    if archived:
        q = q.filter(Job.archived == 1)
    else:
        q = q.filter((Job.archived == 0) | (Job.archived.is_(None)))
    jobs = q.order_by(Job.created_at.desc()).all()
    out = []
    for j in jobs:
        image_count = 0
        reviewed_count = 0
        needs_review_count = 0
        any_unprocessed = False
        for s in j.sessions:
            image_count += len(s.images)
            if s.reviewed:
                reviewed_count += 1
            needs_review_count += sum(1 for c in s.clusters if c.needs_review)
            if s.status != "done":
                any_unprocessed = True
        out.append({
            "id": j.id,
            "name": j.name,
            "root_path": j.root_path,
            "created_at": j.created_at.isoformat() if j.created_at else None,
            "session_count": len(j.sessions),
            "image_count": image_count,
            "reviewed_count": reviewed_count,
            "needs_review_count": needs_review_count,
            "any_unprocessed": any_unprocessed,
            "archived": bool(j.archived),
            "archived_at": j.archived_at.isoformat() if j.archived_at else None,
        })
    return out


class SetArchivedRequest(BaseModel):
    archived: bool


@router.post("/{job_id}/archived")
def set_archived(
    job_id: int, payload: SetArchivedRequest,
    db: DbSession = Depends(get_db),
):
    """Toggle the soft-archive flag on a job. Doesn't delete anything; just
    hides it from the default home listing."""
    from datetime import datetime as _dt
    job = db.query(Job).get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    if payload.archived:
        job.archived = 1
        job.archived_at = _dt.utcnow()
    else:
        job.archived = 0
        job.archived_at = None
    db.commit()
    return {
        "archived": bool(job.archived),
        "archived_at": job.archived_at.isoformat() if job.archived_at else None,
    }


@router.get("/{job_id}")
def get_job(job_id: int, db: DbSession = Depends(get_db)):
    job = db.query(Job).get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")

    sessions_out = []
    for s in sorted(job.sessions, key=lambda x: x.id):
        needs_review_count = sum(1 for c in s.clusters if c.needs_review)
        sessions_out.append({
            "id": s.id,
            "name": s.name,
            "status": s.status,
            "image_count": len(s.images),
            "cluster_count": len(s.clusters),
            "needs_review_count": needs_review_count,
            "reviewed": bool(s.reviewed),
            "reviewed_at": s.reviewed_at.isoformat() if s.reviewed_at else None,
            "progress_stage": s.progress_stage,
            "progress_current": s.progress_current or 0,
            "progress_total": s.progress_total or 0,
        })

    return {
        "id": job.id,
        "name": job.name,
        "root_path": job.root_path,
        "has_lines": bool(job.has_lines),
        "image_subfolder_name": job.image_subfolder_name,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "archived": bool(job.archived),
        "archived_at": job.archived_at.isoformat() if job.archived_at else None,
        "sessions": sessions_out,
    }


@router.delete("/{job_id}")
def delete_job(job_id: int, db: DbSession = Depends(get_db)):
    job = db.query(Job).get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    db.delete(job)
    db.commit()
    return {"status": "deleted"}


# ── Run all sessions in a job ─────────────────────────────────────────────────


@router.post("/{job_id}/run-all")
def run_all(job_id: int, background: BackgroundTasks, db: DbSession = Depends(get_db)):
    job = db.query(Job).get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")

    session_ids = [s.id for s in job.sessions]

    def _run_all():
        for sid in session_ids:
            with SessionLocal() as bg_db:
                try:
                    run_pipeline(bg_db, sid)
                except Exception:
                    logger.exception("Pipeline failed for session %s", sid)

    background.add_task(_run_all)

    for s in job.sessions:
        if s.status not in ("running",):
            s.status = "running"
    db.commit()

    return {"status": "running", "session_count": len(session_ids)}


# ── Export (Section 4) ────────────────────────────────────────────────────────


VALID_MODES = {"copy", "move"}


def _safe_dirname(name: str) -> str:
    """Strip filesystem-invalid characters from a team/folder name."""
    invalid = '<>:"/\\|?*'
    cleaned = "".join(c for c in name if c not in invalid).strip().rstrip(".")
    return cleaned or "unnamed"


def _unique_path(target: Path) -> Path:
    """Return target if it doesn't exist, else target_2, target_3, ..."""
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    n = 2
    while True:
        candidate = target.with_name(f"{stem}_{n}{suffix}")
        if not candidate.exists():
            return candidate
        n += 1


class ExportJobRequest(BaseModel):
    mode: str = "copy"  # "copy" | "move"
    overwrite: bool = True


@router.post("/{job_id}/export")
def export_job(
    job_id: int, payload: ExportJobRequest, db: DbSession = Depends(get_db),
):
    if payload.mode not in VALID_MODES:
        raise HTTPException(400, f"Invalid mode: {payload.mode}")
    job = db.query(Job).get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")

    root = Path(job.root_path)
    out_root = root.parent / f"{root.name}_sorted"

    if out_root.exists():
        if not payload.overwrite:
            raise HTTPException(409, f"Output exists: {out_root}")
        shutil.rmtree(out_root)

    # "To_be_Cropped" is the directory that downstream crop/retouch tooling
    # consumes — every non-rejected image lands here.
    website_root = out_root / "To_be_Cropped"
    team_root = out_root / "Team Images"
    pano_root = out_root / "Pano Images"
    for d in (website_root, team_root, pano_root):
        d.mkdir(parents=True, exist_ok=True)

    files_copied = 0
    files_skipped_rejected = 0
    sessions_skipped: list[dict] = []
    team_count = 0

    for session in job.sessions:
        if session.status != "done":
            sessions_skipped.append({
                "name": session.name, "reason": "pipeline_not_complete",
            })
            continue

        team_count += 1
        safe_team = _safe_dirname(session.name)
        website_team = website_root / safe_team
        team_team = team_root / safe_team
        pano_team = pano_root / safe_team
        website_team.mkdir(parents=True, exist_ok=True)
        team_team.mkdir(parents=True, exist_ok=True)
        pano_team.mkdir(parents=True, exist_ok=True)

        # Pick the "best" role for each image (one image can appear in multiple
        # clusters' ImageRole rows via buddy shots). Priority for ROUTING:
        #   rejected > team > panoramic > buddy > individual.
        # rejected wins so a manually-rejected buddy doesn't sneak in. team/pano
        # win so they end up in their dedicated dirs.
        priority = {
            "rejected": 0, "team": 1, "panoramic": 2, "buddy": 3, "individual": 4,
        }
        for image in session.images:
            roles = (
                db.query(ImageRole)
                .filter_by(image_id=image.id)
                .all()
            )
            if not roles:
                continue
            best = min(roles, key=lambda r: priority.get(r.role, 99))

            if best.role == "rejected":
                files_skipped_rejected += 1
                continue

            src = Path(image.path)
            if not src.exists():
                logger.warning("Source missing for export: %s", src)
                continue

            website_dst = _unique_path(website_team / src.name)
            if payload.mode == "move":
                shutil.move(str(src), str(website_dst))
                primary_for_extra_copy = website_dst
            else:
                shutil.copy2(str(src), str(website_dst))
                primary_for_extra_copy = website_dst
            files_copied += 1

            if best.role == "team":
                extra = _unique_path(team_team / src.name)
                shutil.copy2(str(primary_for_extra_copy), str(extra))
            elif best.role == "panoramic":
                extra = _unique_path(pano_team / src.name)
                shutil.copy2(str(primary_for_extra_copy), str(extra))

    return {
        "output_path": str(out_root),
        "team_count": team_count,
        "files_copied": files_copied,
        "files_skipped_rejected": files_skipped_rejected,
        "sessions_skipped": sessions_skipped,
    }
