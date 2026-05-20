"""Roster mismatch detection (read-time `roster_mismatch` flag).

Covers:
  - Cluster with auto_label == None → abstain.
  - Cluster whose normalized name has no roster match → abstain.
  - Cluster whose roster team == session team → no flag.
  - Cluster whose roster team != session team → flag.
  - Same name on multiple teams in the roster → abstain (lookup-level dedupe).
  - Empty roster (none uploaded) → no flag.
  - `roster_mismatch` is NEVER written to Cluster.review_reason in the DB
    (read-time only); it appears in the /clusters response.
"""
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models.db_models import Cluster, Job, RosterEntry, Session
from app.services.roster import build_lookup, replace_job_roster
from app.services.roster_check import (
    ROSTER_MISMATCH, add_roster_flag, cluster_is_mismatched,
    session_norm_team,
)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'rc-test.db'}",
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


def _setup(db, *, session_team: str, cluster_auto_label: str | None,
           roster: list[tuple[str, str]]):
    """Build a one-job/one-session world: returns (cluster, session, lookup)."""
    job = Job(name="J", root_path="/tmp", has_lines=0)
    db.add(job); db.commit(); db.refresh(job)
    sess = Session(job_id=job.id, name=session_team,
                   source_path=f"/tmp/{session_team}", status="done",
                   created_at=datetime.utcnow())
    db.add(sess); db.commit(); db.refresh(sess)
    c = Cluster(session_id=sess.id, image_count=5, auto_label=cluster_auto_label)
    db.add(c); db.commit(); db.refresh(c)
    if roster:
        replace_job_roster(db, job.id, roster)
        db.commit()
    return c, sess, build_lookup(db, job.id)


# ── cluster_is_mismatched ────────────────────────────────────────────────

def test_no_roster_no_flag(db):
    c, s, lookup = _setup(
        db, session_team="10U-Black-Softball",
        cluster_auto_label="Eleanor-Pederson", roster=[],
    )
    assert lookup == {}
    assert cluster_is_mismatched(c, session_norm_team(s), lookup) is False


def test_auto_label_none_abstains(db):
    c, s, lookup = _setup(
        db, session_team="10U-Black-Softball",
        cluster_auto_label=None,
        roster=[("Eleanor-Pederson", "10U-Black-Softball")],
    )
    assert cluster_is_mismatched(c, session_norm_team(s), lookup) is False


def test_unknown_name_in_roster_abstains(db):
    """Auto_label that doesn't match any roster entry → no flag (could be a
    coach not in this CSV, a guest, a typo, etc.)."""
    c, s, lookup = _setup(
        db, session_team="10U-Black-Softball",
        cluster_auto_label="Unknown-Player",
        roster=[("Eleanor-Pederson", "10U-Black-Softball")],
    )
    assert cluster_is_mismatched(c, session_norm_team(s), lookup) is False


def test_player_on_correct_team_no_flag(db):
    c, s, lookup = _setup(
        db, session_team="10U-Black-Softball",
        cluster_auto_label="Eleanor-Pederson",
        roster=[("Eleanor-Pederson", "10U-Black-Softball")],
    )
    assert cluster_is_mismatched(c, session_norm_team(s), lookup) is False


def test_player_on_wrong_team_flagged(db):
    c, s, lookup = _setup(
        db, session_team="10U-Black-Softball",
        cluster_auto_label="Eleanor-Pederson",
        # Roster says Eleanor belongs to 11UA-Baseball, not 10U-Black-Softball.
        roster=[("Eleanor-Pederson", "11UA-Baseball")],
    )
    assert cluster_is_mismatched(c, session_norm_team(s), lookup) is True


def test_alias_overrides_folder_name_when_set(db):
    """Phase 6.1: with an alias, a folder named '10U Black' counts as the
    CSV's '10U-Black-Softball' for the mismatch check."""
    c, s, lookup = _setup(
        db, session_team="10U Black",   # short folder name
        cluster_auto_label="Eleanor-Pederson",
        roster=[("Eleanor-Pederson", "10U-Black-Softball")],
    )
    # Without alias, the names disagree → cluster would be flagged.
    assert cluster_is_mismatched(c, session_norm_team(s), lookup) is True
    # With alias set to the CSV's long-form team, they agree → no flag.
    s.roster_team_alias = "10U-Black-Softball"
    db.commit()
    assert cluster_is_mismatched(c, session_norm_team(s), lookup) is False


def test_alias_normalized_same_way_as_folder(db):
    """Alias goes through normalize_name just like Session.name does."""
    c, s, lookup = _setup(
        db, session_team="10U Black",
        cluster_auto_label="Eleanor-Pederson",
        roster=[("Eleanor-Pederson", "10U-Black-Softball")],
    )
    # Spaces/punctuation in the stored alias don't matter — same norm.
    s.roster_team_alias = "10u black softball"
    db.commit()
    assert cluster_is_mismatched(c, session_norm_team(s), lookup) is False


