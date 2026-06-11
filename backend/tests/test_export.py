"""Tests for the async job export.

Export is now async: POST /export validates + 409s synchronously, then a
BackgroundTask does the copy and updates Job.export_*; the modal polls
/export-status. Starlette's TestClient runs the background task before the
POST returns, so by the time we poll, status == 'done'. Both the request DB
(get_db override) and the background DB (jobs.SessionLocal) point at one
temp SQLite file.
"""
import errno
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


def _export(client, jid, mode="copy", overwrite=True, destination_path=None):
    """POST then poll once (bg task already ran under TestClient)."""
    body = {"mode": mode, "overwrite": overwrite}
    if destination_path is not None:
        body["destination_path"] = destination_path
    r = client.post(f"/api/jobs/{jid}/export", json=body)
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


def test_destination_path_overrides_default_location(ctx):
    """Phase 9: user-picked destination wins; legacy <root>_sorted is NOT created."""
    client, SL, tmp_path = ctx
    jid, root = _build_job(SL, tmp_path, team_specs=[{"name": "T", "images": [
        {"filename": "a.jpg", "role": "individual"}]}])
    legacy = root.parent / f"{root.name}_sorted"
    picked = tmp_path / "custom" / "exports" / "MyTrip"
    _export(client, jid, destination_path=str(picked))
    # Output lands in the picked path…
    assert (picked / "To_be_Cropped" / "T" / "a.jpg").exists()
    # …and the legacy sibling path is NOT created.
    assert not legacy.exists()


def test_destination_path_works_when_target_missing_parents(ctx):
    """Picked destination several levels deep is auto-created (existing
    mkdir(parents=True) handles this)."""
    client, SL, tmp_path = ctx
    jid, root = _build_job(SL, tmp_path, team_specs=[{"name": "T", "images": [
        {"filename": "a.jpg", "role": "individual"}]}])
    picked = tmp_path / "a" / "b" / "c" / "out"
    assert not picked.exists()
    _export(client, jid, destination_path=str(picked))
    assert (picked / "To_be_Cropped" / "T" / "a.jpg").exists()


def test_destination_path_none_preserves_legacy_default(ctx):
    """Omitting destination_path keeps the original <root>_sorted behavior
    so older callers (and unmodified UIs) continue working unchanged."""
    client, SL, tmp_path = ctx
    jid, root = _build_job(SL, tmp_path, team_specs=[{"name": "T", "images": [
        {"filename": "a.jpg", "role": "individual"}]}])
    res = _export(client, jid)
    assert res.status_code == 200
    legacy = root.parent / f"{root.name}_sorted"
    assert (legacy / "To_be_Cropped" / "T" / "a.jpg").exists()


def test_destination_path_409_when_exists_and_no_overwrite(ctx):
    """Picked destination already populated + overwrite=False → 409 sync."""
    client, SL, tmp_path = ctx
    jid, _ = _build_job(SL, tmp_path, team_specs=[{"name": "T", "images": [
        {"filename": "a.jpg", "role": "individual"}]}])
    picked = tmp_path / "out"
    picked.mkdir()
    (picked / "leftover.txt").write_text("x")
    r = _export(client, jid, overwrite=False, destination_path=str(picked))
    assert r.status_code == 409


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


# ── Export hardening (2026-05-28) ─────────────────────────────────────────────
# Three fixes pair-bound by the same job-28 failure: (a) _safe_copy2 EINVAL
# retry+fallback, (b) per-file fault tolerance in _run_export (no cascade-abort
# on a single bad copy), (c) default role='individual' for Issue 5 orphans.


def test_safe_copy2_succeeds_first_try(tmp_path):
    from app.api.jobs import _safe_copy2
    src = tmp_path / "src.jpg"; src.write_bytes(b"hi")
    dst = tmp_path / "dst.jpg"
    _safe_copy2(src, dst)
    assert dst.read_bytes() == b"hi"


def test_safe_copy2_retries_einval_then_falls_back_to_copyfile(tmp_path, monkeypatch):
    """Transient SMB EINVAL on copy2 → 3 retries → fall back to copyfile so
    bytes still land (metadata is non-essential)."""
    from app.api import jobs as jobs_mod
    from app.api.jobs import _safe_copy2

    src = tmp_path / "s.jpg"; src.write_bytes(b"payload")
    dst = tmp_path / "d.jpg"
    calls = {"copy2": 0, "copyfile": 0}

    def fake_copy2(s, d):
        calls["copy2"] += 1
        raise OSError(errno.EINVAL, "Invalid argument")

    def fake_copyfile(s, d):
        calls["copyfile"] += 1
        Path(d).write_bytes(Path(s).read_bytes())

    monkeypatch.setattr(jobs_mod.shutil, "copy2", fake_copy2)
    monkeypatch.setattr(jobs_mod.shutil, "copyfile", fake_copyfile)
    monkeypatch.setattr(jobs_mod.time, "sleep", lambda _x: None)   # speed up backoff

    _safe_copy2(src, dst)

    assert calls["copy2"] == 3                # all retries exhausted
    assert calls["copyfile"] == 1             # then fell back
    assert dst.read_bytes() == b"payload"     # bytes landed


