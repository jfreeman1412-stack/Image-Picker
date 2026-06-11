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
import errno
import json
import logging
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
from app.models.db_models import Cluster, ImageRole, Image, Job, PlayerMembership, Session
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


class CreateShootRequest(BaseModel):
    """Phase C.2 — make an image-less 'shoot' job pre-shoot so a roster can be
    attached before any photos exist. `root_path` is an optional *future*
    location, stored as-is (NOT validated — the folder may not exist yet)."""
    name: str
    root_path: Optional[str] = None


class ImportImagesRequest(BaseModel):
    """Phase C.2 — bring images into an existing (image-less) job. Same shape as
    CreateJobRequest minus `name`; reuses the exact ingest path."""
    root_path: str
    has_lines: bool
    image_subfolder_name: Optional[str] = None
    auto_run: bool = True


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


def _find_or_create_session_for_team(
    db: DbSession, job_id: int, team_name: str, source_path: str,
) -> tuple[Session, bool]:
    """Phase 1 merge-for-clustering (2026-06-09): if the job already has a
    non-archived session whose normalize_name(name) matches this team's,
    return that session and don't create a new one. Otherwise create a
    fresh pending session and return it.

    Returns (session, was_merged) where was_merged=True iff we returned
    an existing session.

    Job-wide scope: any same-named folder anywhere in this job is a merge
    target (cross-line same-name collision risk accepted — team names are
    unique across lines in the operator's roster-driven outdoor shoots).
    Normalized matching via normalize_name() catches case / whitespace /
    dash variants between paired folders.

    Archived sessions are NOT eligible merge targets — the operator hid
    them deliberately, so a fresh ingest of the same name creates a fresh
    session rather than resurrecting the archived one.

    Source path option (a): the FIRST-encountered path wins. We never
    overwrite an existing session's source_path on merge. Confirmed
    dead-data safe — source_path is only serialized into GET
    /api/sessions/{id} JSON and the frontend doesn't read it; the
    re-ingest path (/import-images) refuses on a job with existing
    sessions, so the dropped second path was never going to be re-read.
    """
    from app.services.roster import normalize_name
    target_norm = normalize_name(team_name)
    if target_norm:
        # Linear scan; jobs have O(30) sessions in practice — microseconds.
        existing = (
            db.query(Session)
            .filter_by(job_id=job_id, archived=0)
            .all()
        )
        for s in existing:
            if normalize_name(s.name) == target_norm:
                return s, True
    new_sess = Session(
        job_id=job_id, name=team_name, source_path=source_path,
        status="pending",
    )
    db.add(new_sess); db.commit(); db.refresh(new_sess)
    return new_sess, False


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
        # Phase 1 merge-for-clustering (2026-06-09): each entry is
        # {folder_name, source_path, into_session_name} so the operator can see
        # which folders merged into which existing teams.
        merged: list[dict] = []
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

                session, was_merged = _find_or_create_session_for_team(
                    db, job_id, team_dir.name, str(team_dir.resolve()),
                )
                if was_merged:
                    merged.append({
                        "folder_name": team_dir.name,
                        "source_path": str(team_dir.resolve()),
                        "into_session_name": session.name,
                    })
                    logger.info(
                        "[ingest] Job %s: merged folder '%s' into existing "
                        "session '%s' (id=%s)",
                        job_id, team_dir.name, session.name, session.id,
                    )

                count = ingest_folder(db, session.id, image_source)
                done += 1
                # Progress counts FOLDERS visited (not unique sessions). The
                # session-count divergence after merge is cosmetic; the progress
                # bar staying accurate to what's being processed is the honest
                # signal. The merge events show up in ingest_error notes below.
                job.ingest_progress = done
                db.commit()
                logger.info(
                    "[ingest] Job %s team %d/%d: %s — %d images found%s",
                    job_id, done, len(team_dirs), team_dir.name, count,
                    " (merged)" if was_merged else "",
                )

            job.ingest_status = "done"
            job.ingest_current_team = None
            # ingest_error doubles as a structured-notes channel for non-fatal
            # events: pre-Phase-1 it held skipped_teams from missing subfolders;
            # Phase 1 adds merged_folders alongside. Both keys are optional; the
            # field stays NULL when neither happened (no-merge regression case).
            notes: dict = {}
            if skipped:
                notes["skipped_teams"] = skipped
            if merged:
                notes["merged_folders"] = merged
            job.ingest_error = json.dumps(notes) if notes else None
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


