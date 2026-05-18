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
