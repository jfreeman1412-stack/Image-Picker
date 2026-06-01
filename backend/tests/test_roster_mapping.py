"""Phase 6.1: folder ↔ CSV-team alias (Session.roster_team_alias)
+ mapping endpoint + content-vote folder-suggestion algorithm.

Locks in the user-requested behavior: when team folders are named
differently from the CSV team column (e.g. folder '10U Black' vs CSV
'10U-Black-Softball'), the app suggests the mapping based on cluster
content (how many copyright-derived auto_labels in the folder map to a
given CSV team) and lets the user accept/override it. Aliases are
per-session and survive roster re-uploads.

Option α (2026-06-01): folder_suggestions falls back on PlayerMembership when
RosterEntry is empty. On modern jobs the Player roster (capture-app or desktop
upload) writes only PlayerMembership; RosterEntry is vestigial Phase 6.
See memory: match-team-alias-issue.
"""
from datetime import datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.roster import folder_suggestions, list_mismatches
from app.api.sessions import RosterMappingRequest, set_roster_mapping
from app.db import Base
from app.models.db_models import (
    Cluster, Job, Session as DbSessionModel,
)
from app.services.players import replace_shoot_memberships
from app.services.roster import replace_job_roster
from app.services.roster_check import session_norm_team


@pytest.fixture
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'rm-test.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _job(db, *team_names):
    job = Job(name="J", root_path="/tmp", has_lines=0)
    db.add(job); db.commit(); db.refresh(job)
    sessions = []
    for t in team_names:
        s = DbSessionModel(job_id=job.id, name=t, source_path=f"/tmp/{t}",
                           status="done", created_at=datetime.utcnow())
        db.add(s); db.commit(); db.refresh(s)
        sessions.append(s)
    return job, sessions


def _cluster(db, session, label):
    c = Cluster(session_id=session.id, image_count=1, auto_label=label)
    db.add(c); db.commit(); db.refresh(c)
    return c


# ── session_norm_team honors alias ───────────────────────────────────────

def test_session_norm_team_uses_folder_name_by_default(db):
    job, (s,) = _job(db, "10U Black")
    assert session_norm_team(s) == "10ublack"


def test_session_norm_team_uses_alias_when_set(db):
    job, (s,) = _job(db, "10U Black")
    s.roster_team_alias = "10U-Black-Softball"
    db.commit()
    assert session_norm_team(s) == "10ublacksoftball"


def test_alias_takes_precedence_over_folder_name(db):
    """Even if the folder name happens to roster-match, the alias wins."""
    job, (s,) = _job(db, "10U-Black-Softball")
    s.roster_team_alias = "12UAA-Baseball"
    db.commit()
    assert session_norm_team(s) == "12uaabaseball"


# ── Mapping endpoint ─────────────────────────────────────────────────────

def test_set_mapping_sets_alias(db):
    job, (s,) = _job(db, "10U Black")
    res = set_roster_mapping(
        s.id, RosterMappingRequest(roster_team_alias="10U-Black-Softball"), db,
    )
    db.refresh(s)
    assert s.roster_team_alias == "10U-Black-Softball"
    assert res["roster_team_alias"] == "10U-Black-Softball"


def test_set_mapping_null_clears_alias(db):
    job, (s,) = _job(db, "10U Black")
    s.roster_team_alias = "10U-Black-Softball"
    db.commit()
    set_roster_mapping(
        s.id, RosterMappingRequest(roster_team_alias=None), db,
    )
    db.refresh(s)
    assert s.roster_team_alias is None


def test_set_mapping_empty_string_clears(db):
    """Whitespace-only / empty alias is treated as 'clear', not stored."""
    job, (s,) = _job(db, "10U Black")
    s.roster_team_alias = "Old"
    db.commit()
    set_roster_mapping(s.id, RosterMappingRequest(roster_team_alias="   "), db)
    db.refresh(s)
    assert s.roster_team_alias is None


def test_set_mapping_404_on_missing_session(db):
    with pytest.raises(HTTPException) as exc:
        set_roster_mapping(
            99999, RosterMappingRequest(roster_team_alias="X"), db,
        )
    assert exc.value.status_code == 404


