"""Families from Sytist into a passcode job's roster (2026-10-08).

A job links to Sytist booking events and/or galleries (`Job.sytist_sync_sources`).
A sync reads every family from those sources (read-only, see sytist_db) and
stores them in `sytist_families`, keyed by (job, source, source_id) so a
re-sync updates instead of duplicating, and a cancelled booking drops out.

Families carry no team (the sign-up forms don't ask), so they're matched to
roster players by name. A match fills the membership's blank parent contact;
contact that came from the roster upload is never overwritten. Families with
no roster match are listed so the operator can add them with a team.
"""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy.orm import Session as DbSession

from app.models.db_models import Job, PlayerMembership, SytistFamily
from app.services.roster import normalize_name
from app.services.sytist_db import SytistSource

CONTACT_FIELDS = (
    "subject_first_name", "subject_last_name",
    "parent_first_name", "parent_last_name", "parent_email", "parent_phone",
)
SOURCE_ORDER = {"booking": 0, "preregister": 1}

# Tests replace this with a fake source factory.
source_factory = SytistSource


def get_source(db: DbSession):
    return source_factory(db)


def get_sources(job: Job) -> dict:
    raw = {}
    if job.sytist_sync_sources:
        try:
            raw = json.loads(job.sytist_sync_sources) or {}
        except (ValueError, TypeError):
            raw = {}
    return {
        "booking_event_ids": [int(x) for x in raw.get("booking_event_ids") or []],
        "gallery_ids": [int(x) for x in raw.get("gallery_ids") or []],
    }


def set_sources(job: Job, booking_event_ids, gallery_ids) -> dict:
    job.sytist_sync_sources = json.dumps({
        "booking_event_ids": sorted({int(x) for x in booking_event_ids or []}),
        "gallery_ids": sorted({int(x) for x in gallery_ids or []}),
    })
    return get_sources(job)


def has_sources(job: Job) -> bool:
    s = get_sources(job)
    return bool(s["booking_event_ids"] or s["gallery_ids"])


def _clean(v) -> str | None:
    v = (str(v).strip() if v is not None else "")
    return v or None


def store_families(db: DbSession, job_id: int, families: list[dict]) -> dict:
    """Upsert the fetched families and drop ones no longer in Sytist.
    Caller commits."""
    existing = {
        (f.source, f.source_id): f
        for f in db.query(SytistFamily).filter_by(job_id=job_id).all()
    }
    seen: set[tuple[str, str]] = set()
    added = updated = 0
    now = datetime.utcnow()
    for fam in families:
        key = (fam["source"], str(fam["source_id"]))
        if key in seen:
            continue
        seen.add(key)
        values = {f: _clean(fam.get(f)) for f in CONTACT_FIELDS}
        norm = normalize_name(
            f"{values['subject_first_name'] or ''}{values['subject_last_name'] or ''}")
        row = existing.get(key)
        if row is None:
            db.add(SytistFamily(job_id=job_id, source=key[0], source_id=key[1],
                                norm_name=norm, synced_at=now, **values))
            added += 1
        else:
            changed = row.norm_name != norm or any(
                getattr(row, f) != v for f, v in values.items())
            for f, v in values.items():
                setattr(row, f, v)
            row.norm_name = norm
            row.synced_at = now
            updated += int(changed)
    removed = 0
    for key, row in existing.items():
        if key not in seen:
            db.delete(row)
            removed += 1
    db.flush()
    return {"families": len(seen), "added": added, "updated": updated,
            "removed": removed}


def apply_families(db: DbSession, job_id: int) -> dict:
    """Fill blank contact on roster memberships from matching families.
    Caller commits. Returns matched/unmatched counts and the unmatched list."""
    families = db.query(SytistFamily).filter_by(job_id=job_id).all()
    families.sort(key=lambda f: (SOURCE_ORDER.get(f.source, 9), f.id))
    by_name: dict[str, list[SytistFamily]] = {}
    for f in families:
        by_name.setdefault(f.norm_name, []).append(f)

    memberships = db.query(PlayerMembership).filter_by(job_id=job_id).all()
    roster_names: set[str] = set()
    filled = 0
    for m in memberships:
        norm = m.player.norm_name
        roster_names.add(norm)
        fams = by_name.get(norm) if norm else None
        if not fams:
            continue
        touched = False
        for field in CONTACT_FIELDS:
            if getattr(m, field):
                continue
            value = next((getattr(f, field) for f in fams if getattr(f, field)), None)
            if value:
                setattr(m, field, value)
                touched = True
        filled += int(touched)
    db.flush()

    # One entry per unmatched player (a kid booked and pre-registered shows
    # once); families with no player name each show on their own.
    unmatched = []
    for norm, fams in by_name.items():
        if not norm:
            unmatched.extend(family_dict(f) for f in fams)
        elif norm not in roster_names:
            unmatched.append(family_dict(fams[0], also=len(fams) - 1))
    matched_players = sum(1 for n in by_name if n and n in roster_names)
    return {
        "matched_players": matched_players,
        "memberships_filled": filled,
        "unmatched": unmatched,
    }


def family_dict(f: SytistFamily, also: int = 0) -> dict:
    return {
        "id": f.id, "source": f.source, "source_id": f.source_id,
        "player": " ".join(x for x in (f.subject_first_name, f.subject_last_name) if x),
        "parent": " ".join(x for x in (f.parent_first_name, f.parent_last_name) if x),
        "parent_email": f.parent_email, "parent_phone": f.parent_phone,
        "other_signups": also,
    }


def sync_job(db: DbSession, job_id: int) -> dict:
    """Pull this job's families from Sytist and apply them. Caller commits.
    Raises SytistDbError when Sytist can't be reached."""
    job = db.query(Job).get(job_id)
    sources = get_sources(job)
    fetched = get_source(db).families(
        sources["booking_event_ids"], sources["gallery_ids"])
    stored = store_families(db, job_id, fetched)
    applied = apply_families(db, job_id)
    return {**stored, **applied, "synced_at": datetime.utcnow().isoformat()}