def test_safe_copy2_non_einval_raises_immediately(tmp_path, monkeypatch):
    """A non-EINVAL OSError (e.g. ENOENT) is NOT retried — we must not mask
    unrelated failures (missing source, permission, disk full, …)."""
    from app.api import jobs as jobs_mod
    from app.api.jobs import _safe_copy2

    src = tmp_path / "s.jpg"; src.write_bytes(b"x")
    dst = tmp_path / "d.jpg"
    calls = {"copy2": 0}

    def fake_copy2(s, d):
        calls["copy2"] += 1
        raise OSError(errno.ENOENT, "No such file")

    monkeypatch.setattr(jobs_mod.shutil, "copy2", fake_copy2)

    with pytest.raises(OSError) as exc_info:
        _safe_copy2(src, dst)
    assert exc_info.value.errno == errno.ENOENT
    assert calls["copy2"] == 1                # no retry on non-EINVAL


def test_export_continues_past_per_file_failure_no_cascade(ctx, monkeypatch):
    """A failing _copy_one in session A must NOT abort session B (the job-28
    cascade-abort bug). The job runs to completion; the failed file is
    recorded in export_result.failures; status='error' signals 'needs
    attention'; every other file actually landed on disk."""
    client, SL, tmp_path = ctx
    from app.api import jobs as jobs_mod

    jid, root = _build_job(SL, tmp_path, team_specs=[
        {"name": "Team A", "images": [
            {"filename": "a1.jpg", "role": "individual"},
            {"filename": "a2.jpg", "role": "team"},        # <- we'll fail this one
            {"filename": "a3.jpg", "role": "individual"},
        ]},
        {"name": "Team B", "images": [
            {"filename": "b1.jpg", "role": "individual"},
            {"filename": "b2.jpg", "role": "panoramic"},
            {"filename": "b3.jpg", "role": "individual"},
        ]},
    ])

    real_copy_one = jobs_mod._copy_one

    def flaky(src, website_dst, secondary_dst, mode):
        if Path(src).name == "a2.jpg":
            raise OSError(errno.EINVAL, "Invalid argument")
        return real_copy_one(src, website_dst, secondary_dst, mode)

    monkeypatch.setattr(jobs_mod, "_copy_one", flaky)

    _export(client, jid)
    s = client.get(f"/api/jobs/{jid}/export-status").json()
    res = s["result"]

    # Job ran to completion across BOTH sessions despite the failure on a2.
    assert s["status"] == "error", s            # signals failures, didn't abort
    assert res["files_failed"] == 1
    assert res["files_copied"] == 5              # 6 attempted - 1 failed
    assert len(res["failures"]) == 1
    assert res["failures"][0]["role"] == "team"
    assert "a2.jpg" in res["failures"][0]["src"]

    out = Path(res["output_path"])
    # The failed file's destination is absent.
    assert not (out / "To_be_Cropped" / "Team A" / "a2.jpg").exists()
    # Every other file in BOTH teams actually landed (no cascade abort).
    for team, fn in [("Team A", "a1.jpg"), ("Team A", "a3.jpg"),
                     ("Team B", "b1.jpg"), ("Team B", "b2.jpg"),
                     ("Team B", "b3.jpg")]:
        assert (out / "To_be_Cropped" / team / fn).exists(), f"{team}/{fn} missing"


def test_export_includes_orphan_image_as_individual(ctx):
    """Issue 5: an image with NO ImageRole (orphan from clustering, e.g. a
    single-image team folder's lone coach photo) must still ship to
    To_be_Cropped as an individual — not silently skipped."""
    client, SL, tmp_path = ctx
    jid, root = _build_job(SL, tmp_path, team_specs=[
        {"name": "Coach Only", "images": [
            {"filename": "coach.jpg"},     # NO role -> orphan
        ]},
        {"name": "Team B", "images": [     # a normal team alongside
            {"filename": "b1.jpg", "role": "individual"},
            {"filename": "b2.jpg", "role": "team"},
        ]},
    ])

    post = _export(client, jid)
    assert post.status_code == 200
    assert post.json()["export_total"] == 3   # orphan counted in total now

    s = client.get(f"/api/jobs/{jid}/export-status").json()
    assert s["status"] == "done", s
    res = s["result"]
    assert res["files_copied"] == 3
    assert res["files_failed"] == 0

    out = Path(res["output_path"])
    # Orphan landed in To_be_Cropped (treated as individual).
    assert (out / "To_be_Cropped" / "Coach Only" / "coach.jpg").exists()
    # No secondary copy (individual role doesn't trigger team/pano).
    assert not (out / "Team Images" / "Coach Only" / "coach.jpg").exists()
    assert not (out / "Pano Images" / "Coach Only" / "coach.jpg").exists()
    # Normal team still works.
    assert (out / "To_be_Cropped" / "Team B" / "b1.jpg").exists()
    assert (out / "Team Images" / "Team B" / "b2.jpg").exists()