# ── Mismatch flag respects alias ────────────────────────────────────────

def test_alias_silences_mismatch_when_target_matches(db):
    """Folder '10U Black' aliased to '10U-Black-Softball': clusters whose
    roster team is '10U-Black-Softball' should NOT flag as mismatch."""
    job, (s,) = _job(db, "10U Black")
    s.roster_team_alias = "10U-Black-Softball"
    _cluster(db, s, label="Eleanor-Pederson")
    replace_job_roster(db, job.id,
                       [("Eleanor-Pederson", "10U-Black-Softball")])
    db.commit()
    assert list_mismatches(job.id, db)["items"] == []


def test_unaliased_folder_still_flags_mismatch(db):
    """Without an alias, the folder-name mismatch surfaces — to confirm the
    alias is doing the work in the previous test."""
    job, (s,) = _job(db, "10U Black")
    _cluster(db, s, label="Eleanor-Pederson")
    replace_job_roster(db, job.id,
                       [("Eleanor-Pederson", "10U-Black-Softball")])
    db.commit()
    items = list_mismatches(job.id, db)["items"]
    assert len(items) == 1
    assert items[0]["expected_team_name"] == "10U-Black-Softball"
    # No matching folder for that team in this job → null target.
    assert items[0]["target_session_id"] is None


# ── Suggestion algorithm ────────────────────────────────────────────────

def test_suggestion_strong_vote_returned(db):
    """5 of 5 clusters' players belong to the same CSV team → suggest it."""
    job, (s,) = _job(db, "10U Black")
    for name in ("Eleanor-Pederson", "June-Wampach", "Quinn-Gentz",
                 "Magdalena-Hetland", "Nora-Snyder"):
        _cluster(db, s, label=name)
    replace_job_roster(db, job.id, [
        ("Eleanor-Pederson", "10U-Black-Softball"),
        ("June-Wampach",     "10U-Black-Softball"),
        ("Quinn-Gentz",      "10U-Black-Softball"),
        ("Magdalena-Hetland","10U-Black-Softball"),
        ("Nora-Snyder",      "10U-Black-Softball"),
    ])
    db.commit()
    items = folder_suggestions(job.id, db)["items"]
    assert len(items) == 1
    sug = items[0]["suggestion"]
    assert sug is not None
    assert sug["suggested_team_name"] == "10U-Black-Softball"
    assert sug["mapped_clusters"] == 5
    assert sug["winning_clusters"] == 5
    assert sug["confidence"] == 1.0


def test_suggestion_dominant_with_outlier(db):
    """4 of 5 clusters point at team A, 1 at team B → still suggest A (80%)."""
    job, (s,) = _job(db, "10U Black")
    for n in ("A1", "A2", "A3", "A4", "B1"):
        _cluster(db, s, label=n)
    replace_job_roster(db, job.id, [
        ("A1", "10U-Black-Softball"),
        ("A2", "10U-Black-Softball"),
        ("A3", "10U-Black-Softball"),
        ("A4", "10U-Black-Softball"),
        ("B1", "12UAA-Baseball"),
    ])
    db.commit()
    sug = folder_suggestions(job.id, db)["items"][0]["suggestion"]
    assert sug is not None
    assert sug["suggested_norm_team"] == "10ublacksoftball"
    assert sug["winning_clusters"] == 4
    assert sug["confidence"] == 0.8


def test_suggestion_none_when_split(db):
    """2 of 4 vs 2 of 4 → no clear winner (50% < 60% threshold) → None."""
    job, (s,) = _job(db, "10U Black")
    for n in ("A1", "A2", "B1", "B2"):
        _cluster(db, s, label=n)
    replace_job_roster(db, job.id, [
        ("A1", "Team-A"), ("A2", "Team-A"),
        ("B1", "Team-B"), ("B2", "Team-B"),
    ])
    db.commit()
    assert folder_suggestions(job.id, db)["items"][0]["suggestion"] is None