def test_alias_cleared_falls_back_to_folder_name(db):
    c, s, lookup = _setup(
        db, session_team="10U Black",
        cluster_auto_label="Eleanor-Pederson",
        roster=[("Eleanor-Pederson", "10U-Black-Softball")],
    )
    s.roster_team_alias = "10U-Black-Softball"
    db.commit()
    assert cluster_is_mismatched(c, session_norm_team(s), lookup) is False
    s.roster_team_alias = None     # clear
    db.commit()
    assert cluster_is_mismatched(c, session_norm_team(s), lookup) is True


def test_team_normalization_accepts_format_differences(db):
    """Folder name and CSV team can differ in punctuation/case — same norm."""
    c, s, lookup = _setup(
        db, session_team="10u black softball",  # space-separated session name
        cluster_auto_label="Eleanor-Pederson",
        roster=[("Eleanor-Pederson", "10U-Black-Softball")],
    )
    assert cluster_is_mismatched(c, session_norm_team(s), lookup) is False


def test_duplicate_name_on_two_teams_abstains(db):
    """Same name on multiple teams → build_lookup omits → no flag."""
    c, s, lookup = _setup(
        db, session_team="10U-Black-Softball",
        cluster_auto_label="Jack-Smith",
        roster=[
            ("Jack-Smith", "10U-Black-Softball"),
            ("Jack-Smith", "12UA-Baseball"),
        ],
    )
    assert "jacksmith" not in lookup  # omitted on purpose
    assert cluster_is_mismatched(c, session_norm_team(s), lookup) is False


def test_compound_hyphen_name_works(db):
    """Real-world multi-hyphen names from the source CSV."""
    c, s, lookup = _setup(
        db, session_team="11UA-Baseball",
        cluster_auto_label="Brooks-Cruz-Carter",
        roster=[("Brooks-Cruz-Carter", "11UA-Baseball")],
    )
    assert cluster_is_mismatched(c, session_norm_team(s), lookup) is False


def test_coach_code_round_trips(db):
    """`Coach-10UBlack-SB` is a regular roster row; same lookup path."""
    c, s, lookup = _setup(
        db, session_team="10U-Black-Softball",
        cluster_auto_label="Coach-10UBlack-SB",
        roster=[("Coach-10UBlack-SB", "10U-Black-Softball")],
    )
    assert cluster_is_mismatched(c, session_norm_team(s), lookup) is False


# ── add_roster_flag ──────────────────────────────────────────────────────

def test_add_flag_appends_when_mismatched():
    assert add_roster_flag(None, True) == ROSTER_MISMATCH
    assert add_roster_flag("", True) == ROSTER_MISMATCH
    assert add_roster_flag("outlier_high", True) == f"outlier_high,{ROSTER_MISMATCH}"


def test_add_flag_does_not_append_when_match():
    assert add_roster_flag("outlier_high", False) == "outlier_high"
    assert add_roster_flag(None, False) is None


def test_add_flag_dedupes_if_already_present():
    # Defensive: in case it ever lands in stored review_reason somehow.
    assert add_roster_flag(ROSTER_MISMATCH, True) == ROSTER_MISMATCH
    assert add_roster_flag(f"outlier_high,{ROSTER_MISMATCH}", True) == \
           f"outlier_high,{ROSTER_MISMATCH}"


def test_add_flag_strips_empty_parts():
    assert add_roster_flag(", outlier_high , ", True) == \
           f"outlier_high,{ROSTER_MISMATCH}"


# ── Flag is NEVER persisted to DB ───────────────────────────────────────

def test_flag_not_persisted_on_cluster(db):
    """After computing a mismatch, Cluster.review_reason in the DB is unchanged."""
    c, s, lookup = _setup(
        db, session_team="10U-Black-Softball",
        cluster_auto_label="Eleanor-Pederson",
        roster=[("Eleanor-Pederson", "11UA-Baseball")],
    )
    # Mid-pipeline reason set by outlier flagger, say:
    c.review_reason = "outlier_high"
    db.commit()

    assert cluster_is_mismatched(c, session_norm_team(s), lookup) is True
    combined = add_roster_flag(c.review_reason, True)
    assert ROSTER_MISMATCH in combined

    # Reload from DB — stored value is still the plain outlier flag,
    # roster_mismatch lives only in the response string.
    db.expire(c); db.refresh(c)
    assert c.review_reason == "outlier_high"
    assert ROSTER_MISMATCH not in c.review_reason
