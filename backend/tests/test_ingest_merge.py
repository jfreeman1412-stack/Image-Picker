"""Phase 1 of the outdoor-shoot folder build: merge same-named folders.

The outdoor-shoot folder structure puts each team in TWO folders under
different parents: `Line N / Team X` (JPG individuals) and
`Line N composite and team / Team X` (PNG individuals + team + pano).
Without merge, ingest creates TWO separate Sessions both named "Team X"
under one job, which then can't be DBSCAN-clustered together. Phase 1
makes _ingest_job MERGE same-named folders into one Session per team
name (normalized), so the team's naturals and composite content cluster
as one team in the UI.

Scope:
  - Job-wide merge: any same-named folders in the job merge into one
    session (decision: cross-line same-name collision risk accepted).
  - Normalized matching via normalize_name() (case/whitespace/dash
    variants collapse together).
  - Option (a) source_path: the FIRST-encountered path wins on a merged
    session; subsequent paths are dropped (confirmed dead-data safe —
    source_path is only displayed, never re-read by any code path).
  - Archived existing sessions are NOT eligible merge targets — operator
    hid them deliberately; a fresh ingest of the same name should create
    a fresh session, not resurrect the archived one.

Tests cover the small extracted helper + the integration through
_ingest_job, plus the merged_folders structured-notes side effect.
"""
from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image as PILImage
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models.db_models import Image, Job, Session as DbSess


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'merge-test.db'}",
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


def _job(db, name="Outdoor"):
    j = Job(name=name, root_path="/tmp", has_lines=1)
    db.add(j); db.commit(); db.refresh(j)
    return j


def _existing_session(db, job, name, archived=False):
    s = DbSess(
        job_id=job.id, name=name, source_path=f"/tmp/{name}",
        status="done", archived=1 if archived else 0,
        created_at=datetime.utcnow(),
    )
    db.add(s); db.commit(); db.refresh(s)
    return s


# ── _find_or_create_session_for_team helper (the merge core) ─────────────


# _existing_session sets source_path=f"/tmp/{name}" so relative to
# job.root_path="/tmp" that resolves to top-level parts[0] = <name>. The
# fallback branch of _extract_line_key returns the lowercased folder name,
# so passing line_key=<name>.lower() below matches the existing session's
# computed key. This exercises the merge logic without needing a real
# "Line N" folder structure; the "line 1"/"line 2" cross-line separation
# is covered by the integration tests below (test_multi_line_duplicate_...).
def test_helper_creates_session_when_no_match(db):
    """No existing session with matching normalized name → create new,
    was_merged=False."""
    from app.api.jobs import _find_or_create_session_for_team
    job = _job(db)
    s, was_merged = _find_or_create_session_for_team(
        db, job.id, job_root="/tmp", team_name="Team One",
        source_path="/tmp/line1/Team One", line_key="line 1",
    )
    assert was_merged is False
    assert s.name == "Team One"
    assert s.source_path == "/tmp/line1/Team One"
    assert s.job_id == job.id
    assert s.status == "pending"


def test_helper_returns_existing_session_on_literal_match(db):
    """Existing session named 'Team One' → return that, was_merged=True."""
    from app.api.jobs import _find_or_create_session_for_team
    job = _job(db)
    existing = _existing_session(db, job, "Team One")
    # existing.source_path is "/tmp/Team One" → parts[0]="Team One" →
    # fallback line_key "team one". Match that on the incoming call.
    s, was_merged = _find_or_create_session_for_team(
        db, job.id, job_root="/tmp", team_name="Team One",
        source_path="/tmp/line1c/Team One", line_key="team one",
    )
    assert was_merged is True
    assert s.id == existing.id
    # First-encountered source_path wins — original preserved.
    assert s.source_path == "/tmp/Team One"


def test_helper_normalizes_case(db):
    """Case-only variant merges into existing — 'team one' → 'Team One'."""
    from app.api.jobs import _find_or_create_session_for_team
    job = _job(db)
    existing = _existing_session(db, job, "Team One")
    s, was_merged = _find_or_create_session_for_team(
        db, job.id, job_root="/tmp", team_name="team one",
        source_path="/tmp/Team One", line_key="team one",
    )
    assert was_merged is True
    assert s.id == existing.id


def test_helper_normalizes_whitespace_and_dashes(db):
    """'Team-One' merges with 'Team One' (dashes/spaces strip out under
    normalize_name)."""
    from app.api.jobs import _find_or_create_session_for_team
    job = _job(db)
    existing = _existing_session(db, job, "Team One")
    s, was_merged = _find_or_create_session_for_team(
        db, job.id, job_root="/tmp", team_name="Team-One",
        source_path="/tmp/Team One", line_key="team one",
    )
    assert was_merged is True
    assert s.id == existing.id


