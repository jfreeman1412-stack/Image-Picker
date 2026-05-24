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
from app.models.db_models import (
    Job, Player, PlayerMembership, ReferenceFace, Session,
)
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
        c.SessionLocal = TestingSessionLocal  # for tests that seed rows directly
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


# ── Phase B.2: roster reference-status (shoot-scoped ✓) ────────────────────

def _seed_ref(client, player_id, captured_job_id):
    """Insert a ReferenceFace row directly (no detector needed — the endpoint
    only reads player_id + captured_job_id)."""
    s = client.SessionLocal()
    try:
        s.add(ReferenceFace(
            player_id=player_id, captured_job_id=captured_job_id,
            image_path="x", embedding=b"\x00", det_score=0.9,
        ))
        s.commit()
    finally:
        s.close()


def test_reference_status_is_shoot_scoped(client):
    """Only a reference captured FOR THIS shoot counts toward ✓. A ref from a
    different shoot, or with no provenance (NULL), does not — and the isolation
    holds in both directions (cross-shoot identity, one Player, two shoots)."""
    job_a, job_b = client.job_a_id, client.job_b_id
    client.post(f"/api/players/roster/{job_a}",
                files={"file": ("a.csv",
                                b"Eleanor-Pederson,T1\nJune-Wampach,T1\n", "text/csv")})
    client.post(f"/api/players/roster/{job_b}",
                files={"file": ("b.csv", b"Eleanor-Pederson,T2\n", "text/csv")})

    roster_a = client.get(f"/api/players/roster/{job_a}").json()["items"]
    pid = {i["name"]: i["player_id"] for i in roster_a}
    eleanor, june = pid["Eleanor-Pederson"], pid["June-Wampach"]

    _seed_ref(client, eleanor, job_a)   # Eleanor: captured FOR job_a
    _seed_ref(client, june, job_b)      # June: captured for a DIFFERENT shoot
    _seed_ref(client, june, None)       # June: a no-provenance (B.1-style) ref

    status_a = client.get(f"/api/players/roster/{job_a}/reference-status")
    assert status_a.status_code == 200, status_a.text
    ids_a = status_a.json()["player_ids_with_references"]
    assert eleanor in ids_a             # ✓ — has a photo for THIS shoot
    assert june not in ids_a            # only other-shoot / NULL refs → not ✓

    # job_b sees the mirror image: June's job_b ref counts; Eleanor's job_a
    # ref does not leak across.
    ids_b = client.get(f"/api/players/roster/{job_b}/reference-status").json()[
        "player_ids_with_references"]
    assert june in ids_b
    assert eleanor not in ids_b


def test_reference_status_empty_when_no_refs_for_shoot(client):
    job_a = client.job_a_id
    client.post(f"/api/players/roster/{job_a}",
                files={"file": ("a.csv", b"Eleanor-Pederson,T1\n", "text/csv")})
    res = client.get(f"/api/players/roster/{job_a}/reference-status")
    assert res.status_code == 200
    assert res.json()["player_ids_with_references"] == []


def test_reference_status_unknown_job_404(client):
    res = client.get("/api/players/roster/99999/reference-status")
    assert res.status_code == 404


# ── Phase C.1 Section 1: roster CSV inspect ───────────────────────────────

from app.services.players import inspect_roster_csv  # noqa: E402


def test_inspect_returns_columns_samples_and_count():
    text = (
        "First,Last,Team,Parent Email\n"
        "Ava,Nguyen,Lions,a@x.com\n"
        "Mason,Reyes,Lions,b@x.com\n"
        "Sofia,Petrov,Tigers,c@x.com\n"
    )
    out = inspect_roster_csv(text)
    assert out["columns"] == ["First", "Last", "Team", "Parent Email"]
    assert out["sample_rows"][0] == ["Ava", "Nguyen", "Lions", "a@x.com"]
    assert len(out["sample_rows"]) == 3       # the 3 data rows (sample_size>=3)
    assert out["total_rows"] == 4             # header included; blank rows dropped


def test_inspect_quoted_comma_stays_one_cell():
    """Proves we parse with the csv module, not str.split(',')."""
    text = 'Name,Team\n"Carter, Jr.",Bears\n'
    out = inspect_roster_csv(text)
    assert out["columns"] == ["Name", "Team"]
    assert out["sample_rows"][0] == ["Carter, Jr.", "Bears"]