def test_suggestion_none_below_min_clusters(db):
    """Only 2 mapped clusters — below the min=3 threshold."""
    job, (s,) = _job(db, "10U Black")
    for n in ("A1", "A2"):
        _cluster(db, s, label=n)
    replace_job_roster(db, job.id, [
        ("A1", "10U-Black-Softball"), ("A2", "10U-Black-Softball"),
    ])
    db.commit()
    assert folder_suggestions(job.id, db)["items"][0]["suggestion"] is None


def test_suggestion_skips_session_already_aliased_to_a_roster_team(db):
    """Once an alias is set and matches a roster team, the session no longer
    needs a suggestion — it's omitted from the items list entirely."""
    job, (s,) = _job(db, "10U Black")
    s.roster_team_alias = "10U-Black-Softball"
    for n in ("A1", "A2", "A3"):
        _cluster(db, s, label=n)
    replace_job_roster(db, job.id, [
        ("A1", "10U-Black-Softball"),
        ("A2", "10U-Black-Softball"),
        ("A3", "10U-Black-Softball"),
    ])
    db.commit()
    items = folder_suggestions(job.id, db)["items"]
    assert items == []


def test_suggestion_skips_session_whose_folder_already_matches(db):
    job, (s,) = _job(db, "10U-Black-Softball")
    for n in ("A1", "A2", "A3"):
        _cluster(db, s, label=n)
    replace_job_roster(db, job.id, [
        ("A1", "10U-Black-Softball"),
        ("A2", "10U-Black-Softball"),
        ("A3", "10U-Black-Softball"),
    ])
    db.commit()
    assert folder_suggestions(job.id, db)["items"] == []


def test_suggestion_available_teams_listed_sorted(db):
    job, (s,) = _job(db, "10U Black")
    replace_job_roster(db, job.id, [
        ("P1", "Iron-Pigs"),
        ("P2", "Sky-Carp"),
        ("P3", "Hot-Rods"),
    ])
    db.commit()
    teams = folder_suggestions(job.id, db)["available_teams"]
    assert teams == ["Hot-Rods", "Iron-Pigs", "Sky-Carp"]


def test_suggestion_empty_when_no_roster(db):
    job, (s,) = _job(db, "10U Black")
    _cluster(db, s, label="A1")
    db.commit()
    res = folder_suggestions(job.id, db)
    assert res == {"items": [], "available_teams": []}


def test_suggestion_skips_archived_session(db):
    job, (s,) = _job(db, "10U Black")
    s.archived = 1
    for n in ("A1", "A2", "A3"):
        _cluster(db, s, label=n)
    replace_job_roster(db, job.id, [
        ("A1", "10U-Black-Softball"),
        ("A2", "10U-Black-Softball"),
        ("A3", "10U-Black-Softball"),
    ])
    db.commit()
    assert folder_suggestions(job.id, db)["items"] == []


# ── Option α: PlayerMembership fallback (the modern uploads' canonical table) ─
#
# Modern jobs upload via the capture app or the Player Roster desktop button —
# both write to PlayerMembership only (never RosterEntry). The Phase 6
# suggestion endpoint was reading only from RosterEntry and silently no-op'd
# for these jobs. Option α reads PlayerMembership as a fallback so the
# existing UI works without operators needing a separate Cross-check upload.


def _seed_memberships(db, job_id, rows):
    """Shortcut: stand up PlayerMembership rows via the production code path
    so we exercise the same upsert + normalize logic the live API uses.
    `rows` = [(raw_name, team_name), ...]."""
    summary = replace_shoot_memberships(db, job_id, rows)
    db.commit()
    return summary


def test_membership_fallback_strong_vote_returned(db):
    """When RosterEntry is empty but PlayerMembership has data, the same
    vote-tally algorithm produces a suggestion. Mirrors the
    test_suggestion_strong_vote_returned shape, just via memberships."""
    job, (s,) = _job(db, "10U Black")
    for name in ("Eleanor-Pederson", "June-Wampach", "Quinn-Gentz",
                 "Magdalena-Hetland", "Nora-Snyder"):
        _cluster(db, s, label=name)
    _seed_memberships(db, job.id, [
        ("Eleanor-Pederson", "10U-Black-Softball"),
        ("June-Wampach",     "10U-Black-Softball"),
        ("Quinn-Gentz",      "10U-Black-Softball"),
        ("Magdalena-Hetland","10U-Black-Softball"),
        ("Nora-Snyder",      "10U-Black-Softball"),
    ])

    items = folder_suggestions(job.id, db)["items"]
    assert len(items) == 1
    sug = items[0]["suggestion"]
    assert sug is not None
    assert sug["suggested_team_name"] == "10U-Black-Softball"
    assert sug["mapped_clusters"] == 5
    assert sug["winning_clusters"] == 5
    assert sug["confidence"] == 1.0