# ── rename-on-export (2026-06-02) ────────────────────────────────────────────
#
# Per-export `rename_by_player` toggle on ExportJobRequest. When OFF (default),
# camera filenames preserved exactly as today. When ON, each clustered image
# gets a filename derived from its cluster's display_label() + role suffix:
#
#   - matched/manual label: <Label>-t.png (team), <Label>-p.png (pano),
#     <Label>_001.png, _002.png, ... (individuals + buddy, capture_time order)
#   - unmatched cluster:   Player_<cluster_id>-t.png / -p.png / _NNN.png
#   - coach via is_coach_for_sort(): Coach_<cluster_id> prefix (honors
#     manual_coach_override — same rule as the naming-errors filter)
#
# Buddy photos (image has >=2 ImageRole rows, one per cluster claiming it):
# copied ONCE PER cluster, each named after that cluster's display_label.
# Multiple clusters in the same session land both copies in that session's
# To_be_Cropped folder.
#
# Truly-orphan images (in session.images but no ImageRole rows): keep
# camera filename — there's no cluster to derive from. Today's legacy
# behavior for this subset survives unchanged into rename mode.


def _rename_job(SL, tmp_path, *, job_name="J", team_specs):
    """Richer build helper for rename-mode tests. Each team spec is:
        {
          "name": str,
          "clusters": [{
              "label": str | None,                  # -> auto_label
              "manual_label": str | None,           # takes precedence
              "is_likely_coach": 0 | 1,
              "manual_coach_override": -1 | 0 | 1,
              "images": [{
                  "filename": str,                  # if shared across clusters,
                                                    # same filename = same Image row
                  "role": "team" | "panoramic" | "individual" | "buddy" | "rejected",
                  "capture_time": datetime | None,
              }]
          }],
          "orphan_images": [{"filename": str}]      # no ImageRole — truly orphan
        }
    Same filename in two clusters of the same team = same Image row =
    buddy expansion target. Across teams, same filename = different
    physical files (different folders).
    """
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
            status="done", created_at=datetime.utcnow(),
        )
        db.add(sess); db.commit(); db.refresh(sess)
        # Map filename -> Image row for THIS session (shared buddy detection).
        image_by_filename: dict[str, Image] = {}
        for cluster_spec in spec.get("clusters", []):
            cl = Cluster(
                session_id=sess.id, image_count=0,
                auto_label=cluster_spec.get("label"),
                manual_label=cluster_spec.get("manual_label"),
                is_likely_coach=cluster_spec.get("is_likely_coach", 0),
                manual_coach_override=cluster_spec.get("manual_coach_override", 0),
            )
            db.add(cl); db.commit(); db.refresh(cl)
            for img_spec in cluster_spec["images"]:
                fn = img_spec["filename"]
                if fn in image_by_filename:
                    img = image_by_filename[fn]
                else:
                    fp = team_dir / fn
                    if not fp.exists():
                        fp.write_bytes(b"jpgbytes")
                    img = Image(
                        session_id=sess.id, path=str(fp.resolve()),
                        filename=fn, capture_time=img_spec.get("capture_time"),
                    )
                    db.add(img); db.commit(); db.refresh(img)
                    image_by_filename[fn] = img
                db.add(ImageRole(image_id=img.id, cluster_id=cl.id,
                                 role=img_spec["role"]))
        for orphan in spec.get("orphan_images", []):
            fn = orphan["filename"]
            fp = team_dir / fn
            if not fp.exists():
                fp.write_bytes(b"orphanbytes")
            db.add(Image(session_id=sess.id, path=str(fp.resolve()),
                         filename=fn))
        db.commit()
    jid = job.id
    db.close()
    return jid, root


def _rename_export(client, jid, **kw):
    """POST /export with rename_by_player=True (other kwargs forwarded)."""
    body = {"mode": "copy", "overwrite": True, "rename_by_player": True}
    body.update(kw)
    return client.post(f"/api/jobs/{jid}/export", json=body)


# ── Toggle OFF: byte-identical to today's behavior ───────────────────────────


def test_rename_toggle_off_byte_identical_to_legacy(ctx):
    """The toggle defaults to False; sending rename_by_player=False (or
    omitting it) must produce exactly today's filenames. The whole
    existing test suite (562 baseline) doubles as the regression net;
    this test pins the explicit-False contract."""
    client, SL, tmp_path = ctx
    jid, root = _build_job(SL, tmp_path, team_specs=[{"name": "T", "images": [
        {"filename": "shot.jpg", "role": "team"},
        {"filename": "x.jpg", "role": "individual"},
    ]}])
    body = {"mode": "copy", "overwrite": True, "rename_by_player": False}
    r = client.post(f"/api/jobs/{jid}/export", json=body)
    assert r.status_code == 200
    out = Path(client.get(f"/api/jobs/{jid}/export-status").json()["result"]["output_path"])
    # Camera filenames preserved exactly.
    assert (out / "To_be_Cropped" / "T" / "shot.jpg").exists()
    assert (out / "To_be_Cropped" / "T" / "x.jpg").exists()
    assert (out / "Team Images" / "T" / "shot.jpg").exists()


# ── Matched player: name + role + sequence ───────────────────────────────────


