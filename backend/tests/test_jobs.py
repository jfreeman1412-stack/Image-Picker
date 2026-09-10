"""Tests for async job creation + ingest-status.

Starlette's TestClient runs BackgroundTasks synchronously before the
client.post(...) call returns, so by the time we poll /ingest-status the
background ingest has already finished. We point both the request DB
(get_db override) and the background-task DB (jobs.SessionLocal) at one
temp SQLite file so the whole flow shares state.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import db as db_module
from app.api import jobs as jobs_module
from app.db import Base, get_db
from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'jobs-test.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(
        autocommit=False, autoflush=False, bind=engine,
    )

    def _override_get_db():
        s = TestingSessionLocal()
        try:
            yield s
        finally:
            s.close()

    # Background task opens its own session via jobs.SessionLocal.
    monkeypatch.setattr(jobs_module, "SessionLocal", TestingSessionLocal)
    app.dependency_overrides[get_db] = _override_get_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _make_job_tree(tmp_path, teams, subfolder=None):
    """tmp_path/Shoot/<team>/[subfolder/]img.png — returns the Shoot root."""
    root = tmp_path / "Shoot"
    root.mkdir()
    for team in teams:
        team_dir = root / team
        img_dir = team_dir / subfolder if subfolder else team_dir
        img_dir.mkdir(parents=True)
        # two tiny valid PNGs per team
        from PIL import Image as PILImage
        for i in range(2):
            PILImage.new("RGB", (8, 8), "white").save(img_dir / f"{team}_{i}.png")
    return root


def test_create_returns_immediately_with_total(client, tmp_path):
    root = _make_job_tree(tmp_path, ["TeamA", "TeamB", "TeamC"])
    r = client.post("/api/jobs", json={
        "name": "J", "root_path": str(root),
        "has_lines": False, "image_subfolder_name": None, "auto_run": False,
    })
    assert r.status_code == 200
    body = r.json()
    assert "job_id" in body
    assert body["ingest_total"] == 3


def test_ingest_completes_with_full_progress(client, tmp_path):
    root = _make_job_tree(tmp_path, ["TeamA", "TeamB"])
    jid = client.post("/api/jobs", json={
        "name": "J", "root_path": str(root),
        "has_lines": False, "image_subfolder_name": None, "auto_run": False,
    }).json()["job_id"]

    # TestClient ran the background task already.
    s = client.get(f"/api/jobs/{jid}/ingest-status").json()
    assert s["status"] == "done"
    assert s["progress"] == 2
    assert s["total"] == 2
    assert s["current_team"] is None
    assert s["skipped_teams"] == []

    detail = client.get(f"/api/jobs/{jid}").json()
    assert len(detail["sessions"]) == 2
    assert {s["name"] for s in detail["sessions"]} == {"TeamA", "TeamB"}


def test_ingest_records_skipped_teams_when_subfolder_missing(client, tmp_path):
    # TeamA has the JPG subfolder; TeamB doesn't.
    root = tmp_path / "Shoot"
    (root / "TeamA" / "JPG").mkdir(parents=True)
    (root / "TeamB").mkdir(parents=True)
    from PIL import Image as PILImage
    PILImage.new("RGB", (8, 8), "white").save(root / "TeamA" / "JPG" / "a.png")

    jid = client.post("/api/jobs", json={
        "name": "J", "root_path": str(root),
        "has_lines": False, "image_subfolder_name": "JPG", "auto_run": False,
    }).json()["job_id"]

    s = client.get(f"/api/jobs/{jid}/ingest-status").json()
    assert s["status"] == "done"
    assert s["progress"] == 1
    names = {t["name"] for t in s["skipped_teams"]}
    assert "TeamB" in names


def test_ingest_status_404_for_missing_job(client):
    r = client.get("/api/jobs/999999/ingest-status")
    assert r.status_code == 404


def test_create_400_for_missing_root(client):
    r = client.post("/api/jobs", json={
        "name": "J", "root_path": r"Z:\does\not\exist",
        "has_lines": False, "image_subfolder_name": None, "auto_run": False,
    })
    assert r.status_code == 400


def test_auto_run_chains_pipeline_per_session(client, tmp_path, monkeypatch):
    """auto_run=True (the default) runs the pipeline for every ingested team
    once import finishes. We stub run_pipeline to record calls — exercising
    the wiring without loading InsightFace."""
    called = []
    monkeypatch.setattr(
        jobs_module, "run_pipeline",
        lambda bg_db, sid: called.append(sid),
    )
    root = _make_job_tree(tmp_path, ["TeamA", "TeamB"])
    jid = client.post("/api/jobs", json={
        "name": "J", "root_path": str(root),
        "has_lines": False, "image_subfolder_name": None, "auto_run": True,
    }).json()["job_id"]

    # TestClient already ran the ingest + chained pipeline background task.
    detail = client.get(f"/api/jobs/{jid}").json()
    session_ids = sorted(s["id"] for s in detail["sessions"])
    assert sorted(called) == session_ids   # pipeline invoked once per team
    assert client.get(f"/api/jobs/{jid}/ingest-status").json()["status"] == "done"


def _seed_manual_work(SessionLocal, job_id, *, reviewed=False,
                       manual_label=False, manual_coach=False,
                       manual_role=False):
    """Inject one (or more) pieces of manual review work into a job so the
    run-all destructive-action gate has something to flag."""
    from app.models.db_models import Cluster, Image, ImageRole, Session
    db = SessionLocal()
    try:
        sess = db.query(Session).filter_by(job_id=job_id).first()
        if reviewed:
            sess.reviewed = 1
        c = Cluster(
            session_id=sess.id, image_count=0,
            manual_label="Renamed" if manual_label else None,
            manual_coach_override=1 if manual_coach else 0,
        )
        db.add(c); db.flush()
        if manual_role:
            img = db.query(Image).filter_by(session_id=sess.id).first()
            if img is None:
                img = Image(session_id=sess.id, path="/tmp/x.png", filename="x.png")
                db.add(img); db.flush()
            db.add(ImageRole(image_id=img.id, cluster_id=c.id, role="team",
                             manual_override=1))
        db.commit()
    finally:
        db.close()


def test_run_all_impact_reports_all_four_counters(client, tmp_path, monkeypatch):
    """The 409 detail's `impact` dict carries each counter independently so
    the UI can build a precise confirm dialog."""
    monkeypatch.setattr(jobs_module, "run_pipeline", lambda *_a: None)
    root = _make_job_tree(tmp_path, ["TeamA"])
    jid = client.post("/api/jobs", json={
        "name": "J", "root_path": str(root),
        "has_lines": False, "image_subfolder_name": None, "auto_run": False,
    }).json()["job_id"]
    _seed_manual_work(
        jobs_module.SessionLocal, jid,
        reviewed=True, manual_label=True,
        manual_coach=True, manual_role=True,
    )
    res = client.post(f"/api/jobs/{jid}/run-all")
    assert res.status_code == 409
    impact = res.json()["detail"]["impact"]
    assert impact["reviewed_teams"] == 1
    assert impact["manual_labels"] >= 1   # the manual_role seed adds a 2nd cluster
    assert impact["manual_coach_overrides"] >= 1
    assert impact["manual_role_decisions"] == 1


def test_auto_run_false_does_not_chain_pipeline(client, tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(
        jobs_module, "run_pipeline",
        lambda bg_db, sid: called.append(sid),
    )
    root = _make_job_tree(tmp_path, ["TeamA"])
    client.post("/api/jobs", json={
        "name": "J", "root_path": str(root),
        "has_lines": False, "image_subfolder_name": None, "auto_run": False,
    })
    assert called == []


# ── Phase 9 follow-up: destructive run-all force gate ─────────────────────


def test_run_all_proceeds_when_no_manual_work(client, tmp_path, monkeypatch):
    """Sessions exist but no reviewed / no manual roles / no renames —
    run-all kicks off normally, no 409."""
    monkeypatch.setattr(jobs_module, "run_pipeline", lambda *a, **k: None)
    # Create a job from scratch via the API so the client fixture's
    # overridden SessionLocal is the one populated.
    root = _make_job_tree(tmp_path, ["TeamA", "TeamB"])
    jid = client.post("/api/jobs", json={
        "name": "J", "root_path": str(root),
        "has_lines": False, "image_subfolder_name": None, "auto_run": False,
    }).json()["job_id"]
    r = client.post(f"/api/jobs/{jid}/run-all")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "running"
    assert body["session_count"] == 2


def test_run_all_blocks_when_reviewed_team_exists(client, tmp_path, monkeypatch):
    """Any reviewed=1 session in the job → 409 with destructive_run_all."""
    monkeypatch.setattr(jobs_module, "run_pipeline", lambda *a, **k: None)
    root = _make_job_tree(tmp_path, ["TeamA"])
    jid = client.post("/api/jobs", json={
        "name": "J", "root_path": str(root),
        "has_lines": False, "image_subfolder_name": None, "auto_run": False,
    }).json()["job_id"]
    # Mark the session reviewed directly in the same SessionLocal the client uses.
    SL = jobs_module.SessionLocal
    from app.models.db_models import Session as DbSessionModel
    db = SL()
    try:
        s = db.query(DbSessionModel).filter_by(job_id=jid).first()
        s.reviewed = 1
        db.commit()
    finally:
        db.close()

    r = client.post(f"/api/jobs/{jid}/run-all")
    assert r.status_code == 409
    body = r.json()
    assert body["detail"]["error"] == "destructive_run_all"
    assert body["detail"]["impact"]["reviewed_teams"] == 1
    assert body["detail"]["job_name"] == "J"


def test_run_all_blocks_when_manual_label_exists(client, tmp_path, monkeypatch):
    """Cluster.manual_label set → 409. Same gate, different signal."""
    monkeypatch.setattr(jobs_module, "run_pipeline", lambda *a, **k: None)
    root = _make_job_tree(tmp_path, ["TeamA"])
    jid = client.post("/api/jobs", json={
        "name": "J", "root_path": str(root),
        "has_lines": False, "image_subfolder_name": None, "auto_run": False,
    }).json()["job_id"]
    from app.models.db_models import Cluster, Session as DbSessionModel
    SL = jobs_module.SessionLocal
    db = SL()
    try:
        s = db.query(DbSessionModel).filter_by(job_id=jid).first()
        db.add(Cluster(session_id=s.id, manual_label="Renamed!",
                       image_count=1))
        db.commit()
    finally:
        db.close()

    r = client.post(f"/api/jobs/{jid}/run-all")
    assert r.status_code == 409
    assert r.json()["detail"]["impact"]["manual_labels"] == 1


def test_run_all_blocks_when_manual_role_exists(client, tmp_path, monkeypatch):
    """ImageRole.manual_override=1 anywhere → 409."""
    monkeypatch.setattr(jobs_module, "run_pipeline", lambda *a, **k: None)
    root = _make_job_tree(tmp_path, ["TeamA"])
    jid = client.post("/api/jobs", json={
        "name": "J", "root_path": str(root),
        "has_lines": False, "image_subfolder_name": None, "auto_run": False,
    }).json()["job_id"]
    from app.models.db_models import (
        Cluster, Image, ImageRole, Session as DbSessionModel,
    )
    SL = jobs_module.SessionLocal
    db = SL()
    try:
        s = db.query(DbSessionModel).filter_by(job_id=jid).first()
        c = Cluster(session_id=s.id, image_count=1)
        db.add(c); db.commit(); db.refresh(c)
        i = Image(session_id=s.id, path="/tmp/x.png", filename="x.png")
        db.add(i); db.commit(); db.refresh(i)
        db.add(ImageRole(image_id=i.id, cluster_id=c.id, role="team",
                         manual_override=1))
        db.commit()
    finally:
        db.close()

    r = client.post(f"/api/jobs/{jid}/run-all")
    assert r.status_code == 409
    assert r.json()["detail"]["impact"]["manual_role_decisions"] == 1


def test_run_all_force_proceeds_through_gate(client, tmp_path, monkeypatch):
    """Same destructive setup as the prior test, but force=true bypasses
    the 409 and the pipeline kicks off normally."""
    called = []
    monkeypatch.setattr(jobs_module, "run_pipeline",
                        lambda bg, sid: called.append(sid))
    root = _make_job_tree(tmp_path, ["TeamA"])
    jid = client.post("/api/jobs", json={
        "name": "J", "root_path": str(root),
        "has_lines": False, "image_subfolder_name": None, "auto_run": False,
    }).json()["job_id"]
    SL = jobs_module.SessionLocal
    from app.models.db_models import Session as DbSessionModel
    db = SL()
    try:
        db.query(DbSessionModel).filter_by(job_id=jid).update({"reviewed": 1})
        db.commit()
    finally:
        db.close()

    # Without force → 409
    r = client.post(f"/api/jobs/{jid}/run-all")
    assert r.status_code == 409

    # With force → proceeds
    r = client.post(f"/api/jobs/{jid}/run-all", json={"force": True})
    assert r.status_code == 200
    assert r.json()["status"] == "running"
    assert len(called) == 1     # pipeline kicked off for the team


# ── Phase C.2 Section 1: create an image-less "shoot" job ─────────────────

def test_create_shoot_makes_image_less_job(client):
    r = client.post("/api/jobs/shoot", json={"name": "Spring Shoot"})
    assert r.status_code == 200, r.text
    jid = r.json()["job_id"]

    detail = client.get(f"/api/jobs/{jid}").json()
    assert detail["name"] == "Spring Shoot"
    assert detail["sessions"] == []          # no images/sessions
    assert detail["root_path"] is None

    status = client.get(f"/api/jobs/{jid}/ingest-status").json()
    assert status["status"] == "awaiting_images"
    assert status["total"] == 0


def test_create_shoot_no_folder_validation(client):
    """Unlike POST /api/jobs, a nonexistent (or future) folder must NOT 400 —
    the shoot exists before its images do."""
    r = client.post("/api/jobs/shoot", json={
        "name": "Pre-shoot", "root_path": r"Z:\not\there\yet",
    })
    assert r.status_code == 200, r.text
    jid = r.json()["job_id"]
    assert client.get(f"/api/jobs/{jid}").json()["root_path"] == r"Z:\not\there\yet"


def test_create_shoot_appears_in_list_as_zero_team(client):
    jid = client.post("/api/jobs/shoot", json={"name": "Listed Shoot"}).json()["job_id"]
    listing = client.get("/api/jobs").json()
    row = next(j for j in listing if j["id"] == jid)
    assert row["session_count"] == 0
    assert row["image_count"] == 0


# ── Phase C.2 Section 3: import images into an existing job ───────────────

def test_import_images_into_shoot_ingests_teams(client, tmp_path):
    jid = client.post("/api/jobs/shoot", json={"name": "Late Images"}).json()["job_id"]
    root = _make_job_tree(tmp_path, ["TeamA", "TeamB"])

    r = client.post(f"/api/jobs/{jid}/import-images", json={
        "root_path": str(root), "has_lines": False,
        "image_subfolder_name": None, "auto_run": False,
    })
    assert r.status_code == 200, r.text
    assert r.json()["ingest_total"] == 2

    # TestClient ran the background ingest synchronously.
    status = client.get(f"/api/jobs/{jid}/ingest-status").json()
    assert status["status"] == "done"
    detail = client.get(f"/api/jobs/{jid}").json()
    assert {s["name"] for s in detail["sessions"]} == {"TeamA", "TeamB"}


def test_import_images_404_unknown_job(client, tmp_path):
    root = _make_job_tree(tmp_path, ["TeamA"])
    r = client.post("/api/jobs/999999/import-images", json={
        "root_path": str(root), "has_lines": False,
        "image_subfolder_name": None, "auto_run": False,
    })
    assert r.status_code == 404


def test_import_images_400_missing_folder(client):
    jid = client.post("/api/jobs/shoot", json={"name": "S"}).json()["job_id"]
    r = client.post(f"/api/jobs/{jid}/import-images", json={
        "root_path": r"Z:\nope", "has_lines": False,
        "image_subfolder_name": None, "auto_run": False,
    })
    assert r.status_code == 400


def test_import_images_409_when_teams_already_exist(client, tmp_path):
    jid = client.post("/api/jobs/shoot", json={"name": "Once Only"}).json()["job_id"]
    root = _make_job_tree(tmp_path, ["TeamA"])
    body = {"root_path": str(root), "has_lines": False,
            "image_subfolder_name": None, "auto_run": False}
    assert client.post(f"/api/jobs/{jid}/import-images", json=body).status_code == 200
    # Second import is refused — the job already has teams (guard precedes the
    # folder check, so reusing the same folder still 409s on the session count).
    r = client.post(f"/api/jobs/{jid}/import-images", json=body)
    assert r.status_code == 409


# ── Capture-ready lifecycle filter: GET /api/jobs?stage=capture ───────────

def _add_roster(client, jid, csv=b"Ava-Nguyen,Lions\n"):
    return client.post(f"/api/players/roster/{jid}",
                       files={"file": ("r.csv", csv, "text/csv")})


def test_stage_capture_filters_to_rostered_image_less(client, tmp_path):
    # capture-ready: image-less shoot WITH a roster
    ready = client.post("/api/jobs/shoot", json={"name": "Ready"}).json()["job_id"]
    _add_roster(client, ready)
    # image-less shoot with NO roster → nothing to capture against
    noroster = client.post("/api/jobs/shoot", json={"name": "No roster"}).json()["job_id"]
    # imported job (has images) even WITH a roster → past the capture window
    root = _make_job_tree(tmp_path, ["TeamA"])
    imported = client.post("/api/jobs", json={
        "name": "Imported", "root_path": str(root),
        "has_lines": False, "image_subfolder_name": None, "auto_run": False,
    }).json()["job_id"]
    _add_roster(client, imported, csv=b"Bob-Lee,TeamA\n")

    capture_ids = {j["id"] for j in client.get("/api/jobs", params={"stage": "capture"}).json()}
    assert ready in capture_ids
    assert noroster not in capture_ids       # no roster
    assert imported not in capture_ids       # already imported

    # Default view is unchanged — every non-archived job, all stages.
    all_ids = {j["id"] for j in client.get("/api/jobs").json()}
    assert {ready, noroster, imported} <= all_ids


def test_stage_capture_excludes_archived(client):
    jid = client.post("/api/jobs/shoot", json={"name": "Archived ready"}).json()["job_id"]
    _add_roster(client, jid)
    assert jid in {j["id"] for j in client.get("/api/jobs", params={"stage": "capture"}).json()}
    client.post(f"/api/jobs/{jid}/archive")
    assert jid not in {j["id"] for j in client.get("/api/jobs", params={"stage": "capture"}).json()}


# ── 2026-09-10: ingest-walker guard + flat-folder auto-fallback ───────────


def _touch_png(path):
    """Write a valid 1-byte PNG at `path` — cheaper than PIL for shape tests."""
    from PIL import Image as PILImage
    PILImage.new("RGB", (4, 4), "white").save(path)


def _touch_cr3(path):
    """Write a 1-byte pseudo-RAW at `path`. Content doesn't matter — the walker
    only cares about the extension, and .cr3 is in RAW_EXTS not SUPPORTED_EXTS."""
    path.write_bytes(b"\x00")


def test_iter_team_folders_flat_folder_with_files_yields_root(tmp_path):
    """Flat folder = only images, no subdirs. Under has_lines=False the walker
    yields root itself so ingest treats it as one team. This is the fix for
    the job-115 silent-success case (imported 12 PNGs directly into a leaf
    folder, ended up with a 'done' job that had 0 sessions)."""
    from app.api.jobs import _iter_team_folders
    root = tmp_path / "Flat"
    root.mkdir()
    _touch_png(root / "a.png")
    _touch_png(root / "b.png")

    yielded = list(_iter_team_folders(root, has_lines=False))
    assert yielded == [root]


def test_iter_team_folders_subdirs_and_loose_files_yields_only_subdirs(tmp_path):
    """Locking today's behavior: a shoot folder with team subdirs + loose
    RAW sidecars at root still yields ONLY the team subdirs. The
    root-level files (RAWs in real jobs) are ignored, same as always."""
    from app.api.jobs import _iter_team_folders
    root = tmp_path / "Shoot"
    (root / "TeamA").mkdir(parents=True)
    (root / "TeamB").mkdir(parents=True)
    _touch_png(root / "TeamA" / "a.png")
    _touch_png(root / "TeamB" / "b.png")
    # Loose sidecar files at root — must be ignored.
    _touch_cr3(root / "sidecar.CR3")
    _touch_png(root / "loose.png")

    yielded = list(_iter_team_folders(root, has_lines=False))
    assert sorted(p.name for p in yielded) == ["TeamA", "TeamB"]


def test_iter_team_folders_empty_root_yields_nothing(tmp_path):
    """No subdirs, no supported files. Walker yields nothing; the endpoint
    guard picks it up and 400s."""
    from app.api.jobs import _iter_team_folders
    root = tmp_path / "Empty"
    root.mkdir()

    assert list(_iter_team_folders(root, has_lines=False)) == []


def test_iter_team_folders_only_raw_files_no_fallback(tmp_path):
    """Root with only RAW files (no SUPPORTED_EXTS) doesn't trigger the
    fallback — RAW_EXTS aren't ingestable, so a folder with only .cr3 /
    .nef is 'nothing to ingest' from the pipeline's perspective."""
    from app.api.jobs import _iter_team_folders
    root = tmp_path / "RawOnly"
    root.mkdir()
    _touch_cr3(root / "one.CR3")
    _touch_cr3(root / "two.CR3")

    assert list(_iter_team_folders(root, has_lines=False)) == []


