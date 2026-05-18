"""Tests for flag-visibility settings + cluster-response filtering."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.settings import KNOWN_FLAGS, filter_visible_reasons, get_flag_visibility_map
from app.db import Base, get_db
from app.main import app


@pytest.fixture
def client(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'settings-test.db'}",
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

    app.dependency_overrides[get_db] = _override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_get_defaults_all_true(client):
    r = client.get("/api/settings/flag-visibility")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == set(KNOWN_FLAGS)
    assert all(v is True for v in body.values())


def test_post_hides_one_flag_others_stay_true(client):
    r = client.post("/api/settings/flag-visibility",
                     json={"team_pick_not_smiling": False})
    assert r.status_code == 200
    body = r.json()
    assert body["team_pick_not_smiling"] is False
    assert body["pano_pick_smiling"] is True
    # Persists across a fresh GET.
    body2 = client.get("/api/settings/flag-visibility").json()
    assert body2["team_pick_not_smiling"] is False


def test_post_ignores_unknown_flags(client):
    r = client.post("/api/settings/flag-visibility",
                     json={"not_a_real_flag": False})
    assert "not_a_real_flag" not in r.json()


def test_post_merges_not_replaces(client):
    client.post("/api/settings/flag-visibility", json={"outlier_high": False})
    client.post("/api/settings/flag-visibility", json={"outlier_low": False})
    body = client.get("/api/settings/flag-visibility").json()
    assert body["outlier_high"] is False
    assert body["outlier_low"] is False
    assert body["team_pick_not_smiling"] is True


# ── filter_visible_reasons unit ──────────────────────────────────────────────


def test_filter_visible_reasons_strips_hidden():
    vis = {f: True for f in KNOWN_FLAGS}
    vis["team_pick_not_smiling"] = False
    out = filter_visible_reasons(
        "team_pick_not_smiling,no_clean_pano_pose", vis
    )
    assert out == ["no_clean_pano_pose"]


def test_filter_visible_reasons_none_and_empty():
    vis = {f: True for f in KNOWN_FLAGS}
    assert filter_visible_reasons(None, vis) == []
    assert filter_visible_reasons("", vis) == []


def test_filter_unknown_reason_defaults_visible():
    """A reason not in the known list isn't hidden by accident."""
    out = filter_visible_reasons("some_future_flag", {})
    assert out == ["some_future_flag"]
