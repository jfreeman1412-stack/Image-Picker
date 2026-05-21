"""Phase A.3 — matching service: cosine, tiered thresholds, margin rule, and
the debug API. Synthetic unit vectors with known cosine; no model, no mocking.
See PHASE_A3_MATCHING_SERVICE.md.
"""
import math

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base, get_db
from app.main import app
from app.models.db_models import Face, Image, Player, ReferenceFace
from app.services import matching


# ── synthetic unit-vector helpers (2-D plane embedded in 512-D) ───────────
# QUERY = e0. A reference r(sim) = [sim, sqrt(1-sim^2), 0, ...] is a unit vector
# whose cosine with QUERY equals exactly `sim`.

def _vec(*coords) -> np.ndarray:
    v = np.zeros(512, dtype=np.float32)
    for i, c in enumerate(coords):
        v[i] = c
    return v


def _ref_at(sim: float) -> np.ndarray:
    """A 512-d unit vector whose cosine with QUERY is `sim`."""
    return _vec(sim, math.sqrt(max(0.0, 1.0 - sim * sim)))


QUERY = _vec(1.0)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'matching-test.db'}",
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


def _player_with_refs(db, name, sims) -> Player:
    p = Player(norm_name=name.lower().replace("-", ""), display_name=name)
    db.add(p); db.flush()
    for s in sims:
        db.add(ReferenceFace(
            player_id=p.id, image_path="/tmp/x.jpg",
            embedding=_ref_at(s).tobytes(), det_score=0.9,
        ))
    db.commit()
    return p


# ── cosine_similarity (pure) ──────────────────────────────────────────────

def test_cosine_identical_is_one():
    v = _ref_at(0.7)
    assert matching.cosine_similarity(v, v) == pytest.approx(1.0, abs=1e-5)


def test_cosine_orthogonal_is_zero():
    assert matching.cosine_similarity(_vec(1.0), _vec(0.0, 1.0)) == \
        pytest.approx(0.0, abs=1e-6)


def test_cosine_known_value():
    assert matching.cosine_similarity(QUERY, _ref_at(0.7)) == \
        pytest.approx(0.7, abs=1e-4)


def test_cosine_zero_vector_guarded():
    assert matching.cosine_similarity(_vec(0.0), _vec(1.0)) == 0.0


# ── match_embedding: tiering / margin / aggregation ──────────────────────

def test_no_references_returns_none(db):
    res = matching.match_embedding(db, QUERY)
    assert res["tier"] == "none"
    assert res["reason"] == "no_match"
    assert res["player_id"] is None
    assert res["score"] is None
    assert res["candidates"] == []


def test_clean_high(db):
    alice = _player_with_refs(db, "Alice", [1.0])
    _player_with_refs(db, "Bob", [0.1])
    res = matching.match_embedding(db, QUERY)
    assert res["tier"] == "high"
    assert res["reason"] == "auto_label"
    assert res["needs_review"] is False
    assert res["player_id"] == alice.id
    assert res["player_name"] == "Alice"
    # candidates echo the ranked players (Alice on top).
    assert res["candidates"][0]["player_id"] == alice.id
    assert alice.id in {c["player_id"] for c in res["candidates"]}


def test_high_with_distant_runner_up_not_downgraded(db):
    alice = _player_with_refs(db, "Alice", [0.7])
    _player_with_refs(db, "Bob", [0.3])  # below HIGH → margin rule inert
    res = matching.match_embedding(db, QUERY)
    assert res["tier"] == "high"
    assert res["player_id"] == alice.id


def test_ambiguous_margin_downgrade(db):
    alice = _player_with_refs(db, "Alice", [0.70])
    bob = _player_with_refs(db, "Bob", [0.69])  # both >= HIGH, gap 0.01 < 0.05
    res = matching.match_embedding(db, QUERY)
    assert res["tier"] == "low"
    assert res["reason"] == "ambiguous_margin"
    assert res["needs_review"] is True
    assert res["player_id"] == alice.id          # still the suggestion
    assert res["runner_up"]["player_id"] == bob.id


def test_margin_exactly_min_stays_high(db):
    alice = _player_with_refs(db, "Alice", [0.70])
    _player_with_refs(db, "Bob", [0.65])         # gap exactly 0.05 → high
    res = matching.match_embedding(db, QUERY)
    assert res["tier"] == "high"
    assert res["player_id"] == alice.id