def test_rename_matched_player_full_sequence(ctx):
    """5 individuals + 1 team + 1 pano for a matched player → 7 renamed
    files with role suffixes and zero-padded 3-digit sequence ordered by
    capture_time."""
    client, SL, tmp_path = ctx
    t0 = datetime(2026, 1, 1, 10, 0, 0)
    images = [
        # Out-of-order capture_times to verify the sort key.
        {"filename": "a.jpg", "role": "individual",
         "capture_time": datetime(2026, 1, 1, 10, 2, 0)},
        {"filename": "b.jpg", "role": "individual",
         "capture_time": datetime(2026, 1, 1, 10, 0, 0)},
        {"filename": "c.jpg", "role": "individual",
         "capture_time": datetime(2026, 1, 1, 10, 4, 0)},
        {"filename": "d.jpg", "role": "individual",
         "capture_time": datetime(2026, 1, 1, 10, 1, 0)},
        {"filename": "e.jpg", "role": "individual",
         "capture_time": datetime(2026, 1, 1, 10, 3, 0)},
        {"filename": "tm.jpg", "role": "team",
         "capture_time": datetime(2026, 1, 1, 10, 5, 0)},
        {"filename": "pn.jpg", "role": "panoramic",
         "capture_time": datetime(2026, 1, 1, 10, 6, 0)},
    ]
    jid, root = _rename_job(SL, tmp_path, team_specs=[{
        "name": "T", "clusters": [
            {"label": "Aviel-Afonya", "images": images}
        ]
    }])
    r = _rename_export(client, jid)
    assert r.status_code == 200
    out = Path(client.get(f"/api/jobs/{jid}/export-status").json()["result"]["output_path"])
    cropped = out / "To_be_Cropped" / "T"
    files = sorted(p.name for p in cropped.iterdir())
    # Sequence: b (10:00), d (10:01), a (10:02), e (10:03), c (10:04)
    assert files == [
        "Aviel-Afonya-p.jpg",
        "Aviel-Afonya-t.jpg",
        "Aviel-Afonya_001.jpg",   # b at 10:00
        "Aviel-Afonya_002.jpg",   # d at 10:01
        "Aviel-Afonya_003.jpg",   # a at 10:02
        "Aviel-Afonya_004.jpg",   # e at 10:03
        "Aviel-Afonya_005.jpg",   # c at 10:04
    ]
    # Team + pano also in their dedicated folders.
    assert (out / "Team Images" / "T" / "Aviel-Afonya-t.jpg").exists()
    assert (out / "Pano Images" / "T" / "Aviel-Afonya-p.jpg").exists()


# ── Unmatched: Player_<cluster_id> ───────────────────────────────────────────


def test_rename_unmatched_uses_cluster_id(ctx):
    """No auto_label, no manual_label, not a coach → Player_<cluster_id>."""
    client, SL, tmp_path = ctx
    jid, root = _rename_job(SL, tmp_path, team_specs=[{
        "name": "T", "clusters": [
            {"label": None, "images": [
                {"filename": "x1.jpg", "role": "individual"},
                {"filename": "x2.jpg", "role": "team"},
            ]}
        ]
    }])
    _rename_export(client, jid)
    out = Path(client.get(f"/api/jobs/{jid}/export-status").json()["result"]["output_path"])
    files = sorted(p.name for p in (out / "To_be_Cropped" / "T").iterdir())
    # Cluster id is unknown to the test, but the format must match Player_<id>_001.jpg.
    assert any(f.startswith("Player_") and f.endswith("_001.jpg") for f in files)
    assert any(f.startswith("Player_") and f.endswith("-t.jpg") for f in files)


# ── Coach: is_coach_for_sort() resolves manual overrides ─────────────────────


def test_rename_singleton_coach_uses_coach_prefix(ctx):
    """is_likely_coach=1, no label → Coach_<cluster_id>."""
    client, SL, tmp_path = ctx
    jid, root = _rename_job(SL, tmp_path, team_specs=[{
        "name": "T", "clusters": [
            {"label": None, "is_likely_coach": 1, "images": [
                {"filename": "c.jpg", "role": "team"},
            ]}
        ]
    }])
    _rename_export(client, jid)
    out = Path(client.get(f"/api/jobs/{jid}/export-status").json()["result"]["output_path"])
    files = list((out / "To_be_Cropped" / "T").iterdir())
    assert len(files) == 1
    assert files[0].name.startswith("Coach_") and files[0].name.endswith("-t.jpg")


def test_rename_coach_demoted_to_player_uses_player_prefix(ctx):
    """is_likely_coach=1 BUT manual_coach_override=-1 → is_coach_for_sort()
    returns False → Player_<cluster_id> prefix."""
    client, SL, tmp_path = ctx
    jid, root = _rename_job(SL, tmp_path, team_specs=[{
        "name": "T", "clusters": [
            {"label": None, "is_likely_coach": 1, "manual_coach_override": -1,
             "images": [{"filename": "x.jpg", "role": "individual"}]}
        ]
    }])
    _rename_export(client, jid)
    out = Path(client.get(f"/api/jobs/{jid}/export-status").json()["result"]["output_path"])
    files = list((out / "To_be_Cropped" / "T").iterdir())
    assert any(f.name.startswith("Player_") for f in files)
    assert not any(f.name.startswith("Coach_") for f in files)


