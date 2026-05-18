"""Tests for the async job export.

Export is now async: POST /export validates + 409s synchronously, then a
BackgroundTask does the copy and updates Job.export_*; the modal polls
/export-status. Starlette's TestClient runs the background task before the
POST returns, so by the time we poll, status == 'done'. Both the request DB
(get_db override) and the background DB (jobs.SessionLocal) point at one
temp SQLite file.
"""
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api import jobs as jobs_module
from app.api.jobs import _safe_dirname, _unique_path
from app.db import Base, get_db
from app.main import app
from app.models.db_models import (
    Cluster, Image, ImageRole, Job, Session as DbSessionModel,
)


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'export-test.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    SL = sessionmaker(bind=engine)

    def _override():
        s = SL()
        try:
            yield s
        finally:
            s.close()

    monkeypatch.setattr(jobs_module, "SessionLocal", SL)
    app.dependency_overrides[get_db] = _override
    try:
        yield TestClient(app), SL, tmp_path
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _build_job(SL, tmp_path, *, job_name="Test Job", team_specs):
    """team_specs = [{name, status?, archived?, images:[{filename, role}]}]."""
    root = tmp_path / job_name
    root.mkdir(parents=True, exist_ok=True)
    db = SL()
    job = Job(name=job_name, root_path=str(root.resolve()), has_lines=0,
              created_at=datetime.utcnow())
    db.add(job); db.commit(); db.refresh(job)
    for spec in team_specs:
        team_dir = root / spec["name"]
        team_dir.mkdir(parents=True, exist_ok=True)
        sess = DbSessionModel(
            job_id=job.id, name=spec["name"], source_path=str(team_dir.resolve()),
            status=spec.get("status", "done"),
            archived=1 if spec.get("archived") else 0,
            created_at=datetime.utcnow(),
        )
        db.add(sess); db.commit(); db.refresh(sess)
        cluster = Cluster(session_id=sess.id, image_count=0)
        db.add(cluster); db.commit(); db.refresh(cluster)
        for img in spec["images"]:
            fp = team_dir / img["filename"]
            fp.write_bytes(img.get("content", b"jpgbytes"))
            row = Image(session_id=sess.id, path=str(fp.resolve()),
                        filename=img["filename"])
            db.add(row); db.commit(); db.refresh(row)
            if img.get("role"):
                db.add(ImageRole(image_id=row.id, cluster_id=cluster.id,
                                 role=img["role"]))
        db.commit()
    jid = job.id
    db.close()
    return jid, root


def _export(client, jid, mode="copy", overwrite=True):
    """POST then poll once (bg task already ran under TestClient)."""
    r = client.post(f"/api/jobs/{jid}/export",
                     json={"mode": mode, "overwrite": overwrite})
    return r


# ── Pure helpers ──────────────────────────────────────────────────────────────


def test_safe_dirname_strips_invalid_chars():
    assert _safe_dirname('11a/Eagles?') == "11aEagles"
    assert _safe_dirname("Team A") == "Team A"
    assert _safe_dirname("   ") == "unnamed"
    assert _safe_dirname("..") == "unnamed"


def test_unique_path_appends_suffix(tmp_path):
    target = tmp_path / "img.jpg"
    target.write_bytes(b"a")
    s = _unique_path(target); s.write_bytes(b"b")
    assert s.name == "img_2.jpg"
    assert _unique_path(target).name == "img_3.jpg"


def test_unique_path_when_target_free(tmp_path):
    assert _unique_path(tmp_path / "free.jpg") == tmp_path / "free.jpg"


# ── Async export behavior ─────────────────────────────────────────────────────


def test_copy_mode_two_teams_mixed_roles(ctx):
    client, SL, tmp_path = ctx
    jid, root = _build_job(SL, tmp_path, team_specs=[
        {"name": "Team A", "images": [
            {"filename": "ind1.jpg", "role": "individual"},
            {"filename": "ind2.jpg", "role": "individual"},
            {"filename": "buddy.jpg", "role": "buddy"},
            {"filename": "pano.jpg", "role": "panoramic"},
            {"filename": "team.jpg", "role": "team"},
        ]},
        {"name": "Team B", "images": [
            {"filename": "x.jpg", "role": "individual"},
            {"filename": "y.jpg", "role": "team"},
            {"filename": "z.jpg", "role": "panoramic"},
            {"filename": "w.jpg", "role": "rejected"},
            {"filename": "v.jpg", "role": "buddy"},
        ]},
    ])

    post = _export(client, jid)
    assert post.status_code == 200
    assert post.json()["export_total"] == 9  # non-rejected images

    s = client.get(f"/api/jobs/{jid}/export-status").json()
    assert s["status"] == "done"
    res = s["result"]
    out = Path(res["output_path"])
    assert out.name == f"{root.name}_sorted"
    assert res["files_copied"] == 9
    assert res["files_skipped_rejected"] == 1

    assert {p.name for p in (out / "To_be_Cropped" / "Team A").iterdir()} == {
        "ind1.jpg", "ind2.jpg", "buddy.jpg", "pano.jpg", "team.jpg"}
    assert {p.name for p in (out / "To_be_Cropped" / "Team B").iterdir()} == {
        "x.jpg", "y.jpg", "z.jpg", "v.jpg"}
    assert {p.name for p in (out / "Team Images" / "Team A").iterdir()} == {"team.jpg"}
    assert {p.name for p in (out / "Pano Images" / "Team B").iterdir()} == {"z.jpg"}


