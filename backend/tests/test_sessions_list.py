"""Lock the GET /api/sessions endpoint's image_count correctness.

The endpoint was rewritten 2026-09-10 to use a single bulk GROUP BY on
the images table instead of per-session `len(s.images)` lazy loads
(N+1 that pushed the endpoint to 20+ seconds and timing out once the
DB reached ~900 sessions). This test asserts image_count still comes
back correct for each session and correctly returns 0 for sessions
that have no image rows.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import db as db_module   # noqa: F401  (kept for parity with test_jobs)
from app.api import jobs as jobs_module
from app.db import Base, get_db
from app.main import app
from app.models.db_models import Image, Session as SessionModel


@pytest.fixture
def client(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'sessions-list-test.db'}",
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

    monkeypatch.setattr(jobs_module, "SessionLocal", TestingSessionLocal)
    app.dependency_overrides[get_db] = _override_get_db
    try:
        yield TestClient(app), TestingSessionLocal
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _seed(SessionLocal, sessions_and_counts):
    """Create sessions with the given (name, image_count) tuples.
    Returns a dict {name: session_id}."""
    db = SessionLocal()
    try:
        out = {}
        for name, count in sessions_and_counts:
            s = SessionModel(
                name=name, status="done", source_path=f"/tmp/{name}",
            )
            db.add(s); db.commit(); db.refresh(s)
            for i in range(count):
                db.add(Image(
                    session_id=s.id,
                    path=f"/tmp/{name}/{i}.jpg",
                    filename=f"{i}.jpg",
                ))
            db.commit()
            out[name] = s.id
        return out
    finally:
        db.close()


def test_list_sessions_reports_correct_image_counts(client):
    tc, SessionLocal = client
    ids = _seed(SessionLocal, [
        ("empty", 0),
        ("small", 3),
        ("medium", 25),
        ("big", 100),
    ])

    res = tc.get("/api/sessions")
    assert res.status_code == 200
    body = res.json()

    counts_by_name = {row["name"]: row["image_count"] for row in body}
    assert counts_by_name["empty"] == 0
    assert counts_by_name["small"] == 3
    assert counts_by_name["medium"] == 25
    assert counts_by_name["big"] == 100


def test_list_sessions_empty_db(client):
    """Zero sessions in DB → empty list, no crash from the GROUP BY on
    an empty images table."""
    tc, _ = client
    res = tc.get("/api/sessions")
    assert res.status_code == 200
    assert res.json() == []


def test_list_sessions_legacy_only_filter_still_works(client):
    """The legacy_only=True filter (job_id IS NULL) must still exclude
    job-bound sessions. This test guards against the refactor
    accidentally dropping the .filter() clause."""
    tc, SessionLocal = client
    _seed(SessionLocal, [
        ("legacy_a", 5),  # job_id NULL by default in _seed
        ("legacy_b", 7),
    ])
    # Mark one as job-bound.
    db = SessionLocal()
    try:
        from app.models.db_models import Job
        j = Job(name="attached-job", root_path="/tmp/x")
        db.add(j); db.commit(); db.refresh(j)
        s = SessionModel(
            name="job_bound", status="done", source_path="/tmp/j",
            job_id=j.id,
        )
        db.add(s); db.commit()
    finally:
        db.close()

    body = tc.get("/api/sessions", params={"legacy_only": True}).json()
    names = {row["name"] for row in body}
    assert names == {"legacy_a", "legacy_b"}
    assert "job_bound" not in names


def test_list_sessions_bulk_query_matches_lazy_load_for_arbitrary_shapes(client):
    """The bulk GROUP BY must return identical counts to what the ORM's
    lazy-loaded len(s.images) would have. This test seeds a mixed batch
    and cross-checks each session's count against the actual Image row
    count for that session_id."""
    tc, SessionLocal = client
    _seed(SessionLocal, [
        ("a", 1),
        ("b", 2),
        ("c", 0),
        ("d", 50),
        ("e", 0),
    ])

    body = tc.get("/api/sessions").json()

    # Cross-check every returned image_count against a fresh direct query.
    db = SessionLocal()
    try:
        for row in body:
            true_count = db.query(Image).filter_by(session_id=row["id"]).count()
            assert row["image_count"] == true_count, (
                f"session {row['id']} ({row['name']!r}): endpoint returned "
                f"image_count={row['image_count']}, actual={true_count}"
            )
    finally:
        db.close()
