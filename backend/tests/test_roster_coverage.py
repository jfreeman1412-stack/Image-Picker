"""Phase 9 Section 5: per-session roster-coverage report.

For each session, compare the roster's expected players for that team
against the clusters the pipeline actually produced, and bucket the
result into present / missing / extra / unidentified.
"""
from datetime import datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.sessions import roster_coverage
from app.db import Base
from app.models.db_models import (
    Cluster, Job, RosterEntry, Session as DbSession,
)
from app.services.roster import replace_job_roster


@pytest.fixture
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'cov-test.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    SL = sessionmaker(bind=engine)
    s = SL()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _job(db, *teams):
    job = Job(name="J", root_path="/tmp", has_lines=0)
    db.add(job); db.commit(); db.refresh(job)
    sessions = []
    for t in teams:
        sess = DbSession(job_id=job.id, name=t, source_path=f"/tmp/{t}",
                         status="done", created_at=datetime.utcnow())
        db.add(sess); db.commit(); db.refresh(sess)
        sessions.append(sess)
    return job, sessions


def _cluster(db, session, *, label=None, image_count=3):
    c = Cluster(session_id=session.id, auto_label=label, image_count=image_count)
    db.add(c); db.commit(); db.refresh(c)
    return c


# ── happy paths ─────────────────────────────────────────────────────────

def test_all_expected_present_no_missing(db):
    _, (sess,) = _job(db, "10U-Black-Softball")
    _cluster(db, sess, label="Eleanor-Pederson")
    _cluster(db, sess, label="June-Wampach")
    replace_job_roster(db, sess.job_id, [
        ("Eleanor-Pederson", "10U-Black-Softball"),
        ("June-Wampach",     "10U-Black-Softball"),
    ])
    db.commit()
    res = roster_coverage(sess.id, db)
    assert len(res["expected_players"]) == 2
    assert len(res["present_players"]) == 2
    assert res["missing_players"] == []
    assert res["extra_clusters"] == []
    assert res["unidentified_clusters"] == []


def test_missing_player_listed_by_name(db):
    _, (sess,) = _job(db, "10U-Black-Softball")
    _cluster(db, sess, label="Eleanor-Pederson")
    replace_job_roster(db, sess.job_id, [
        ("Eleanor-Pederson", "10U-Black-Softball"),
        ("June-Wampach",     "10U-Black-Softball"),
    ])
    db.commit()
    res = roster_coverage(sess.id, db)
    missing = [m["raw_name"] for m in res["missing_players"]]
    assert missing == ["June-Wampach"]
    present = [p["raw_name"] for p in res["present_players"]]
    assert present == ["Eleanor-Pederson"]


def test_extra_cluster_on_different_team_carries_team_raw(db):
    """Cluster's auto_label maps to a player on a DIFFERENT team in the roster
    → extra_cluster with `roster_team_raw` set to that other team."""
    _, (a, b) = _job(db, "10U-Black-Softball", "11UA-Baseball")
    rogue = _cluster(db, a, label="Jack-Smith")    # photographed in team A
    replace_job_roster(db, a.job_id, [
        ("Jack-Smith", "11UA-Baseball"),            # but roster says team B
    ])
    db.commit()
    res = roster_coverage(a.id, db)
    assert len(res["extra_clusters"]) == 1
    extra = res["extra_clusters"][0]
    assert extra["cluster_id"] == rogue.id
    assert extra["roster_team_raw"] == "11UA-Baseball"


def test_extra_cluster_not_in_roster_at_all(db):
    """Cluster auto-labeled with a name not on the roster anywhere →
    extra_cluster with roster_team_raw = None."""
    _, (sess,) = _job(db, "10U-Black-Softball")
    rogue = _cluster(db, sess, label="Unknown-Player")
    replace_job_roster(db, sess.job_id, [
        ("Eleanor-Pederson", "10U-Black-Softball"),
    ])
    db.commit()
    res = roster_coverage(sess.id, db)
    assert len(res["extra_clusters"]) == 1
    assert res["extra_clusters"][0]["cluster_id"] == rogue.id
    assert res["extra_clusters"][0]["roster_team_raw"] is None


