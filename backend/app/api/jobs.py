"""Job endpoints — multi-team shoot management.

A Job groups multiple Sessions (one per team). The wizard flow uses /peek to
inspect folders and /jobs (POST) to commit the structure once the user has
answered the walkthrough questions.

POST   /api/jobs/peek                  inspect folder contents
POST   /api/jobs                       create a job + sessions from confirmed structure
GET    /api/jobs                       list jobs (?include_archived=true to show archived)
GET    /api/jobs/{id}                  job detail (?include_archived=true for archived sessions)
POST   /api/jobs/{id}/run-all          kick off pipeline for non-archived sessions
POST   /api/jobs/{id}/export           kick off async export (archived sessions skipped)
GET    /api/jobs/{id}/export-status    poll export progress + ETA
POST   /api/jobs/{id}/archive          soft-hide a job
POST   /api/jobs/{id}/unarchive        restore an archived job
DELETE /api/jobs/{id}                  hard-delete job + cascade (incl. thumbs)
"""
import json
import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session as DbSession

from app.db import DATA_DIR, SessionLocal, get_db
from app.models.db_models import Cluster, ImageRole, Image, Job, Session
from app.api.sessions import pipeline_progress_fields
from app.services.eta import eta_seconds
from app.services.face_pipeline import run_pipeline
from app.services.ingest import RAW_EXTS, SUPPORTED_EXTS, ingest_folder

logger = logging.getLogger(__name__)

THUMB_DIR = DATA_DIR / "thumbs"

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
    auto_run: bool = True  # chain the pipeline once import finishes


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


def _ingest_job(
    job_id: int, root: Path, has_lines: bool, subfolder: Optional[str],
    auto_run: bool = True,
) -> None:
    """Background task: walk team folders, ingest each, track progress on Job.
    When auto_run, chain straight into the pipeline for every ingested
    (non-archived) session once import finishes — so 'point at folder, walk
    away' is one action while the structure checkpoint stays available if the
    caller passed auto_run=False."""
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
            session_ids = [s.id for s in job.sessions if not s.archived]
            db.commit()
        except Exception as exc:  # noqa: BLE001 — surface any failure to the UI
            logger.exception("[ingest] Job %s failed", job_id)
            job.ingest_status = "error"
            job.ingest_error = str(exc)
            job.ingest_current_team = None
            db.commit()
            return

    # Ingest succeeded. If requested, run the pipeline for each team now.
    # Fresh DB session per run (mirrors run-all) so a slow pipeline doesn't
    # hold one transaction open for the whole job.
    if auto_run:
        for sid in session_ids:
            with SessionLocal() as bg_db:
                try:
                    run_pipeline(bg_db, sid)
                except Exception:
                    logger.exception(
                        "[ingest] auto-run pipeline failed for session %s", sid,
                    )


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
        _ingest_job, job.id, root, payload.has_lines,
        payload.image_subfolder_name, payload.auto_run,
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
def list_jobs(db: DbSession = Depends(get_db), include_archived: bool = False):
    """List jobs. Archived jobs are excluded unless ?include_archived=true."""
    q = db.query(Job)
    if not include_archived:
        q = q.filter((Job.archived == 0) | (Job.archived.is_(None)))
    jobs = q.order_by(Job.created_at.desc()).all()
    out = []
    for j in jobs:
        # Stats reflect non-archived sessions only — an archived team
        # shouldn't inflate counts or block the "ready" banner.
        active = [s for s in j.sessions if not s.archived]
        image_count = 0
        reviewed_count = 0
        needs_review_count = 0
        any_unprocessed = False
        for s in active:
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
            "session_count": len(active),
            "image_count": image_count,
            "reviewed_count": reviewed_count,
            "needs_review_count": needs_review_count,
            "any_unprocessed": any_unprocessed,
            "archived": bool(j.archived),
            "archived_at": j.archived_at.isoformat() if j.archived_at else None,
        })
    return out


@router.post("/{job_id}/archive")
def archive_job(job_id: int, db: DbSession = Depends(get_db)):
    """Soft-hide a job from the default home listing. Idempotent."""
    job = db.query(Job).get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    if not job.archived:
        job.archived = 1
        job.archived_at = datetime.utcnow()
        db.commit()
    return {"status": "archived", "archived": True,
            "archived_at": job.archived_at.isoformat() if job.archived_at else None}