def test_iter_team_folders_has_lines_empty_yields_nothing_no_fallback(tmp_path):
    """has_lines=True is untouched by the fallback — an empty leagues-shape
    structure is a real structural error, and the guard should 400 it."""
    from app.api.jobs import _iter_team_folders
    root = tmp_path / "Leagues"
    root.mkdir()
    _touch_png(root / "stray.png")   # would trip has_lines=False fallback,
                                     # but under has_lines=True we still yield nothing.

    assert list(_iter_team_folders(root, has_lines=True)) == []


def test_create_400_when_root_empty(client, tmp_path):
    """Sync guard: POST /api/jobs against a completely empty folder is a
    400, not a green 'done' with 0 sessions. Also verifies the response
    message names the accepted shapes for the user."""
    empty = tmp_path / "Empty"
    empty.mkdir()

    r = client.post("/api/jobs", json={
        "name": "J", "root_path": str(empty),
        "has_lines": False, "image_subfolder_name": None, "auto_run": False,
    })
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "No team folders or importable images" in detail
    assert ".jpg" in detail   # message enumerates supported extensions


def test_create_200_flat_folder_creates_one_session_named_after_folder(
    client, tmp_path, monkeypatch,
):
    """Flat single-team folder auto-fallback: 5 PNGs directly under root,
    no subdirs. Ingest completes with 1 session named after the folder,
    5 Image rows attached to that session. This is the exact shape that
    used to silently succeed with 0 sessions (job 115, 2026-09-10)."""
    monkeypatch.setattr(jobs_module, "run_pipeline", lambda *a, **k: None)
    root = tmp_path / "Adjusted"
    root.mkdir()
    for i in range(5):
        _touch_png(root / f"IMG_{i:03d}.png")

    r = client.post("/api/jobs", json={
        "name": "MU Seniors", "root_path": str(root),
        "has_lines": False, "image_subfolder_name": None, "auto_run": False,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ingest_total"] == 1
    jid = body["job_id"]

    # TestClient ran the background ingest inline.
    status = client.get(f"/api/jobs/{jid}/ingest-status").json()
    assert status["status"] == "done"
    assert status["progress"] == 1
    assert status["total"] == 1

    detail = client.get(f"/api/jobs/{jid}").json()
    assert len(detail["sessions"]) == 1
    sess = detail["sessions"][0]
    assert sess["name"] == "Adjusted"
    assert sess["image_count"] == 5


def test_ingest_marks_error_when_all_teams_skipped_missing_subfolder(
    client, tmp_path,
):
    """Async guard: the walker yielded team folders but every one was
    skipped for a missing subfolder → 0 images added. Must not finish
    as ingest_status='done'; must be 'error' with a structured message
    that keeps skipped_teams alongside."""
    root = tmp_path / "Shoot"
    (root / "TeamA").mkdir(parents=True)
    (root / "TeamB").mkdir(parents=True)
    # Neither team has the expected "JPG" subfolder.

    r = client.post("/api/jobs", json={
        "name": "J", "root_path": str(root),
        "has_lines": False, "image_subfolder_name": "JPG", "auto_run": False,
    })
    assert r.status_code == 200, r.text
    jid = r.json()["job_id"]

    status = client.get(f"/api/jobs/{jid}/ingest-status").json()
    assert status["status"] == "error"
    assert {t["name"] for t in status["skipped_teams"]} == {"TeamA", "TeamB"}
    # The ingest_status endpoint surfaces the message so the UI can render it.
    assert status.get("error"), "expected an error message on the status payload"


def test_import_images_400_when_root_empty(client, tmp_path):
    """The sync guard fires on /import-images too, not just /jobs POST.
    Same message shape; same silent-success prevention."""
    jid = client.post("/api/jobs/shoot", json={"name": "S"}).json()["job_id"]
    empty = tmp_path / "Empty"
    empty.mkdir()
    r = client.post(f"/api/jobs/{jid}/import-images", json={
        "root_path": str(empty), "has_lines": False,
        "image_subfolder_name": None, "auto_run": False,
    })
    assert r.status_code == 400
    assert "No team folders or importable images" in r.json()["detail"]
