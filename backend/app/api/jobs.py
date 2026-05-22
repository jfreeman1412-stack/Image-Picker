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
import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException
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


# ── Scan image locations across ALL team folders (wizard step 6) ──────────────


class ScanImageLocationsRequest(BaseModel):
    root_path: str
    has_lines: bool
    max_teams: int = 60  # bound the scan on a slow/large UNC share


def _count_dir_by_ext(path: Path) -> tuple[int, int, list[str]]:
    """(supported_images, raws, subdir_names) for one directory, one scandir
    pass. Uses os.scandir so the dirent type is cached — far fewer stat() round
    trips than Path.iterdir()+is_file(), which matters a lot over UNC shares."""
    imgs = raws = 0
    subdirs: list[str] = []
    try:
        with os.scandir(path) as it:
            for e in it:
                try:
                    if e.is_dir():
                        subdirs.append(e.name)
                        continue
                    if e.is_file():
                        ext = os.path.splitext(e.name)[1].lower()
                        if ext in SUPPORTED_EXTS:
                            imgs += 1
                        elif ext in RAW_EXTS:
                            raws += 1
                except OSError:
                    continue
    except (PermissionError, OSError) as exc:
        logger.warning("[scan] could not read %s: %s", path, exc)
    return imgs, raws, subdirs