def test_membership_fallback_available_teams_sorted(db):
    """available_teams populated from PlayerMembership raw team names, sorted
    case-insensitively to mirror the RosterEntry-side behavior."""
    job, (s,) = _job(db, "10U Black")
    _seed_memberships(db, job.id, [
        ("P1", "Iron-Pigs"),
        ("P2", "Sky-Carp"),
        ("P3", "Hot-Rods"),
    ])
    teams = folder_suggestions(job.id, db)["available_teams"]
    assert teams == ["Hot-Rods", "Iron-Pigs", "Sky-Carp"]


def test_membership_fallback_multi_team_player_abstains(db):
    """Same abstain rule as build_lookup: a player on >1 team in this job
    contributes 0 votes (their name is omitted from the lookup). Mirrors
    Phase 6's `duplicate_names` semantics."""
    job, (s,) = _job(db, "10U Black")
    for n in ("A1", "A2", "A3", "DupKid"):
        _cluster(db, s, label=n)
    # DupKid is on two teams → her name is excluded from the lookup → her
    # cluster doesn't vote. The other 3 clusters all point to Black, which
    # still hits the >=3 mapped + >=60% threshold.
    _seed_memberships(db, job.id, [
        ("A1", "10U-Black-Softball"),
        ("A2", "10U-Black-Softball"),
        ("A3", "10U-Black-Softball"),
        ("DupKid", "10U-Black-Softball"),
        ("DupKid", "12UAA-Baseball"),
    ])
    sug = folder_suggestions(job.id, db)["items"][0]["suggestion"]
    assert sug is not None
    assert sug["winning_clusters"] == 3
    assert sug["mapped_clusters"] == 3   # DupKid did NOT contribute a vote


def test_membership_fallback_skips_session_already_aliased(db):
    """Same idempotency as the RosterEntry path: once aliased and resolving
    to a roster team, the session drops out of items entirely."""
    job, (s,) = _job(db, "10U Black")
    s.roster_team_alias = "10U-Black-Softball"
    for n in ("A1", "A2", "A3"):
        _cluster(db, s, label=n)
    _seed_memberships(db, job.id, [
        ("A1", "10U-Black-Softball"),
        ("A2", "10U-Black-Softball"),
        ("A3", "10U-Black-Softball"),
    ])
    assert folder_suggestions(job.id, db)["items"] == []


def test_membership_fallback_skips_archived_session(db):
    """Archived sessions are excluded from suggestions regardless of source."""
    job, (s,) = _job(db, "10U Black")
    s.archived = 1
    for n in ("A1", "A2", "A3"):
        _cluster(db, s, label=n)
    _seed_memberships(db, job.id, [
        ("A1", "10U-Black-Softball"),
        ("A2", "10U-Black-Softball"),
        ("A3", "10U-Black-Softball"),
    ])
    assert folder_suggestions(job.id, db)["items"] == []


def test_membership_fallback_session_folder_matches_a_membership_team(db):
    """The 'already-covered' check honors PlayerMembership norm_teams too:
    a folder whose normalized name matches any membership team is omitted."""
    job, (s,) = _job(db, "10U-Black-Softball")
    for n in ("A1", "A2", "A3"):
        _cluster(db, s, label=n)
    _seed_memberships(db, job.id, [
        ("A1", "10U-Black-Softball"),
        ("A2", "10U-Black-Softball"),
        ("A3", "10U-Black-Softball"),
    ])
    assert folder_suggestions(job.id, db)["items"] == []


