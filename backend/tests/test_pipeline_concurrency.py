"""Fix-1 tests for the 2026-09-10 pipeline concurrency wedge.

Central claim being tested: `run_pipeline` acquires `_PIPELINE_LOCK` at
entry, so N concurrent triggers serialize instead of deadlocking on the
shared FER (TensorFlow) and InsightFace (ONNX) model instances the way
job 114's real run-all wave did.

The tests fully mock the heavy inference calls (detect_faces,
classify_expression, match_session_clusters) so no models load and no
GPU is touched. Clustering (numpy/sklearn DBSCAN), coach detection,
labeling, sorting, and outlier flagging all run for real — they're
cheap Python + SQLAlchemy work that also lives inside the lock and we
want them exercised in the concurrent-run scenario.
"""
import threading
import time
from pathlib import Path

import numpy as np
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models.db_models import Session as SessionModel, Image
from app.services import face_pipeline
from app.services.face_pipeline import run_pipeline, _PIPELINE_LOCK


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def test_engine(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'pipeline-lock-test.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    return engine


@pytest.fixture
def SessionLocal(test_engine):
    return sessionmaker(autocommit=False, autoflush=False, bind=test_engine)


def _seed_session(SessionLocal, name: str, n_images: int) -> int:
    """Create one Session row + n_images Image rows. Returns session id."""
    db = SessionLocal()
    try:
        s = SessionModel(name=name, status="pending",
                         source_path=f"/tmp/{name}")
        db.add(s); db.commit(); db.refresh(s)
        for i in range(n_images):
            db.add(Image(
                session_id=s.id,
                path=f"/tmp/{name}/{name}_{i}.jpg",
                filename=f"{name}_{i}.jpg",
            ))
        db.commit()
        return s.id
    finally:
        db.close()


# ── Mock helpers ────────────────────────────────────────────────────────


def _install_mocks(monkeypatch, *, on_detect=None):
    """Patch face_detector.detect_faces, expression.classify_expression, and
    match_session_clusters with lightweight fakes so no real models load.

    If `on_detect` is passed, it's called at the start of every detect_faces
    invocation — the serialization test uses this to observe concurrent
    entries via a shared counter. Otherwise detect_faces just returns a
    single face per image with a unit embedding whose direction varies
    per call so DBSCAN doesn't collapse everything into one cluster.
    """
    call_seed = [0]

    def _detect(image_path):
        if on_detect is not None:
            on_detect(image_path)
        # Unique-ish embedding per call — one-hot in a rotating slot.
        idx = call_seed[0] % 512
        call_seed[0] += 1
        emb = np.zeros(512, dtype=np.float32)
        emb[idx] = 1.0
        return [{
            "bbox": [0, 0, 32, 32],
            "embedding": emb,
            "det_score": 0.9,
            "age": 15.0,
            "yaw": 0.0,
            "pitch": 0.0,
            "face_area_ratio": 0.10,
            "smile_score": 0.5,
        }]

    monkeypatch.setattr(face_pipeline.face_detector, "detect_faces", _detect)
    monkeypatch.setattr(
        face_pipeline.expression, "classify_expression",
        lambda image_path, bbox: ("smiling", 0.9),
    )
    monkeypatch.setattr(
        face_pipeline, "match_session_clusters",
        lambda db, session: None,
    )


# ── Tests ───────────────────────────────────────────────────────────────


def test_run_pipeline_serializes_concurrent_calls(SessionLocal, monkeypatch):
    """The load-bearing Fix-1 test.

    N=6 threads race to `run_pipeline`. The detect_faces mock tracks how
    many callers are inside the pipeline's critical section at once. If
    the lock is present and correctly wraps the body, max_concurrent
    stays at 1. If it's missing or bypassed (e.g. someone refactors
    `run_pipeline` out from under the `with` block), max_concurrent
    reaches N and the assertion fails — which is exactly what we want a
    regression to look like.

    This is the direct simulation of the 2026-09-10 job-114 wave: multiple
    concurrent triggers hitting run_pipeline at once. Under the old
    (unserialized) code the shared FER model would deadlock; under the
    fix every call queues on the lock and completes to done.
    """
    N = 6
    session_ids = [_seed_session(SessionLocal, f"s{i}", 3) for i in range(N)]

    active_counter = [0]
    max_active = [0]
    active_lock = threading.Lock()

    def _observe_entry(_image_path):
        with active_lock:
            active_counter[0] += 1
            if active_counter[0] > max_active[0]:
                max_active[0] = active_counter[0]
        # Small sleep to guarantee that if the lock is missing, other
        # threads reliably pile up here and bump max_active > 1. Without
        # this, a fast enough machine could serialize by accident.
        time.sleep(0.05)
        with active_lock:
            active_counter[0] -= 1

    _install_mocks(monkeypatch, on_detect=_observe_entry)

    errors: list[BaseException] = []

    def worker(sid: int) -> None:
        db = SessionLocal()
        try:
            run_pipeline(db, sid)
        except BaseException as exc:  # noqa: BLE001 — test infra
            errors.append(exc)
        finally:
            db.close()

    threads = [threading.Thread(target=worker, args=(sid,))
               for sid in session_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)   # generous — even N pipelines shouldn't need this

    # Fail loud if any thread didn't join in time.
    still_alive = [t for t in threads if t.is_alive()]
    assert not still_alive, f"threads did not complete: {still_alive}"

    assert not errors, f"pipeline errors: {errors!r}"

    assert max_active[0] == 1, (
        f"Expected max_concurrent == 1 (lock should serialize), got "
        f"max_concurrent = {max_active[0]}. The pipeline is NOT serialized "
        f"— under real load, concurrent triggers would deadlock the shared "
        f"FER/InsightFace models the way job 114 did on 2026-09-10."
    )

    # Every session must have completed to done.
    db = SessionLocal()
    try:
        for sid in session_ids:
            s = db.query(SessionModel).get(sid)
            assert s.status == "done", (
                f"session {sid} status={s.status!r} — expected 'done'"
            )
    finally:
        db.close()


def test_exception_in_pipeline_releases_lock(SessionLocal, monkeypatch):
    """If run_pipeline raises, the `with _PIPELINE_LOCK` block must still
    release. Otherwise one broken pipeline (say, a corrupted image that
    detect_faces refuses) would permanently lock out every future run —
    a strictly worse failure mode than the wedge we're fixing.

    Two-phase test: (1) trigger a raise inside the pipeline, confirm the
    lock is unheld afterward; (2) run a normal pipeline call and confirm
    it proceeds to done, proving the lock is genuinely free.
    """
    sid_bad = _seed_session(SessionLocal, "bad_session", 2)
    sid_good = _seed_session(SessionLocal, "good_session", 2)

    call_seed = [0]

    def _detect(image_path):
        if "bad_session" in str(image_path):
            raise RuntimeError("simulated detection failure")
        idx = call_seed[0] % 512
        call_seed[0] += 1
        emb = np.zeros(512, dtype=np.float32)
        emb[idx] = 1.0
        return [{
            "bbox": [0, 0, 32, 32], "embedding": emb, "det_score": 0.9,
            "age": None, "yaw": None, "pitch": None,
            "face_area_ratio": None, "smile_score": None,
        }]

    monkeypatch.setattr(face_pipeline.face_detector, "detect_faces", _detect)
    monkeypatch.setattr(
        face_pipeline.expression, "classify_expression",
        lambda image_path, bbox: ("smiling", 0.9),
    )
    monkeypatch.setattr(
        face_pipeline, "match_session_clusters",
        lambda db, session: None,
    )

    # Phase 1: bad session should raise; run_pipeline's inner try/except
    # marks status='error' then re-raises. The `with _PIPELINE_LOCK`
    # context manager releases on the way out regardless.
    db = SessionLocal()
    try:
        with pytest.raises(RuntimeError, match="simulated detection failure"):
            run_pipeline(db, sid_bad)
    finally:
        db.close()

    assert not _PIPELINE_LOCK.locked(), (
        "lock was NOT released after the pipeline raised — one broken run "
        "would permanently lock out all future pipelines"
    )

    # Phase 2: subsequent good session must complete cleanly.
    db = SessionLocal()
    try:
        run_pipeline(db, sid_good)
        s = db.query(SessionModel).get(sid_good)
        assert s.status == "done"
    finally:
        db.close()


def test_solo_run_pipeline_still_completes(SessionLocal, monkeypatch):
    """Regression baseline: a single-threaded, uncontended pipeline call
    completes exactly as it did pre-fix. Also serves as a canary that
    the mock set doesn't silently break the pipeline for the concurrent
    test above — if this fails, the concurrent-test's assertions might
    be reading a false 'done' state that never actually happened.
    """
    sid = _seed_session(SessionLocal, "solo", 4)
    _install_mocks(monkeypatch)

    db = SessionLocal()
    try:
        run_pipeline(db, sid)
        s = db.query(SessionModel).get(sid)
        assert s.status == "done"
        assert s.progress_stage is None
        # pipeline_finished_at should be set on success.
        assert s.pipeline_finished_at is not None
    finally:
        db.close()


def test_pipeline_lock_is_a_real_threading_lock():
    """Sanity check that _PIPELINE_LOCK exists and is a threading primitive
    — a cheap smoke test that catches any refactor that accidentally
    replaces it with something else (an `asyncio.Lock`, a dummy, etc.)."""
    assert isinstance(_PIPELINE_LOCK, type(threading.Lock())), (
        f"_PIPELINE_LOCK is {type(_PIPELINE_LOCK).__name__}, not a "
        f"threading.Lock — BackgroundTasks run in real threads, an "
        f"asyncio.Lock would silently fail to serialize them."
    )
