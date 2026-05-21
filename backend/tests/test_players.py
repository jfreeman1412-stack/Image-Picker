"""Phase A.1 — Player + PlayerMembership models, load service, and API.

The reference-photo system's identity spine: a global `Player` (unique person)
plus per-shoot `PlayerMembership` rows. Distinct from the Phase 6 `RosterEntry`
(roster cross-check) — see PHASE_A1_ROSTER_MODEL.md.
"""
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.db import Base, get_db
from app.main import app
from app.models.db_models import Job, Player, PlayerMembership, Session
from app.services.players import (
    is_coach_name, load_shoot_roster_from_text, replace_shoot_memberships,
    upsert_player,
)
from app.services.roster import normalize_name, parse_csv


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


# ── Section 2: coach detection ────────────────────────────────────────────

def test_is_coach_name_detects_prefix():
    assert is_coach_name("Coach-10UBlack-SB") is True
    assert is_coach_name("coach-anybody") is True       # case-insensitive
    assert is_coach_name("  Coach-Spaced  ") is True     # leading whitespace
    assert is_coach_name("Eleanor-Pederson") is False
    assert is_coach_name("Coachman-Lee") is False        # no hyphen after Coach
    assert is_coach_name("Coach") is False               # no hyphen at all


# ── Section 2: player identity / upsert ───────────────────────────────────

def test_upsert_creates_then_reuses(db):
    player1, created1 = upsert_player(db, "Eleanor-Pederson")
    assert created1 is True
    player2, created2 = upsert_player(db, "eleanor pederson")  # same norm
    assert created2 is False
    assert player2.id == player1.id
    assert db.query(Player).count() == 1


def test_upsert_keeps_first_seen_display_name(db):
    p1, _ = upsert_player(db, "carter-Johanson")
    p2, created = upsert_player(db, "Carter-Johanson")  # same norm, diff case
    assert created is False
    assert p2.id == p1.id
    assert p2.display_name == "carter-Johanson"   # first-seen form preserved
    assert db.query(Player).count() == 1


def test_upsert_skips_empty_normalized_name(db):
    player, created = upsert_player(db, "---")   # normalizes to ""
    assert player is None
    assert created is False
    assert db.query(Player).count() == 0


# ── Section 2: membership loading ─────────────────────────────────────────

def test_load_one_membership_per_row_with_coach_flag(db):
    job = _job(db)
    summary = load_shoot_roster_from_text(db, job.id, SAMPLE_CSV)
    assert summary["memberships_loaded"] == 8
    assert summary["players_created"] == 8
    assert summary["coaches"] == 1
    assert summary["distinct_teams"] == 4
    assert summary["entries_skipped"] == 0

    coach_rows = db.query(PlayerMembership).filter_by(is_coach=1).all()
    assert len(coach_rows) == 1
    assert coach_rows[0].player.display_name == "Coach-10UBlack-SB"
    # Everyone else is a player (is_coach=0).
    assert db.query(PlayerMembership).filter_by(is_coach=0).count() == 7


def test_cross_shoot_identity_one_player_two_memberships(db):
    """The same name in two shoots → ONE Player, TWO memberships."""
    job_a = _job(db, name="Shoot A")
    job_b = _job(db, name="Shoot B")
    csv_a = "Eleanor-Pederson,10U-Black-Softball\n"
    csv_b = "Eleanor-Pederson,Fall-Travel\n"

    sum_a = load_shoot_roster_from_text(db, job_a.id, csv_a)
    sum_b = load_shoot_roster_from_text(db, job_b.id, csv_b)

    assert sum_a["players_created"] == 1
    assert sum_b["players_created"] == 0
    assert sum_b["players_existing"] == 1   # reused from shoot A
    assert db.query(Player).count() == 1
    player = db.query(Player).one()
    assert len(player.memberships) == 2
    assert {m.job_id for m in player.memberships} == {job_a.id, job_b.id}


def test_reupload_replaces_not_appends(db):
    job = _job(db)
    load_shoot_roster_from_text(db, job.id, SAMPLE_CSV)
    load_shoot_roster_from_text(db, job.id, SAMPLE_CSV)   # same again
    assert db.query(PlayerMembership).filter_by(job_id=job.id).count() == 8
    assert db.query(Player).count() == 8   # not duplicated