def test_rename_player_promoted_to_coach_uses_coach_prefix(ctx):
    """is_likely_coach=0 BUT manual_coach_override=1 → is_coach_for_sort()
    returns True → Coach_<cluster_id> prefix."""
    client, SL, tmp_path = ctx
    jid, root = _rename_job(SL, tmp_path, team_specs=[{
        "name": "T", "clusters": [
            {"label": None, "is_likely_coach": 0, "manual_coach_override": 1,
             "images": [{"filename": "x.jpg", "role": "individual"}]}
        ]
    }])
    _rename_export(client, jid)
    out = Path(client.get(f"/api/jobs/{jid}/export-status").json()["result"]["output_path"])
    files = list((out / "To_be_Cropped" / "T").iterdir())
    assert any(f.name.startswith("Coach_") for f in files)


# ── Manual label takes precedence ────────────────────────────────────────────


def test_rename_manual_label_wins_over_auto(ctx):
    """display_label() precedence: manual > auto > Player_<id>. Manual
    label becomes the customer-visible filename (after _safe_dirname)."""
    client, SL, tmp_path = ctx
    jid, root = _rename_job(SL, tmp_path, team_specs=[{
        "name": "T", "clusters": [
            {"label": "Auto-Name", "manual_label": "Manual-Name",
             "images": [{"filename": "x.jpg", "role": "individual"}]}
        ]
    }])
    _rename_export(client, jid)
    out = Path(client.get(f"/api/jobs/{jid}/export-status").json()["result"]["output_path"])
    files = [p.name for p in (out / "To_be_Cropped" / "T").iterdir()]
    assert any("Manual-Name" in f for f in files)
    assert not any("Auto-Name" in f for f in files)


def test_rename_manual_label_with_invalid_chars_stripped(ctx):
    """Manual label characters that are filesystem-invalid get stripped
    by _safe_dirname. Operator discipline plus the safety net."""
    client, SL, tmp_path = ctx
    jid, root = _rename_job(SL, tmp_path, team_specs=[{
        "name": "T", "clusters": [
            {"manual_label": "Aviel/Afonya?",
             "images": [{"filename": "x.jpg", "role": "individual"}]}
        ]
    }])
    _rename_export(client, jid)
    out = Path(client.get(f"/api/jobs/{jid}/export-status").json()["result"]["output_path"])
    files = [p.name for p in (out / "To_be_Cropped" / "T").iterdir()]
    assert files == ["AvielAfonya_001.jpg"]


# ── Buddy expansion: 1 source → N copies ─────────────────────────────────────


def test_rename_buddy_cross_team_two_copies(ctx):
    """2-kid buddy photo, kids on DIFFERENT teams → one copy per team's
    folder, each named after the kid in that team."""
    client, SL, tmp_path = ctx
    # Same image lives physically in Team A's source folder. Both kids'
    # clusters claim it via ImageRole. Cluster A is in Team A's session;
    # cluster B is in Team B's session — wait, that's the cross-team case
    # we can't model with a shared filename across teams in this helper.
    # Instead, simulate by directly adding ImageRole rows that reference
    # the same image_id from clusters in different sessions.
    jid, root = _rename_job(SL, tmp_path, team_specs=[
        {"name": "Team A", "clusters": [
            {"label": "Alice", "images": [
                {"filename": "buddy.jpg", "role": "buddy"},
            ]}
        ]},
        {"name": "Team B", "clusters": [
            {"label": "Bob", "images": []}  # set up cluster; ImageRole added below
        ]},
    ])
    # Wire the buddy.jpg image (physically in Team A) into Bob's cluster too.
    db = SL()
    img = db.query(Image).filter_by(filename="buddy.jpg").first()
    bob_cluster = db.query(Cluster).filter_by(auto_label="Bob").first()
    db.add(ImageRole(image_id=img.id, cluster_id=bob_cluster.id, role="buddy"))
    db.commit(); db.close()

    _rename_export(client, jid)
    out = Path(client.get(f"/api/jobs/{jid}/export-status").json()["result"]["output_path"])
    # One copy in each team's To_be_Cropped folder, named for that team's kid.
    assert (out / "To_be_Cropped" / "Team A" / "Alice_001.jpg").exists()
    assert (out / "To_be_Cropped" / "Team B" / "Bob_001.jpg").exists()
    # Buddy does NOT go to Team Images or Pano Images.
    assert not (out / "Team Images" / "Team A").exists() or \
           not any((out / "Team Images" / "Team A").iterdir())
    assert not (out / "Team Images" / "Team B").exists() or \
           not any((out / "Team Images" / "Team B").iterdir())