def test_helper_skips_archived_sessions_creates_new(db):
    """An archived existing session is NOT a merge target — operator
    hid it deliberately. A fresh same-named folder creates a new
    session."""
    from app.api.jobs import _find_or_create_session_for_team
    job = _job(db)
    archived = _existing_session(db, job, "Team One", archived=True)
    s, was_merged = _find_or_create_session_for_team(
        db, job.id, job_root="/tmp", team_name="Team One",
        source_path="/tmp/line1/Team One", line_key="line 1",
    )
    assert was_merged is False
    assert s.id != archived.id
    assert s.archived == 0


def test_helper_scoped_to_job(db):
    """Same name in a DIFFERENT job is not a merge target."""
    from app.api.jobs import _find_or_create_session_for_team
    job_a = _job(db, name="JobA")
    job_b = _job(db, name="JobB")
    _existing_session(db, job_a, "Team One")
    s, was_merged = _find_or_create_session_for_team(
        db, job_b.id, job_root="/tmp", team_name="Team One",
        source_path="/tmp/Team One", line_key="team one",
    )
    assert was_merged is False
    assert s.job_id == job_b.id


def test_helper_different_names_stay_separate(db):
    """Genuinely different team names create separate sessions."""
    from app.api.jobs import _find_or_create_session_for_team
    job = _job(db)
    _existing_session(db, job, "Team One")
    s, was_merged = _find_or_create_session_for_team(
        db, job.id, job_root="/tmp", team_name="Team Two",
        source_path="/tmp/Team Two", line_key="team two",
    )
    assert was_merged is False
    assert s.name == "Team Two"


def test_helper_scoped_by_line_key_no_cross_line_merge(db):
    """2026-09-14 multi-line duplicate-name fix — the merge is now scoped
    by line_key. A 'Team One' in line 2 does NOT merge into an existing
    'Team One' in line 1: they get separate sessions."""
    from app.api.jobs import _find_or_create_session_for_team
    job = _job(db)
    # Existing session for line 1's Team One.
    _existing_session(db, job, "Team One")  # source_path=/tmp/Team One
    # Same-named team, different line — different line_key.
    s, was_merged = _find_or_create_session_for_team(
        db, job.id, job_root="/tmp", team_name="Team One",
        source_path="/tmp/line2/Team One", line_key="line 2",
    )
    assert was_merged is False
    assert s.name == "Team One"
    assert s.source_path == "/tmp/line2/Team One"


# ── _ingest_job integration: synthetic folder structures ─────────────────
#
# These tests exercise the full ingest path: real folders on disk, real
# tiny PNG/JPG files, real ingest_folder() runs. auto_run=False so the
# face-detection pipeline doesn't fire (we're testing folder→session
# mapping, not face detection).


def _make_image(path: Path, mode: str = "RGB", color: str = "white"):
    """Tiny image file at `path`. Format is inferred from suffix."""
    PILImage.new(mode, (8, 8), color=color).save(path)


def _outdoor_structure(root: Path, teams_per_line: list[tuple[str, list[str]]]):
    """Build the outdoor-shoot folder structure:
      root/
        Line 1/                       (JPGs)
          Team One/  ...jpg files
          Team Two/  ...
        Line 1 composite and team/    (PNGs)
          Team One/  ...png files
          Team Two/  ...
    teams_per_line: [("Line 1", ["Team One", "Team Two"]), ...]
    """
    for line_name, team_names in teams_per_line:
        line_dir = root / line_name
        line_dir.mkdir()
        for team in team_names:
            tdir = line_dir / team
            tdir.mkdir()
            # 2 JPGs per team in the natural line
            _make_image(tdir / f"{team}-001.jpg")
            _make_image(tdir / f"{team}-002.jpg")

        composite_dir = root / f"{line_name} composite and team"
        composite_dir.mkdir()
        for team in team_names:
            tdir = composite_dir / team
            tdir.mkdir()
            # 3 PNGs per team in the composite (individual + team + pano)
            _make_image(tdir / f"{team}-c001.png")
            _make_image(tdir / f"{team}-c002.png")
            _make_image(tdir / f"{team}-c003.png")


