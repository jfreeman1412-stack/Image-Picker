"""Archive + delete: job/session flags, list filtering, cascade delete with
thumb cleanup, and export/run-all skipping archived sessions."""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api import jobs as jobs_module
from app.db import Base, get_db
from app.main import app
from app.models.db_models import (
    Cluster, Face, Image, ImageRole, Job, Session as DbSessionModel,
)


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'arch-test.db'}",
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

    thumb_dir = tmp_path / "thumbs"
    thumb_dir.mkdir()
    monkeypatch.setattr(jobs_module, "THUMB_DIR", thumb_dir)
    monkeypatch.setattr(jobs_module, "SessionLocal", TestingSessionLocal)
    app.dependency_overrides[get_db] = _override
    client = TestClient(app)
    try:
        yield client, TestingSessionLocal, thumb_dir
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _seed_job(SessionLocal, thumb_dir, n_sessions=2, with_rows=False):
    db = SessionLocal()
    job = Job(name="J", root_path="/tmp/J", has_lines=0,
              created_at=datetime.utcnow())
    db.add(job); db.commit(); db.refresh(job)
    sess_ids = []
    for i in range(n_sessions):
        s = DbSessionModel(job_id=job.id, name=f"T{i}", source_path=f"/tmp/J/T{i}",
                            status="done", created_at=datetime.utcnow())
        db.add(s); db.commit(); db.refresh(s)
        sess_ids.append(s.id)
        if with_rows:
            img = Image(session_id=s.id, path=f"/tmp/J/T{i}/a.png", filename="a.png")
            db.add(img); db.commit(); db.refresh(img)
            cl = Cluster(session_id=s.id, image_count=1)
            db.add(cl); db.commit(); db.refresh(cl)
            db.add(Face(image_id=img.id, bbox="[0,0,1,1]", det_score=0.9,
                        cluster_id=cl.id))
            db.add(ImageRole(image_id=img.id, cluster_id=cl.id, role="team"))
            db.commit()
            (thumb_dir / f"{img.id}.jpg").write_bytes(b"x")
    jid = job.id
    db.close()
    return jid, sess_ids


def test_archive_job_idempotent(ctx):
    client, SL, td = ctx
    jid, _ = _seed_job(SL, td)
    r1 = client.post(f"/api/jobs/{jid}/archive").json()
    assert r1["archived"] is True and r1["archived_at"]
    r2 = client.post(f"/api/jobs/{jid}/archive").json()
    assert r2["archived"] is True  # idempotent, no error


def test_unarchive_job_clears_flags(ctx):
    client, SL, td = ctx
    jid, _ = _seed_job(SL, td)
    client.post(f"/api/jobs/{jid}/archive")
    r = client.post(f"/api/jobs/{jid}/unarchive").json()
    assert r["archived"] is False


def test_list_jobs_excludes_archived_by_default(ctx):
    client, SL, td = ctx
    jid, _ = _seed_job(SL, td)
    client.post(f"/api/jobs/{jid}/archive")
    assert client.get("/api/jobs").json() == []
    incl = client.get("/api/jobs?include_archived=true").json()
    assert [j["id"] for j in incl] == [jid]
    assert incl[0]["archived"] is True


def test_delete_job_cascades_and_removes_thumbs(ctx):
    client, SL, td = ctx
    jid, sess_ids = _seed_job(SL, td, with_rows=True)
    # thumb files exist pre-delete
    assert any(td.iterdir())

    r = client.delete(f"/api/jobs/{jid}")
    assert r.status_code == 200

    db = SL()
    try:
        assert db.query(Job).get(jid) is None
        assert db.query(DbSessionModel).filter_by(job_id=jid).count() == 0
        assert db.query(Image).count() == 0
        assert db.query(Face).count() == 0
        assert db.query(Cluster).count() == 0
        assert db.query(ImageRole).count() == 0
    finally:
        db.close()
    assert not any(td.iterdir())  # thumbs gone