def test_rename_buddy_same_team_both_copies_in_one_folder(ctx):
    """Two kids on the same team in a buddy → both copies land in that
    team's To_be_Cropped folder under their respective names."""
    client, SL, tmp_path = ctx
    # Two clusters in the same session, both claim 'buddy.jpg'.
    jid, root = _rename_job(SL, tmp_path, team_specs=[
        {"name": "T", "clusters": [
            {"label": "Carol", "images": [
                {"filename": "buddy.jpg", "role": "buddy"},
            ]},
            {"label": "Dan", "images": [
                {"filename": "buddy.jpg", "role": "buddy"},  # SAME filename = SAME Image row
            ]},
        ]},
    ])
    _rename_export(client, jid)
    out = Path(client.get(f"/api/jobs/{jid}/export-status").json()["result"]["output_path"])
    files = sorted(p.name for p in (out / "To_be_Cropped" / "T").iterdir())
    assert files == ["Carol_001.jpg", "Dan_001.jpg"]


def test_rename_buddy_three_kids_three_copies(ctx):
    """3-kid buddy across 3 different teams → 3 copies in 3 folders."""
    client, SL, tmp_path = ctx
    jid, root = _rename_job(SL, tmp_path, team_specs=[
        {"name": "T1", "clusters": [{"label": "Eve", "images": [
            {"filename": "buddy3.jpg", "role": "buddy"},
        ]}]},
        {"name": "T2", "clusters": [{"label": "Frank", "images": []}]},
        {"name": "T3", "clusters": [{"label": "Grace", "images": []}]},
    ])
    db = SL()
    img = db.query(Image).filter_by(filename="buddy3.jpg").first()
    for label in ("Frank", "Grace"):
        cl = db.query(Cluster).filter_by(auto_label=label).first()
        db.add(ImageRole(image_id=img.id, cluster_id=cl.id, role="buddy"))
    db.commit(); db.close()

    _rename_export(client, jid)
    out = Path(client.get(f"/api/jobs/{jid}/export-status").json()["result"]["output_path"])
    assert (out / "To_be_Cropped" / "T1" / "Eve_001.jpg").exists()
    assert (out / "To_be_Cropped" / "T2" / "Frank_001.jpg").exists()
    assert (out / "To_be_Cropped" / "T3" / "Grace_001.jpg").exists()


def test_rename_buddy_matched_plus_unmatched_mix(ctx):
    """Buddy with one matched + one unmatched cluster: matched gets named,
    unmatched gets Player_<cluster_id>."""
    client, SL, tmp_path = ctx
    jid, root = _rename_job(SL, tmp_path, team_specs=[
        {"name": "T1", "clusters": [{"label": "Henry", "images": [
            {"filename": "bm.jpg", "role": "buddy"},
        ]}]},
        {"name": "T2", "clusters": [{"label": None, "images": []}]},  # unmatched
    ])
    db = SL()
    img = db.query(Image).filter_by(filename="bm.jpg").first()
    unmatched = db.query(Cluster).filter(Cluster.auto_label.is_(None)).first()
    db.add(ImageRole(image_id=img.id, cluster_id=unmatched.id, role="buddy"))
    db.commit(); db.close()

    _rename_export(client, jid)
    out = Path(client.get(f"/api/jobs/{jid}/export-status").json()["result"]["output_path"])
    assert (out / "To_be_Cropped" / "T1" / "Henry_001.jpg").exists()
    t2_files = [p.name for p in (out / "To_be_Cropped" / "T2").iterdir()]
    assert any(f.startswith("Player_") for f in t2_files)


# ── Multiple-team / multiple-pano error condition ────────────────────────────


def test_rename_multiple_team_photos_log_and_skip_extras(ctx):
    """A cluster with 2 team-role images should NOT silently overwrite —
    write the first, skip the second, log the duplicate."""
    client, SL, tmp_path = ctx
    jid, root = _rename_job(SL, tmp_path, team_specs=[{
        "name": "T", "clusters": [
            {"label": "Iris", "images": [
                {"filename": "t1.jpg", "role": "team",
                 "capture_time": datetime(2026, 1, 1, 10, 0, 0)},
                {"filename": "t2.jpg", "role": "team",
                 "capture_time": datetime(2026, 1, 1, 10, 1, 0)},
            ]}
        ]
    }])
    _rename_export(client, jid)
    out = Path(client.get(f"/api/jobs/{jid}/export-status").json()["result"]["output_path"])
    cropped = out / "To_be_Cropped" / "T"
    team_dir = out / "Team Images" / "T"
    # Exactly one team file lands in each dir under Iris-t.jpg.
    assert (cropped / "Iris-t.jpg").exists()
    assert (team_dir / "Iris-t.jpg").exists()
    # The second team image is skipped from the rename plan (not written
    # under a colliding name or _2 suffix — explicitly omitted).
    assert not (cropped / "Iris-t_2.jpg").exists()


# ── Cross-session uniqueness ─────────────────────────────────────────────────


def test_rename_same_player_two_team_folders_no_collision(ctx):
    """Same player legitimately in two team folders (e.g., a multi-team
    coach or a Q6 guest) → both files share the same name in their
    respective folders. Different paths, no collision."""
    client, SL, tmp_path = ctx
    jid, root = _rename_job(SL, tmp_path, team_specs=[
        {"name": "T1", "clusters": [{"label": "Coach-Joe", "images": [
            {"filename": "j1.jpg", "role": "team"},
        ]}]},
        {"name": "T2", "clusters": [{"label": "Coach-Joe", "images": [
            {"filename": "j2.jpg", "role": "team"},
        ]}]},
    ])
    _rename_export(client, jid)
    out = Path(client.get(f"/api/jobs/{jid}/export-status").json()["result"]["output_path"])
    assert (out / "To_be_Cropped" / "T1" / "Coach-Joe-t.jpg").exists()
    assert (out / "To_be_Cropped" / "T2" / "Coach-Joe-t.jpg").exists()


