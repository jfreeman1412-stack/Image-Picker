"""Phase A.4 — the reference-matching pipeline stage.

Pipeline-facing logic, kept separate from the pure scorer (services/matching.py)
so it's seed-and-call testable without running detection / clustering /
expression. `face_pipeline.run_pipeline` calls `match_session_clusters` as the
'matching' stage (after labeling, before sorting).

For each cluster in a session it scores the cluster's face embeddings against
the reference library — roster-scoped when the shoot has a roster, else global
(decision 1) — persists the result on the Cluster row, and applies the locked
label (decision 2) and coach (decision 3) rules. The match itself is stored;
the read-time team-mismatch flag is computed later in /clusters (Section 4).
See PHASE_A4_PIPELINE_INTEGRATION.md.
"""
from __future__ import annotations

import logging

import numpy as np
from sqlalchemy.orm import Session as DbSession

from app.models.db_models import Cluster, Face, PlayerMembership, ReferenceFace
from app.services import matching
from app.services.roster import normalize_name
from app.services.roster_check import session_norm_team

logger = logging.getLogger(__name__)


def _add_flag(review_reason: str | None, flag: str) -> str | None:
    """Append a review-reason code, deduped — same shape as
    roster_check.add_roster_flag (we keep the stored-flag set comma-joined)."""
    existing = [r.strip() for r in (review_reason or "").split(",") if r.strip()]
    if flag not in existing:
        existing.append(flag)
    return ",".join(existing) if existing else None


def match_session_clusters(db: DbSession, session) -> None:
    """Match every cluster in a session against the reference library and
    persist results onto the Cluster rows. Mutates rows; the caller commits."""
    # ── Candidate scope (decision 1) ─────────────────────────────────────────
    roster_player_ids: set[int] | None = None
    coach_player_ids: set[int] = set()
    member_norm_teams: dict[int, set[str]] = {}   # player_id → {norm_team}
    member_raw_teams: dict[int, set[str]] = {}    # player_id → {team_name} (logs)
    scope = "global_fallback"

    if session.job_id is not None:
        memberships = (
            db.query(PlayerMembership).filter_by(job_id=session.job_id).all()
        )
        if memberships:
            scope = "roster"
            roster_player_ids = {m.player_id for m in memberships}
            coach_player_ids = {m.player_id for m in memberships if m.is_coach}
            for m in memberships:
                member_norm_teams.setdefault(m.player_id, set()).add(m.norm_team)
                member_raw_teams.setdefault(m.player_id, set()).add(m.team_name)

    if scope == "global_fallback":
        n_refs = db.query(ReferenceFace).count()
        logger.warning(
            "[matching] No roster for job %s — falling back to global match "
            "across %d references.", session.job_id, n_refs,
        )

    index = matching.load_reference_index(db, player_ids=roster_player_ids)
    sess_norm = session_norm_team(session)

    # ── Per-cluster match ────────────────────────────────────────────────────
    clusters = db.query(Cluster).filter_by(session_id=session.id).all()
    for c in clusters:
        faces = db.query(Face).filter_by(cluster_id=c.id).all()
        embs = [np.frombuffer(f.embedding, dtype=np.float32) for f in faces]
        if not embs:
            continue   # empty cluster — leave match columns NULL/untouched

        result = matching.match_against_index(index, np.stack(embs))
        c.match_scope = scope
        c.match_tier = result["tier"]
        c.matched_player_id = result["player_id"]   # None for 'none'
        c.match_confidence = result["score"]

        if result["tier"] == "high":
            pid = result["player_id"]
            name = result["player_name"]
            if not (c.auto_label or "").strip():
                # Gap-fill: no copyright tag → adopt the matched name cleanly.
                # (NOT an error; the future steady state has no copyright.)
                c.auto_label = name
                c.auto_label_source = "match"
            elif normalize_name(c.auto_label) != normalize_name(name or ""):
                # Copyright present and disagrees → copyright wins, flag it.
                c.review_reason = _add_flag(c.review_reason, "match_label_conflict")
            # (agree → leave the copyright auto_label + source untouched, no flag)

            # Coach promotion (decision 3): roster-scope only, promote-only.
            # manual_coach_override still wins downstream via is_coach_for_sort().
            if pid in coach_player_ids:
                c.is_likely_coach = 1

            # Run-time legibility for the validation gate (decision 4): the flag
            # itself is computed read-time in /clusters; here we just log it.
            teams = member_norm_teams.get(pid)
            if teams and sess_norm not in teams:
                logger.warning(
                    "[matching] Cluster %s (%s): high match to %s (%.3f), but "
                    "that player is rostered for team(s) %s, not this session's "
                    "team '%s'.",
                    c.id, c.display_label(), name,
                    c.match_confidence if c.match_confidence is not None else 0.0,
                    sorted(member_raw_teams.get(pid, set())), session.name,
                )
        elif result["tier"] == "low":
            c.review_reason = _add_flag(c.review_reason, "low_confidence_match")
        # 'none' → no label change, no flag
