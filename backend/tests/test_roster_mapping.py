"""Phase 6.1: folder ↔ CSV-team alias (Session.roster_team_alias)
+ mapping endpoint + content-vote folder-suggestion algorithm.

Locks in the user-requested behavior: when team folders are named
differently from the CSV team column (e.g. folder '10U Black' vs CSV
'10U-Black-Softball'), the app suggests the mapping based on cluster
content (how many copyright-derived auto_labels in the folder map to a
given CSV team) and lets the user accept/override it. Aliases are
per-session and survive roster re-uploads.
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