def test_delete_job_404(ctx):
    client, SL, td = ctx
    assert client.delete("/api/jobs/99999").status_code == 404


def test_session_archive_unarchive_and_get_job_filtering(ctx):
    client, SL, td = ctx
    jid, sess_ids = _seed_job(SL, td, n_sessions=3)
    client.post(f"/api/sessions/{sess_ids[0]}/archive")

    detail = client.get(f"/api/jobs/{jid}").json()
    assert {s["id"] for s in detail["sessions"]} == set(sess_ids[1:])

    incl = client.get(f"/api/jobs/{jid}?include_archived=true").json()
    assert {s["id"] for s in incl["sessions"]} == set(sess_ids)
    arch = next(s for s in incl["sessions"] if s["id"] == sess_ids[0])
    assert arch["archived"] is True

    client.post(f"/api/sessions/{sess_ids[0]}/unarchive")
    detail2 = client.get(f"/api/jobs/{jid}").json()
    assert {s["id"] for s in detail2["sessions"]} == set(sess_ids)


def test_delete_session_keeps_job(ctx):
    client, SL, td = ctx
    jid, sess_ids = _seed_job(SL, td, n_sessions=2, with_rows=True)
    r = client.delete(f"/api/sessions/{sess_ids[0]}")
    assert r.status_code == 200
    db = SL()
    try:
        assert db.query(Job).get(jid) is not None        # job intact
        assert db.query(DbSessionModel).get(sess_ids[0]) is None
        assert db.query(DbSessionModel).get(sess_ids[1]) is not None
    finally:
        db.close()


def test_export_skips_archived_sessions(ctx, tmp_path):
    client, SL, td = ctx
    # Build a real job dir so export can run.
    root = tmp_path / "ShootX"
    root.mkdir()
    db = SL()
    job = Job(name="ShootX", root_path=str(root), has_lines=0,
              created_at=datetime.utcnow())
    db.add(job); db.commit(); db.refresh(job)
    s_active = DbSessionModel(job_id=job.id, name="Active", source_path=str(root / "Active"),
                              status="done", created_at=datetime.utcnow())
    s_arch = DbSessionModel(job_id=job.id, name="Arch", source_path=str(root / "Arch"),
                            status="done", archived=1, archived_at=datetime.utcnow(),
                            created_at=datetime.utcnow())
    db.add_all([s_active, s_arch]); db.commit()
    jid = job.id
    db.close()

    r = client.post(f"/api/jobs/{jid}/export", json={"mode": "copy", "overwrite": True})
    assert r.status_code == 200
    # Export is async now — skipped sessions land in the polled status result
    # (TestClient already ran the background task).
    res = client.get(f"/api/jobs/{jid}/export-status").json()["result"]
    skipped = {x["name"]: x["reason"] for x in res["sessions_skipped"]}
    assert skipped.get("Arch") == "archived"


def test_run_all_skips_archived(ctx):
    client, SL, td = ctx
    jid, sess_ids = _seed_job(SL, td, n_sessions=3)
    client.post(f"/api/sessions/{sess_ids[0]}/archive")
    r = client.post(f"/api/jobs/{jid}/run-all").json()
    assert r["session_count"] == 2  # archived one not counted

    db = SL()
    try:
        archived = db.query(DbSessionModel).get(sess_ids[0])
        assert archived.status != "running"  # not kicked off
    finally:
        db.close()


def test_next_unreviewed_skips_archived(ctx):
    client, SL, td = ctx
    jid, sess_ids = _seed_job(SL, td, n_sessions=3)
    # Archive the middle one; from session 0, next-unreviewed should skip it.
    client.post(f"/api/sessions/{sess_ids[1]}/archive")
    r = client.get(f"/api/sessions/{sess_ids[0]}/next-unreviewed").json()
    assert r["session_id"] == sess_ids[2]
