"""Roster-vs-cluster mismatch detection.

This is the read-time half of the roster feature. We do NOT bake
`roster_mismatch` into Cluster.review_reason — the roster can be uploaded /
cleared / re-uploaded at any time, and we never want to re-run the pipeline
to refresh a flag. The /clusters API computes it on every read using the
pre-fetched lookup from `roster.build_lookup`.

A cluster is flagged iff:
  - its session belongs to a job that has a non-empty roster, AND
  - `Cluster.auto_label` is populated (None = ambiguous copyright; abstain), AND
  - normalize_name(auto_label) resolves to a single roster row's team
    (multi-team / ambiguous lookups abstain), AND
  - that team's normalized name != normalize_name(session.name).

Coach rows live in the roster as regular rows (with a "Coach-XXX-YY"
placeholder in col 0) so the same lookup path covers them.
"""
from __future__ import annotations

from app.models.db_models import Cluster, Session
from app.services.roster import normalize_name


ROSTER_MISMATCH = "roster_mismatch"


def cluster_is_mismatched(
    cluster: Cluster, session_norm_team: str, lookup: dict[str, str],
) -> bool:
    """Return True iff this cluster's roster-derived team disagrees with
    its session's team. `lookup` is the dict from `roster.build_lookup`."""
    if not lookup:
        return False  # graceful no-op: no roster, no flags
    auto = cluster.auto_label
    if not auto:
        return False  # abstain — ambiguous copyright in the cluster
    expected = lookup.get(normalize_name(auto))
    if expected is None:
        return False  # unknown name or ambiguous (multi-team) — abstain
    return expected != session_norm_team


def add_roster_flag(review_reason: str | None, mismatched: bool) -> str | None:
    """Return a `review_reason` string with `roster_mismatch` appended if
    `mismatched`, deduplicated. Caller passes the resulting string to
    `filter_visible_reasons` exactly like any other reason. We never mutate
    Cluster.review_reason in the DB — this is read-time only."""
    existing = [r.strip() for r in (review_reason or "").split(",") if r.strip()]
    if mismatched and ROSTER_MISMATCH not in existing:
        existing.append(ROSTER_MISMATCH)
    return ",".join(existing) if existing else None


def session_norm_team(session: Session) -> str:
    """Effective normalized team key for this session.

    Phase 6.1: if the user has set a roster_team_alias (folder->CSV-team
    mapping), use that instead of the folder name. Lets the app accept
    folders like "10U Black" when the roster CSV calls the team
    "10U-Black-Softball" without renaming either side.
    """
    return normalize_name(session.roster_team_alias or session.name)
