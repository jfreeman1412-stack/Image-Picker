"""Tests for the job export endpoint.

Builds an isolated SQLite DB + temp filesystem per test so the real dev DB is
untouched. We call export_job() directly with a real DbSession instead of
going through the HTTP layer — quicker and easier to assert against.
"""
from datetime import datetime
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.jobs import ExportJobRequest, _safe_dirname, _unique_path, export_job
from app.db import Base
from app.models.db_models import (
    Cluster, Image, ImageRole, Job, Session as DbSessionModel,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'export-test.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _make_image_file(path: Path, content: bytes = b"\xff\xd8\xff\xe0fake_jpg"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _build_job(
    db, tmp_path, *, job_name="Test Job", team_specs: list[dict],
    job_status_override=None,
):
    """Create a Job + Session per team_spec + Image rows + ImageRole rows.

    team_specs = [
        {
            "name": "Team A",
            "status": "done",
            "images": [{"filename": "f.jpg", "role": "team", "content": optional}],
        },
        ...
    ]
    Filesystem layout: tmp_path / "<job_name>" / "<team name>" / <files>.
    Returns (job, root_path).
    """
    root_path = tmp_path / job_name
    root_path.mkdir(parents=True, exist_ok=True)

    job = Job(
        name=job_name,
        root_path=str(root_path.resolve()),
        has_lines=0,
        image_subfolder_name=None,
        created_at=datetime.utcnow(),
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    for spec in team_specs:
        team_dir = root_path / spec["name"]
        team_dir.mkdir(parents=True, exist_ok=True)

        sess = DbSessionModel(
            job_id=job.id,
            name=spec["name"],
            source_path=str(team_dir.resolve()),
            status=spec.get("status", "done"),
            created_at=datetime.utcnow(),
        )
        db.add(sess)
        db.commit()
        db.refresh(sess)

        # Sessions need a cluster for ImageRole rows to point at, even though
        # the export only looks at ImageRole.role + Image.path.
        cluster = Cluster(session_id=sess.id, image_count=0)
        db.add(cluster)
        db.commit()
        db.refresh(cluster)

        for img_spec in spec["images"]:
            file_path = team_dir / img_spec["filename"]
            _make_image_file(file_path, content=img_spec.get("content", b"jpgbytes"))
            image_row = Image(
                session_id=sess.id,
                path=str(file_path.resolve()),
                filename=img_spec["filename"],
            )
            db.add(image_row)
            db.commit()
            db.refresh(image_row)

            if img_spec.get("role"):
                db.add(ImageRole(
                    image_id=image_row.id,
                    cluster_id=cluster.id,
                    role=img_spec["role"],
                ))
        db.commit()

    return job, root_path


# ── Helper unit tests ─────────────────────────────────────────────────────────


def test_safe_dirname_strips_invalid_chars():
    assert _safe_dirname('11a/Eagles?') == "11aEagles"
    assert _safe_dirname("Team A") == "Team A"
    assert _safe_dirname("   ") == "unnamed"
    assert _safe_dirname("..") == "unnamed"


def test_unique_path_appends_suffix(tmp_path):
    target = tmp_path / "img.jpg"
    target.write_bytes(b"a")
    second = _unique_path(target)
    assert second.name == "img_2.jpg"
    second.write_bytes(b"b")
    third = _unique_path(target)
    assert third.name == "img_3.jpg"


def test_unique_path_when_target_free(tmp_path):
    target = tmp_path / "free.jpg"
    assert _unique_path(target) == target


# ── Export behavior ───────────────────────────────────────────────────────────


def test_copy_mode_two_teams_mixed_roles(db, tmp_path):
    job, root = _build_job(db, tmp_path, team_specs=[
        {
            "name": "Team A",
            "images": [
                {"filename": "ind1.jpg", "role": "individual"},
                {"filename": "ind2.jpg", "role": "individual"},
                {"filename": "buddy.jpg", "role": "buddy"},
                {"filename": "pano.jpg", "role": "panoramic"},
                {"filename": "team.jpg", "role": "team"},
            ],
        },
        {
            "name": "Team B",
            "images": [
                {"filename": "x.jpg", "role": "individual"},
                {"filename": "y.jpg", "role": "team"},
                {"filename": "z.jpg", "role": "panoramic"},
                {"filename": "w.jpg", "role": "rejected"},
                {"filename": "v.jpg", "role": "buddy"},
            ],
        },
    ])

    result = export_job(job.id, ExportJobRequest(mode="copy", overwrite=True), db)

    out = Path(result["output_path"])
    assert out.exists()
    assert out.name == f"{root.name}_sorted"

    # To_be_Cropped holds every non-rejected image (5 + 4 = 9)
    website_a = list((out / "To_be_Cropped" / "Team A").iterdir())
    website_b = list((out / "To_be_Cropped" / "Team B").iterdir())
    assert {p.name for p in website_a} == {"ind1.jpg", "ind2.jpg", "buddy.jpg", "pano.jpg", "team.jpg"}
    assert {p.name for p in website_b} == {"x.jpg", "y.jpg", "z.jpg", "v.jpg"}

    # Team Images holds only team-role images
    assert {p.name for p in (out / "Team Images" / "Team A").iterdir()} == {"team.jpg"}
    assert {p.name for p in (out / "Team Images" / "Team B").iterdir()} == {"y.jpg"}

    # Pano Images holds only panoramic-role images
    assert {p.name for p in (out / "Pano Images" / "Team A").iterdir()} == {"pano.jpg"}
    assert {p.name for p in (out / "Pano Images" / "Team B").iterdir()} == {"z.jpg"}

    # Stats
    assert result["team_count"] == 2
    assert result["files_copied"] == 9
    assert result["files_skipped_rejected"] == 1

    # Copy mode: originals still exist
    for f in ["ind1.jpg", "ind2.jpg", "buddy.jpg", "pano.jpg", "team.jpg"]:
        assert (root / "Team A" / f).exists()


def test_rejected_images_never_appear_anywhere(db, tmp_path):
    job, root = _build_job(db, tmp_path, team_specs=[{
        "name": "T",
        "images": [
            {"filename": "keep.jpg", "role": "individual"},
            {"filename": "drop.jpg", "role": "rejected"},
        ],
    }])

    result = export_job(job.id, ExportJobRequest(mode="copy", overwrite=True), db)
    out = Path(result["output_path"])

    for subdir in ["To_be_Cropped/T", "Team Images/T", "Pano Images/T"]:
        files = {p.name for p in (out / subdir).iterdir()} if (out / subdir).exists() else set()
        assert "drop.jpg" not in files

    assert result["files_skipped_rejected"] == 1


def test_move_mode_removes_originals_keeps_raws(db, tmp_path):
    job, root = _build_job(db, tmp_path, team_specs=[{
        "name": "T",
        "images": [{"filename": "shot.jpg", "role": "team"}],
    }])
    # Drop a raw file in the same team folder. Move shouldn't touch it.
    raw = root / "T" / "shot.cr3"
    raw.write_bytes(b"raw")

    export_job(job.id, ExportJobRequest(mode="move", overwrite=True), db)

    assert not (root / "T" / "shot.jpg").exists(), "JPG should be moved out"
    assert raw.exists(), "Raw file should be untouched"


def test_team_image_appears_in_website_and_team_dir(db, tmp_path):
    """The team-role image must exist in BOTH To_be_Cropped and Team Images (even in move mode)."""
    job, root = _build_job(db, tmp_path, team_specs=[{
        "name": "T",
        "images": [{"filename": "team.jpg", "role": "team"}],
    }])

    result = export_job(job.id, ExportJobRequest(mode="copy", overwrite=True), db)
    out = Path(result["output_path"])
    assert (out / "To_be_Cropped" / "T" / "team.jpg").exists()
    assert (out / "Team Images" / "T" / "team.jpg").exists()


def test_pano_image_appears_in_website_and_pano_dir(db, tmp_path):
    job, root = _build_job(db, tmp_path, team_specs=[{
        "name": "T",
        "images": [{"filename": "p.jpg", "role": "panoramic"}],
    }])

    result = export_job(job.id, ExportJobRequest(mode="copy", overwrite=True), db)
    out = Path(result["output_path"])
    assert (out / "To_be_Cropped" / "T" / "p.jpg").exists()
    assert (out / "Pano Images" / "T" / "p.jpg").exists()


def test_naming_collision_appends_suffix(db, tmp_path):
    """Two images in the same team/role with the same filename → second gets _2."""
    job, root = _build_job(db, tmp_path, team_specs=[{"name": "T", "images": []}])

    # Manually create two source files with the same name in different folders.
    sub_a = root / "T" / "subA"
    sub_b = root / "T" / "subB"
    sub_a.mkdir(parents=True)
    sub_b.mkdir(parents=True)
    a = sub_a / "same.jpg"
    b = sub_b / "same.jpg"
    a.write_bytes(b"A")
    b.write_bytes(b"B")

    sess = db.query(DbSessionModel).filter_by(job_id=job.id).first()
    cluster = db.query(Cluster).filter_by(session_id=sess.id).first()
    for src in (a, b):
        img = Image(session_id=sess.id, path=str(src.resolve()), filename=src.name)
        db.add(img)
        db.commit()
        db.refresh(img)
        db.add(ImageRole(image_id=img.id, cluster_id=cluster.id, role="individual"))
    db.commit()

    result = export_job(job.id, ExportJobRequest(mode="copy", overwrite=True), db)
    out = Path(result["output_path"])
    names = sorted(p.name for p in (out / "To_be_Cropped" / "T").iterdir())
    assert names == ["same.jpg", "same_2.jpg"]


def test_skips_sessions_not_done(db, tmp_path):
    job, root = _build_job(db, tmp_path, team_specs=[
        {
            "name": "Done", "status": "done",
            "images": [{"filename": "ok.jpg", "role": "individual"}],
        },
        {
            "name": "Running", "status": "running",
            "images": [{"filename": "wip.jpg", "role": "individual"}],
        },
    ])

    result = export_job(job.id, ExportJobRequest(mode="copy", overwrite=True), db)
    out = Path(result["output_path"])

    assert (out / "To_be_Cropped" / "Done").exists()
    assert not (out / "To_be_Cropped" / "Running").exists()
    assert result["team_count"] == 1
    assert {s["name"] for s in result["sessions_skipped"]} == {"Running"}
    assert result["sessions_skipped"][0]["reason"] == "pipeline_not_complete"


def test_overwrite_true_replaces_existing_dir(db, tmp_path):
    job, root = _build_job(db, tmp_path, team_specs=[{
        "name": "T",
        "images": [{"filename": "a.jpg", "role": "individual"}],
    }])
    out_root = root.parent / f"{root.name}_sorted"

    # Pre-create with stale content.
    out_root.mkdir(parents=True)
    stale = out_root / "leftover.txt"
    stale.write_text("old")

    export_job(job.id, ExportJobRequest(mode="copy", overwrite=True), db)

    assert not stale.exists(), "overwrite=True should wipe the output dir first"
    assert (out_root / "To_be_Cropped" / "T" / "a.jpg").exists()


def test_overwrite_false_with_existing_returns_409(db, tmp_path):
    job, root = _build_job(db, tmp_path, team_specs=[{
        "name": "T",
        "images": [{"filename": "a.jpg", "role": "individual"}],
    }])
    out_root = root.parent / f"{root.name}_sorted"
    out_root.mkdir(parents=True)

    with pytest.raises(HTTPException) as exc:
        export_job(job.id, ExportJobRequest(mode="copy", overwrite=False), db)
    assert exc.value.status_code == 409
