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
DUPLICATE_AUTO_LABEL = "duplicate_auto_label"
MATCH_TEAM_MISMATCH = "match_team_mismatch"


def find_duplicate_label_cluster_ids(clusters) -> set[int]:
    """Phase 9: cross-cluster duplicate detection.

    Group clusters in the same session by normalized auto_label. Any
    group with size >= 2 means every cluster in that group claims the
    same player by name — flag all of them. Clusters with no auto_label
    are not grouped (they go through the existing `ambiguous_copyright`
    flow instead).
    """
    by_norm: dict[str, list[int]] = {}
    for c in clusters:
        norm = normalize_name(c.auto_label)
        if norm:
            by_norm.setdefault(norm, []).append(c.id)
    return {cid for cids in by_norm.values() if len(cids) > 1 for cid in cids}


def add_duplicate_label_flag(
    review_reason: str | None, is_duplicate: bool,
) -> str | None:
    """Same shape as `add_roster_flag` but for the duplicate-label case."""
    existing = [r.strip() for r in (review_reason or "").split(",") if r.strip()]
    if is_duplicate and DUPLICATE_AUTO_LABEL not in existing:
        existing.append(DUPLICATE_AUTO_LABEL)
    return ",".join(existing) if existing else None


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


def cluster_match_team_mismatch(
    cluster: Cluster,
    session_norm_team: str,
    membership_teams_by_player: dict[int, set[str]],
) -> bool:
    """Phase A.4: True iff this cluster has a HIGH reference-match to a player
    who is rostered for this job but NOT for this session's team — a strong
    "mis-clustered or wrong-team photo" signal.

    `membership_teams_by_player` maps {player_id: {norm_team, ...}} for this
    session's job. We abstain (return False) when the matched player isn't
    rostered for this job at all (e.g. a global-fallback match), or for non-high
    tiers — a low-tier suggestion is too uncertain to assert a team conflict.
    Computed at read time (the roster can change without re-running), mirroring
    `cluster_is_mismatched`."""
    if cluster.match_tier != "high" or cluster.matched_player_id is None:
        return False
    teams = membership_teams_by_player.get(cluster.matched_player_id)
    if not teams:
        return False  # matched player not rostered for this job → abstain
    return session_norm_team not in teams


def add_match_team_mismatch_flag(
    review_reason: str | None, is_mismatch: bool,
) -> str | None:
    """Same shape as `add_roster_flag` / `add_duplicate_label_flag`, for the
    read-time match-team-mismatch case."""
    existing = [r.strip() for r in (review_reason or "").split(",") if r.strip()]
    if is_mismatch and MATCH_TEAM_MISMATCH not in existing:
        existing.append(MATCH_TEAM_MISMATCH)
    return ",".join(existing) if existing else None


def session_norm_team(session: Session) -> str:
    """Effective normalized team key for this session.

    Phase 6.1: if the user has set a roster_team_alias (folder->CSV-team
    mapping), use that instead of the folder name. Lets the app accept
    folders like "10U Black" when the roster CSV calls the team
    "10U-Black-Softball" without renaming either side.
    """
    return normalize_name(session.roster_team_alias or session.name)
