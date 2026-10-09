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
from app.services.players import upsert_player
from app.services.roster import normalize_name
from app.services.sytist_db import SytistSource
from app.services.sytist_passcodes import ensure_job_passcodes

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
        "booking_dates": [str(x) for x in raw.get("booking_dates") or []],
        "gallery_ids": [int(x) for x in raw.get("gallery_ids") or []],
        # The roster IS the sign-ups (a booking-calendar job): sign-ups not
        # on the roster are added to it, with no team, on every sync.
        "auto_add": bool(raw.get("auto_add")),
    }


def set_sources(job: Job, booking_event_ids, gallery_ids, booking_dates=(),
                auto_add: bool = False) -> dict:
    job.sytist_sync_sources = json.dumps({
        "booking_event_ids": sorted({int(x) for x in booking_event_ids or []}),
        "booking_dates": sorted({str(x) for x in booking_dates or []}),
        "gallery_ids": sorted({int(x) for x in gallery_ids or []}),
        "auto_add": bool(auto_add),
    })
    return get_sources(job)


def has_sources(job: Job) -> bool:
    s = get_sources(job)
    return bool(s["booking_event_ids"] or s["booking_dates"] or s["gallery_ids"])


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
        values["booked_at"] = _clean(fam.get("booked_at"))
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


def booked_at_by_name(db: DbSession, job_id: int) -> dict[str, str]:
    """Each synced kid's earliest booking slot, by normalized name."""
    out: dict[str, str] = {}
    rows = (db.query(SytistFamily.norm_name, SytistFamily.booked_at)
            .filter(SytistFamily.job_id == job_id, SytistFamily.booked_at.isnot(None)).all())
    for norm, when in rows:
        if norm and (norm not in out or when < out[norm]):
            out[norm] = when
    return out


def family_dict(f: SytistFamily, also: int = 0) -> dict:
    return {
        "id": f.id, "source": f.source, "source_id": f.source_id,
        "player": " ".join(x for x in (f.subject_first_name, f.subject_last_name) if x),
        "parent": " ".join(x for x in (f.parent_first_name, f.parent_last_name) if x),
        "parent_email": f.parent_email, "parent_phone": f.parent_phone,
        "other_signups": also,
    }


def add_unmatched_to_roster(db: DbSession, job_id: int) -> int:
    """Booking-calendar jobs: put every named sign-up that isn't on the
    roster yet onto it with no team (the forms don't ask for one; export
    takes the team from the folder the kid's photos are sorted into).
    Idempotent. Caller commits. Returns how many players were added."""
    on_roster = {
        m.player.norm_name
        for m in db.query(PlayerMembership).filter_by(job_id=job_id).all()
    }
    families = db.query(SytistFamily).filter_by(job_id=job_id).all()
    families.sort(key=lambda f: (SOURCE_ORDER.get(f.source, 9), f.id))
    added = 0
    for f in families:
        if not f.norm_name or f.norm_name in on_roster:
            continue
        name = " ".join(x for x in (f.subject_first_name, f.subject_last_name) if x)
        player, _ = upsert_player(db, name)
        if player is None:
            continue
        db.add(PlayerMembership(player_id=player.id, job_id=job_id,
                                team_name="", norm_team="", is_coach=0))
        on_roster.add(f.norm_name)
        added += 1
    db.flush()
    return added


def sync_job(db: DbSession, job_id: int) -> dict:
    """Pull this job's families from Sytist and apply them. Caller commits.
    Raises SytistDbError when Sytist can't be reached."""
    job = db.query(Job).get(job_id)
    sources = get_sources(job)
    fetched = get_source(db).families(
        sources["booking_event_ids"], sources["gallery_ids"], sources["booking_dates"])
    stored = store_families(db, job_id, fetched)
    roster_added = add_unmatched_to_roster(db, job_id) if sources["auto_add"] else 0
    applied = apply_families(db, job_id)
    # New sign-ups need codes, and new contact can put siblings on one
    # family code.
    if job.sytist_passcodes:
        ensure_job_passcodes(db, job_id)
    return {**stored, **applied, "roster_added": roster_added,
            "synced_at": datetime.utcnow().isoformat()}