def _run_ingest(db_url, job_id, root, has_lines=True):
    """Run _ingest_job directly with auto_run=False, sharing the test's
    engine via a custom SessionLocal monkey-patch. The function opens its
    own SessionLocal, so we override DataLocal to point at the test DB."""
    from app import db as db_module
    from app.api import jobs as jobs_module
    # Monkey-patch SessionLocal to use the test engine.
    test_engine = create_engine(db_url, connect_args={"check_same_thread": False})
    test_SL = sessionmaker(bind=test_engine)
    original_SL = jobs_module.SessionLocal
    jobs_module.SessionLocal = test_SL
    try:
        jobs_module._ingest_job(job_id, root, has_lines, None, auto_run=False)
    finally:
        jobs_module.SessionLocal = original_SL
        test_engine.dispose()


def test_ingest_job_merges_same_named_pair_to_one_session(db, tmp_path):
    """The full outdoor-shoot pattern: 2 teams per line, naturals + composite
    folders. After ingest, EACH team gets ONE session containing both JPGs
    and PNGs — not two sessions."""
    root = tmp_path / "shoot"
    root.mkdir()
    _outdoor_structure(root, [("Line 1", ["Team One", "Team Two"])])

    # 4 leaf team folders, 2 unique team names → expect 2 sessions.
    job = _job(db)
    db_url = str(db.bind.url)
    _run_ingest(db_url, job.id, root, has_lines=True)

    # Re-open db to see _ingest_job's writes (it used its own SessionLocal).
    db.expire_all()
    sessions = db.query(DbSess).filter_by(job_id=job.id).all()
    assert len(sessions) == 2
    names = sorted(s.name for s in sessions)
    assert names == ["Team One", "Team Two"]

    # Each merged session has BOTH JPGs and PNGs.
    for s in sessions:
        images = db.query(Image).filter_by(session_id=s.id).all()
        suffixes = sorted({Path(i.path).suffix.lower() for i in images})
        assert ".jpg" in suffixes
        assert ".png" in suffixes
        # 2 JPGs + 3 PNGs per team.
        assert len(images) == 5


def test_ingest_job_records_merged_folders_in_notes(db, tmp_path):
    """Phase 1 surfaces merge events in the existing ingest_error
    structured-notes field (alongside the existing skipped_teams list)."""
    import json
    root = tmp_path / "shoot"
    root.mkdir()
    _outdoor_structure(root, [("Line 1", ["Team One"])])

    job = _job(db)
    _run_ingest(str(db.bind.url), job.id, root, has_lines=True)

    db.expire_all()
    job = db.query(Job).get(job.id)
    notes = json.loads(job.ingest_error) if job.ingest_error else {}
    merged = notes.get("merged_folders", [])
    assert len(merged) == 1
    entry = merged[0]
    assert entry["folder_name"] == "Team One"
    assert entry["into_session_name"] == "Team One"
    assert "source_path" in entry  # the duplicate-folder path that got merged in


def test_ingest_job_no_merge_when_names_differ(db, tmp_path):
    """Two folders with different names → two separate sessions (no
    accidental merge)."""
    root = tmp_path / "shoot"
    root.mkdir()
    (root / "Line 1").mkdir()
    (root / "Line 1" / "Alpha").mkdir()
    _make_image(root / "Line 1" / "Alpha" / "a.jpg")
    (root / "Line 1" / "Beta").mkdir()
    _make_image(root / "Line 1" / "Beta" / "b.jpg")

    job = _job(db)
    _run_ingest(str(db.bind.url), job.id, root, has_lines=True)

    db.expire_all()
    sessions = db.query(DbSess).filter_by(job_id=job.id).all()
    assert len(sessions) == 2


def test_ingest_job_normalized_variants_merge(db, tmp_path):
    """Case/whitespace/dash variants of the same team name merge:
    'Team-One' folder under Line 1 + 'Team One' folder under Line 1
    composite collapse to ONE session."""
    root = tmp_path / "shoot"
    root.mkdir()
    (root / "Line 1").mkdir()
    (root / "Line 1" / "Team-One").mkdir()
    _make_image(root / "Line 1" / "Team-One" / "a.jpg")
    (root / "Line 1 composite").mkdir()
    (root / "Line 1 composite" / "Team One").mkdir()
    _make_image(root / "Line 1 composite" / "Team One" / "b.png")

    job = _job(db)
    _run_ingest(str(db.bind.url), job.id, root, has_lines=True)

    db.expire_all()
    sessions = db.query(DbSess).filter_by(job_id=job.id).all()
    assert len(sessions) == 1
    # First-encountered name wins (Line 1 sorts before Line 1 composite).
    assert sessions[0].name == "Team-One"
    images = db.query(Image).filter_by(session_id=sessions[0].id).all()
    assert len(images) == 2