@router.post("/shoot")
def create_shoot(payload: CreateShootRequest, db: DbSession = Depends(get_db)):
    """Create an image-less job (Phase C.2). No folder, no ingest — the operator
    attaches a Player roster pre-shoot and imports images later via
    /import-images. `ingest_status="awaiting_images"` marks the deferred state;
    `root_path` (optional future location) is stored verbatim, not validated."""
    root = (payload.root_path or "").strip() or None
    job = Job(
        name=payload.name,
        root_path=root,
        ingest_status="awaiting_images",
        ingest_progress=0,
        ingest_total=0,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return {"job_id": job.id}


@router.post("/{job_id}/import-images")
def import_images(
    job_id: int, payload: ImportImagesRequest, background: BackgroundTasks,
    db: DbSession = Depends(get_db),
):
    """Ingest images into an EXISTING job (Phase C.2) — the back half of the
    wizard, run later against a job created image-less via /shoot. Reuses the
    exact `_ingest_job` background task. 404 if the job is missing; 400 if the
    folder is missing; 409 if the job already has teams (no double-ingest —
    re-import is out of scope). Returns the same shape as job creation so the
    wizard's ingest-status polling works unchanged."""
    job = db.query(Job).get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    if job.sessions:
        raise HTTPException(409, "This job already has imported teams")
    root = Path(payload.root_path)
    if not root.exists() or not root.is_dir():
        raise HTTPException(400, f"Folder not found: {payload.root_path}")

    team_total = sum(1 for _ in _iter_team_folders(root, payload.has_lines))
    job.root_path = str(root.resolve())
    job.has_lines = 1 if payload.has_lines else 0
    job.image_subfolder_name = payload.image_subfolder_name
    job.ingest_status = "pending"
    job.ingest_progress = 0
    job.ingest_total = team_total
    job.ingest_error = None
    db.commit()

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
def list_jobs(
    db: DbSession = Depends(get_db),
    include_archived: bool = False,
    stage: str | None = None,
):
    """List jobs. Archived jobs are excluded unless ?include_archived=true.

    `?stage=capture` returns the **capture-ready** slice for the mobile capture
    app: not archived, a roster attached (≥1 PlayerMembership), and no images
    imported yet (no sessions). This is derived from existing data — importing
    images (which creates sessions) drops a shoot off the list automatically, so
    the capture view self-manages without manual archiving. The desktop app
    omits `stage` and keeps seeing every non-archived job (all lifecycle stages).
    """
    if stage == "capture":
        include_archived = False  # capture view never includes archived
    q = db.query(Job)
    if not include_archived:
        q = q.filter((Job.archived == 0) | (Job.archived.is_(None)))
    jobs = q.order_by(Job.created_at.desc()).all()

    if stage == "capture":
        job_ids = [j.id for j in jobs]
        rostered: set[int] = set()
        if job_ids:
            rostered = {
                jid for (jid,) in db.query(PlayerMembership.job_id)
                .filter(PlayerMembership.job_id.in_(job_ids))
                .distinct().all()
            }
        # has a roster AND no images imported yet (no sessions at all)
        jobs = [j for j in jobs if j.id in rostered and len(j.sessions) == 0]

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


# ── Move-card Phase 2 (2026-06-08): move-targets dropdown source ────────────


@router.get("/{job_id}/move-targets")
def get_move_targets(job_id: int, db: DbSession = Depends(get_db)):
    """Return every team that's a candidate destination for a move-card move:
    union of (a) sessions in this job and (b) distinct PlayerMembership
    teams in this job. A team that has both a session AND roster rows
    collapses into ONE row with session_id populated.

    Shape: {"teams": [{"name", "norm_name", "session_id"|null, "archived"}]}
    Sorted by name (case-insensitive) for stable dropdown order.

    The frontend's smart-suggestion button now uses this list directly — a
    target with session_id=null fires Case 2 (auto-create session + move),
    a target with session_id set fires Case 1 (Phase 1's /move-with-guards).
    The "+ Add new team…" modal at the bottom of the dropdown drives Case 3.
    """
    from app.services.roster import normalize_name
    job = db.query(Job).get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")

    # Sessions first — they carry the authoritative archived/session_id state.
    by_norm: dict[str, dict] = {}
    for s in job.sessions:
        norm = normalize_name(s.name) or s.name.lower()
        by_norm[norm] = {
            "name": s.name,
            "norm_name": norm,
            "session_id": s.id,
            "archived": bool(s.archived),
        }

    # Layer roster-only teams: skip any team whose norm collides with a
    # session row (the session is the source of truth for the display name +
    # archived state).
    for m in db.query(PlayerMembership).filter_by(job_id=job_id).all():
        if m.norm_team in by_norm:
            continue
        by_norm[m.norm_team] = {
            "name": m.team_name,
            "norm_name": m.norm_team,
            "session_id": None,
            "archived": False,
        }

    teams = sorted(by_norm.values(), key=lambda t: t["name"].lower())
    return {"teams": teams}


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
    # 2026-06-02: rename each exported file after its cluster's player.
    # Default False = today's behavior (camera filenames preserved); the
    # toggle-OFF path is byte-identical to the pre-build code path.
    # When True, filenames derive from Cluster.display_label() + role
    # suffix (-t / -p) or zero-padded sequence (_001, _002, ...). Buddy
    # photos copy once per cluster that claims them, into each cluster's
    # session folder. Images with no ImageRole (truly orphan — no cluster
    # claim) keep their camera filename. See PHASE_C3 design + memory.
    rename_by_player: bool = False


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
    denominator (one primary copy/move op per image). Images with no
    ImageRole row are treated as 'individual' by the export (Issue 5
    orphans), so they're counted too — otherwise export_total would
    undercount and the progress bar wouldn't match what gets copied."""
    all_ids = [img.id for s in sessions for img in s.images]
    role_map = _best_role_map(db, all_ids)
    return sum(
        1 for image_id in all_ids
        if role_map.get(image_id) != "rejected"
    )


# Phase 7: copy workers for the export. UNC writes are I/O-bound — threads
# saturate the share with much less wall-clock time than the prior serial
# loop. Per-image work (website + optional team/pano secondary) stays in a
# single worker so progress accounting (one image = one tick) is unchanged.
_EXPORT_WORKERS = 8


def _allocate_dests(
    srcs: list[Path], target_dir: Path,
    *, basenames: list[str | None] | None = None,
) -> list[Path]:
    """Reserve non-colliding destination paths under target_dir for each
    src. The basename defaults to src.name (legacy behavior). When
    `basenames` is provided, an entry of None falls back to src.name and
    a non-None entry overrides — this is how rename-on-export injects
    derived names (Aviel-Afonya_001.jpg, Coach_5-t.jpg, etc.) while
    keeping the same suffix-on-collision (_2, _3, ...) mechanism. Resolved
    on the main thread so parallel copy workers can write to distinct
    paths without racing each other on _unique_path."""
    reserved: set[Path] = set()
    out: list[Path] = []
    for idx, src in enumerate(srcs):
        chosen = src.name
        if basenames is not None and basenames[idx] is not None:
            chosen = basenames[idx]
        candidate = target_dir / chosen
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


_COPY_RETRY_ATTEMPTS = 3        # transient SMB EINVAL retry budget
_COPY_RETRY_BACKOFF = 0.5       # seconds (linear backoff factor)


def _safe_copy2(src: Path, dst: Path) -> None:
    """shutil.copy2 with EINVAL tolerance for SMB / UNC shares.

    Real shoots write to a network share that intermittently returns
    [Errno 22] Invalid argument from `shutil.copy2`'s `copyfile` step (bytes
    write rejected) or its `copystat` step (timestamps/attrs rejected). Retry
    up to _COPY_RETRY_ATTEMPTS with a brief linear backoff; on exhaustion fall
    back to `shutil.copyfile` so the BYTES still land — metadata is
    non-essential for the customer-facing deliverable, especially for the
    secondary copy whose primary already carries the same metadata. Any
    non-EINVAL OSError raises immediately so we never mask an unrelated
    failure (missing source, permission denied, disk full, …).
    """
    last_err: OSError | None = None
    for attempt in range(_COPY_RETRY_ATTEMPTS):
        try:
            shutil.copy2(str(src), str(dst))
            return
        except OSError as exc:
            if exc.errno != errno.EINVAL:
                raise
            last_err = exc
            time.sleep(_COPY_RETRY_BACKOFF * (attempt + 1))
    try:
        shutil.copyfile(str(src), str(dst))
        logger.warning(
            "[export] copy2 EINVAL exhausted after %d attempts; "
            "copyfile (bytes-only) succeeded: %s -> %s",
            _COPY_RETRY_ATTEMPTS, src, dst,
        )
    except OSError as final:
        raise final from last_err


def _copy_one(src: Path, website_dst: Path, secondary_dst: Optional[Path],
              mode: str) -> None:
    """One image's full export. The TEAM/PANO secondary copy reads from
    website_dst rather than src, which is required for mode='move' since
    src no longer exists by then.

    Both copies go through `_safe_copy2` so transient SMB EINVAL hiccups
    retry-then-degrade-to-bytes-only instead of aborting the file.
    """
    if mode == "move":
        shutil.move(str(src), str(website_dst))
    else:
        _safe_copy2(src, website_dst)
    if secondary_dst is not None:
        _safe_copy2(website_dst, secondary_dst)


def _resolve_out_root(root: Path, destination_path: str | None) -> Path:
    """Pick the export destination: user-provided absolute path, or the
    legacy `<root>_sorted` sibling so older callers keep working."""
    if destination_path:
        return Path(destination_path)
    return root.parent / f"{root.name}_sorted"


# ── Rename-on-export helpers (2026-06-02) ────────────────────────────────────
#
# When ExportJobRequest.rename_by_player is True, each clustered image gets a
# filename derived from its cluster's display_label() + role suffix or
# sequence number. Buddy photos copy once per cluster that claims them
# (ImageRole row per cluster), each into the cluster's session folder.
# Images with no ImageRole stay as camera filenames — see memory:
# clustering-singleton-edge for Issue 5 context.


def _label_for_filename(cluster: Cluster) -> str:
    """Resolve the cluster's customer-facing label for rename-mode filenames.

    Precedence mirrors Cluster.display_label() with one extension: when the
    fallback fires (no manual, no auto), the prefix becomes Coach_<id> when
    is_coach_for_sort() returns True (honors manual_coach_override per the
    naming-errors filter rule). _safe_dirname strips filesystem-invalid
    characters from operator-typed manual labels — discipline + safety net.
    """
    manual = (cluster.manual_label or "").strip()
    if manual:
        return _safe_dirname(manual)
    auto = (cluster.auto_label or "").strip()
    if auto:
        return _safe_dirname(auto)
    prefix = "Coach" if cluster.is_coach_for_sort() else "Player"
    return f"{prefix}_{cluster.id}"


def _derive_export_basename(
    cluster: Cluster, role: str, seq_num: int, src_filename: str,
) -> str:
    """Build the rename-mode basename. `seq_num` is the per-cluster
    sequence index for individual/buddy roles (1-based, zero-padded to 3
    digits). Team and pano use role suffixes instead of sequence numbers.
    The source extension is preserved (e.g. .jpg / .png)."""
    label = _label_for_filename(cluster)
    ext = Path(src_filename).suffix
    if role == "team":
        return f"{label}-t{ext}"
    if role == "panoramic":
        return f"{label}-p{ext}"
    # individual or buddy → zero-padded 3-digit sequence
    return f"{label}_{seq_num:03d}{ext}"


def _build_rename_plan_for_session(
    db: DbSession, session: Session, role_map: dict[int, str],
) -> list[tuple[Path, str, str | None]]:
    """Produce (src_path, basename, secondary_subdir) entries for ONE session
    under rename mode. secondary_subdir is 'Team Images' or 'Pano Images'
    when role is team/panoramic, else None.

    Per-cluster iteration: each cluster in this session contributes its
    ImageRole rows (excluding rejected). Buddy images naturally produce
    multiple entries (one per cluster that claims them). Multiple team or
    pano roles in the same cluster are an error condition — log + keep
    the first, drop the rest. Images in session.images with no ImageRole
    fall through to the camera-filename orphan path.
    """
    image_by_id = {img.id: img for img in session.images}
    seen_in_rename: set[int] = set()    # image ids that got at least one
                                        # ImageRole-driven rename plan entry
    out: list[tuple[Path, str, str | None]] = []

    for cluster in session.clusters:
        # Collect this cluster's ImageRole rows (one query per cluster keeps
        # the working set small; export endpoint already iterates clusters).
        rows = (
            db.query(ImageRole.image_id, ImageRole.role)
            .filter(ImageRole.cluster_id == cluster.id)
            .all()
        )
        # Partition by role; surface multiple-team / multiple-pano errors.
        team_image_id: int | None = None
        pano_image_id: int | None = None
        seq_candidates: list[tuple[float, int, int, str]] = []
            # (capture_time_ts, image_id, role_priority, role)
        for image_id, role in rows:
            if role == "rejected":
                continue
            # 2026-06-11 reject-leak fix: also honor the GLOBAL rejected-wins
            # rule. role above is THIS cluster's ImageRole — but the same
            # image may have a 'rejected' ImageRole in another cluster
            # (buddy shot rejected via one of its cluster claims). Legacy
            # export already enforces this via _best_role_map (priority
            # {rejected: 0, ...}); the rename plan needs the same gate or
            # rejected buddies leak under the non-rejected cluster's renamed
            # basename. Matches legacy behavior: reject anywhere = skipped
            # everywhere.
            if role_map.get(image_id) == "rejected":
                continue
            img = image_by_id.get(image_id)
            if img is None:
                # Buddy from a different session — Image lives in another
                # session.images. Re-fetch.
                img = db.query(Image).get(image_id)
                if img is None:
                    continue
            if role == "team":
                if team_image_id is not None:
                    logger.warning(
                        "[export-rename] cluster %s has multiple team-role "
                        "images; keeping first (image_id=%s), skipping %s",
                        cluster.id, team_image_id, image_id,
                    )
                    continue
                team_image_id = image_id
            elif role == "panoramic":
                if pano_image_id is not None:
                    logger.warning(
                        "[export-rename] cluster %s has multiple pano-role "
                        "images; keeping first (image_id=%s), skipping %s",
                        cluster.id, pano_image_id, image_id,
                    )
                    continue
                pano_image_id = image_id
            ts = img.capture_time.timestamp() if img.capture_time else 0.0
            seq_candidates.append((ts, image_id, _ROLE_PRIORITY.get(role, 99), role))

        # Emit team + pano first (their order in the folder doesn't matter
        # since they have distinct suffixes), then individuals/buddy in
        # capture-time order with assigned sequence numbers.
        if team_image_id is not None:
            img = image_by_id.get(team_image_id) or db.query(Image).get(team_image_id)
            basename = _derive_export_basename(cluster, "team", 0, img.filename)
            out.append((Path(img.path), basename, "Team Images"))
            seen_in_rename.add(team_image_id)
        if pano_image_id is not None:
            img = image_by_id.get(pano_image_id) or db.query(Image).get(pano_image_id)
            basename = _derive_export_basename(cluster, "panoramic", 0, img.filename)
            out.append((Path(img.path), basename, "Pano Images"))
            seen_in_rename.add(pano_image_id)

        # Individuals + buddy share a sequence; sort by capture_time.
        seq_only = [
            (ts, image_id, role)
            for (ts, image_id, _, role) in seq_candidates
            if role in ("individual", "buddy")
        ]
        seq_only.sort(key=lambda r: (r[0], r[1]))   # tie-break by image_id
        for seq, (_, image_id, role) in enumerate(seq_only, start=1):
            img = image_by_id.get(image_id) or db.query(Image).get(image_id)
            basename = _derive_export_basename(cluster, role, seq, img.filename)
            out.append((Path(img.path), basename, None))
            seen_in_rename.add(image_id)

    # Orphan images: in session.images but never picked up by any cluster's
    # ImageRole iteration above. Keep camera filename.
    for img in session.images:
        if img.id in seen_in_rename:
            continue
        if role_map.get(img.id) == "rejected":
            continue
        out.append((Path(img.path), img.filename, None))

    return out


def _run_export(
    job_id: int, mode: str, overwrite: bool,
    destination_path: str | None = None,
    rename_by_player: bool = False,
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
            failures: list[dict] = []   # per-file copy failures across the run

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
                #
                # Toggle-OFF (legacy, default): iterate session.images, derive
                # role from role_map. One entry per image; secondary dest for
                # team/pano. Camera filenames preserved.
                #
                # Toggle-ON (rename-by-player, 2026-06-02): iterate this
                # session's clusters' ImageRoles. A buddy image surfaces under
                # each cluster that claims it — naturally producing one copy
                # per kid into the kid's session folder. Filenames derive from
                # cluster.display_label(). Truly-orphan images (no ImageRole)
                # fall back to camera filename.
                srcs: list[Path] = []
                roles: list[str] = []
                # For rename mode we override the per-source basename used by
                # _allocate_dests. legacy mode leaves this empty and
                # _allocate_dests uses src.name as today.
                rename_basenames: list[str | None] = []
                # Maps src str -> (secondary_subdir, role) so the threadpool
                # loop can look up the secondary destination after dests are
                # allocated. Legacy mode still uses team_dsts / pano_dsts.
                rename_secondary: dict[str, str] = {}

                if rename_by_player:
                    plan = _build_rename_plan_for_session(db, session, role_map)
                    for src, basename, secondary_subdir in plan:
                        if not src.exists():
                            logger.warning("Source missing for export: %s", src)
                            continue
                        srcs.append(src)
                        # The role tag here is only used for progress / failure
                        # bookkeeping; the actual destination subdir is encoded
                        # in rename_secondary so legacy code paths that branch
                        # on `role == "team"` stay unconfused.
                        roles.append(
                            "team" if secondary_subdir == "Team Images"
                            else "panoramic" if secondary_subdir == "Pano Images"
                            else "individual"
                        )
                        rename_basenames.append(basename)
                        if secondary_subdir:
                            rename_secondary[str(src)] = secondary_subdir
                else:
                    for image in session.images:
                        role = role_map.get(image.id)
                        if role == "rejected":
                            files_skipped_rejected += 1
                            continue
                        if role is None:
                            # Issue 5: orphan image (no cluster → no ImageRole)
                            # — still ship the photo as an individual so single-
                            # image team folders (often a lone coach shot) don't
                            # get silently dropped from the deliverable. See
                            # memory: clustering-singleton-edge.
                            role = "individual"
                        src = Path(image.path)
                        if not src.exists():
                            logger.warning("Source missing for export: %s", src)
                            continue
                        srcs.append(src)
                        roles.append(role)
                        rename_basenames.append(None)

                website_dsts = _allocate_dests(
                    srcs, website_team, basenames=rename_basenames,
                )
                # Build secondary plans. Rename mode resolves via
                # rename_secondary (per-source subdir choice); legacy mode
                # picks via role tag like today.
                if rename_by_player:
                    team_secondary_srcs = [
                        s for s in srcs
                        if rename_secondary.get(str(s)) == "Team Images"
                    ]
                    pano_secondary_srcs = [
                        s for s in srcs
                        if rename_secondary.get(str(s)) == "Pano Images"
                    ]
                    team_secondary_basenames = [
                        b for s, b in zip(srcs, rename_basenames)
                        if rename_secondary.get(str(s)) == "Team Images"
                    ]
                    pano_secondary_basenames = [
                        b for s, b in zip(srcs, rename_basenames)
                        if rename_secondary.get(str(s)) == "Pano Images"
                    ]
                    team_dsts = dict(zip(
                        [str(s) for s in team_secondary_srcs],
                        _allocate_dests(team_secondary_srcs, team_team,
                                        basenames=team_secondary_basenames),
                    ))
                    pano_dsts = dict(zip(
                        [str(s) for s in pano_secondary_srcs],
                        _allocate_dests(pano_secondary_srcs, pano_team,
                                        basenames=pano_secondary_basenames),
                    ))
                else:
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

                # Parallel copy fan-out, sequential progress accounting. Per-file
                # failures are caught + logged + skipped (B.3-style lossless): the
                # job continues across ALL sessions and reports failures at the
                # end. One bad SMB copy never aborts the run.
                with ThreadPoolExecutor(max_workers=_EXPORT_WORKERS) as ex:
                    futures_meta: dict = {}
                    for src, role, website_dst in zip(srcs, roles, website_dsts):
                        secondary = None
                        if rename_by_player:
                            sub = rename_secondary.get(str(src))
                            if sub == "Team Images":
                                secondary = team_dsts.get(str(src))
                            elif sub == "Pano Images":
                                secondary = pano_dsts.get(str(src))
                        else:
                            if role == "team":
                                secondary = team_dsts.get(str(src))
                            elif role == "panoramic":
                                secondary = pano_dsts.get(str(src))
                        fut = ex.submit(_copy_one, src, website_dst, secondary, mode)
                        futures_meta[fut] = (src, role, website_dst, secondary)
                    for fut in as_completed(futures_meta):
                        try:
                            fut.result()
                            files_copied += 1
                        except Exception as exc:  # noqa: BLE001 — caught + logged + skipped
                            src, role, wdst, sec = futures_meta[fut]
                            failures.append({
                                "session": session.name,
                                "role": role,
                                "src": str(src),
                                "website_dst": str(wdst),
                                "secondary_dst": str(sec) if sec else None,
                                "error": f"{type(exc).__name__}: {exc}",
                            })
                            logger.exception(
                                "[export] copy FAILED (session=%s, role=%s, "
                                "src=%s)", session.name, role, src,
                            )
                        # Commit progress every 10 completions (success OR fail)
                        # so the UI doesn't stall on bursts of failures.
                        if (files_copied + len(failures)) % 10 == 0:
                            job.export_progress = files_copied
                            db.commit()

                job.export_progress = files_copied
                db.commit()
                logger.info(
                    "[export] session %s: %d files copied in %.1fs",
                    session.name, files_copied - session_copied_start,
                    time.monotonic() - session_start,
                )

            job.export_current_team = None
            job.export_progress = files_copied
            result = {
                "output_path": str(out_root),
                "team_count": len(to_export),
                "files_copied": files_copied,
                "files_skipped_rejected": files_skipped_rejected,
                "files_failed": len(failures),
                "sessions_skipped": sessions_skipped,
                "failures": failures,
            }
            job.export_result = json.dumps(result)
            if failures:
                # Per-file failures DON'T abort the job (the run completed
                # across all sessions); the status surfaces "needs attention"
                # so the UI / operator notices, with the per-file detail in
                # export_result.failures.
                job.export_status = "error"
                job.export_error = (
                    f"{len(failures)} file(s) failed to copy — "
                    "see export_result.failures for details."
                )
            else:
                job.export_status = "done"
                job.export_error = None
            db.commit()
            logger.info(
                "[export] Job %s complete: %d copied, %d failed, %d teams",
                job_id, files_copied, len(failures), len(to_export),
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
        payload.destination_path, payload.rename_by_player,
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
