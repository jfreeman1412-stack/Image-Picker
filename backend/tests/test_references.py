"""Phase A.2 — ReferenceFace model, quality gate, load service, and API.

A reference photo + its 512-d InsightFace embedding, attached to the global
A.1 `Player`. Provenance is `captured_job_id` (nullable). See
PHASE_A2_REFERENCE_UPLOAD.md.
"""
import numpy as np
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models.db_models import Job, Player, ReferenceFace


@pytest.fixture
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'references-test.db'}",
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


def _player(db, norm_name="eleanorpederson", display_name="Eleanor-Pederson") -> Player:
    p = Player(norm_name=norm_name, display_name=display_name)
    db.add(p); db.commit(); db.refresh(p)
    return p


def _job(db, name="Shoot A") -> Job:
    job = Job(name=name, root_path="/tmp", has_lines=0)
    db.add(job); db.commit(); db.refresh(job)
    return job


# ── Section 1: model ──────────────────────────────────────────────────────

def test_table_created_and_embedding_round_trips(db):
    """create_all builds reference_faces; the 512-d embedding round-trips."""
    player = _player(db)
    job = _job(db)
    vec = np.random.rand(512).astype(np.float32)
    ref = ReferenceFace(
        player_id=player.id,
        captured_job_id=job.id,
        image_path="/tmp/ref.jpg",
        original_filename="ref.jpg",
        embedding=vec.tobytes(),
        det_score=0.91,
        bbox="[10, 20, 100, 120]",
        face_area_ratio=0.2,
    )
    db.add(ref); db.commit()

    stored = db.query(ReferenceFace).one()
    back = np.frombuffer(stored.embedding, dtype=np.float32)
    assert back.shape == (512,)
    assert np.allclose(back, vec)
    assert stored.player.display_name == "Eleanor-Pederson"
    assert player.references[0].id == stored.id


def test_captured_job_id_nullable(db):
    """Provenance is optional — a reference with no shoot context is valid."""
    player = _player(db)
    ref = ReferenceFace(
        player_id=player.id,
        captured_job_id=None,
        image_path="/tmp/ref.jpg",
        embedding=np.zeros(512, dtype=np.float32).tobytes(),
        det_score=0.8,
    )
    db.add(ref); db.commit()
    assert db.query(ReferenceFace).one().captured_job_id is None


def test_player_cascade_deletes_references(db):
    """Deleting a Player cascades to its ReferenceFace rows (ORM-level)."""
    player = _player(db)
    for _ in range(3):
        db.add(ReferenceFace(
            player_id=player.id,
            image_path="/tmp/r.jpg",
            embedding=np.zeros(512, dtype=np.float32).tobytes(),
            det_score=0.8,
        ))
    db.commit()
    assert db.query(ReferenceFace).count() == 3

    db.delete(player); db.commit()
    assert db.query(ReferenceFace).count() == 0