def test_ingest_job_existing_session_path_preserved_on_merge(db, tmp_path):
    """source_path option (a): FIRST-encountered path stays on the merged
    session. Subsequent folders' paths are NOT written into source_path."""
    root = tmp_path / "shoot"
    root.mkdir()
    _outdoor_structure(root, [("Line 1", ["Team One"])])

    job = _job(db)
    _run_ingest(str(db.bind.url), job.id, root, has_lines=True)

    db.expire_all()
    s = db.query(DbSess).filter_by(job_id=job.id).one()
    # _iter_team_folders walks "Line 1" before "Line 1 composite and team"
    # (sorted alphabetically), so the JPG folder's path should be the kept one.
    assert "composite" not in s.source_path
    assert s.source_path.endswith("Team One")


def test_ingest_progress_counts_folders_not_sessions(db, tmp_path):
    """Cosmetic divergence accepted: ingest_progress counts FOLDERS visited
    (4 in a 2-team outdoor structure), not the 2 merged sessions. The
    progress bar stays accurate to what's being processed."""
    root = tmp_path / "shoot"
    root.mkdir()
    _outdoor_structure(root, [("Line 1", ["Team One", "Team Two"])])

    job = _job(db)
    _run_ingest(str(db.bind.url), job.id, root, has_lines=True)

    db.expire_all()
    job = db.query(Job).get(job.id)
    assert job.ingest_progress == 4  # 4 folders walked
    sessions = db.query(DbSess).filter_by(job_id=job.id).count()
    assert sessions == 2          # 2 unique team names


def test_ingest_job_no_merge_path_unchanged_regression(db, tmp_path):
    """When all folder names are unique (no merge), behavior is byte-
    identical to pre-Phase-1: N folders → N sessions, no merged_folders
    in notes."""
    import json
    root = tmp_path / "shoot"
    root.mkdir()
    (root / "Line 1").mkdir()
    for team in ["Alpha", "Beta", "Gamma"]:
        d = root / "Line 1" / team
        d.mkdir()
        _make_image(d / f"{team}.jpg")

    job = _job(db)
    _run_ingest(str(db.bind.url), job.id, root, has_lines=True)

    db.expire_all()
    sessions = db.query(DbSess).filter_by(job_id=job.id).all()
    assert len(sessions) == 3
    job = db.query(Job).get(job.id)
    notes = json.loads(job.ingest_error) if job.ingest_error else {}
    assert "merged_folders" not in notes  # no merge → no notes key


# ── 2026-09-14 multi-line duplicate-name fix regression tests ────────────


def test_multi_line_duplicate_team_names_no_cross_line_merge(db, tmp_path):
    """Two lines each with a same-named team → TWO separate sessions.
    Before the 2026-09-14 line-scoped-merge fix, cross-line same-name
    folders silently collapsed into the first-encountered line's session
    (Line 2's images got mixed into Line 1's Rockets). Now they stay
    separate."""
    import json
    root = tmp_path / "shoot"
    root.mkdir()
    # Line 1: Rockets + Falcons
    (root / "Line 1").mkdir()
    (root / "Line 1" / "Rockets").mkdir()
    _make_image(root / "Line 1" / "Rockets" / "l1r1.jpg")
    (root / "Line 1" / "Falcons").mkdir()
    _make_image(root / "Line 1" / "Falcons" / "l1f1.jpg")
    # Line 2: Rockets + Falcons (duplicate names — the bug case)
    (root / "Line 2").mkdir()
    (root / "Line 2" / "Rockets").mkdir()
    _make_image(root / "Line 2" / "Rockets" / "l2r1.jpg")
    (root / "Line 2" / "Falcons").mkdir()
    _make_image(root / "Line 2" / "Falcons" / "l2f1.jpg")

    # Need the job's root_path pointed at the real folder so the line_key
    # extraction in _find_or_create_session_for_team sees "Line 1"/"Line 2"
    # as parts[0] of each session's source_path.
    job = Job(name="MultiLine", root_path=str(root), has_lines=1)
    db.add(job); db.commit(); db.refresh(job)
    _run_ingest(str(db.bind.url), job.id, root, has_lines=True)

    db.expire_all()
    sessions = db.query(DbSess).filter_by(job_id=job.id).all()
    # 4 folders on disk, 2 unique names — but line-scoped: 4 sessions.
    assert len(sessions) == 4
    names = sorted(s.name for s in sessions)
    assert names == ["Falcons", "Falcons", "Rockets", "Rockets"]

    # Rockets sessions: one under Line 1, one under Line 2. Each has 1 image.
    rockets = [s for s in sessions if s.name == "Rockets"]
    r_paths = sorted(s.source_path for s in rockets)
    assert r_paths[0].endswith("Line 1\\Rockets") or r_paths[0].endswith("Line 1/Rockets")
    assert r_paths[1].endswith("Line 2\\Rockets") or r_paths[1].endswith("Line 2/Rockets")
    for s in rockets:
        assert len(db.query(Image).filter_by(session_id=s.id).all()) == 1

    # No merged_folders in notes — nothing merged.
    job = db.query(Job).get(job.id)
    notes = json.loads(job.ingest_error) if job.ingest_error else {}
    assert "merged_folders" not in notes