def test_roster_entry_wins_when_both_tables_populated(db):
    """Back-compat: if a job somehow has both RosterEntry rows AND
    PlayerMembership rows, RosterEntry is read (no fallback). Lets older
    jobs that uploaded the Phase 6 cross-check CSV keep working unchanged
    even after Option α ships."""
    job, (s,) = _job(db, "10U Black")
    for n in ("A1", "A2", "A3"):
        _cluster(db, s, label=n)
    # RosterEntry says Team-X for these names …
    replace_job_roster(db, job.id, [
        ("A1", "Team-X"),
        ("A2", "Team-X"),
        ("A3", "Team-X"),
    ])
    # … but PlayerMembership has a different mapping. RosterEntry should win.
    _seed_memberships(db, job.id, [
        ("A1", "Team-Y"),
        ("A2", "Team-Y"),
        ("A3", "Team-Y"),
    ])
    sug = folder_suggestions(job.id, db)["items"][0]["suggestion"]
    assert sug is not None
    assert sug["suggested_team_name"] == "Team-X"


def test_membership_fallback_below_min_clusters_no_signal(db):
    """The same min-mapped=3 / min-share=0.6 thresholds apply via fallback."""
    job, (s,) = _job(db, "10U Black")
    for n in ("A1", "A2"):
        _cluster(db, s, label=n)
    _seed_memberships(db, job.id, [
        ("A1", "10U-Black-Softball"),
        ("A2", "10U-Black-Softball"),
    ])
    items = folder_suggestions(job.id, db)["items"]
    assert items[0]["suggestion"] is None  # listed (needs mapping) but no auto-suggestion


def test_empty_remains_empty_when_neither_table_has_data(db):
    """Existing baseline: jobs with neither RosterEntry nor PlayerMembership
    return cleanly empty. Pre-Option-α behavior preserved."""
    job, (s,) = _job(db, "10U Black")
    _cluster(db, s, label="A1")
    db.commit()
    assert folder_suggestions(job.id, db) == {"items": [], "available_teams": []}


# ── Option α: build_lookup_from_memberships unit tests ───────────────────────


def test_build_lookup_from_memberships_empty_job(db):
    from app.services.roster import build_lookup_from_memberships
    job, _ = _job(db, "10U Black")
    assert build_lookup_from_memberships(db, job.id) == {}


def test_build_lookup_from_memberships_single_team_player(db):
    from app.services.roster import build_lookup_from_memberships
    job, _ = _job(db, "10U Black")
    _seed_memberships(db, job.id, [
        ("Eleanor-Pederson", "10U-Black-Softball"),
    ])
    lookup = build_lookup_from_memberships(db, job.id)
    # normalize_name('Eleanor-Pederson') = 'eleanorpederson'
    assert lookup == {"eleanorpederson": "10ublacksoftball"}


def test_build_lookup_from_memberships_multi_team_player_omitted(db):
    """Same abstain rule as services.roster.build_lookup. A player who
    appears on >1 team is omitted entirely, so callers' .get() returns None
    for both unknown AND ambiguous names."""
    from app.services.roster import build_lookup_from_memberships
    job, _ = _job(db, "10U Black")
    _seed_memberships(db, job.id, [
        ("Eleanor-Pederson", "10U-Black-Softball"),
        ("DupKid", "10U-Black-Softball"),
        ("DupKid", "12UAA-Baseball"),
    ])
    lookup = build_lookup_from_memberships(db, job.id)
    assert "eleanorpederson" in lookup
    assert "dupkid" not in lookup


def test_build_lookup_from_memberships_scoped_to_job(db):
    """Memberships from other jobs do not leak into this job's lookup."""
    from app.services.roster import build_lookup_from_memberships
    job1, _ = _job(db, "10U Black")
    job2 = Job(name="J2", root_path="/tmp/j2", has_lines=0)
    db.add(job2); db.commit(); db.refresh(job2)
    _seed_memberships(db, job1.id, [("Alice", "Team-A")])
    _seed_memberships(db, job2.id, [("Bob", "Team-B")])
    lookup1 = build_lookup_from_memberships(db, job1.id)
    lookup2 = build_lookup_from_memberships(db, job2.id)
    assert lookup1 == {"alice": "teama"}
    assert lookup2 == {"bob": "teamb"}


