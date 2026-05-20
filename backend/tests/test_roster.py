"""Roster CSV parse, normalize, store, and endpoint round-trip."""
import io

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.roster import load_roster_from_text, delete_roster, get_roster
from app.db import Base, get_db
from app.main import app
from app.models.db_models import Job, RosterEntry, Session
from app.services.roster import (
    CsvParseError, decode_bytes, get_duplicate_names, lookup_expected_team,
    normalize_name, parse_csv, replace_job_roster,
)


# Real shape from the Princeton sample the user pasted — same as their
# production CSV so the tests cover the format that actually lands.
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
        f"sqlite:///{tmp_path / 'roster-test.db'}",
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


def _job(db, *team_names: str) -> Job:
    job = Job(name="Princeton 5-12", root_path="/tmp", has_lines=0)
    db.add(job); db.commit(); db.refresh(job)
    for t in team_names:
        s = Session(job_id=job.id, name=t, source_path=f"/tmp/{t}", status="done")
        db.add(s)
    db.commit()
    return job


# ── normalize_name ────────────────────────────────────────────────────────

def test_normalize_basic_hyphen():
    assert normalize_name("Eleanor-Pederson") == "eleanorpederson"


def test_normalize_multi_hyphen():
    # Compound surnames the user explicitly called out as in scope.
    assert normalize_name("Brooks-Cruz-Carter") == "brookscruzcarter"
    assert normalize_name("Lincoln-St-Marie")    == "lincolnstmarie"
    assert normalize_name("Aiden-Lira-Campos")   == "aidenliracampos"


def test_normalize_case_quirks():
    # Source CSV has these case oddities — they must collapse.
    assert normalize_name("carter-Johanson")   == "carterjohanson"
    assert normalize_name("Dublin-McDOnnell")  == "dublinmcdonnell"


def test_normalize_punctuation_and_whitespace():
    assert normalize_name("  Eleanor . Pederson_ ") == "eleanorpederson"
    assert normalize_name("Mary-Jo O'Brien")        == "maryjoobrien"


def test_normalize_unicode_strips_accents():
    assert normalize_name("José-García") == "josegarcia"
    assert normalize_name("Renée-Müller") == "reneemuller"


def test_normalize_empty_and_none():
    assert normalize_name("") == ""
    assert normalize_name(None) == ""
    assert normalize_name("   ") == ""


def test_normalize_team_matches_session_name():
    # The whole point: team column normalizes the same way Session.name does.
    assert normalize_name("10U-Black-Softball") == normalize_name("10u black softball")
    assert normalize_name("Majors-1-Baseball")   == normalize_name("majors1baseball")


# ── parse_csv ─────────────────────────────────────────────────────────────

def test_parse_csv_happy_path():
    rows, skips = parse_csv(SAMPLE_CSV)
    assert len(rows) == 8
    assert rows[0] == ("Eleanor-Pederson", "10U-Black-Softball")
    assert rows[3] == ("Brooks-Cruz-Carter", "11UA-Baseball")
    assert skips == []


def test_parse_csv_trims_whitespace_per_cell():
    rows, _ = parse_csv("  Eleanor-Pederson  ,  10U-Black-Softball  \n")
    assert rows == [("Eleanor-Pederson", "10U-Black-Softball")]


def test_parse_csv_skips_blank_and_malformed_rows():
    text = (
        "Eleanor-Pederson,10U-Black-Softball\n"
        "\n"                                   # blank
        ",10U-Black-Softball\n"                # empty name
        "Hannah-Thoennes,\n"                   # empty team
        "Solo,Cell,ExtraCol\n"                 # 3 cols
        "Lonely\n"                             # 1 col
        "June-Wampach,10U-Black-Softball\n"
    )
    rows, skips = parse_csv(text)
    assert rows == [
        ("Eleanor-Pederson", "10U-Black-Softball"),
        ("June-Wampach",     "10U-Black-Softball"),
    ]
    skip_reasons = [r for _, r in skips]
    assert "blank" in skip_reasons
    assert "empty name" in skip_reasons
    assert "empty team" in skip_reasons
    assert any("3" in r for r in skip_reasons)
    assert any("1" in r for r in skip_reasons)
    # Line numbers should be 1-based and monotonic.
    line_nums = [n for n, _ in skips]
    assert line_nums == sorted(line_nums)