def test_reupload_one_shoot_does_not_touch_another(db):
    job_a = _job(db, name="A")
    job_b = _job(db, name="B")
    load_shoot_roster_from_text(db, job_a.id, "Eleanor-Pederson,T1\n")
    load_shoot_roster_from_text(db, job_b.id, "June-Wampach,T2\n")
    # Re-upload A with a different roster; B must be untouched.
    load_shoot_roster_from_text(db, job_a.id, "Quinn-Gentz,T1\n")

    assert db.query(PlayerMembership).filter_by(job_id=job_b.id).count() == 1
    assert db.query(Player).filter_by(norm_name="junewampach").count() == 1
    # A's old player still exists (orphaned), B's intact.
    assert db.query(Player).filter_by(norm_name="eleanorpederson").count() == 1


def test_player_on_two_teams_same_shoot_two_memberships(db):
    job = _job(db)
    csv_text = "Jack-Smith,10U-Black-Softball\nJack-Smith,11UA-Baseball\n"
    summary = load_shoot_roster_from_text(db, job.id, csv_text)
    assert summary["memberships_loaded"] == 2
    assert db.query(Player).count() == 1
    assert db.query(PlayerMembership).filter_by(job_id=job.id).count() == 2


def test_malformed_rows_skipped_and_counted(db):
    job = _job(db)
    csv_text = (
        "Eleanor-Pederson,10U-Black-Softball\n"
        "\n"                       # blank
        "Lonely\n"                 # 1 col
        "Solo,Cell,Extra\n"        # 3 cols
        "June-Wampach,10U-Black-Softball\n"
    )
    summary = load_shoot_roster_from_text(db, job.id, csv_text)
    assert summary["memberships_loaded"] == 2
    assert summary["entries_skipped"] == 3


def test_blank_normalized_name_row_skipped(db):
    """A row that parses (non-empty cells) but normalizes to empty is counted
    as rows_skipped_blank_name, not loaded as a membership."""
    job = _job(db)
    rows, _ = parse_csv("---,SomeTeam\nEleanor-Pederson,10U-Black-Softball\n")
    summary = replace_shoot_memberships(db, job.id, rows)
    db.commit()
    assert summary["memberships_loaded"] == 1
    assert summary["rows_skipped_blank_name"] == 1


def test_load_service_job_cascade(db):
    """db.delete(job) removes that job's memberships, leaves Players."""
    job = _job(db)
    load_shoot_roster_from_text(db, job.id, SAMPLE_CSV)
    assert db.query(PlayerMembership).count() == 8

    db.delete(job); db.commit()
    assert db.query(PlayerMembership).count() == 0
    assert db.query(Player).count() == 8   # global, survive the shoot


def test_load_service_unknown_job_404(db):
    with pytest.raises(HTTPException) as exc:
        load_shoot_roster_from_text(db, 99999, SAMPLE_CSV)
    assert exc.value.status_code == 404


# ── Section 3: HTTP endpoints ─────────────────────────────────────────────