def test_margin_just_below_min_downgrades(db):
    _player_with_refs(db, "Alice", [0.70])
    _player_with_refs(db, "Bob", [0.66])         # gap 0.04 < 0.05 → low
    res = matching.match_embedding(db, QUERY)
    assert res["tier"] == "low"
    assert res["reason"] == "ambiguous_margin"


def test_low_confidence_tier(db):
    alice = _player_with_refs(db, "Alice", [0.5])  # >= LOW, < HIGH
    res = matching.match_embedding(db, QUERY)
    assert res["tier"] == "low"
    assert res["reason"] == "low_confidence"
    assert res["needs_review"] is True
    assert res["player_id"] == alice.id


def test_no_match_tier_reports_best_score(db):
    _player_with_refs(db, "Alice", [0.3])          # < LOW
    res = matching.match_embedding(db, QUERY)
    assert res["tier"] == "none"
    assert res["player_id"] is None
    assert res["score"] == pytest.approx(0.3, abs=1e-3)  # best still reported


def test_high_threshold_boundary(db):
    alice = _player_with_refs(db, "Alice", [0.6])  # exactly HIGH
    res = matching.match_embedding(db, QUERY)
    assert res["tier"] == "high"
    assert res["player_id"] == alice.id


def test_low_threshold_boundary(db):
    alice = _player_with_refs(db, "Alice", [0.4])  # exactly LOW
    res = matching.match_embedding(db, QUERY)
    assert res["tier"] == "low"
    assert res["reason"] == "low_confidence"
    assert res["player_id"] == alice.id


def test_max_aggregation_uses_best_reference(db):
    alice = _player_with_refs(db, "Alice", [0.5, 0.7])  # MAX = 0.7
    res = matching.match_embedding(db, QUERY)
    assert res["score"] == pytest.approx(0.7, abs=1e-3)
    assert res["tier"] == "high"
    assert res["player_id"] == alice.id


def test_same_player_top_two_not_ambiguous(db):
    alice = _player_with_refs(db, "Alice", [0.70, 0.69])  # both A's own refs
    _player_with_refs(db, "Bob", [0.30])                  # runner player far
    res = matching.match_embedding(db, QUERY)
    assert res["tier"] == "high"
    assert res["reason"] == "auto_label"
    assert res["player_id"] == alice.id


# ── Section 2: debug API ──────────────────────────────────────────────────

@pytest.fixture
def client(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'matching-http.db'}",
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
        seed = TestingSessionLocal()
        try:
            alice = Player(norm_name="alice", display_name="Alice")
            seed.add(alice); seed.flush()
            seed.add(ReferenceFace(
                player_id=alice.id, image_path="/tmp/a.jpg",
                embedding=_ref_at(1.0).tobytes(), det_score=0.9))
            img = Image(path="/tmp/face.jpg", filename="face.jpg")
            seed.add(img); seed.flush()
            match_face = Face(image_id=img.id, bbox="[0,0,10,10]",
                              embedding=_ref_at(1.0).tobytes(), det_score=0.9)
            far_face = Face(image_id=img.id, bbox="[0,0,10,10]",
                            embedding=_ref_at(0.1).tobytes(), det_score=0.9)
            seed.add_all([match_face, far_face]); seed.commit()
            c_alice_id = alice.id
            c_match_face_id = match_face.id
            c_far_face_id = far_face.id
        finally:
            seed.close()
        c = TestClient(app)
        c.alice_id = c_alice_id
        c.match_face_id = c_match_face_id
        c.far_face_id = c_far_face_id
        yield c
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_http_match_face_high(client):
    res = client.get(f"/api/matching/face/{client.match_face_id}")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["tier"] == "high"
    assert body["player_id"] == client.alice_id
    assert body["thresholds"]["high"] == 0.6


def test_http_match_face_none(client):
    res = client.get(f"/api/matching/face/{client.far_face_id}")
    assert res.status_code == 200
    assert res.json()["tier"] == "none"


def test_http_unknown_face_404(client):
    res = client.get("/api/matching/face/99999")
    assert res.status_code == 404