@router.post("/{job_id}/unarchive")
def unarchive_job(job_id: int, db: DbSession = Depends(get_db)):
    """Restore an archived job. Idempotent."""
    job = db.query(Job).get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    job.archived = 0
    job.archived_at = None
    db.commit()
    return {"status": "unarchived", "archived": False}


@router.get("/{job_id}")
def get_job(
    job_id: int, db: DbSession = Depends(get_db),
    include_archived: bool = False,
):
    job = db.query(Job).get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")

    sessions_out = []
    for s in sorted(job.sessions, key=lambda x: x.id):
        if s.archived and not include_archived:
            continue
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
            "archived": bool(s.archived),
            "archived_at": s.archived_at.isoformat() if s.archived_at else None,
            **pipeline_progress_fields(s),
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


def _delete_thumbs(image_ids: list[int]) -> None:
    """Best-effort removal of cached thumbnail files. Missing files are fine."""
    for iid in image_ids:
        try:
            (THUMB_DIR / f"{iid}.jpg").unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Could not remove thumb for image %s: %s", iid, exc)


def _purge_session_rows(db: DbSession, session) -> None:
    """ImageRole has no ORM relationship, so cascade won't reach it. Delete
    those rows + cached thumbs explicitly before the ORM cascade handles
    images/faces/clusters."""
    image_ids = [i.id for i in session.images]
    if image_ids:
        db.query(ImageRole).filter(
            ImageRole.image_id.in_(image_ids)
        ).delete(synchronize_session=False)
    _delete_thumbs(image_ids)


@router.delete("/{job_id}")
def delete_job(job_id: int, db: DbSession = Depends(get_db)):
    """Permanently delete a job: cascades to sessions, images, faces,
    clusters, image_roles, and cached thumbnails."""
    job = db.query(Job).get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    for s in list(job.sessions):
        _purge_session_rows(db, s)
    db.delete(job)  # ORM cascade: sessions → images/faces, clusters
    db.commit()
    return {"status": "deleted"}


# ── Run all sessions in a job ─────────────────────────────────────────────────


@router.post("/{job_id}/run-all")
def run_all(job_id: int, background: BackgroundTasks, db: DbSession = Depends(get_db)):
    job = db.query(Job).get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")

    # Archived sessions are skipped entirely — not run, not counted.
    session_ids = [s.id for s in job.sessions if not s.archived]

    def _run_all():
        for sid in session_ids:
            with SessionLocal() as bg_db:
                try:
                    run_pipeline(bg_db, sid)
                except Exception:
                    logger.exception("Pipeline failed for session %s", sid)

    background.add_task(_run_all)

    for s in job.sessions:
        if not s.archived and s.status not in ("running",):
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


_ROLE_PRIORITY = {
    "rejected": 0, "team": 1, "panoramic": 2, "buddy": 3, "individual": 4,
}


def _best_role(db: DbSession, image_id: int) -> Optional[str]:
    """An image can have ImageRole rows in several clusters (buddy shots).
    rejected wins (so a manually-rejected buddy can't sneak in); team/pano
    win next so they reach their dedicated dirs."""
    roles = db.query(ImageRole).filter_by(image_id=image_id).all()
    if not roles:
        return None
    return min(roles, key=lambda r: _ROLE_PRIORITY.get(r.role, 99)).role


def _exportable_sessions(job: Job):
    """(sessions_to_export, sessions_skipped[]) — archived / not-done skipped."""
    to_export, skipped = [], []
    for s in job.sessions:
        if s.archived:
            skipped.append({"name": s.name, "reason": "archived"})
        elif s.status != "done":
            skipped.append({"name": s.name, "reason": "pipeline_not_complete"})
        else:
            to_export.append(s)
    return to_export, skipped


def _count_export_files(db: DbSession, sessions) -> int:
    """Non-rejected images across the given sessions — the progress
    denominator (one primary copy/move op per image)."""
    total = 0
    for s in sessions:
        for image in s.images:
            role = _best_role(db, image.id)
            if role is not None and role != "rejected":
                total += 1
    return total