@router.post("/scan-image-locations")
def scan_image_locations(payload: ScanImageLocationsRequest):
    """Aggregate where the sortable images live across EVERY team folder, so
    the wizard's 'Where are the images?' step doesn't get fooled by an
    anomalous first folder (e.g. a `_Color Swatch` calibration folder that
    sorts first but has no `Adjusted/` subfolder). Returns candidate locations
    — the team-folder root plus every subfolder name seen — each with the total
    sortable-image count and how many teams contain it, sorted best-first.
    """
    root = Path(payload.root_path)
    if not root.exists() or not root.is_dir():
        raise HTTPException(400, f"Not a directory: {payload.root_path}")

    team_dirs = list(_iter_team_folders(root, payload.has_lines))
    scanned = team_dirs[: max(1, payload.max_teams)]

    root_imgs = root_raws = root_teams = 0
    sub_imgs: dict[str, int] = {}
    sub_raws: dict[str, int] = {}
    sub_teams: dict[str, int] = {}

    for team in scanned:
        t_imgs, t_raws, subdirs = _count_dir_by_ext(team)
        root_imgs += t_imgs
        root_raws += t_raws
        if t_imgs:
            root_teams += 1
        for name in subdirs:
            s_imgs, s_raws, _ = _count_dir_by_ext(team / name)
            sub_imgs[name] = sub_imgs.get(name, 0) + s_imgs
            sub_raws[name] = sub_raws.get(name, 0) + s_raws
            sub_teams[name] = sub_teams.get(name, 0) + 1

    candidates = [{
        "subfolder": None,
        "image_count": root_imgs,
        "raw_count": root_raws,
        "teams_with": root_teams,
    }]
    for name in sub_imgs:
        candidates.append({
            "subfolder": name,
            "image_count": sub_imgs[name],
            "raw_count": sub_raws[name],
            "teams_with": sub_teams[name],
        })
    # Best first: most sortable images, then most teams covered.
    candidates.sort(key=lambda c: (c["image_count"], c["teams_with"]), reverse=True)

    return {
        "team_count": len(scanned),
        "team_total": len(team_dirs),
        "candidates": candidates,
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


class RunAllRequest(BaseModel):
    # Phase 9 follow-up safety guard: `run-all` calls `_clear_prior_results`
    # for every session, which deletes Faces, Clusters, and ImageRoles —
    # including manual_override=1 picks the user made by hand. To prevent
    # accidental wipes, callers must pass force=true if the job has any
    # existing review work (manual labels, role decisions, coach overrides,
    # or reviewed-status). The frontend hits the endpoint without force
    # first, gets a 409 + impact summary back, shows a type-the-job-name
    # confirm dialog, and only then retries with force=true.
    force: bool = False


def _runall_impact(db: DbSession, session_ids: list[int]) -> dict:
    """Count the manual review work in scope of a run-all wipe.
    Each non-zero counter is real work the user did that would be lost."""
    if not session_ids:
        return {
            "reviewed_teams": 0, "manual_labels": 0,
            "manual_coach_overrides": 0, "manual_role_decisions": 0,
        }
    reviewed_teams = (
        db.query(Session)
        .filter(Session.id.in_(session_ids), Session.reviewed == 1)
        .count()
    )
    manual_labels = (
        db.query(Cluster)
        .filter(Cluster.session_id.in_(session_ids),
                Cluster.manual_label.isnot(None))
        .count()
    )
    manual_coach = (
        db.query(Cluster)
        .filter(Cluster.session_id.in_(session_ids),
                Cluster.manual_coach_override != 0)
        .count()
    )
    manual_roles = (
        db.query(ImageRole)
        .join(Image, ImageRole.image_id == Image.id)
        .filter(Image.session_id.in_(session_ids),
                ImageRole.manual_override == 1)
        .count()
    )
    return {
        "reviewed_teams": reviewed_teams,
        "manual_labels": manual_labels,
        "manual_coach_overrides": manual_coach,
        "manual_role_decisions": manual_roles,
    }


@router.post("/{job_id}/run-all")
def run_all(
    job_id: int,
    background: BackgroundTasks,
    payload: RunAllRequest | None = Body(default=None),
    db: DbSession = Depends(get_db),
):
    job = db.query(Job).get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    payload = payload or RunAllRequest()

    # Archived sessions are skipped entirely — not run, not counted.
    session_ids = [s.id for s in job.sessions if not s.archived]

    if not payload.force:
        impact = _runall_impact(db, session_ids)
        if any(impact.values()):
            # 409 with the impact dict so the UI can surface exact counts
            # in its confirm dialog — "X reviewed teams + Y manual picks
            # will be deleted, type the job name to proceed".
            raise HTTPException(409, detail={
                "error": "destructive_run_all",
                "message": (
                    "Running the pipeline on all teams deletes existing "
                    "clusters, role decisions, and manual labels for "
                    "every team in this job."
                ),
                "impact": impact,
                "job_name": job.name,
            })

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
    # Phase 9: user-pickable export destination. Absolute path (UNC ok).
    # When None, falls back to the legacy `<root>_sorted` sibling layout
    # so existing callers / wizard runs continue to work unchanged.
    destination_path: str | None = None


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


def _best_role_map(db: DbSession, image_ids: list[int]) -> dict[int, str]:
    """Bulk version of _best_role: one SQL fetch for all ImageRole rows,
    then resolve the priority winner per image in Python. Replaces N+1
    queries during export — for ~2,000 images that's a ~4,000× cut in
    serial DB latency."""
    if not image_ids:
        return {}
    rows = (
        db.query(ImageRole.image_id, ImageRole.role)
        .filter(ImageRole.image_id.in_(image_ids))
        .all()
    )
    by_image: dict[int, list[str]] = {}
    for image_id, role in rows:
        by_image.setdefault(image_id, []).append(role)
    return {
        image_id: min(roles, key=lambda r: _ROLE_PRIORITY.get(r, 99))
        for image_id, roles in by_image.items()
    }


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
    all_ids = [img.id for s in sessions for img in s.images]
    role_map = _best_role_map(db, all_ids)
    return sum(
        1 for r in role_map.values() if r is not None and r != "rejected"
    )


# Phase 7: copy workers for the export. UNC writes are I/O-bound — threads
# saturate the share with much less wall-clock time than the prior serial
# loop. Per-image work (website + optional team/pano secondary) stays in a
# single worker so progress accounting (one image = one tick) is unchanged.
_EXPORT_WORKERS = 8


def _allocate_dests(srcs: list[Path], target_dir: Path) -> list[Path]:
    """Reserve non-colliding destination paths under target_dir for each
    src.name. Resolved in the main thread so parallel copy workers can
    write to distinct paths without racing each other on _unique_path."""
    reserved: set[Path] = set()
    out: list[Path] = []
    for src in srcs:
        candidate = target_dir / src.name
        if candidate in reserved or candidate.exists():
            stem, suffix = candidate.stem, candidate.suffix
            n = 2
            while True:
                candidate = target_dir / f"{stem}_{n}{suffix}"
                if candidate not in reserved and not candidate.exists():
                    break
                n += 1
        reserved.add(candidate)
        out.append(candidate)
    return out


def _clear_out_root(out_root: Path) -> None:
    """Make out_root empty without paying the UNC rmtree cost up front.

    - Missing → nothing to do.
    - Empty   → reuse as-is (no rename, no delete).
    - Has content → rename to a `.deleting-<timestamp>` sidecar (a sibling
      path; rename is near-instant on the same volume), then `shutil.rmtree`
      the sidecar from a daemon thread so the export can start immediately.
      Background failures are logged, not raised — even a partial cleanup
      doesn't compromise the export, since the sidecar is no longer on the
      live output path.
    """
    if not out_root.exists():
        return
    try:
        next(out_root.iterdir())  # has at least one entry
    except StopIteration:
        return                    # empty — leave it alone

    stale = out_root.parent / (
        f"{out_root.name}.deleting-{datetime.utcnow():%Y%m%d-%H%M%S-%f}"
    )
    out_root.rename(stale)
    logger.info("[export] queued background cleanup of %s", stale)

    def _bg_delete() -> None:
        try:
            shutil.rmtree(stale, ignore_errors=True)
        except Exception:  # noqa: BLE001 — background, swallow + log
            logger.exception("[export] background cleanup failed for %s", stale)

    threading.Thread(target=_bg_delete, daemon=True,
                     name=f"export-cleanup-{stale.name}").start()


def _copy_one(src: Path, website_dst: Path, secondary_dst: Optional[Path],
              mode: str) -> None:
    """One image's full export. The TEAM/PANO secondary copy reads from
    website_dst rather than src, which is required for mode='move' since
    src no longer exists by then."""
    if mode == "move":
        shutil.move(str(src), str(website_dst))
    else:
        shutil.copy2(str(src), str(website_dst))
    if secondary_dst is not None:
        shutil.copy2(str(website_dst), str(secondary_dst))


def _resolve_out_root(root: Path, destination_path: str | None) -> Path:
    """Pick the export destination: user-provided absolute path, or the
    legacy `<root>_sorted` sibling so older callers keep working."""
    if destination_path:
        return Path(destination_path)
    return root.parent / f"{root.name}_sorted"


def _run_export(
    job_id: int, mode: str, overwrite: bool,
    destination_path: str | None = None,
) -> None:
    """Background task: the actual copy/move, updating Job.export_* as it
    goes so the modal can show a live bar + ETA."""
    with SessionLocal() as db:
        job = db.query(Job).get(job_id)
        if job is None:
            return
        root = Path(job.root_path)
        out_root = _resolve_out_root(root, destination_path)
        try:
            # rmtree is slow over UNC — instead rename any stale out_root to
            # a sidecar and delete it in a background thread so the export
            # starts copying immediately.
            if overwrite:
                _clear_out_root(out_root)

            website_root = out_root / "To_be_Cropped"
            team_root = out_root / "Team Images"
            pano_root = out_root / "Pano Images"
            for d in (website_root, team_root, pano_root):
                d.mkdir(parents=True, exist_ok=True)

            to_export, sessions_skipped = _exportable_sessions(job)

            # One bulk SQL fetch for every image's best role — replaces the
            # per-image _best_role calls that turned into N+1 queries.
            role_map = _best_role_map(
                db, [img.id for s in to_export for img in s.images],
            )
            files_copied = 0
            files_skipped_rejected = 0

            for session in to_export:
                job.export_current_team = session.name
                db.commit()
                session_start = time.monotonic()
                session_copied_start = files_copied
                safe_team = _safe_dirname(session.name)
                website_team = website_root / safe_team
                team_team = team_root / safe_team
                pano_team = pano_root / safe_team
                for d in (website_team, team_team, pano_team):
                    d.mkdir(parents=True, exist_ok=True)

                # Build copy plans on the main thread so destination paths are
                # reserved sequentially (no race between workers on _unique_path).
                srcs: list[Path] = []
                roles: list[str] = []
                for image in session.images:
                    role = role_map.get(image.id)
                    if role is None:
                        continue
                    if role == "rejected":
                        files_skipped_rejected += 1
                        continue
                    src = Path(image.path)
                    if not src.exists():
                        logger.warning("Source missing for export: %s", src)
                        continue
                    srcs.append(src)
                    roles.append(role)

                website_dsts = _allocate_dests(srcs, website_team)
                team_srcs = [s for s, r in zip(srcs, roles) if r == "team"]
                pano_srcs = [s for s, r in zip(srcs, roles) if r == "panoramic"]
                team_dsts = dict(zip(
                    [str(s) for s in team_srcs],
                    _allocate_dests(team_srcs, team_team),
                ))
                pano_dsts = dict(zip(
                    [str(s) for s in pano_srcs],
                    _allocate_dests(pano_srcs, pano_team),
                ))

                # Parallel copy fan-out, sequential progress accounting.
                with ThreadPoolExecutor(max_workers=_EXPORT_WORKERS) as ex:
                    futures = []
                    for src, role, website_dst in zip(srcs, roles, website_dsts):
                        secondary = None
                        if role == "team":
                            secondary = team_dsts.get(str(src))
                        elif role == "panoramic":
                            secondary = pano_dsts.get(str(src))
                        futures.append(
                            ex.submit(_copy_one, src, website_dst, secondary, mode)
                        )
                    for fut in as_completed(futures):
                        fut.result()  # re-raise any worker exception
                        files_copied += 1
                        # Commit progress every 10 files (network commits aren't free).
                        if files_copied % 10 == 0:
                            job.export_progress = files_copied
                            db.commit()

                job.export_progress = files_copied
                db.commit()
                logger.info(
                    "[export] session %s: %d files copied in %.1fs",
                    session.name, files_copied - session_copied_start,
                    time.monotonic() - session_start,
                )

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
    out_root = _resolve_out_root(root, payload.destination_path)
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

    background.add_task(
        _run_export, job.id, payload.mode, payload.overwrite,
        payload.destination_path,
    )
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
