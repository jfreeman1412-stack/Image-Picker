"""Sytist connection, family sync and final-folder CSV (2026-10-08).

    GET  /api/sytist/settings                      connection settings (no password)
    PUT  /api/sytist/settings                      save them (blank password keeps it)
    POST /api/sytist/test                          try the connection
    GET  /api/sytist/booking-events                recent booking-calendar photo days
    GET  /api/sytist/galleries?q=                  galleries, with pre-registration counts
    GET  /api/sytist/jobs/{job_id}                 this job's sources + synced families
    PUT  /api/sytist/jobs/{job_id}/sources         link booking events / galleries
    POST /api/sytist/jobs/{job_id}/sync            pull families now
    POST /api/sytist/jobs/{job_id}/families/{id}/add   put an unmatched family on a team
    POST /api/sytist/jobs/{job_id}/csv-from-folder build the import CSV from the final folder

Everything that reads Sytist is read-only (see services.sytist_db).
"""
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session as DbSession

from app.db import get_db
from app.models.db_models import Job, SytistFamily
from app.services.players import add_walkup_player
from app.services.roster import normalize_name
from app.services.sytist_db import SytistDbError, public_config, save_config
from app.services.sytist_passcodes import build_csv_from_folder, ensure_job_passcodes
from app.services import sytist_sync

router = APIRouter()


def _job(db: DbSession, job_id: int) -> Job:
    job = db.query(Job).get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return job


def _sytist_error(exc: SytistDbError):
    return HTTPException(502, detail={"error": "sytist_unreachable", "message": str(exc)})


class SytistSettings(BaseModel):
    host: str | None = None
    port: int | None = None
    user: str | None = None
    password: str | None = None
    database: str | None = None


@router.get("/settings")
def get_settings(db: DbSession = Depends(get_db)):
    return public_config(db)


@router.put("/settings")
def put_settings(payload: SytistSettings, db: DbSession = Depends(get_db)):
    out = save_config(db, payload.model_dump())
    db.commit()
    return out


@router.post("/test")
def test_connection(db: DbSession = Depends(get_db)):
    try:
        return sytist_sync.get_source(db).ping()
    except SytistDbError as exc:
        raise _sytist_error(exc)


@router.get("/booking-events")
def booking_events(db: DbSession = Depends(get_db)):
    try:
        return {"items": sytist_sync.get_source(db).booking_events()}
    except SytistDbError as exc:
        raise _sytist_error(exc)


@router.get("/galleries")
def galleries(q: str = "", db: DbSession = Depends(get_db)):
    try:
        return {"items": sytist_sync.get_source(db).galleries(q)}
    except SytistDbError as exc:
        raise _sytist_error(exc)


@router.get("/jobs/{job_id}")
def job_sync_state(job_id: int, db: DbSession = Depends(get_db)):
    job = _job(db, job_id)
    applied = sytist_sync.apply_families(db, job_id)
    db.commit()
    last = (db.query(SytistFamily.synced_at).filter_by(job_id=job_id)
            .order_by(SytistFamily.synced_at.desc()).first())
    return {
        "sources": sytist_sync.get_sources(job),
        "families": db.query(SytistFamily).filter_by(job_id=job_id).count(),
        "last_synced_at": last[0].isoformat() if last else None,
        **applied,
    }


class SourcesRequest(BaseModel):
    booking_event_ids: list[int] = []
    gallery_ids: list[int] = []


@router.put("/jobs/{job_id}/sources")
def put_sources(job_id: int, payload: SourcesRequest, db: DbSession = Depends(get_db)):
    job = _job(db, job_id)
    sources = sytist_sync.set_sources(job, payload.booking_event_ids, payload.gallery_ids)
    db.commit()
    return {"sources": sources}


@router.post("/jobs/{job_id}/sync")
def sync(job_id: int, db: DbSession = Depends(get_db)):
    job = _job(db, job_id)
    if not sytist_sync.has_sources(job):
        raise HTTPException(400, detail={
            "error": "no_sources",
            "message": "Pick a booking event or gallery for this job first."})
    try:
        result = sytist_sync.sync_job(db, job_id)
    except SytistDbError as exc:
        db.rollback()
        raise _sytist_error(exc)
    db.commit()
    return result


class AddFamilyRequest(BaseModel):
    team: str
    name: str | None = None


@router.post("/jobs/{job_id}/families/{family_id}/add")
def add_family(job_id: int, family_id: int, payload: AddFamilyRequest,
               db: DbSession = Depends(get_db)):
    job = _job(db, job_id)
    fam = db.query(SytistFamily).filter_by(id=family_id, job_id=job_id).one_or_none()
    if fam is None:
        raise HTTPException(404, "Family not found")
    team = (payload.team or "").strip()
    name = (payload.name or " ".join(
        x for x in (fam.subject_first_name, fam.subject_last_name) if x)).strip()
    if not team:
        raise HTTPException(400, detail={"error": "missing_team", "message": "Team is required."})
    if not name:
        raise HTTPException(400, detail={
            "error": "missing_name", "message": "This sign-up has no player name. Type one in."})
    try:
        result = add_walkup_player(db, job_id, name, team)
    except ValueError as exc:
        raise HTTPException(400, detail={"error": "invalid_name", "message": str(exc)})
    if payload.name:
        # A typed name: tie the family to it so the contact lands there.
        fam.norm_name = normalize_name(payload.name)
    sytist_sync.apply_families(db, job_id)
    if job.sytist_passcodes:
        ensure_job_passcodes(db, job_id)
    db.commit()
    return result


class FolderRequest(BaseModel):
    folder: str


@router.post("/jobs/{job_id}/csv-from-folder")
def csv_from_folder(job_id: int, payload: FolderRequest, db: DbSession = Depends(get_db)):
    job = _job(db, job_id)
    if not job.sytist_passcodes:
        raise HTTPException(400, detail={
            "error": "passcodes_off", "message": "Sytist passcodes are off for this job."})
    folder = Path((payload.folder or "").strip().strip('"'))
    try:
        return build_csv_from_folder(db, job_id, folder, job.sytist_manifest)
    except FileNotFoundError as exc:
        raise HTTPException(404, detail={"error": "folder_not_found", "message": str(exc)})
    except ValueError as exc:
        raise HTTPException(400, detail={"error": "no_export", "message": str(exc)})
