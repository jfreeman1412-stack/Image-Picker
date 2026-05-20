"""Roster CSV ingest, normalization, and lookup.

Per-job CSV with no header, two positional columns:
  col 0 = player name (e.g. "Eleanor-Pederson", "Brooks-Cruz-Carter")
  col 1 = team name  (e.g. "10U-Black-Softball")

The name string is exactly what the photographer burns into the EXIF
Copyright tag for that player's photos, so matching is strict equality
after normalize_name() — no fuzzy matching, no Levenshtein.
"""
from __future__ import annotations

import csv
import io
import logging
import re
import unicodedata
from typing import Iterable

from sqlalchemy.orm import Session as DbSession

from app.models.db_models import RosterEntry

logger = logging.getLogger(__name__)

_NON_ALNUM = re.compile(r"[^a-z0-9]")


def normalize_name(s: str | None) -> str:
    """Lowercase, NFKD-fold, strip every non-alphanumeric character.

    Handles single + compound hyphens ("Brooks-Cruz-Carter"), case quirks
    ("carter-Johanson", "Dublin-McDOnnell"), accents, dots, underscores,
    accidental whitespace — uniformly. Same function used for names and
    teams so both sides of the equality use the same key shape.
    """
    if not s:
        return ""
    folded = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return _NON_ALNUM.sub("", folded.lower())


class CsvParseError(ValueError):
    """Malformed CSV. Carries the 1-based line number of the bad row."""

    def __init__(self, message: str, line: int):
        super().__init__(message)
        self.line = line


def parse_csv(text: str) -> tuple[list[tuple[str, str]], list[tuple[int, str]]]:
    """Parse a headerless two-column CSV.

    Returns (rows, skips):
      rows  = [(raw_name, team_name), ...] with each cell trimmed.
      skips = [(line_number_1based, reason), ...] — informational, for logs.

    Raises CsvParseError on csv.reader exceptions (e.g. unterminated quote).
    """
    rows: list[tuple[str, str]] = []
    skips: list[tuple[int, str]] = []
    reader = csv.reader(io.StringIO(text))
    try:
        for line_no, raw in enumerate(reader, start=1):
            cells = [c.strip() for c in raw]
            if not cells or all(c == "" for c in cells):
                skips.append((line_no, "blank"))
                continue
            if len(cells) != 2:
                skips.append((line_no, f"expected 2 columns, got {len(cells)}"))
                continue
            name, team = cells
            if not name:
                skips.append((line_no, "empty name"))
                continue
            if not team:
                skips.append((line_no, "empty team"))
                continue
            rows.append((name, team))
    except csv.Error as exc:
        raise CsvParseError(str(exc), line=reader.line_num) from exc
    return rows, skips


def replace_job_roster(
    db: DbSession, job_id: int, rows: Iterable[tuple[str, str]]
) -> int:
    """Wipe this job's existing roster, insert the new set. Caller commits.

    Returns count inserted. Each insert pre-computes both normalized fields
    so the hot lookup path is a single indexed query, no Python work.
    """
    db.query(RosterEntry).filter_by(job_id=job_id).delete(synchronize_session=False)
    count = 0
    for raw_name, team_name in rows:
        db.add(RosterEntry(
            job_id=job_id,
            raw_name=raw_name,
            norm_name=normalize_name(raw_name),
            team_name=team_name,
            norm_team=normalize_name(team_name),
        ))
        count += 1
    db.flush()
    return count


def lookup_expected_team(
    db: DbSession, job_id: int, name: str | None
) -> str | None:
    """Return the normalized team for the player whose name matches, or None.

    None covers every abstain case: empty input, no row found, AND ambiguous
    (same normalized name on more than one team in this job — surfaced as a
    `duplicate_names` warning at upload time; callers treat it as abstain).
    """
    norm = normalize_name(name)
    if not norm:
        return None
    rows = (
        db.query(RosterEntry)
        .filter(RosterEntry.job_id == job_id, RosterEntry.norm_name == norm)
        .all()
    )
    if not rows:
        return None
    teams = {r.norm_team for r in rows}
    if len(teams) != 1:
        return None  # ambiguous — same name on multiple teams
    return rows[0].norm_team


def build_lookup(db: DbSession, job_id: int) -> dict[str, str]:
    """Bulk-fetch this job's roster into a `{norm_name: norm_team}` dict.

    Names appearing on multiple teams are intentionally OMITTED so callers
    that do `lookup.get(norm)` get `None` for both unknown names and
    ambiguous names — both abstain cases collapse to a single check.
    Empty dict if the job has no roster, which is how Phase 6 stays a
    no-op when roster data isn't uploaded.
    """
    teams_by_name: dict[str, set[str]] = {}
    rows = (
        db.query(RosterEntry.norm_name, RosterEntry.norm_team)
        .filter(RosterEntry.job_id == job_id)
        .all()
    )
    for nn, nt in rows:
        teams_by_name.setdefault(nn, set()).add(nt)
    return {
        nn: next(iter(teams))
        for nn, teams in teams_by_name.items()
        if len(teams) == 1
    }


def get_duplicate_names(db: DbSession, job_id: int) -> list[str]:
    """Raw names that map to more than one team in this job's roster.
    Used by the upload response so the user sees them as warnings."""
    rows = (
        db.query(RosterEntry.norm_name, RosterEntry.norm_team, RosterEntry.raw_name)
        .filter(RosterEntry.job_id == job_id)
        .all()
    )
    teams_by_name: dict[str, set[str]] = {}
    raw_by_name: dict[str, str] = {}
    for nn, nt, raw in rows:
        teams_by_name.setdefault(nn, set()).add(nt)
        raw_by_name.setdefault(nn, raw)
    return sorted(raw_by_name[n] for n, teams in teams_by_name.items() if len(teams) > 1)


def decode_bytes(data: bytes) -> str:
    """Best-effort decode for an uploaded CSV.

    UTF-8 (with optional BOM) is the happy path; cp1252 catches Windows
    Excel exports that aren't explicitly UTF-8. Last resort replaces
    undecodable bytes rather than failing the whole upload — the parser
    will surface bad rows as skips with line numbers.
    """
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")