def test_rejected_never_appears(ctx):
    client, SL, tmp_path = ctx
    jid, root = _build_job(SL, tmp_path, team_specs=[{"name": "T", "images": [
        {"filename": "keep.jpg", "role": "individual"},
        {"filename": "drop.jpg", "role": "rejected"},
    ]}])
    _export(client, jid)
    res = client.get(f"/api/jobs/{jid}/export-status").json()["result"]
    out = Path(res["output_path"])
    for sub in ["To_be_Cropped/T", "Team Images/T", "Pano Images/T"]:
        d = out / sub
        files = {p.name for p in d.iterdir()} if d.exists() else set()
        assert "drop.jpg" not in files
    assert res["files_skipped_rejected"] == 1


def test_move_mode_removes_originals(ctx):
    client, SL, tmp_path = ctx
    jid, root = _build_job(SL, tmp_path, team_specs=[{"name": "T", "images": [
        {"filename": "shot.jpg", "role": "team"},
    ]}])
    raw = root / "T" / "shot.cr3"
    raw.write_bytes(b"raw")
    _export(client, jid, mode="move")
    assert not (root / "T" / "shot.jpg").exists()
    assert raw.exists()  # raws untouched


def test_team_and_pano_in_both_dirs(ctx):
    client, SL, tmp_path = ctx
    jid, root = _build_job(SL, tmp_path, team_specs=[{"name": "T", "images": [
        {"filename": "tm.jpg", "role": "team"},
        {"filename": "pn.jpg", "role": "panoramic"},
    ]}])
    _export(client, jid)
    out = Path(client.get(f"/api/jobs/{jid}/export-status").json()["result"]["output_path"])
    assert (out / "To_be_Cropped" / "T" / "tm.jpg").exists()
    assert (out / "Team Images" / "T" / "tm.jpg").exists()
    assert (out / "To_be_Cropped" / "T" / "pn.jpg").exists()
    assert (out / "Pano Images" / "T" / "pn.jpg").exists()


def test_naming_collision_suffix(ctx):
    client, SL, tmp_path = ctx
    jid, root = _build_job(SL, tmp_path, team_specs=[{"name": "T", "images": []}])
    sub_a = root / "T" / "a"; sub_b = root / "T" / "b"
    sub_a.mkdir(parents=True); sub_b.mkdir(parents=True)
    (sub_a / "same.jpg").write_bytes(b"A")
    (sub_b / "same.jpg").write_bytes(b"B")
    db = SL()
    sess = db.query(DbSessionModel).filter_by(job_id=jid).first()
    cl = db.query(Cluster).filter_by(session_id=sess.id).first()
    for src in (sub_a / "same.jpg", sub_b / "same.jpg"):
        im = Image(session_id=sess.id, path=str(src.resolve()), filename="same.jpg")
        db.add(im); db.commit(); db.refresh(im)
        db.add(ImageRole(image_id=im.id, cluster_id=cl.id, role="individual"))
    db.commit(); db.close()

    _export(client, jid)
    out = Path(client.get(f"/api/jobs/{jid}/export-status").json()["result"]["output_path"])
    assert sorted(p.name for p in (out / "To_be_Cropped" / "T").iterdir()) == \
        ["same.jpg", "same_2.jpg"]


def test_skips_not_done_and_archived(ctx):
    client, SL, tmp_path = ctx
    jid, root = _build_job(SL, tmp_path, team_specs=[
        {"name": "Done", "status": "done",
         "images": [{"filename": "ok.jpg", "role": "individual"}]},
        {"name": "Running", "status": "running",
         "images": [{"filename": "wip.jpg", "role": "individual"}]},
        {"name": "Arch", "status": "done", "archived": True,
         "images": [{"filename": "a.jpg", "role": "individual"}]},
    ])
    _export(client, jid)
    res = client.get(f"/api/jobs/{jid}/export-status").json()["result"]
    skipped = {x["name"]: x["reason"] for x in res["sessions_skipped"]}
    assert skipped["Running"] == "pipeline_not_complete"
    assert skipped["Arch"] == "archived"
    assert res["team_count"] == 1


def test_overwrite_true_replaces(ctx):
    client, SL, tmp_path = ctx
    jid, root = _build_job(SL, tmp_path, team_specs=[{"name": "T", "images": [
        {"filename": "a.jpg", "role": "individual"}]}])
    out_root = root.parent / f"{root.name}_sorted"
    out_root.mkdir(parents=True)
    stale = out_root / "leftover.txt"
    stale.write_text("old")
    _export(client, jid, overwrite=True)
    assert not stale.exists()
    assert (out_root / "To_be_Cropped" / "T" / "a.jpg").exists()


def test_overwrite_false_existing_returns_409_synchronously(ctx):
    client, SL, tmp_path = ctx
    jid, root = _build_job(SL, tmp_path, team_specs=[{"name": "T", "images": [
        {"filename": "a.jpg", "role": "individual"}]}])
    (root.parent / f"{root.name}_sorted").mkdir(parents=True)
    r = _export(client, jid, overwrite=False)
    assert r.status_code == 409


def test_export_status_reports_total_and_eta_shape(ctx):
    client, SL, tmp_path = ctx
    jid, root = _build_job(SL, tmp_path, team_specs=[{"name": "T", "images": [
        {"filename": "a.jpg", "role": "individual"},
        {"filename": "b.jpg", "role": "team"},
    ]}])
    _export(client, jid)
    s = client.get(f"/api/jobs/{jid}/export-status").json()
    assert s["total"] == 2
    assert s["progress"] == 2
    assert s["status"] == "done"
    assert "eta_seconds" in s and "elapsed_seconds" in s


def test_export_404(ctx):
    client, SL, tmp_path = ctx
    assert client.post("/api/jobs/99999/export", json={}).status_code == 404
    assert client.get("/api/jobs/99999/export-status").status_code == 404