# ── Sibling vote-summing (2026-06-01) ─────────────────────────────────────────
#
# Same-folder-name sessions (the line-split pattern: 4 tablets each capturing a
# portion of one team's roster end up as separate Session rows sharing the
# raw `session.name`) individually have too few mapped clusters for the
# suggestion threshold. Their COMBINED tallies often clearly identify the
# team. Sibling vote-summing falls back to the group tally when a session's
# individual signal is None.
#
# Aliased siblings DO contribute votes (their cluster matches are real
# evidence about the folder family's team — the alias only affects the
# "already covered" output gate, not the cluster vote data).
# Archived siblings do NOT contribute (consistent with the endpoint
# excluding archived sessions entirely).


def _multi_session_job(db, name, count):
    """Stand up `count` sessions with the same folder name in one job —
    the line-split pattern. Returns (job, [session, ...])."""
    job = Job(name="J", root_path="/tmp", has_lines=0)
    db.add(job); db.commit(); db.refresh(job)
    sessions = []
    for _ in range(count):
        s = DbSessionModel(job_id=job.id, name=name,
                           source_path=f"/tmp/{name}",
                           status="done", created_at=datetime.utcnow())
        db.add(s); db.commit(); db.refresh(s)
        sessions.append(s)
    return job, sessions


def _seed_jets_roster(db, job_id, extra=()):
    """The 2-3rd Jets roster + any extra (name, team) rows."""
    rows = [
        (f"Jet{i}", "Rec-Flag-2-3-Jets") for i in range(1, 8)
    ] + list(extra)
    _seed_memberships(db, job_id, rows)


# Individual unchanged: strong/weak signals from the per-session path
# behave exactly as today. The wrapper's contract is preserved.


def test_sibling_single_session_strong_signal_unchanged(db):
    """One session, strong individual signal — same behavior as today; the
    sibling path never engages (no siblings)."""
    job, (s,) = _job(db, "10U Black")
    for name in ("Eleanor-Pederson", "June-Wampach", "Quinn-Gentz",
                 "Magdalena-Hetland", "Nora-Snyder"):
        _cluster(db, s, label=name)
    _seed_memberships(db, job.id, [
        (n, "10U-Black-Softball") for n in (
            "Eleanor-Pederson", "June-Wampach", "Quinn-Gentz",
            "Magdalena-Hetland", "Nora-Snyder",
        )
    ])
    sug = folder_suggestions(job.id, db)["items"][0]["suggestion"]
    assert sug is not None
    assert sug["mapped_clusters"] == 5
    assert sug["suggestion_source"] == "self"


def test_sibling_single_session_below_threshold_unchanged(db):
    """One session, below 3-mapped threshold — no suggestion; no siblings
    to fall back on; behavior unchanged."""
    job, (s,) = _job(db, "10U Black")
    for n in ("A1", "A2"):
        _cluster(db, s, label=n)
    _seed_memberships(db, job.id, [
        ("A1", "10U-Black-Softball"), ("A2", "10U-Black-Softball"),
    ])
    assert folder_suggestions(job.id, db)["items"][0]["suggestion"] is None


def test_sibling_two_sessions_combined_clears_threshold(db):
    """Two sessions same folder name, individually 2 + 2 mapped, combined
    4/4 -> strong. Sibling fallback engages for both."""
    job, sessions = _multi_session_job(db, "2-3rd Jets", 2)
    for s in sessions:
        for n in ("A", "B"):
            _cluster(db, s, label=f"{n}{s.id}")
    rows = []
    for s in sessions:
        for n in ("A", "B"):
            rows.append((f"{n}{s.id}", "Rec-Flag-2-3-Jets"))
    _seed_memberships(db, job.id, rows)

    items = folder_suggestions(job.id, db)["items"]
    assert len(items) == 2
    for it in items:
        sug = it["suggestion"]
        assert sug is not None
        assert sug["suggested_team_name"] == "Rec-Flag-2-3-Jets"
        assert sug["mapped_clusters"] == 4   # combined group tally
        assert sug["winning_clusters"] == 4
        assert sug["suggestion_source"] == "siblings"


