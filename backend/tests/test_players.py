"""Phase A.1 — Player + PlayerMembership models, load service, and API.

The reference-photo system's identity spine: a global `Player` (unique person)
plus per-shoot `PlayerMembership` rows. Distinct from the Phase 6 `RosterEntry`
(roster cross-check) — see PHASE_A1_ROSTER_MODEL.md.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models.db_models import Job, Player, PlayerMembership
from app.services.roster import normalize_name


# Real shape from the Princeton sample — same CSV format the Phase 6 roster
# uses, plus a Coach- row (the one rule A.1 adds).
SAMPLE_CSV = (
    "Eleanor-Pederson,10U-Black-Softball\n"
    "June-Wampach,10U-Black-Softball\n"
    "Quinn-Gentz,10U-Black-Softball\n"
    "Brooks-Cruz-Carter,11UA-Baseball\n"
    "Lincoln-St-Marie,9UAA-Baseball\n"
    "carter-Johanson,Majors-2-Baseball\n"
    "Dublin-McDOnnell,Majors-2-Baseball\n"
    "Coach-10UBlack-SB,10U-Black-Softball\n"
)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'players-test.db'}",
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


def _job(db, name="Princeton 5-12") -> Job:
    job = Job(name=name, root_path="/tmp", has_lines=0)
    db.add(job); db.commit(); db.refresh(job)
    return job


# ── Section 1: models ─────────────────────────────────────────────────────

def test_tables_created_and_round_trip(db):
    """Base.metadata.create_all builds both tables; insert proves it."""
    job = _job(db)
    player = Player(norm_name="eleanorpederson", display_name="Eleanor-Pederson")
    db.add(player); db.flush()
    db.add(PlayerMembership(
        player_id=player.id, job_id=job.id,
        team_name="10U-Black-Softball",
        norm_team=normalize_name("10U-Black-Softball"),
        is_coach=0,
    ))
    db.commit()

    assert db.query(Player).count() == 1
    assert db.query(PlayerMembership).count() == 1
    membership = db.query(PlayerMembership).one()
    assert membership.player.display_name == "Eleanor-Pederson"
    assert membership.job.id == job.id
    assert player.memberships[0].id == membership.id


def test_player_norm_name_unique(db):
    """norm_name is the global dedup key — duplicates raise IntegrityError."""
    db.add(Player(norm_name="jacksmith", display_name="Jack-Smith"))
    db.commit()
    db.add(Player(norm_name="jacksmith", display_name="Jack-Smith-II"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_job_cascade_deletes_memberships_keeps_players(db):
    """Deleting a Job removes its memberships but leaves global Players."""
    job = _job(db)
    player = Player(norm_name="eleanorpederson", display_name="Eleanor-Pederson")
    db.add(player); db.flush()
    db.add(PlayerMembership(
        player_id=player.id, job_id=job.id,
        team_name="10U-Black-Softball", norm_team="10ublacksoftball", is_coach=0,
    ))
    db.commit()

    db.delete(job); db.commit()
    assert db.query(PlayerMembership).count() == 0   # cascaded away
    assert db.query(Player).count() == 1             # Player survives
