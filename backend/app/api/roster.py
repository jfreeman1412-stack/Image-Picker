"""Roster CSV endpoints (per-job).

POST   /api/jobs/{job_id}/roster    upload (multipart CSV) — replaces existing
GET    /api/jobs/{job_id}/roster    list entries + summary + warnings
DELETE /api/jobs/{job_id}/roster    clear roster for this job
"""
import logging

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session as DbSession

from app.db import get_db
from app.models.db_models import Cluster, Face, Job, RosterEntry, Session
from app.services.roster import (
    CsvParseError, build_lookup, build_lookup_from_memberships, decode_bytes,
    get_duplicate_names, membership_raw_team_by_norm, normalize_name, parse_csv,
    replace_job_roster,
)
from app.services.roster_check import cluster_is_mismatched, session_norm_team

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
    # Effective norm: respect roster_team_alias (Phase 6.1 mapping) so a
    # session the user has already mapped to a CSV team isn't flagged as
    # "missing from roster" — it isn't, it's just named differently.
    session_pairs = [
        (s.name, normalize_name(s.roster_team_alias or s.name))
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
    # CSV teams (raw, deduped + sorted) — drive the mapping dropdown.
    seen: set[str] = set()
    csv_teams: list[str] = []
    for e in entries:
        if e.norm_team not in seen:
            seen.add(e.norm_team)
            csv_teams.append(e.team_name)
    csv_teams.sort(key=str.lower)

    sessions_out = [
        {
            "id": s.id,
            "name": s.name,
            "archived": bool(s.archived),
            "roster_team_alias": s.roster_team_alias,
        }
        for s in db.query(Session)
            .filter_by(job_id=job_id)
            .order_by(Session.id.asc())
            .all()
    ]

    return {
        "entries_loaded": len(entries),
        "entries": [
            {"id": e.id, "name": e.raw_name, "team": e.team_name}
            for e in entries
        ],
        "distinct_teams": len({e.norm_team for e in entries}),
        "csv_teams": csv_teams,         # for mapping dropdown
        "sessions": sessions_out,        # id, name, archived, current alias
        "warnings": _warnings_for(db, job_id) if entries else {
            "unmatched_csv_teams": [],
            "sessions_missing_from_roster": [],
            "duplicate_names": [],
        },
    }


@router.get("/{job_id}/roster-mismatches")
def list_mismatches(job_id: int, db: DbSession = Depends(get_db)):
    """Per-job aggregate of every cluster flagged `roster_mismatch`.

    Returns one row per flagged cluster across all sessions in the job. The
    panel uses `target_cluster_id` to decide whether to default-suggest
    'Merge into existing' (non-null) or 'Create new cluster' (null), but the
    user can always pick the other.
    """
    job = db.query(Job).get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")

    lookup = build_lookup(db, job_id)
    if not lookup:
        return {"items": []}

    # Raw team-name display: pick one raw form per normalized team.
    raw_team_by_norm: dict[str, str] = {}
    for r in db.query(RosterEntry.team_name, RosterEntry.norm_team).filter_by(
        job_id=job_id,
    ).all():
        raw_team_by_norm.setdefault(r.norm_team, r.team_name)

    # Non-archived sessions only — archived teams can't be source OR target.
    sessions = [
        s for s in
        db.query(Session).filter_by(job_id=job_id).all()
        if not s.archived
    ]
    session_by_norm = {session_norm_team(s): s for s in sessions}

    items = []
    for s in sessions:
        sess_norm = session_norm_team(s)
        for c in s.clusters:
            if not cluster_is_mismatched(c, sess_norm, lookup):
                continue
            expected_norm = lookup[normalize_name(c.auto_label)]
            target_sess = session_by_norm.get(expected_norm)
            target_cluster_id = None
            if target_sess is not None:
                # Same name-match rule the move endpoint enforces, so the
                # panel and the action stay consistent.
                src_keys = {
                    n for n in (normalize_name(c.manual_label),
                                normalize_name(c.auto_label)) if n
                }
                matches = [
                    tc for tc in target_sess.clusters
                    if (src_keys & {
                        n for n in (normalize_name(tc.manual_label),
                                    normalize_name(tc.auto_label)) if n
                    })
                ]
                if len(matches) == 1:
                    target_cluster_id = matches[0].id
                # 0 or 2+ → leave null; panel surfaces only 'Create new'.

            image_ids = sorted({
                f.image_id for f in db.query(Face).filter_by(cluster_id=c.id).all()
            })
            items.append({
                "source_session_id": s.id,
                "source_session_name": s.name,
                "source_cluster_id": c.id,
                "source_cluster_label": c.display_label(),
                "source_image_count": c.image_count or 0,
                "source_image_ids": image_ids,   # for "Reject as stray" client loop
                "expected_team_name": raw_team_by_norm.get(expected_norm, expected_norm),
                "target_session_id": target_sess.id if target_sess else None,
                "target_cluster_id": target_cluster_id,
                "target_session_reviewed": bool(target_sess.reviewed) if target_sess else False,
            })
    return {"items": items}


# ── Phase 6.1: folder ↔ CSV-team suggestions ────────────────────────────────


_MIN_MAPPED = 3
_MIN_SHARE = 0.6


def _tally_votes(
    session: Session, lookup: dict[str, str],
) -> tuple[dict[str, int], int, int]:
    """Pure vote-tally over a session's clusters. Returns (votes, mapped, total).

    Extracted from `_suggest_alias_for_session` so sibling vote-summing can
    reuse the per-session tally without re-iterating clusters. Same multi-team
    abstain (lookup.get(norm) == None → skip) as before.
    """
    votes: dict[str, int] = {}
    total = 0
    for c in session.clusters:
        total += 1
        if not c.auto_label:
            continue
        norm = normalize_name(c.auto_label)
        if not norm:
            continue
        expected = lookup.get(norm)
        if expected is None:
            continue  # unknown name OR ambiguous (dup) — abstain
        votes[expected] = votes.get(expected, 0) + 1
    return votes, sum(votes.values()), total


def _format_suggestion(
    votes: dict[str, int], mapped: int, total: int,
    raw_team_by_norm: dict[str, str], *, source: str = "self",
) -> dict | None:
    """Apply the ≥3 mapped + ≥60% share thresholds and format the suggestion
    dict. Returns None when the signal isn't strong enough.

    `source` is informational: "self" for per-session signal, "siblings" for
    a group-level signal aggregated from same-folder-name sessions in the
    job. FE may ignore the field; existing UI consumes only suggested_team_name
    + mapped_clusters + winning_clusters.
    """
    if mapped < _MIN_MAPPED:
        return None
    winner, win_count = max(votes.items(), key=lambda kv: kv[1])
    share = win_count / mapped
    if share < _MIN_SHARE:
        return None
    return {
        "suggested_team_name": raw_team_by_norm.get(winner, winner),
        "suggested_norm_team": winner,
        "confidence": round(share, 3),
        "winning_clusters": win_count,
        "mapped_clusters": mapped,
        "total_clusters": total,
        "suggestion_source": source,
    }


def _suggest_alias_for_session(
    session: Session, lookup: dict[str, str], raw_team_by_norm: dict[str, str],
) -> dict | None:
    """Vote-tally suggestion for a single session. Thin wrapper around the
    pure helpers — preserves the public signature so existing callers and
    tests are unaffected by the sibling-aggregation refactor."""
    votes, mapped, total = _tally_votes(session, lookup)
    return _format_suggestion(votes, mapped, total, raw_team_by_norm)


@router.get("/{job_id}/roster-folder-suggestions")
def folder_suggestions(job_id: int, db: DbSession = Depends(get_db)):
    """For each session whose folder doesn't normalize-match any roster
    team and isn't already mapped via roster_team_alias, propose the best
    CSV team based on content-vote across its clusters' auto_labels.

    Sessions whose folder ALREADY matches the roster (or are aliased to
    something that matches) are omitted — no mapping is needed.
    """
    job = db.query(Job).get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")

    # Option α: prefer RosterEntry when present (Phase 6 back-compat),
    # otherwise fall back to PlayerMembership — the canonical roster source
    # on modern jobs (capture-app + Player Roster desktop upload both write
    # only PlayerMembership). When both tables are empty, return cleanly
    # empty so the UI's "no roster" state is preserved.
    lookup = build_lookup(db, job_id)
    if lookup:
        raw_team_by_norm: dict[str, str] = {}
        for r in db.query(RosterEntry.team_name, RosterEntry.norm_team).filter_by(
            job_id=job_id,
        ).all():
            raw_team_by_norm.setdefault(r.norm_team, r.team_name)
    else:
        lookup = build_lookup_from_memberships(db, job_id)
        if not lookup:
            return {"items": [], "available_teams": []}
        raw_team_by_norm = membership_raw_team_by_norm(db, job_id)

    available = sorted(raw_team_by_norm.values())
    roster_norms = set(raw_team_by_norm.keys())

    # Sibling vote-summing: line-split sessions share a raw folder name and
    # individually have too few mapped clusters for the threshold. Their
    # combined tallies often cleanly identify the team. Aliased siblings
    # DO contribute votes (their cluster matches are real evidence about
    # the folder family's team — the alias only affects the "already
    # covered" output gate). Archived sessions don't contribute, consistent
    # with the endpoint excluding them from work entirely.
    sessions_active = [
        s for s in db.query(Session).filter_by(job_id=job_id).all()
        if not s.archived
    ]
    per_session_tally: dict[int, tuple[dict[str, int], int, int]] = {
        s.id: _tally_votes(s, lookup) for s in sessions_active
    }
    group_votes: dict[str, dict[str, int]] = {}
    group_mapped: dict[str, int] = {}
    group_total: dict[str, int] = {}
    sibling_count: dict[str, int] = {}
    for s in sessions_active:
        v, m, t = per_session_tally[s.id]
        g = group_votes.setdefault(s.name, {})
        for team, c in v.items():
            g[team] = g.get(team, 0) + c
        group_mapped[s.name] = group_mapped.get(s.name, 0) + m
        group_total[s.name] = group_total.get(s.name, 0) + t
        sibling_count[s.name] = sibling_count.get(s.name, 0) + 1

    items = []
    for s in sessions_active:
        effective = session_norm_team(s)  # honors existing alias
        if effective in roster_norms:
            continue  # already covered (by folder name OR existing alias)
        v, m, t = per_session_tally[s.id]
        suggestion = _format_suggestion(v, m, t, raw_team_by_norm, source="self")
        if suggestion is None and sibling_count[s.name] > 1:
            # Sibling fallback — combine same-folder-name siblings' votes.
            suggestion = _format_suggestion(
                group_votes[s.name],
                group_mapped[s.name],
                group_total[s.name],
                raw_team_by_norm,
                source="siblings",
            )
        items.append({
            "session_id": s.id,
            "session_name": s.name,
            "current_alias": s.roster_team_alias,
            "suggestion": suggestion,   # None if no strong signal
        })
    return {"items": items, "available_teams": available}


@router.get("/{job_id}/guest-clusters")
def guest_clusters(job_id: int, db: DbSession = Depends(get_db)):
    """Cross-team guest clusters (Phase 11): phantom clusters that are
    actually a player from another team caught in a buddy shot. See
    services/guest_clusters.py."""
    job = db.query(Job).get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    from app.services.guest_clusters import find_guest_clusters
    return {"items": find_guest_clusters(db, job_id)}


@router.get("/{job_id}/naming-errors")
def naming_errors(job_id: int, db: DbSession = Depends(get_db)):
    """Cross-team duplicate-name detection (Phase 10): names that appear as
    clusters in 2+ teams, classified same_face (mis-foldered) vs
    different_face (photographer didn't update the copyright field). See
    services/naming_errors.py."""
    job = db.query(Job).get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    from app.services.naming_errors import find_cross_team_name_collisions
    return {"items": find_cross_team_name_collisions(db, job_id)}


@router.delete("/{job_id}/roster")
def delete_roster(job_id: int, db: DbSession = Depends(get_db)):
    job = db.query(Job).get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    n = db.query(RosterEntry).filter_by(job_id=job_id).delete()
    db.commit()
    return {"deleted": n}