# Note: stdlib `csv.reader` is intentionally tolerant — an unterminated
# quoted field just runs to EOF rather than raising. CsvParseError is kept
# for the rare csv.Error cases (field-size limits, etc.); not worth a test
# that fakes one.


# ── decode_bytes ─────────────────────────────────────────────────────────

def test_decode_bytes_utf8_bom():
    assert decode_bytes(b"\xef\xbb\xbfEleanor-Pederson,10U\n").startswith("Eleanor")


def test_decode_bytes_cp1252_fallback():
    # cp1252 \xe9 = é; not valid utf-8 alone, must fall back.
    assert "é" in decode_bytes(b"Ren\xe9e-M,Team\n")


# ── replace_job_roster + lookup ──────────────────────────────────────────

def test_replace_and_lookup_round_trip(db):
    job = _job(db, "10U-Black-Softball", "11UA-Baseball", "9UAA-Baseball")
    rows, _ = parse_csv(SAMPLE_CSV)
    inserted = replace_job_roster(db, job.id, rows)
    db.commit()
    assert inserted == 8
    assert db.query(RosterEntry).filter_by(job_id=job.id).count() == 8

    assert lookup_expected_team(db, job.id, "Eleanor-Pederson")  == "10ublacksoftball"
    assert lookup_expected_team(db, job.id, "eleanor pederson")  == "10ublacksoftball"
    assert lookup_expected_team(db, job.id, "Brooks-Cruz-Carter") == "11uabaseball"
    assert lookup_expected_team(db, job.id, "Lincoln-St-Marie")   == "9uaabaseball"
    # Coach code matches the same way as a normal name.
    assert lookup_expected_team(db, job.id, "Coach-10UBlack-SB") == "10ublacksoftball"


def test_replace_is_idempotent_not_appending(db):
    job = _job(db)
    rows, _ = parse_csv(SAMPLE_CSV)
    replace_job_roster(db, job.id, rows); db.commit()
    replace_job_roster(db, job.id, rows); db.commit()
    # Same row count, not double.
    assert db.query(RosterEntry).filter_by(job_id=job.id).count() == 8


def test_lookup_returns_none_when_unknown(db):
    job = _job(db)
    replace_job_roster(db, job.id, [("Eleanor-Pederson", "10U-Black-Softball")])
    db.commit()
    assert lookup_expected_team(db, job.id, "Someone-Else") is None
    assert lookup_expected_team(db, job.id, "") is None
    assert lookup_expected_team(db, job.id, None) is None


def test_lookup_abstains_on_ambiguous_name(db):
    """Same normalized name on two teams → abstain (return None)."""
    job = _job(db)
    rows = [
        ("Jack-Smith", "10U-Black-Softball"),
        ("Jack-Smith", "12UA-Baseball"),
    ]
    replace_job_roster(db, job.id, rows); db.commit()
    assert lookup_expected_team(db, job.id, "Jack-Smith") is None


def test_get_duplicate_names_lists_ambiguous(db):
    job = _job(db)
    rows = [
        ("Jack-Smith",       "10U-Black-Softball"),
        ("Jack-Smith",       "12UA-Baseball"),
        ("Eleanor-Pederson", "10U-Black-Softball"),
    ]
    replace_job_roster(db, job.id, rows); db.commit()
    dupes = get_duplicate_names(db, job.id)
    assert dupes == ["Jack-Smith"]


def test_duplicate_same_team_is_not_ambiguous(db):
    """Same name twice on the SAME team isn't a conflict — still resolves."""
    job = _job(db, "10U-Black-Softball")
    rows = [
        ("Jack-Smith", "10U-Black-Softball"),
        ("Jack-Smith", "10u black softball"),  # different raw, same norm
    ]
    replace_job_roster(db, job.id, rows); db.commit()
    assert get_duplicate_names(db, job.id) == []
    assert lookup_expected_team(db, job.id, "Jack-Smith") == "10ublacksoftball"


# ── load_roster_from_text endpoint helper ────────────────────────────────