@pytest.fixture
def client(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'players-http.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(bind=engine)

    def _override():
        s = TestingSessionLocal()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override
    try:
        # Seed two jobs (two shoots) + a couple sessions on the first, parity
        # with test_roster.py and so cross-shoot identity is testable over HTTP.
        seed = TestingSessionLocal()
        try:
            job_a = Job(name="Shoot A", root_path="/tmp", has_lines=0)
            job_b = Job(name="Shoot B", root_path="/tmp", has_lines=0)
            seed.add_all([job_a, job_b]); seed.commit()
            seed.refresh(job_a); seed.refresh(job_b)
            for t in ("10U-Black-Softball", "11UA-Baseball"):
                seed.add(Session(job_id=job_a.id, name=t,
                                 source_path=f"/tmp/{t}", status="done"))
            seed.commit()
            job_a_id, job_b_id = job_a.id, job_b.id
        finally:
            seed.close()
        c = TestClient(app)
        c.job_a_id = job_a_id
        c.job_b_id = job_b_id
        yield c
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_endpoint_upload_get_delete_round_trip(client):
    job_id = client.job_a_id
    res = client.post(
        f"/api/players/roster/{job_id}",
        files={"file": ("roster.csv", SAMPLE_CSV.encode("utf-8"), "text/csv")},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["memberships_loaded"] == 8
    assert body["coaches"] == 1
    assert body["players_created"] == 8

    res = client.get(f"/api/players/roster/{job_id}")
    assert res.status_code == 200
    listing = res.json()
    assert listing["memberships_loaded"] == 8
    assert len(listing["items"]) == 8
    coach_items = [i for i in listing["items"] if i["is_coach"]]
    assert len(coach_items) == 1
    assert coach_items[0]["name"] == "Coach-10UBlack-SB"

    res = client.delete(f"/api/players/roster/{job_id}")
    assert res.status_code == 200
    assert res.json()["deleted"] == 8

    res = client.get(f"/api/players/roster/{job_id}")
    assert res.json()["memberships_loaded"] == 0


def test_endpoint_upload_unknown_job_404(client):
    res = client.post(
        "/api/players/roster/99999",
        files={"file": ("r.csv", b"A,B\n", "text/csv")},
    )
    assert res.status_code == 404


def test_endpoint_upload_malformed_rows_skipped(client):
    job_id = client.job_a_id
    csv_text = (
        "Eleanor-Pederson,10U-Black-Softball\n"
        "\n"                  # blank
        "Lonely\n"            # short row
        "June-Wampach,10U-Black-Softball\n"
    )
    res = client.post(
        f"/api/players/roster/{job_id}",
        files={"file": ("r.csv", csv_text.encode("utf-8"), "text/csv")},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["memberships_loaded"] == 2
    assert body["entries_skipped"] > 0


def test_endpoint_list_players_query_and_paging(client):
    job_id = client.job_a_id
    client.post(
        f"/api/players/roster/{job_id}",
        files={"file": ("roster.csv", SAMPLE_CSV.encode("utf-8"), "text/csv")},
    )
    # All 8 unique players.
    res = client.get("/api/players")
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 8
    assert len(body["items"]) == 8
    assert all("membership_count" in i for i in body["items"])

    # ?q= filters by normalized substring.
    res = client.get("/api/players", params={"q": "eleanor"})
    body = res.json()
    assert body["total"] == 1
    assert body["items"][0]["name"] == "Eleanor-Pederson"
    assert body["items"][0]["membership_count"] == 1

    # ?limit/?offset pages (total stays the full match count).
    res = client.get("/api/players", params={"limit": 3, "offset": 0})
    body = res.json()
    assert body["total"] == 8
    assert len(body["items"]) == 3
    res2 = client.get("/api/players", params={"limit": 3, "offset": 3})
    assert len(res2.json()["items"]) == 3
    # Disjoint pages.
    page1_ids = {i["id"] for i in body["items"]}
    page2_ids = {i["id"] for i in res2.json()["items"]}
    assert page1_ids.isdisjoint(page2_ids)


def test_endpoint_player_detail_cross_shoot(client):
    """GET /api/players/{id} shows memberships across two uploaded shoots."""
    job_a, job_b = client.job_a_id, client.job_b_id
    client.post(
        f"/api/players/roster/{job_a}",
        files={"file": ("a.csv", b"Eleanor-Pederson,10U-Black-Softball\n", "text/csv")},
    )
    client.post(
        f"/api/players/roster/{job_b}",
        files={"file": ("b.csv", b"Eleanor-Pederson,Fall-Travel\n", "text/csv")},
    )
    # Find the (single) player id.
    listing = client.get("/api/players", params={"q": "eleanor"}).json()
    assert listing["total"] == 1
    player_id = listing["items"][0]["id"]
    assert listing["items"][0]["membership_count"] == 2

    res = client.get(f"/api/players/{player_id}")
    assert res.status_code == 200
    body = res.json()
    assert body["name"] == "Eleanor-Pederson"
    job_ids = {m["job_id"] for m in body["memberships"]}
    assert job_ids == {job_a, job_b}
    teams = {m["team"] for m in body["memberships"]}
    assert teams == {"10U-Black-Softball", "Fall-Travel"}


def test_endpoint_player_detail_unknown_404(client):
    res = client.get("/api/players/99999")
    assert res.status_code == 404