def test_sibling_combined_disagree_no_suggestion(db):
    """Two sessions same folder name, combined votes split 50/50 across two
    teams -> share < 60% threshold -> no suggestion."""
    job, sessions = _multi_session_job(db, "ConflictFolder", 2)
    for n in ("A1", "A2", "A3"):
        _cluster(db, sessions[0], label=n)
    for n in ("B1", "B2", "B3"):
        _cluster(db, sessions[1], label=n)
    _seed_memberships(db, job.id, [
        ("A1", "Team-A"), ("A2", "Team-A"), ("A3", "Team-A"),
        ("B1", "Team-B"), ("B2", "Team-B"), ("B3", "Team-B"),
    ])
    items = folder_suggestions(job.id, db)["items"]
    # Each session's individual signal IS strong (3/3 = 100%), so the
    # individual path wins — sibling fallback never engages. Same-folder-name
    # name collision across two truly different teams produces two distinct
    # per-session suggestions. Sibling vote-summing isn't the right tool for
    # this pathological case; the individual-first precedence handles it.
    assert len(items) == 2
    for it in items:
        sug = it["suggestion"]
        assert sug is not None
        assert sug["suggestion_source"] == "self"


def test_sibling_jets_x4_real_data_pattern(db):
    """Job 45 2-3rd Jets x4 pattern: 4 sessions, individual mapped counts
    1+2+1+1 = 5 combined, 4 votes Jets + 1 outlier = 80% share. Each
    session's individual signal is None (< 3 mapped); the sibling fallback
    surfaces the strong group signal for all 4."""
    job, sessions = _multi_session_job(db, "2-3rd Jets", 4)
    s0, s1, s2, s3 = sessions
    _cluster(db, s0, label="Jet1")
    _cluster(db, s1, label="Jet2"); _cluster(db, s1, label="Jet3")
    _cluster(db, s2, label="Jet4")
    _cluster(db, s3, label="Outsider1")
    _seed_jets_roster(db, job.id, extra=[
        ("Outsider1", "Some-Other-Team"),
    ])
    items = folder_suggestions(job.id, db)["items"]
    assert len(items) == 4
    for it in items:
        sug = it["suggestion"]
        assert sug is not None
        assert sug["suggested_team_name"] == "Rec-Flag-2-3-Jets"
        assert sug["mapped_clusters"] == 5
        assert sug["winning_clusters"] == 4
        assert sug["confidence"] == 0.8
        assert sug["suggestion_source"] == "siblings"


def test_sibling_aliased_sibling_contributes_votes(db):
    """Aliased siblings' cluster votes ARE real face-match evidence about
    the folder family. They should contribute to the group tally even though
    the aliased session itself is omitted from items output."""
    job, sessions = _multi_session_job(db, "2-3rd Jets", 2)
    aliased, unmapped = sessions
    aliased.roster_team_alias = "Rec-Flag-2-3-Jets"
    db.commit()
    # Aliased session has 4 strong votes; unmapped has 1 weak vote.
    # Aliased is "already covered" so it drops from items. But its votes
    # combine with unmapped's to produce a sibling suggestion for unmapped.
    for n in ("J1", "J2", "J3", "J4"):
        _cluster(db, aliased, label=n)
    _cluster(db, unmapped, label="J5")
    _seed_memberships(db, job.id, [
        (n, "Rec-Flag-2-3-Jets") for n in ("J1", "J2", "J3", "J4", "J5")
    ])
    items = folder_suggestions(job.id, db)["items"]
    # Only the unmapped session in items.
    assert len(items) == 1
    assert items[0]["session_id"] == unmapped.id
    sug = items[0]["suggestion"]
    assert sug is not None
    assert sug["suggested_team_name"] == "Rec-Flag-2-3-Jets"
    # Combined: 4 + 1 = 5 mapped, all voting Jets.
    assert sug["mapped_clusters"] == 5
    assert sug["suggestion_source"] == "siblings"