def test_load_roster_from_text_response_shape(db):
    job = _job(db, "10U-Black-Softball", "11UA-Baseball", "9UAA-Baseball",
               "Majors-2-Baseball")
    result = load_roster_from_text(db, job.id, SAMPLE_CSV)
    assert result["entries_loaded"] == 8
    assert result["entries_skipped"] == 0
    assert result["distinct_teams"] == 4
    warnings = result["warnings"]
    # No unmatched CSV teams since every team has a matching Session.
    assert warnings["unmatched_csv_teams"] == []
    assert warnings["duplicate_names"] == []


def test_load_roster_from_text_unknown_job_404(db):
    with pytest.raises(HTTPException) as exc:
        load_roster_from_text(db, 99999, SAMPLE_CSV)
    assert exc.value.status_code == 404


def test_load_roster_warning_unmatched_csv_team(db):
    """A team in the CSV that doesn't match any Session shows up as warning."""
    job = _job(db, "10U-Black-Softball")  # only one matching session
    result = load_roster_from_text(db, job.id, SAMPLE_CSV)
    # The CSV has 11UA-Baseball, 9UAA-Baseball, Majors-2-Baseball as teams
    # without matching sessions in this job — they should all be warnings.
    unmatched = result["warnings"]["unmatched_csv_teams"]
    assert "11UA-Baseball" in unmatched
    assert "9UAA-Baseball" in unmatched
    assert "Majors-2-Baseball" in unmatched
    # 10U-Black-Softball matches, so not in the warning list.
    assert "10U-Black-Softball" not in unmatched


def test_load_roster_warning_session_missing_from_roster(db):
    """A Session with no matching roster row is surfaced as a warning."""
    job = _job(db, "10U-Black-Softball", "16U-Softball")
    csv_text = "Eleanor-Pederson,10U-Black-Softball\n"  # nothing for 16U-Softball
    result = load_roster_from_text(db, job.id, csv_text)
    missing = result["warnings"]["sessions_missing_from_roster"]
    assert "16U-Softball" in missing
    assert "10U-Black-Softball" not in missing


def test_load_roster_warning_duplicate_names(db):
    job = _job(db, "10U-Black-Softball", "12UA-Baseball")
    csv_text = "Jack-Smith,10U-Black-Softball\nJack-Smith,12UA-Baseball\n"
    result = load_roster_from_text(db, job.id, csv_text)
    assert result["warnings"]["duplicate_names"] == ["Jack-Smith"]


def test_load_roster_replaces_existing(db):
    job = _job(db, "10U-Black-Softball")
    load_roster_from_text(db, job.id, SAMPLE_CSV)  # 8 entries
    load_roster_from_text(db, job.id, "Single-Player,10U-Black-Softball\n")
    assert db.query(RosterEntry).filter_by(job_id=job.id).count() == 1


# ── HTTP endpoint round-trip (multipart upload + GET + DELETE) ───────────

@pytest.fixture
def client(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'roster-http.db'}",
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
        # Seed a job + a couple sessions in the same DB the client will hit.
        seed = TestingSessionLocal()
        try:
            job = Job(name="J", root_path="/tmp", has_lines=0)
            seed.add(job); seed.commit(); seed.refresh(job)
            for t in ("10U-Black-Softball", "11UA-Baseball"):
                seed.add(Session(job_id=job.id, name=t,
                                 source_path=f"/tmp/{t}", status="done"))
            seed.commit()
            job_id = job.id
        finally:
            seed.close()
        c = TestClient(app)
        c.job_id = job_id  # stash for tests
        yield c
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_endpoint_post_get_delete_round_trip(client):
    job_id = client.job_id
    res = client.post(
        f"/api/jobs/{job_id}/roster",
        files={"file": ("roster.csv", SAMPLE_CSV.encode("utf-8"), "text/csv")},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["entries_loaded"] == 8

    res = client.get(f"/api/jobs/{job_id}/roster")
    assert res.status_code == 200
    assert res.json()["entries_loaded"] == 8
    assert len(res.json()["entries"]) == 8

    res = client.delete(f"/api/jobs/{job_id}/roster")
    assert res.status_code == 200
    assert res.json()["deleted"] == 8

    res = client.get(f"/api/jobs/{job_id}/roster")
    assert res.json()["entries_loaded"] == 0


def test_endpoint_unknown_job_404(client):
    res = client.post(
        "/api/jobs/99999/roster",
        files={"file": ("r.csv", b"A,B\n", "text/csv")},
    )
    assert res.status_code == 404