def _run_export(job_id: int, mode: str, overwrite: bool) -> None:
    """Background task: the actual copy/move, updating Job.export_* as it
    goes so the modal can show a live bar + ETA."""
    with SessionLocal() as db:
        job = db.query(Job).get(job_id)
        if job is None:
            return
        root = Path(job.root_path)
        out_root = root.parent / f"{root.name}_sorted"
        try:
            # rmtree can itself be slow over the network share — do it here,
            # not in the request, so the modal already shows "working".
            if out_root.exists() and overwrite:
                shutil.rmtree(out_root)

            website_root = out_root / "To_be_Cropped"
            team_root = out_root / "Team Images"
            pano_root = out_root / "Pano Images"
            for d in (website_root, team_root, pano_root):
                d.mkdir(parents=True, exist_ok=True)

            to_export, sessions_skipped = _exportable_sessions(job)
            files_copied = 0
            files_skipped_rejected = 0

            for session in to_export:
                job.export_current_team = session.name
                db.commit()
                safe_team = _safe_dirname(session.name)
                website_team = website_root / safe_team
                team_team = team_root / safe_team
                pano_team = pano_root / safe_team
                for d in (website_team, team_team, pano_team):
                    d.mkdir(parents=True, exist_ok=True)

                for image in session.images:
                    role = _best_role(db, image.id)
                    if role is None:
                        continue
                    if role == "rejected":
                        files_skipped_rejected += 1
                        continue
                    src = Path(image.path)
                    if not src.exists():
                        logger.warning("Source missing for export: %s", src)
                        continue

                    website_dst = _unique_path(website_team / src.name)
                    if mode == "move":
                        shutil.move(str(src), str(website_dst))
                    else:
                        shutil.copy2(str(src), str(website_dst))
                    files_copied += 1
                    if role == "team":
                        shutil.copy2(str(website_dst),
                                     str(_unique_path(team_team / src.name)))
                    elif role == "panoramic":
                        shutil.copy2(str(website_dst),
                                     str(_unique_path(pano_team / src.name)))

                    # Commit progress every 10 files (network commits aren't free).
                    if files_copied % 10 == 0:
                        job.export_progress = files_copied
                        db.commit()

                job.export_progress = files_copied
                db.commit()

            job.export_status = "done"
            job.export_current_team = None
            job.export_progress = files_copied
            job.export_result = json.dumps({
                "output_path": str(out_root),
                "team_count": len(to_export),
                "files_copied": files_copied,
                "files_skipped_rejected": files_skipped_rejected,
                "sessions_skipped": sessions_skipped,
            })
            db.commit()
            logger.info(
                "[export] Job %s done: %d files, %d teams",
                job_id, files_copied, len(to_export),
            )
        except Exception as exc:  # noqa: BLE001 — surface to the modal
            logger.exception("[export] Job %s failed", job_id)
            job.export_status = "error"
            job.export_error = str(exc)
            job.export_current_team = None
            db.commit()


@router.post("/{job_id}/export")
def export_job(
    job_id: int, payload: ExportJobRequest, background: BackgroundTasks,
    db: DbSession = Depends(get_db),
):
    """Kick off an async export. The slow copy runs in the background;
    the modal polls /export-status."""
    if payload.mode not in VALID_MODES:
        raise HTTPException(400, f"Invalid mode: {payload.mode}")
    job = db.query(Job).get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    if job.export_status == "exporting":
        raise HTTPException(409, "An export is already running for this job")

    root = Path(job.root_path)
    out_root = root.parent / f"{root.name}_sorted"
    # 409 must be synchronous so the user gets it immediately, not via polling.
    if out_root.exists() and not payload.overwrite:
        raise HTTPException(409, f"Output exists: {out_root}")

    to_export, _ = _exportable_sessions(job)
    total = _count_export_files(db, to_export)

    job.export_status = "exporting"
    job.export_progress = 0
    job.export_total = total
    job.export_current_team = None
    job.export_started_at = datetime.utcnow()
    job.export_error = None
    job.export_result = None
    db.commit()

    background.add_task(_run_export, job.id, payload.mode, payload.overwrite)
    return {"job_id": job.id, "export_total": total}


@router.get("/{job_id}/export-status")
def export_status(job_id: int, db: DbSession = Depends(get_db)):
    """Poll target for the export modal. eta_seconds is null until there's
    enough signal."""
    job = db.query(Job).get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")

    elapsed = 0.0
    if job.export_started_at:
        elapsed = (datetime.utcnow() - job.export_started_at).total_seconds()
    eta = eta_seconds(job.export_progress or 0, job.export_total or 0, elapsed)

    result = None
    if job.export_result:
        try:
            result = json.loads(job.export_result)
        except (ValueError, TypeError):
            result = None

    return {
        "status": job.export_status or "idle",
        "progress": job.export_progress or 0,
        "total": job.export_total or 0,
        "current_team": job.export_current_team,
        "error": job.export_error,
        "eta_seconds": eta,
        "elapsed_seconds": int(elapsed),
        "result": result,
    }
