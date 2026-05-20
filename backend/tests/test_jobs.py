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
