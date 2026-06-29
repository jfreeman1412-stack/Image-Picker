"""Regression test for the list_clusters sort key (2026-06-09).

Pre-fix bug: `sorted(image_rows, key=lambda i: i.capture_time or i.filename)`
returned a mix of datetime and str across a cluster's images and raised
TypeError when the cluster contained BOTH images with a capture_time set
(EXIF DateTimeOriginal) AND images without one. The mixed-population case
surfaces in any merged session whose source folders disagree on EXIF
presence — e.g., the outdoor-shoot Phase 1 merge where PNGs from a
composite folder (converted from RAW, keep EXIF) and JPGs from the
naturals line (camera-direct, no DateTimeOriginal) end up in one session.

Fix: tuple sort-key `(i.capture_time or datetime.min, i.filename)` — the
datetime.min sentinel makes the first element uniformly comparable and
the filename is the tiebreaker.
"""
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.clusters import list_clusters
from app.db import Base
from app.models.db_models import (
    Cluster, Face, Image, Job, Session as DbSession,
)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'mixed-captime.db'}",
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


def _setup_cluster_with_mixed_capture_times(db):
    """One cluster, one image with capture_time set + one image with None.
    Pre-fix this combo crashed list_clusters with str-vs-datetime TypeError."""
    job = Job(name="J", root_path="/tmp", has_lines=0)
    db.add(job); db.commit(); db.refresh(job)
    sess = DbSession(job_id=job.id, name="T", source_path="/tmp/T",
                     status="done", created_at=datetime.utcnow())
    db.add(sess); db.commit(); db.refresh(sess)
    cluster = Cluster(session_id=sess.id, image_count=2)
    db.add(cluster); db.commit(); db.refresh(cluster)

    # Image 1: PNG from a composite folder — has EXIF capture_time.
    img1 = Image(
        session_id=sess.id, path="/tmp/T/composite.png",
        filename="composite.png",
        capture_time=datetime(2026, 1, 1, 12, 0, 0),
    )
    # Image 2: JPG from a naturals folder — no EXIF DateTimeOriginal.
    img2 = Image(
        session_id=sess.id, path="/tmp/T/naturals.jpg",
        filename="naturals.jpg",
        capture_time=None,
    )
    db.add_all([img1, img2]); db.commit()
    db.refresh(img1); db.refresh(img2)
    db.add_all([
        Face(image_id=img1.id, cluster_id=cluster.id, bbox="[0,0,10,10]",
             det_score=0.9, face_area_ratio=0.2),
        Face(image_id=img2.id, cluster_id=cluster.id, bbox="[0,0,10,10]",
             det_score=0.9, face_area_ratio=0.2),
        # 2026-06-29: Face-only fixture (no ImageRole rows) is a valid
        # pipeline state — buddy-only coaches occupy this shape in
        # production. list_clusters now derives image membership from
        # ImageRole UNION Face (clusters.py:86), so this fixture
        # exercises the Face-side of the union. The 2026-06-25 fixture
        # alignment that bolted ImageRole rows on was reverted when
        # the UNION fix landed.
    ])
    db.commit()
    return sess


def test_list_clusters_handles_mixed_capture_time(db):
    """A cluster mixing images with and without capture_time must not
    crash list_clusters. Pre-fix this raised
    TypeError: '<' not supported between instances of 'str' and 'datetime.datetime'."""
    sess = _setup_cluster_with_mixed_capture_times(db)
    out = list_clusters(sess.id, db)
    assert len(out) == 1
    images = out[0]["images"]
    assert len(images) == 2
    # Untimed image sorts first (datetime.min sentinel) — verify the order
    # is stable and predictable for the operator's review UI.
    assert images[0]["filename"] == "naturals.jpg"
    assert images[0]["capture_time"] is None
    assert images[1]["filename"] == "composite.png"
    assert images[1]["capture_time"] is not None
