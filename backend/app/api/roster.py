"""Roster CSV endpoints (per-job).

POST   /api/jobs/{job_id}/roster    upload (multipart CSV) — replaces existing
GET    /api/jobs/{job_id}/roster    list entries + summary + warnings
DELETE /api/jobs/{job_id}/roster    clear roster for this job
"""
import logging

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session as DbSession

from app.db import get_db
from app.models.db_models import Job, RosterEntry, Session
from app.services.roster import (
    CsvParseError, decode_bytes, get_duplicate_names, normalize_name,
    parse_csv, replace_job_roster,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _warnings_for(db: DbSession, job_id: int) -> dict:
    """Compute upload warnings against the current roster + sessions.

    `unmatched_csv_teams`: teams in the roster that don't match any session
    name in this job (likely "wrong CSV uploaded").
    `sessions_missing_from_roster`: sessions in the job with no roster row
    pointing at them (some teams won't get flag coverage).
    `duplicate_names`: names on more than one team in this job's roster
    (lookup will abstain on these).
    """
    roster_rows = (
        db.query(RosterEntry.team_name, RosterEntry.norm_team)
        .filter_by(job_id=job_id)
        .distinct()
        .all()
    )
    session_pairs = [
        (s.name, normalize_name(s.name))
        for s in db.query(Session).filter_by(job_id=job_id).all()
    ]
    session_norms = {n for _, n in session_pairs}
    roster_norms = {n for _, n in roster_rows}

    unmatched_csv_teams = sorted({
        raw for raw, norm in roster_rows if norm not in session_norms
    })
    sessions_missing = sorted({
        raw for raw, norm in session_pairs if norm not in roster_norms
    })
    return {
        "unmatched_csv_teams": unmatched_csv_teams,
        "sessions_missing_from_roster": sessions_missing,
        "duplicate_names": get_duplicate_names(db, job_id),
    }


def load_roster_from_text(db: DbSession, job_id: int, text: str) -> dict:
    """Parse the given CSV text, atomically replace this job's roster, and
    return the upload-response dict. Tests drive this directly to avoid
    standing up multipart/HTTP. Raises CsvParseError on malformed CSV.
    """
    job = db.query(Job).get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    rows, skips = parse_csv(text)
    inserted = replace_job_roster(db, job_id, rows)
    db.commit()
    for line_no, reason in skips:
        logger.info("[roster job %s] skipped line %d: %s", job_id, line_no, reason)
    distinct_teams = len({normalize_name(team) for _, team in rows})
    return {
        "entries_loaded": inserted,
        "entries_skipped": len(skips),
        "distinct_teams": distinct_teams,
        "warnings": _warnings_for(db, job_id),
    }


@router.post("/{job_id}/roster")
async def upload_roster(
    job_id: int,
    file: UploadFile = File(...),
    db: DbSession = Depends(get_db),
):
    data = await file.read()
    text = decode_bytes(data)
    try:
        return load_roster_from_text(db, job_id, text)
    except CsvParseError as exc:
        raise HTTPException(400, detail={
            "error": "csv_parse",
            "line": exc.line,
            "message": str(exc),
        })


@router.get("/{job_id}/roster")
def get_roster(job_id: int, db: DbSession = Depends(get_db)):
    job = db.query(Job).get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    entries = (
        db.query(RosterEntry)
        .filter_by(job_id=job_id)
        .order_by(RosterEntry.id.asc())
        .all()
    )
    return {
        "entries_loaded": len(entries),
        "entries": [
            {"id": e.id, "name": e.raw_name, "team": e.team_name}
            for e in entries
        ],
        "distinct_teams": len({e.norm_team for e in entries}),
        "warnings": _warnings_for(db, job_id) if entries else {
            "unmatched_csv_teams": [],
            "sessions_missing_from_roster": [],
            "duplicate_names": [],
        },
    }


@router.delete("/{job_id}/roster")
def delete_roster(job_id: int, db: DbSession = Depends(get_db)):
    job = db.query(Job).get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    n = db.query(RosterEntry).filter_by(job_id=job_id).delete()
    db.commit()
    return {"deleted": n}