def test_cluster_with_null_label_goes_to_unidentified(db):
    _, (sess,) = _job(db, "10U-Black-Softball")
    c = _cluster(db, sess, label=None)
    replace_job_roster(db, sess.job_id, [
        ("Eleanor-Pederson", "10U-Black-Softball"),
    ])
    db.commit()
    res = roster_coverage(sess.id, db)
    assert len(res["unidentified_clusters"]) == 1
    assert res["unidentified_clusters"][0]["cluster_id"] == c.id


def test_cluster_with_blank_label_goes_to_unidentified(db):
    _, (sess,) = _job(db, "10U-Black-Softball")
    _cluster(db, sess, label="   ")
    replace_job_roster(db, sess.job_id, [
        ("Eleanor-Pederson", "10U-Black-Softball"),
    ])
    db.commit()
    assert len(roster_coverage(sess.id, db)["unidentified_clusters"]) == 1


# ── alias-mapped sessions ───────────────────────────────────────────────

def test_alias_drives_expected_team(db):
    """Folder is '10U Black' but mapped to '10U-Black-Softball' via Phase 6.1.
    Coverage should use the alias as the effective team."""
    _, (sess,) = _job(db, "10U Black")
    sess.roster_team_alias = "10U-Black-Softball"
    db.commit()
    _cluster(db, sess, label="Eleanor-Pederson")
    replace_job_roster(db, sess.job_id, [
        ("Eleanor-Pederson", "10U-Black-Softball"),
    ])
    db.commit()
    res = roster_coverage(sess.id, db)
    assert res["expected_team_raw"] == "10U-Black-Softball"
    assert len(res["present_players"]) == 1


# ── no roster / empty cases ─────────────────────────────────────────────

def test_no_roster_returns_empty_buckets(db):
    _, (sess,) = _job(db, "10U-Black-Softball")
    _cluster(db, sess, label="Anyone")
    db.commit()
    res = roster_coverage(sess.id, db)
    assert res["expected_players"] == []
    assert res["present_players"] == []
    assert res["missing_players"] == []
    # Without a roster, every cluster's label fails the lookup → extras.
    # We don't claim mismatch here, just bucket as "extra not in roster".
    assert all(x["roster_team_raw"] is None for x in res["extra_clusters"])


def test_jobless_session_returns_empty(db):
    sess = DbSession(name="standalone", source_path="/tmp/x", status="done",
                     created_at=datetime.utcnow())
    db.add(sess); db.commit(); db.refresh(sess)
    res = roster_coverage(sess.id, db)
    assert res["expected_players"] == []


def test_404_when_session_missing(db):
    with pytest.raises(HTTPException) as exc:
        roster_coverage(99999, db)
    assert exc.value.status_code == 404


# ── duplicate names abstain (Phase 6 rule) ─────────────────────────────

def test_duplicate_roster_name_doesnt_pollute_extras(db):
    """Same player name on two teams in the CSV → ambiguous; should NOT
    cause a 'roster_team_raw' to be claimed for an extra cluster."""
    _, (a, b) = _job(db, "10U-Black-Softball", "11UA-Baseball")
    _cluster(db, a, label="Jack-Smith")
    replace_job_roster(db, a.job_id, [
        ("Jack-Smith", "10U-Black-Softball"),       # in BOTH teams' roster
        ("Jack-Smith", "11UA-Baseball"),
    ])
    db.commit()
    # Session A's expected team is 10U-Black-Softball. With duplicate roster
    # rows, Phase 6's lookup is ambiguous — we shouldn't end up saying
    # "extra on team B" since we can't pick a team unambiguously. The
    # cluster IS present in team A's expected set, so it's present_players.
    res = roster_coverage(a.id, db)
    assert any(p["raw_name"] == "Jack-Smith" for p in res["present_players"])


# ── archived clusters not surfaced specially (covered by clusters list) ─

def test_archived_session_still_returns_coverage(db):
    """We don't filter by archived here — coverage is informational, the UI
    can decide to hide it. Just don't crash."""
    _, (sess,) = _job(db, "10U-Black-Softball")
    sess.archived = 1
    _cluster(db, sess, label="Eleanor-Pederson")
    replace_job_roster(db, sess.job_id, [
        ("Eleanor-Pederson", "10U-Black-Softball"),
    ])
    db.commit()
    res = roster_coverage(sess.id, db)
    assert len(res["present_players"]) == 1