def test_multi_line_within_line_naturals_composite_still_merges(db, tmp_path):
    """The within-line naturals+composite merge that jobs 40/57/63 rely on
    is preserved: "Line 1 Team and Pano/Rockets" + "Line 1- Lauren Standard/
    Rockets" both extract to line_key='line 1' and merge into ONE session."""
    import json
    root = tmp_path / "shoot"
    root.mkdir()
    # Two top-level folders both belonging to Line 1 (composite + naturals).
    (root / "Line 1 Team and Pano").mkdir()
    (root / "Line 1 Team and Pano" / "Rockets").mkdir()
    _make_image(root / "Line 1 Team and Pano" / "Rockets" / "comp1.png")
    _make_image(root / "Line 1 Team and Pano" / "Rockets" / "comp2.png")

    (root / "Line 1- Lauren Standard").mkdir()
    (root / "Line 1- Lauren Standard" / "Rockets").mkdir()
    _make_image(root / "Line 1- Lauren Standard" / "Rockets" / "nat1.jpg")

    # Second team, only in one line — separate session.
    (root / "Line 1 Team and Pano" / "Falcons").mkdir()
    _make_image(root / "Line 1 Team and Pano" / "Falcons" / "f1.png")

    job = Job(name="OutdoorLine1", root_path=str(root), has_lines=1)
    db.add(job); db.commit(); db.refresh(job)
    _run_ingest(str(db.bind.url), job.id, root, has_lines=True)

    db.expire_all()
    sessions = db.query(DbSess).filter_by(job_id=job.id).all()
    # 3 folders on disk (2 Rockets + 1 Falcons) → 2 sessions after merge.
    assert len(sessions) == 2
    names = sorted(s.name for s in sessions)
    assert names == ["Falcons", "Rockets"]

    # Rockets session has both composite PNGs AND the natural JPG.
    rockets = next(s for s in sessions if s.name == "Rockets")
    imgs = db.query(Image).filter_by(session_id=rockets.id).all()
    assert len(imgs) == 3
    suffixes = sorted({Path(i.path).suffix.lower() for i in imgs})
    assert suffixes == [".jpg", ".png"]

    # merged_folders SHOULD record the within-line merge.
    job = db.query(Job).get(job.id)
    notes = json.loads(job.ingest_error) if job.ingest_error else {}
    merged = notes.get("merged_folders", [])
    assert len(merged) == 1
    assert merged[0]["folder_name"] == "Rockets"
    assert merged[0]["into_session_name"] == "Rockets"


def test_extract_line_key_regex_and_fallback():
    """Unit: line_key extraction covers the operator's real folder naming
    conventions from jobs 40/57/63/95/96/100/103/118 in production DB,
    plus the fallback for non-'Line N' top-level names."""
    from app.api.jobs import _extract_line_key
    # "Line N" prefix variants — all match the same line_key across
    # spacing / hyphen / dash / trailing labels.
    assert _extract_line_key("Line 1") == "line 1"
    assert _extract_line_key("Line 2") == "line 2"
    assert _extract_line_key("Line 1- Lauren Standard") == "line 1"
    assert _extract_line_key("Line 1 Team and Pano") == "line 1"
    assert _extract_line_key("Line 2-Joey-7") == "line 2"
    assert _extract_line_key("line 1 composite and team") == "line 1"
    assert _extract_line_key("LINE 3") == "line 3"
    # Fallback: no "Line N" pattern → lowercased folder name.
    assert _extract_line_key("Senior Line") == "senior line"
    assert _extract_line_key("IND Pano Team") == "ind pano team"
    assert _extract_line_key("Taylor-7") == "taylor-7"
    assert _extract_line_key("6-2-2026") == "6-2-2026"
    # Empty / None safety.
    assert _extract_line_key("") == ""
    assert _extract_line_key(None) == ""
