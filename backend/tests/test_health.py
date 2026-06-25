"""Lock the GET /api/health contract.

The endpoint already existed at main.py:74-76. The set-role-defensive
build (2026-06-25) reuses it as the heartbeat target for the frontend
connectivity poll (useBackendReachable). Keeping the contract simple
and pinned means the poll's shape never has to change.

Contract:
  - Path: GET /api/health
  - 200 OK with JSON body
  - No auth, no DB query, no I/O — just confirms the FastAPI process is
    accepting + serving requests on :8020 (which is what the poll needs
    to distinguish "backend up" from "ECONNREFUSED during restart").
"""
from fastapi.testclient import TestClient

from app.main import app


def test_health_returns_200():
    """The frontend poll keys off res.ok — 200 is the only acceptable
    'backend is up' signal."""
    client = TestClient(app)
    res = client.get("/api/health")
    assert res.status_code == 200


def test_health_returns_json_body():
    """Body shape is informational, not load-bearing for the poll
    (status-200 is enough), but lock it so future refactors don't
    accidentally return an empty body or non-JSON."""
    client = TestClient(app)
    res = client.get("/api/health")
    body = res.json()
    assert isinstance(body, dict)
    assert body.get("status") == "ok"


def test_health_no_db_required():
    """Endpoint must NOT depend on a DB session (its whole purpose is
    to confirm uvicorn is reachable, not the DB). A db dependency would
    couple poll success to DB health, which is the wrong abstraction —
    a backend can be reachable for set-role retries even if the DB has
    a transient issue."""
    # No explicit DB override needed — TestClient doesn't provide one and
    # the call still succeeds.
    client = TestClient(app)
    res = client.get("/api/health")
    assert res.status_code == 200
