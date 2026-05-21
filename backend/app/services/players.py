"""Phase A.1 — load a per-shoot roster CSV into the Player identity model.

This is the reference-photo system's spine: a global `Player` (one row per
unique person, deduped by normalized name across all shoots) plus per-shoot
`PlayerMembership` rows. It reuses the exact same CSV plumbing as the Phase 6
roster cross-check (`normalize_name`, `parse_csv`, `decode_bytes`,
`CsvParseError` from services/roster) — same CSV format, different tables for a
different purpose. See PHASE_A1_ROSTER_MODEL.md.

The CSV is headerless, two columns: `Player-Name,Team-Name`. The one rule A.1
adds on top of the Phase 6 format: a name starting `Coach-` flags a coach.
"""
from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy.orm import Session as DbSession

from app.models.db_models import Job, Player, PlayerMembership
from app.services.roster import normalize_name, parse_csv

logger = logging.getLogger(__name__)

COACH_PREFIX = "coach-"


def is_coach_name(raw_name: str) -> bool:
    """A roster name is a coach iff it starts with 'Coach-' (case-insensitive),
    hyphen included. 'Coachman-Lee' is NOT a coach (no hyphen after 'Coach')."""
    return raw_name.strip().lower().startswith(COACH_PREFIX)


def upsert_player(db: DbSession, raw_name: str) -> tuple[Player | None, bool]:
    """Find-or-create a Player by normalized name. Returns (player, created).
    Returns (None, False) for a row whose normalized name is empty (junk) — the
    caller skips it. Uses db.flush() so the new id is available immediately.

    `display_name` keeps the first-seen raw form, so `carter-Johanson` and a
    later `Carter-Johanson` resolve to the same Player, displaying the first.
    """
    norm = normalize_name(raw_name)
    if not norm:
        return None, False
    player = db.query(Player).filter_by(norm_name=norm).one_or_none()
    if player is not None:
        return player, False
    player = Player(norm_name=norm, display_name=raw_name.strip())
    db.add(player)
    db.flush()
    return player, True


def replace_shoot_memberships(db: DbSession, job_id: int, rows) -> dict:
    """Wipe this job's memberships, upsert Players, insert fresh memberships.
    Caller commits. Idempotent: re-running with the same rows yields the same
    membership count (not doubled) and does not duplicate Players. Does NOT
    touch other jobs' memberships or any Player's other memberships.
    """
    db.query(PlayerMembership).filter_by(job_id=job_id).delete(synchronize_session=False)
    players_created = players_existing = memberships_loaded = 0
    rows_skipped_blank_name = coaches = 0
    teams: set[str] = set()
    for raw_name, team_name in rows:
        player, created = upsert_player(db, raw_name)
        if player is None:
            rows_skipped_blank_name += 1
            continue
        players_created += int(created)
        players_existing += int(not created)
        coach = is_coach_name(raw_name)
        db.add(PlayerMembership(
            player_id=player.id, job_id=job_id,
            team_name=team_name, norm_team=normalize_name(team_name),
            is_coach=1 if coach else 0,
        ))
        memberships_loaded += 1
        coaches += int(coach)
        teams.add(normalize_name(team_name))
    db.flush()
    return {
        "players_created": players_created,
        "players_existing": players_existing,
        "memberships_loaded": memberships_loaded,
        "coaches": coaches,
        "distinct_teams": len(teams),
        "rows_skipped_blank_name": rows_skipped_blank_name,
    }


def load_shoot_roster_from_text(db: DbSession, job_id: int, text: str) -> dict:
    """Parse CSV text, atomically replace this shoot's memberships, commit,
    return the summary dict. Tests drive this directly. 404 if job missing.
    Raises CsvParseError (from parse_csv) on malformed CSV — the HTTP wrapper
    translates that to a 400.
    """
    job = db.query(Job).get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    rows, skips = parse_csv(text)
    summary = replace_shoot_memberships(db, job_id, rows)
    db.commit()
    for line_no, reason in skips:
        logger.info("[shoot roster job %s] skipped line %d: %s", job_id, line_no, reason)
    summary["entries_skipped"] = len(skips)  # malformed/blank CSV rows
    return summary