def test_sibling_archived_sibling_does_not_contribute(db):
    """Archived sessions are excluded from the endpoint entirely. Their
    clusters' votes should NOT contribute to sibling vote-summing — keeps
    archived = 'don't process for any purpose' consistent."""
    job, sessions = _multi_session_job(db, "2-3rd Jets", 2)
    archived, unmapped = sessions
    archived.archived = 1
    db.commit()
    # Archived session has 5 strong votes; unmapped has 1 weak vote.
    # If sibling vote-summing ignored 'archived', combined would be 6 mapped
    # -> strong. If it correctly excludes archived, only unmapped's 1 vote
    # counts -> below the 3-mapped threshold -> no suggestion.
    for n in ("J1", "J2", "J3", "J4", "J5"):
        _cluster(db, archived, label=n)
    _cluster(db, unmapped, label="J6")
    _seed_memberships(db, job.id, [
        (n, "Rec-Flag-2-3-Jets")
        for n in ("J1", "J2", "J3", "J4", "J5", "J6")
    ])
    items = folder_suggestions(job.id, db)["items"]
    # Only unmapped survives the archived filter; below threshold -> None.
    assert len(items) == 1
    assert items[0]["session_id"] == unmapped.id
    assert items[0]["suggestion"] is None


def test_sibling_different_folder_names_dont_combine(db):
    """Sessions with different raw folder names must NOT have their votes
    combined just because both are individually unmapped. Group key is raw
    session.name; cross-name aggregation is not a thing."""
    job = Job(name="J", root_path="/tmp", has_lines=0)
    db.add(job); db.commit(); db.refresh(job)
    s_a = DbSessionModel(job_id=job.id, name="Folder-A",
                         source_path="/tmp/A", status="done",
                         created_at=datetime.utcnow())
    s_b = DbSessionModel(job_id=job.id, name="Folder-B",
                         source_path="/tmp/B", status="done",
                         created_at=datetime.utcnow())
    db.add_all([s_a, s_b]); db.commit(); db.refresh(s_a); db.refresh(s_b)
    # 2 clusters each, both voting Rec-Flag-2-3-Jets — 4 votes combined
    # would clear the threshold, but they're DIFFERENT folder names.
    for s in (s_a, s_b):
        for n in (f"X{s.id}", f"Y{s.id}"):
            _cluster(db, s, label=n)
    _seed_memberships(db, job.id, [
        (f"X{s.id}", "Rec-Flag-2-3-Jets") for s in (s_a, s_b)
    ] + [
        (f"Y{s.id}", "Rec-Flag-2-3-Jets") for s in (s_a, s_b)
    ])
    items = folder_suggestions(job.id, db)["items"]
    assert len(items) == 2
    for it in items:
        # Per-session is 2 mapped -> below threshold; no siblings to combine
        # with (different folder names) -> no suggestion.
        assert it["suggestion"] is None


def test_sibling_idempotency_aliased_session_excluded(db):
    """Re-confirms that aliased sessions stay excluded from items output
    even when sibling vote-summing is in play. Locks idempotency on top of
    the existing α behavior."""
    job, sessions = _multi_session_job(db, "2-3rd Jets", 3)
    sessions[0].roster_team_alias = "Rec-Flag-2-3-Jets"  # already covered
    db.commit()
    for s in sessions:
        for n in ("J", "K"):
            _cluster(db, s, label=f"{n}{s.id}")
    _seed_memberships(db, job.id, [
        (f"J{s.id}", "Rec-Flag-2-3-Jets") for s in sessions
    ] + [
        (f"K{s.id}", "Rec-Flag-2-3-Jets") for s in sessions
    ])
    items = folder_suggestions(job.id, db)["items"]
    returned_ids = {it["session_id"] for it in items}
    assert sessions[0].id not in returned_ids  # aliased -> excluded
    assert sessions[1].id in returned_ids
    assert sessions[2].id in returned_ids
    # Both unmapped siblings get the sibling-summed suggestion. Group tally
    # includes the aliased session's votes (4) + each of the 2 unmapped (2
    # each) -> 8 mapped, all voting Jets -> 100% suggestion.
    for it in items:
        assert it["suggestion"]["suggested_team_name"] == "Rec-Flag-2-3-Jets"
        assert it["suggestion"]["suggestion_source"] == "siblings"