# ── Orphan handling (user's added cases) ─────────────────────────────────────


def test_rename_truly_orphan_image_keeps_camera_filename(ctx):
    """Image with no ImageRole rows (no cluster claim) → camera filename
    preserved in rename mode. Today's legacy behavior for this subset
    survives unchanged."""
    client, SL, tmp_path = ctx
    jid, root = _rename_job(SL, tmp_path, team_specs=[{
        "name": "T", "clusters": [
            {"label": "Jack", "images": [
                {"filename": "j.jpg", "role": "individual"},
            ]}
        ],
        "orphan_images": [{"filename": "105A5333.png"}],
    }])
    _rename_export(client, jid)
    out = Path(client.get(f"/api/jobs/{jid}/export-status").json()["result"]["output_path"])
    files = sorted(p.name for p in (out / "To_be_Cropped" / "T").iterdir())
    assert files == ["105A5333.png", "Jack_001.jpg"]


def test_rename_mixed_clustered_and_orphan_coexist(ctx):
    """Mix of clustered + orphan images in the same session → renamed
    files and camera names coexist correctly in To_be_Cropped."""
    client, SL, tmp_path = ctx
    jid, root = _rename_job(SL, tmp_path, team_specs=[{
        "name": "T", "clusters": [
            {"label": "Kate", "images": [
                {"filename": "k1.jpg", "role": "individual"},
                {"filename": "k2.jpg", "role": "team"},
            ]}
        ],
        "orphan_images": [
            {"filename": "test1.png"}, {"filename": "test2.png"},
        ],
    }])
    _rename_export(client, jid)
    out = Path(client.get(f"/api/jobs/{jid}/export-status").json()["result"]["output_path"])
    files = sorted(p.name for p in (out / "To_be_Cropped" / "T").iterdir())
    assert files == ["Kate-t.jpg", "Kate_001.jpg", "test1.png", "test2.png"]


def test_rename_all_orphan_session_keeps_camera_names(ctx):
    """A Color_Swatch-style session with no clusters (all images are
    truly orphan) → every file keeps its camera filename. No renamed
    files appear."""
    client, SL, tmp_path = ctx
    jid, root = _rename_job(SL, tmp_path, team_specs=[{
        "name": "Color_Swatch",
        "clusters": [],
        "orphan_images": [
            {"filename": "cs1.png"}, {"filename": "cs2.png"},
            {"filename": "cs3.png"},
        ],
    }])
    _rename_export(client, jid)
    out = Path(client.get(f"/api/jobs/{jid}/export-status").json()["result"]["output_path"])
    files = sorted(p.name for p in (out / "To_be_Cropped" / "Color_Swatch").iterdir())
    assert files == ["cs1.png", "cs2.png", "cs3.png"]


# ── Reject-leak fix (2026-06-11) ─────────────────────────────────────────────
#
# Pre-fix bug: _build_rename_plan_for_session checked rejection per cluster
# (`if role == "rejected": continue`) but not globally — so a buddy image with
# ImageRole rows (rejected in cluster A, buddy in cluster B) would skip
# cluster A's loop iteration but still get included via cluster B's loop. The
# rejected image then exported under cluster B's renamed basename, silently
# defeating the operator's reject. Legacy export was always safe — it uses
# _best_role_map which encodes "rejected wins across all of an image's
# cluster claims" (priority 0).
#
# Reference real-data case: session 79 (10-11 QC) in job 8 had images 7256,
# 7257, 7260 — each was rejected in cluster 364 but still buddy in cluster
# 363. _best_role_map resolved them to "rejected" (so legacy export would
# skip), but the rename plan emitted them as Player_363_006/007/010.png and
# would copy them into To_be_Cropped/10-11 QC/. Three rejected photos that
# the customer should never have seen.
#
# Fix: also check role_map.get(image_id) == "rejected" inside the cluster
# loop, in addition to the per-cluster role check. Reject anywhere = skipped
# everywhere, matching legacy behavior.


def test_rename_buddy_rejected_in_all_clusters_skipped(ctx):
    """Regression lock: buddy image rejected in ALL its clusters is skipped
    in rename mode (this already worked pre-fix; lock the invariant).

    Test mechanics: rename mode renames files away from their source
    names — `shared.jpg` becomes `Alice_NNN.jpg` or `Bob_NNN.jpg`. So we
    detect leaks by the per-cluster sequence COUNT, not by the string
    "shared". One leaked emission per cluster claim adds one
    `<Cluster>_NNN.jpg` entry."""
    client, SL, tmp_path = ctx
    jid, root = _rename_job(SL, tmp_path, team_specs=[{
        "name": "T", "clusters": [
            {"label": "Alice", "images": [
                {"filename": "alice.jpg", "role": "individual"},
                {"filename": "shared.jpg", "role": "rejected"},   # buddy rejected here
            ]},
            {"label": "Bob", "images": [
                {"filename": "bob.jpg", "role": "individual"},
                {"filename": "shared.jpg", "role": "rejected"},   # AND rejected here
            ]},
        ],
    }])
    _rename_export(client, jid)
    out = Path(client.get(f"/api/jobs/{jid}/export-status").json()["result"]["output_path"])
    files = sorted(p.name for p in (out / "To_be_Cropped" / "T").iterdir())
    # Expected: ONE individual per cluster, nothing else. The buddy is
    # rejected in both → emits zero copies.
    assert files == ["Alice_001.jpg", "Bob_001.jpg"], (
        f"rejected buddy leaked or unexpected file set: {files}"
    )