def test_inspect_empty_csv():
    assert inspect_roster_csv("\n\n") == {
        "columns": [], "sample_rows": [], "total_rows": 0,
    }


def test_inspect_sample_size_caps_rows():
    text = "H1,H2\n" + "".join(f"a{i},b{i}\n" for i in range(20))
    out = inspect_roster_csv(text, sample_size=5)
    assert len(out["sample_rows"]) == 5
    assert out["total_rows"] == 21            # header + 20 data rows


def test_inspect_endpoint_200(client):
    job_id = client.job_a_id
    csv_bytes = b"Player Name,Team\nEleanor-Pederson,10U-Black-Softball\n"
    res = client.post(
        f"/api/players/roster/{job_id}/inspect",
        files={"file": ("roster.csv", csv_bytes, "text/csv")},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["columns"] == ["Player Name", "Team"]
    assert body["total_rows"] == 2


def test_inspect_endpoint_unknown_job_404(client):
    res = client.post(
        "/api/players/roster/99999/inspect",
        files={"file": ("r.csv", b"A,B\n", "text/csv")},
    )
    assert res.status_code == 404


# ── Phase C.1 Section 2: mapped parse + strict validation ─────────────────

from app.services.players import (  # noqa: E402
    build_validation_report, parse_mapped_roster, validate_mapping,
)

_FULL = {"has_header": True, "name_mode": "full",
         "name_column": "Name", "team_column": "Team"}
_SPLIT = {"has_header": True, "name_mode": "split",
          "first_name_column": "First", "last_name_column": "Last",
          "team_column": "Team"}


def test_validate_mapping_complete_and_incomplete():
    assert validate_mapping(_FULL) == []
    assert validate_mapping(_SPLIT) == []
    # full without a name column
    assert "name column not mapped" in validate_mapping(
        {"name_mode": "full", "team_column": "Team"})
    # split with neither first nor last
    assert any("at least one" in e for e in validate_mapping(
        {"name_mode": "split", "team_column": "Team"}))
    # no team
    assert "team column not mapped" in validate_mapping(
        {"name_mode": "full", "name_column": "Name"})
    # bad mode
    assert any("name mode" in e for e in validate_mapping({"team_column": "Team"}))


def test_parse_full_mode_happy():
    text = "Name,Team\nEleanor-Pederson,10U\nCoach-Lions,10U\n"
    rows, errs = parse_mapped_roster(text, _FULL)
    assert rows == [("Eleanor-Pederson", "10U"), ("Coach-Lions", "10U")]
    assert errs == []


def test_parse_split_mode_joins_first_last():
    text = "First,Last,Team\nAva,Nguyen,Lions\n"
    rows, errs = parse_mapped_roster(text, _SPLIT)
    assert rows == [("Ava Nguyen", "Lions")]
    assert errs == []


def test_parse_split_last_name_only_is_valid():
    text = "First,Last,Team\n,Nguyen,Lions\nAva,,Tigers\n"
    rows, errs = parse_mapped_roster(text, _SPLIT)
    assert rows == [("Nguyen", "Lions"), ("Ava", "Tigers")]  # both valid
    assert errs == []


def test_parse_missing_name_and_team_with_excel_row_numbers():
    # Header is row 1; data rows are 2,3,4,5. A blank record is skipped but
    # still consumes its row number, so downstream rows keep Excel numbering.
    text = (
        "Name,Team\n"          # row 1 (header)
        "Ava,Lions\n"          # row 2 ok
        "Mason,\n"             # row 3 missing team
        "\n"                   # row 4 blank → skipped (not an error)
        ",Bears\n"             # row 5 missing name
    )
    rows, errs = parse_mapped_roster(text, _FULL)
    assert rows == [("Ava", "Lions")]
    by_reason = {e["reason"]: e["rows"] for e in errs}
    assert by_reason["missing_team"] == [3]
    assert by_reason["missing_name"] == [5]


def test_parse_headerless_uses_positional_and_rows_from_one():
    mapping = {"has_header": False, "name_mode": "full",
               "name_column": "Column 1", "team_column": "Column 2"}
    text = "Ava,Lions\n,Tigers\n"          # row 1 ok, row 2 missing name
    rows, errs = parse_mapped_roster(text, mapping)
    assert rows == [("Ava", "Lions")]
    assert errs[0]["reason"] == "missing_name"
    assert errs[0]["rows"] == [2]


def test_parse_ignores_unmapped_pii_columns():
    text = (
        "First,Last,Team,Parent Email,Phone\n"
        "Ava,Nguyen,Lions,a@x.com,555-0100\n"
    )
    rows, _ = parse_mapped_roster(text, _SPLIT)
    assert rows == [("Ava Nguyen", "Lions")]  # PII columns never read


def test_parse_quoted_comma_in_team():
    text = 'Name,Team\nAva,"Lions, A"\n'
    rows, _ = parse_mapped_roster(text, _FULL)
    assert rows == [("Ava", "Lions, A")]


def test_parse_ragged_short_row_treated_as_missing():
    text = "Name,Team\nAva\n"  # row 2 has no team cell at all
    rows, errs = parse_mapped_roster(text, _FULL)
    assert rows == []
    assert errs[0]["reason"] == "missing_team" and errs[0]["rows"] == [2]


def test_report_ok_summary_and_preview():
    text = ("Name,Team\nEleanor-Pederson,10U\nJune-Wampach,10U\n"
            "Coach-Lions,11U\n")
    rep = build_validation_report(text, _FULL)
    assert rep["ok"] is True
    assert rep["row_errors"] == [] and rep["mapping_errors"] == []
    assert rep["summary"] == {
        "valid_rows": 3, "invalid_rows": 0, "distinct_teams": 2, "coaches": 1,
        "duplicate_rows_collapsed": 0,
    }
    assert rep["preview"][0] == {"name": "Eleanor-Pederson", "team": "10U",
                                 "is_coach": False}
    assert rep["preview"][2]["is_coach"] is True   # Coach- flagged


def test_report_mapping_incomplete_short_circuits():
    rep = build_validation_report("Name,Team\nAva,Lions\n",
                                  {"name_mode": "full", "team_column": "Team"})
    assert rep["ok"] is False
    assert rep["error"] == "roster_validation"
    assert "name column not mapped" in rep["mapping_errors"]
    assert rep["row_errors"] == []   # didn't parse rows


def test_report_row_errors_block_and_dedupe_invalid_count():
    # Row 3 has empty name AND empty team but a non-blank (unmapped) cell, so
    # it isn't skipped — it lands in BOTH error groups yet counts once.
    text = (
        "Name,Team,Note\n"   # row 1 header
        "Ava,Lions,x\n"      # row 2 ok
        ",,keep\n"           # row 3 missing name AND team (not blank: 'keep')
    )
    rep = build_validation_report(text, _FULL)
    assert rep["ok"] is False
    assert rep["error"] == "roster_validation"
    assert rep["summary"]["valid_rows"] == 1
    assert rep["summary"]["invalid_rows"] == 1   # row 3 counted once, not twice
    reasons = {e["reason"] for e in rep["row_errors"]}
    assert reasons == {"missing_name", "missing_team"}
    for e in rep["row_errors"]:
        assert e["rows"] == [3]


# ── Phase C.1 Section 3: mapped commit endpoint + refs-replace guard ──────

import json  # noqa: E402


def _post_mapped(client, job_id, csv_text, mapping, *,
                 dry_run=False, confirm_replace=False):
    return client.post(
        f"/api/players/roster/{job_id}/mapped",
        data={
            "mapping": json.dumps(mapping),
            "dry_run": "true" if dry_run else "false",
            "confirm_replace": "true" if confirm_replace else "false",
        },
        files={"file": ("r.csv", csv_text.encode("utf-8"), "text/csv")},
    )


def test_mapped_dry_run_ok_writes_nothing(client):
    job_id = client.job_a_id
    csv_text = "Name,Team\nEleanor-Pederson,10U\nCoach-Lions,10U\n"
    res = _post_mapped(client, job_id, csv_text, _FULL, dry_run=True)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["summary"]["valid_rows"] == 2
    assert body["summary"]["coaches"] == 1
    assert body["references_warning"]["count"] == 0
    # nothing written
    assert client.get(f"/api/players/roster/{job_id}").json()["memberships_loaded"] == 0


def test_mapped_dry_run_reports_bad_rows(client):
    job_id = client.job_a_id
    csv_text = "Name,Team\nAva,Lions\nMason,\n"  # row 3 missing team
    res = _post_mapped(client, job_id, csv_text, _FULL, dry_run=True)
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is False
    by_reason = {e["reason"]: e["rows"] for e in body["row_errors"]}
    assert by_reason["missing_team"] == [3]


def test_mapped_commit_happy_flags_coach(client):
    job_id = client.job_a_id
    csv_text = ("Name,Team\nEleanor-Pederson,10U\nJune-Wampach,10U\n"
                "Coach-Lions,11U\n")
    res = _post_mapped(client, job_id, csv_text, _FULL)
    assert res.status_code == 200, res.text
    assert res.json()["memberships_loaded"] == 3
    assert res.json()["coaches"] == 1
    listing = client.get(f"/api/players/roster/{job_id}").json()
    assert listing["memberships_loaded"] == 3
    assert [i for i in listing["items"] if i["is_coach"]][0]["name"] == "Coach-Lions"


def test_mapped_commit_split_mode_joins(client):
    job_id = client.job_a_id
    csv_text = "First,Last,Team\nAva,Nguyen,Lions\n,Solo,Tigers\n"
    res = _post_mapped(client, job_id, csv_text, _SPLIT)
    assert res.status_code == 200, res.text
    names = {i["name"] for i in client.get(f"/api/players/roster/{job_id}").json()["items"]}
    assert names == {"Ava Nguyen", "Solo"}   # last-name-only kept


def test_mapped_commit_bad_rows_400_writes_nothing(client):
    job_id = client.job_a_id
    res = _post_mapped(client, job_id, "Name,Team\nAva,\n", _FULL)
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert detail["error"] == "roster_validation"
    assert detail["row_errors"][0]["reason"] == "missing_team"
    assert client.get(f"/api/players/roster/{job_id}").json()["memberships_loaded"] == 0


def test_mapped_commit_incomplete_mapping_400(client):
    job_id = client.job_a_id
    bad_map = {"has_header": True, "name_mode": "full", "name_column": "Name"}  # no team
    res = _post_mapped(client, job_id, "Name,Team\nAva,Lions\n", bad_map)
    assert res.status_code == 400
    assert "team column not mapped" in res.json()["detail"]["mapping_errors"]


def test_mapped_commit_bad_json_400(client):
    job_id = client.job_a_id
    res = client.post(
        f"/api/players/roster/{job_id}/mapped",
        data={"mapping": "not-json", "dry_run": "false", "confirm_replace": "false"},
        files={"file": ("r.csv", b"Name,Team\nAva,Lions\n", "text/csv")},
    )
    assert res.status_code == 400
    assert res.json()["detail"]["error"] == "bad_mapping_json"


def test_mapped_reupload_replaces_not_appends(client):
    job_id = client.job_a_id
    csv_text = "Name,Team\nEleanor-Pederson,10U\nJune-Wampach,10U\n"
    _post_mapped(client, job_id, csv_text, _FULL)
    _post_mapped(client, job_id, csv_text, _FULL)   # same again
    assert client.get(f"/api/players/roster/{job_id}").json()["memberships_loaded"] == 2


def test_mapped_unknown_job_404(client):
    res = _post_mapped(client, 99999, "Name,Team\nAva,Lions\n", _FULL)
    assert res.status_code == 404


def test_mapped_replace_guarded_by_captured_references(client):
    """A roster replace is blocked with 409 when references were already
    captured for this shoot, unless confirm_replace is set."""
    job_id = client.job_a_id
    _post_mapped(client, job_id, "Name,Team\nEleanor-Pederson,10U\n", _FULL)
    pid = client.get(f"/api/players/roster/{job_id}").json()["items"][0]["player_id"]
    _seed_ref(client, pid, job_id)   # a reference captured FOR this shoot

    blocked = _post_mapped(client, job_id, "Name,Team\nJune-Wampach,10U\n", _FULL)
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["error"] == "references_exist"
    assert blocked.json()["detail"]["count"] == 1
    # roster unchanged by the blocked call
    assert client.get(f"/api/players/roster/{job_id}").json()["items"][0]["name"] \
        == "Eleanor-Pederson"

    ok = _post_mapped(client, job_id, "Name,Team\nJune-Wampach,10U\n", _FULL,
                      confirm_replace=True)
    assert ok.status_code == 200


# ── Phase C.1 fix: duplicate (player, team) rows collapse, never 500 ──────

def test_replace_collapses_duplicate_player_team(db):
    """Same person on the same team twice → one membership, no IntegrityError
    (the uq_membership_job_player_team constraint that crashed real rosters)."""
    job = _job(db)
    rows = [("Coach-Squirt", "Squirt-B2-White"),
            ("Coach-Squirt", "Squirt-B2-White"),   # exact repeat (the 500 case)
            ("Ava-Nguyen", "Squirt-B2-White")]
    summary = replace_shoot_memberships(db, job.id, rows)
    db.commit()
    assert summary["memberships_loaded"] == 2
    assert summary["duplicate_memberships_collapsed"] == 1
    assert db.query(PlayerMembership).filter_by(job_id=job.id).count() == 2


def test_replace_same_player_two_teams_not_collapsed(db):
    """A player on two DIFFERENT teams is not a duplicate — both kept."""
    job = _job(db)
    summary = replace_shoot_memberships(
        db, job.id, [("Jack-Smith", "TeamX"), ("Jack-Smith", "TeamY")])
    db.commit()
    assert summary["memberships_loaded"] == 2
    assert summary["duplicate_memberships_collapsed"] == 0
    assert db.query(Player).count() == 1


def test_mapped_commit_dedups_duplicate_rows(client):
    """The real-roster 500 reproduction: a coach listed twice on a team now
    commits cleanly with the duplicate collapsed."""
    job_id = client.job_a_id
    csv_text = ("Name,Team\nCoach-Squirt,Squirt-B2-White\n"
                "Coach-Squirt,Squirt-B2-White\nAva-Nguyen,Squirt-B2-White\n")
    res = _post_mapped(client, job_id, csv_text, _FULL)
    assert res.status_code == 200, res.text     # was 500 before the fix
    assert res.json()["memberships_loaded"] == 2
    assert res.json()["duplicate_memberships_collapsed"] == 1


def test_dry_run_reports_collapsed_duplicates(client):
    job_id = client.job_a_id
    csv_text = "Name,Team\nCoach-Squirt,Squirt-B2-White\nCoach-Squirt,Squirt-B2-White\n"
    body = _post_mapped(client, job_id, csv_text, _FULL, dry_run=True).json()
    assert body["ok"] is True
    assert body["summary"]["valid_rows"] == 1
    assert body["summary"]["duplicate_rows_collapsed"] == 1


def test_captured_reference_survives_roster_replace(client):
    """THE durable-identity invariant: replacing a job's roster re-points
    memberships only — it never destroys the global Player or its captured
    reference photos, even when the new roster omits that player."""
    job_id = client.job_a_id
    _post_mapped(client, job_id,
                 "Name,Team\nEleanor-Pederson,10U\nJune-Wampach,10U\n", _FULL)
    items = client.get(f"/api/players/roster/{job_id}").json()["items"]
    eleanor = {i["name"]: i["player_id"] for i in items}["Eleanor-Pederson"]
    _seed_ref(client, eleanor, job_id)   # Eleanor has a captured reference

    # Re-upload a roster that OMITS Eleanor (only June). Confirm past the guard.
    res = _post_mapped(client, job_id, "Name,Team\nJune-Wampach,10U\n", _FULL,
                       confirm_replace=True)
    assert res.status_code == 200, res.text

    s = client.SessionLocal()
    try:
        # Player identity preserved (orphaned of a membership, but intact)…
        player = s.query(Player).filter_by(id=eleanor).one_or_none()
        assert player is not None and player.display_name == "Eleanor-Pederson"
        # …her captured reference is untouched (player + shoot provenance intact)…
        ref = s.query(ReferenceFace).filter_by(player_id=eleanor).one_or_none()
        assert ref is not None and ref.captured_job_id == job_id
        # …only her membership for this shoot is gone; June's is present.
        assert s.query(PlayerMembership).filter_by(
            job_id=job_id, player_id=eleanor).count() == 0
        assert s.query(PlayerMembership).filter_by(job_id=job_id).count() == 1
    finally:
        s.close()
