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

import csv
import io
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


# ── Phase C.1: mapping-aware roster upload ────────────────────────────────
# A header-aware, arbitrary-column path that sits IN FRONT OF the positional
# load above. The desktop operator maps each CSV's columns to the canonical
# fields (name + team), we validate strictly, then feed the same
# `replace_shoot_memberships` core — the positional endpoint stays untouched.
# See PHASE_C1_ROSTER_UPLOAD.md.


def inspect_roster_csv(text: str, *, sample_size: int = 5) -> dict:
    """Read an arbitrary CSV so the mapping UI can build its column dropdowns.

    Returns the first non-blank row's cells as `columns` (the candidate
    header), up to `sample_size` following rows as `sample_rows`, and
    `total_rows` (non-blank rows, the first row included). Uses the `csv`
    module so a quoted comma stays inside one cell. Pure / no DB — tests drive
    it directly. Whether row 1 is actually a header is the caller's call (the
    `has_header` toggle); this function does not decide that.
    """
    reader = csv.reader(io.StringIO(text))
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    if not rows:
        return {"columns": [], "sample_rows": [], "total_rows": 0}
    return {
        "columns": [cell.strip() for cell in rows[0]],
        "sample_rows": [list(row) for row in rows[1:1 + sample_size]],
        "total_rows": len(rows),
    }


def validate_mapping(mapping: dict) -> list[str]:
    """Pre-flight: is the column mapping complete enough to parse rows at all?

    Returns a list of human-readable problems (empty == complete). Required:
    a team column, plus a name source — for `name_mode == "full"` a name
    column, for `"split"` at least one of first/last (a last-name-only roster
    is legitimate).
    """
    errors: list[str] = []
    name_mode = mapping.get("name_mode")
    if name_mode == "full":
        if not mapping.get("name_column"):
            errors.append("name column not mapped")
    elif name_mode == "split":
        if not mapping.get("first_name_column") and not mapping.get("last_name_column"):
            errors.append("map at least one of first-name / last-name column")
    else:
        errors.append("name mode must be 'full' or 'split'")
    if not mapping.get("team_column"):
        errors.append("team column not mapped")
    return errors


def _resolve_header(records: list[list[str]], has_header: bool):
    """Return (header, enumerated_data_rows). With a header, row 1 is the
    header and data rows are numbered from 2 (matching the file/Excel row the
    operator sees). Headerless rosters get synthetic 'Column N' (1-based)
    labels and data numbered from 1. Column references in the mapping resolve
    against `header` either way."""
    if has_header:
        header = [c.strip() for c in records[0]]
        return header, list(enumerate(records[1:], start=2))
    width = max((len(r) for r in records), default=0)
    header = [f"Column {i + 1}" for i in range(width)]
    return header, list(enumerate(records, start=1))


def parse_mapped_roster(text: str, mapping: dict):
    """Apply a column mapping to an arbitrary CSV → canonical `(name, team)`
    rows + grouped per-row errors. Pure / no DB.

    Returns `(canonical_rows, row_errors)` where `row_errors` is a list of
    `{reason, label, rows}` (1-based file row numbers). A blank record (all
    cells empty) is skipped, not errored, mirroring the positional `parse_csv`.
    Unmapped columns (e.g. parent contact) are never read. Coaches need no
    special handling here — the `Coach-` flag is derived downstream from the
    assembled name.
    """
    records = list(csv.reader(io.StringIO(text)))
    if not records:
        return [], []
    has_header = bool(mapping.get("has_header", True))
    header, data = _resolve_header(records, has_header)
    idx = {name: i for i, name in enumerate(header)}

    def col(ref):
        return idx.get(ref) if ref else None

    name_mode = mapping.get("name_mode")
    team_i = col(mapping.get("team_column"))
    name_i = col(mapping.get("name_column")) if name_mode == "full" else None
    first_i = col(mapping.get("first_name_column")) if name_mode == "split" else None
    last_i = col(mapping.get("last_name_column")) if name_mode == "split" else None

    def cell(cells, i):
        return cells[i].strip() if (i is not None and i < len(cells)) else ""

    canonical: list[tuple[str, str]] = []
    missing_name: list[int] = []
    missing_team: list[int] = []
    for row_no, cells in data:
        if not any(c.strip() for c in cells):
            continue  # blank record — skipped, not an error
        if name_mode == "full":
            name = cell(cells, name_i)
        else:
            name = " ".join(p for p in (cell(cells, first_i), cell(cells, last_i)) if p)
        team = cell(cells, team_i)
        ok = True
        if not name:
            missing_name.append(row_no); ok = False
        if not team:
            missing_team.append(row_no); ok = False
        if ok:
            canonical.append((name, team))

    row_errors = []
    if missing_team:
        row_errors.append({"reason": "missing_team", "label": "missing a team",
                           "rows": missing_team})
    if missing_name:
        row_errors.append({"reason": "missing_name", "label": "missing a name",
                           "rows": missing_name})
    return canonical, row_errors


def build_validation_report(text: str, mapping: dict, *, preview_size: int = 10) -> dict:
    """Strict validation of a mapped roster. Pure / no DB — the endpoint adds
    `references_warning` (needs the job's captured references). This is the body
    returned for a dry-run and used as the 400 `detail` when a commit is
    blocked. An incomplete mapping short-circuits before any row parsing.
    """
    mapping_errors = validate_mapping(mapping)
    if mapping_errors:
        return {
            "ok": False,
            "error": "roster_validation",
            "mapping_errors": mapping_errors,
            "row_errors": [],
            "summary": {"valid_rows": 0, "invalid_rows": 0,
                        "distinct_teams": 0, "coaches": 0},
            "preview": [],
        }
    canonical, row_errors = parse_mapped_roster(text, mapping)
    invalid_rows = len({r for e in row_errors for r in e["rows"]})
    report = {
        "ok": not row_errors,
        "mapping_errors": [],
        "row_errors": row_errors,
        "summary": {
            "valid_rows": len(canonical),
            "invalid_rows": invalid_rows,
            "distinct_teams": len({normalize_name(t) for _, t in canonical}),
            "coaches": sum(1 for n, _ in canonical if is_coach_name(n)),
        },
        "preview": [
            {"name": n, "team": t, "is_coach": is_coach_name(n)}
            for n, t in canonical[:preview_size]
        ],
    }
    if row_errors:
        report["error"] = "roster_validation"
    return report