def test_rename_buddy_rejected_in_some_clusters_skipped(ctx):
    """THE FIX: buddy image rejected in cluster A and still 'buddy' in
    cluster B must NOT export in rename mode (legacy already skipped it
    via _best_role_map; rename now matches). Pre-fix this leaked as
    `<NonRejectedCluster>_002.jpg`.

    Mirrors the real-data leak found on session 79 / job 8 / images
    7256, 7257, 7260 — rejected in cluster 364, still buddy in cluster
    363, leaked as `Player_363_006/007/010.png`."""
    client, SL, tmp_path = ctx
    jid, root = _rename_job(SL, tmp_path, team_specs=[{
        "name": "T", "clusters": [
            {"label": "Alice", "images": [
                {"filename": "alice.jpg", "role": "individual"},
                {"filename": "shared.jpg", "role": "buddy"},      # still buddy here
            ]},
            {"label": "Bob", "images": [
                {"filename": "bob.jpg", "role": "individual"},
                {"filename": "shared.jpg", "role": "rejected"},   # rejected here
            ]},
        ],
    }])
    _rename_export(client, jid)
    out = Path(client.get(f"/api/jobs/{jid}/export-status").json()["result"]["output_path"])
    files = sorted(p.name for p in (out / "To_be_Cropped" / "T").iterdir())
    # Pre-fix: shared.jpg emitted as Alice_002.jpg via the cluster-Alice
    # loop (its per-cluster role is "buddy", not "rejected"), even though
    # _best_role_map globally resolves it to "rejected". Post-fix: skipped.
    # Expected: ONLY Alice_001.jpg + Bob_001.jpg.
    assert files == ["Alice_001.jpg", "Bob_001.jpg"], (
        f"rejected buddy LEAKED via non-rejected cluster's basename: {files}"
    )


def test_rename_buddy_not_rejected_exports_to_all_kids(ctx):
    """Don't over-correct: a non-rejected buddy image still exports under
    EACH cluster that claims it. The fix must skip rejected buddies
    without affecting non-rejected ones."""
    client, SL, tmp_path = ctx
    jid, root = _rename_job(SL, tmp_path, team_specs=[{
        "name": "T", "clusters": [
            {"label": "Alice", "images": [
                {"filename": "alice.jpg", "role": "individual"},
                {"filename": "shared.jpg", "role": "buddy"},      # buddy in both
            ]},
            {"label": "Bob", "images": [
                {"filename": "bob.jpg", "role": "individual"},
                {"filename": "shared.jpg", "role": "buddy"},      # buddy in both
            ]},
        ],
    }])
    _rename_export(client, jid)
    out = Path(client.get(f"/api/jobs/{jid}/export-status").json()["result"]["output_path"])
    files = sorted(p.name for p in (out / "To_be_Cropped" / "T").iterdir())
    # shared.jpg buddy in both clusters → exports TWICE, under each cluster's
    # renamed basename. Critical: the fix must not break this.
    # Alice: individual (001) + buddy shared (002). Bob: same shape.
    # Sequence ordering depends on capture_time — both rows have capture_time=None
    # so tiebreak is image_id (Alice's individual was created first).
    assert "Alice_001.jpg" in files
    assert "Alice_002.jpg" in files
    assert "Bob_001.jpg" in files
    assert "Bob_002.jpg" in files
    assert len(files) == 4, (
        f"non-rejected buddy should export twice (once per cluster): {files}"
    )


def test_rename_single_cluster_image_rejected_skipped(ctx):
    """Regression lock: a single-cluster image (no buddy expansion)
    rejected in its only cluster is skipped in rename mode. This worked
    pre-fix via the per-cluster check; lock it to ensure the new
    role_map check doesn't break the simple case either."""
    client, SL, tmp_path = ctx
    jid, root = _rename_job(SL, tmp_path, team_specs=[{
        "name": "T", "clusters": [
            {"label": "Alice", "images": [
                {"filename": "alice.jpg", "role": "individual"},
                {"filename": "rejected_solo.jpg", "role": "rejected"},
            ]},
        ],
    }])
    _rename_export(client, jid)
    out = Path(client.get(f"/api/jobs/{jid}/export-status").json()["result"]["output_path"])
    files = {p.name for p in (out / "To_be_Cropped" / "T").iterdir()}
    assert not any("rejected_solo" in f for f in files), (
        f"single-cluster rejected image leaked: {files}"
    )
    assert any("Alice" in f for f in files)
