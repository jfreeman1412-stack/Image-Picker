"""Phase A.2 — ReferenceFace model, quality gate, load service, and API.

A reference photo + its 512-d InsightFace embedding, attached to the global
A.1 `Player`. Provenance is `captured_job_id` (nullable). See
PHASE_A2_REFERENCE_UPLOAD.md.
"""
from pathlib import Path

import numpy as np
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base, get_db
from app.main import app
from app.models.db_models import Job, Player, ReferenceFace
from app.services import references


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


# ── Section 2: helpers (fake detector / detection dicts) ──────────────────

def _face(det_score=0.9, area=0.2, embedding=None) -> dict:
    """A detection dict shaped like face_detector.detect_faces output."""
    if embedding is None:
        embedding = np.arange(512, dtype=np.float32)
    return {
        "bbox": [10, 20, 100, 120],
        "embedding": embedding,
        "det_score": det_score,
        "age": 30.0,
        "yaw": 0.0,
        "pitch": 0.0,
        "face_area_ratio": area,
    }


def _fake_detector(faces):
    def _detect(_path):
        return list(faces)
    return _detect


def _mock(monkeypatch, tmp_path, faces):
    """Point REFERENCES_DIR at tmp_path and stub the detector to `faces`."""
    monkeypatch.setattr(references, "REFERENCES_DIR", tmp_path)
    monkeypatch.setattr(references.face_detector, "detect_faces",
                        _fake_detector(faces))


# ── Section 2: pure quality gate (no mocking) ─────────────────────────────

def test_gate_no_face():
    with pytest.raises(references.ReferenceQualityError) as e:
        references.evaluate_reference_quality([])
    assert e.value.code == "no_face"


def test_gate_multiple_faces():
    with pytest.raises(references.ReferenceQualityError) as e:
        references.evaluate_reference_quality([_face(), _face()])
    assert e.value.code == "multiple_faces"


def test_gate_low_confidence():
    with pytest.raises(references.ReferenceQualityError) as e:
        references.evaluate_reference_quality([_face(det_score=0.55)])
    assert e.value.code == "low_confidence"


def test_gate_face_too_small():
    with pytest.raises(references.ReferenceQualityError) as e:
        references.evaluate_reference_quality([_face(det_score=0.9, area=0.005)])
    assert e.value.code == "face_too_small"


def test_gate_good_face_returned():
    f = _face(det_score=0.9, area=0.2)
    assert references.evaluate_reference_quality([f]) is f


# ── Section 2: add_reference ──────────────────────────────────────────────

def test_add_reference_happy_path(db, tmp_path, monkeypatch):
    emb = (np.arange(512, dtype=np.float32) / 512.0)
    _mock(monkeypatch, tmp_path, [_face(det_score=0.92, area=0.3, embedding=emb)])
    player = _player(db)

    summary = references.add_reference(
        db, player.id, b"\xff\xd8imagebytes", captured_job_id=None,
        original_filename="ref.jpg")

    assert summary["player_id"] == player.id
    assert summary["det_score"] == pytest.approx(0.92)
    assert summary["face_area_ratio"] == pytest.approx(0.3)

    row = db.query(ReferenceFace).one()
    assert row.id == summary["id"]
    final = tmp_path / str(player.id) / f"{row.id}.jpg"
    assert final.exists()
    assert row.image_path == str(final)
    # embedding round-trips
    assert np.allclose(np.frombuffer(row.embedding, dtype=np.float32), emb)
    # no leftover temp file
    assert not list(tmp_path.glob(".tmp-*"))


def test_multiple_references_allowed(db, tmp_path, monkeypatch):
    _mock(monkeypatch, tmp_path, [_face()])
    player = _player(db)
    references.add_reference(db, player.id, b"a", original_filename="a.jpg")
    references.add_reference(db, player.id, b"b", original_filename="b.png")
    assert db.query(ReferenceFace).filter_by(player_id=player.id).count() == 2
    files = sorted((tmp_path / str(player.id)).iterdir())
    assert len(files) == 2  # one .jpg, one .png


@pytest.mark.parametrize("faces,code", [
    ([], "no_face"),
    ([_face(), _face()], "multiple_faces"),
    ([_face(det_score=0.55)], "low_confidence"),
    ([_face(area=0.005)], "face_too_small"),
])
def test_add_reference_rejects_bad_quality(db, tmp_path, monkeypatch, faces, code):
    _mock(monkeypatch, tmp_path, faces)
    player = _player(db)
    with pytest.raises(references.ReferenceQualityError) as exc:
        references.add_reference(db, player.id, b"x", original_filename="r.jpg")
    assert exc.value.code == code
    assert db.query(ReferenceFace).count() == 0          # no row
    assert not list(tmp_path.glob(".tmp-*"))             # no leftover temp


def test_add_reference_unknown_player_404(db, tmp_path, monkeypatch):
    _mock(monkeypatch, tmp_path, [_face()])
    with pytest.raises(HTTPException) as exc:
        references.add_reference(db, 99999, b"x", original_filename="r.jpg")
    assert exc.value.status_code == 404


def test_add_reference_unknown_job_404(db, tmp_path, monkeypatch):
    _mock(monkeypatch, tmp_path, [_face()])
    player = _player(db)
    with pytest.raises(HTTPException) as exc:
        references.add_reference(db, player.id, b"x", captured_job_id=99999,
                                 original_filename="r.jpg")
    assert exc.value.status_code == 404
    assert db.query(ReferenceFace).count() == 0


def test_add_reference_null_job_stored(db, tmp_path, monkeypatch):
    _mock(monkeypatch, tmp_path, [_face()])
    player = _player(db)
    references.add_reference(db, player.id, b"x", captured_job_id=None,
                             original_filename="r.jpg")
    assert db.query(ReferenceFace).one().captured_job_id is None


def test_add_reference_cross_shoot_provenance(db, tmp_path, monkeypatch):
    _mock(monkeypatch, tmp_path, [_face()])
    player = _player(db)
    job_a = _job(db, "Shoot A")
    job_b = _job(db, "Shoot B")
    references.add_reference(db, player.id, b"a", captured_job_id=job_a.id,
                             original_filename="a.jpg")
    references.add_reference(db, player.id, b"b", captured_job_id=job_b.id,
                             original_filename="b.jpg")
    listing = references.list_references(db, player.id)
    assert len(listing["items"]) == 2
    assert {i["captured_job_id"] for i in listing["items"]} == {job_a.id, job_b.id}


# ── Section 2: list / delete / replace ────────────────────────────────────

def test_list_references_unknown_player_404(db):
    with pytest.raises(HTTPException) as exc:
        references.list_references(db, 99999)
    assert exc.value.status_code == 404


def test_delete_reference_removes_row_and_file(db, tmp_path, monkeypatch):
    _mock(monkeypatch, tmp_path, [_face()])
    player = _player(db)
    references.add_reference(db, player.id, b"x", original_filename="r.jpg")
    row = db.query(ReferenceFace).one()
    path = Path(row.image_path)
    assert path.exists()

    result = references.delete_reference(db, player.id, row.id)
    assert result == {"deleted": 1}
    assert db.query(ReferenceFace).count() == 0
    assert not path.exists()


def test_delete_reference_wrong_player_404(db, tmp_path, monkeypatch):
    _mock(monkeypatch, tmp_path, [_face()])
    player_a = _player(db)
    player_b = _player(db, "junewampach", "June-Wampach")
    references.add_reference(db, player_a.id, b"x", original_filename="r.jpg")
    ref_id = db.query(ReferenceFace).one().id
    with pytest.raises(HTTPException) as exc:
        references.delete_reference(db, player_b.id, ref_id)
    assert exc.value.status_code == 404
    assert db.query(ReferenceFace).count() == 1   # untouched


def test_replace_wipes_and_sets_one(db, tmp_path, monkeypatch):
    _mock(monkeypatch, tmp_path, [_face()])
    player = _player(db)
    references.add_reference(db, player.id, b"a", original_filename="a.jpg")
    references.add_reference(db, player.id, b"b", original_filename="b.jpg")
    assert db.query(ReferenceFace).filter_by(player_id=player.id).count() == 2

    summary = references.replace_player_references(
        db, player.id, b"new", original_filename="new.jpg")
    refs = db.query(ReferenceFace).filter_by(player_id=player.id).all()
    assert len(refs) == 1
    assert refs[0].id == summary["id"]
    # only the new file remains on disk
    files = list((tmp_path / str(player.id)).iterdir())
    assert len(files) == 1
    assert files[0].name == f"{summary['id']}.jpg"


def test_replace_failed_gate_keeps_existing(db, tmp_path, monkeypatch):
    monkeypatch.setattr(references, "REFERENCES_DIR", tmp_path)
    player = _player(db)
    # Seed a good existing reference.
    monkeypatch.setattr(references.face_detector, "detect_faces",
                        _fake_detector([_face()]))
    references.add_reference(db, player.id, b"good", original_filename="good.jpg")
    good = db.query(ReferenceFace).one()
    good_path = Path(good.image_path)

    # Replace with a two-face photo → must raise and leave the old ref intact.
    monkeypatch.setattr(references.face_detector, "detect_faces",
                        _fake_detector([_face(), _face()]))
    with pytest.raises(references.ReferenceQualityError) as exc:
        references.replace_player_references(db, player.id, b"bad",
                                             original_filename="bad.jpg")
    assert exc.value.code == "multiple_faces"
    assert db.query(ReferenceFace).count() == 1
    assert db.query(ReferenceFace).one().id == good.id
    assert good_path.exists()
    assert not list(tmp_path.glob(".tmp-*"))


# ── Section 3: HTTP endpoints ─────────────────────────────────────────────

@pytest.fixture
def client(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'references-http.db'}",
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
    # Reference files land under tmp_path (not the real data/references dir).
    monkeypatch.setattr(references, "REFERENCES_DIR", tmp_path)
    try:
        seed = TestingSessionLocal()
        try:
            player = Player(norm_name="eleanorpederson",
                            display_name="Eleanor-Pederson")
            job_a = Job(name="Shoot A", root_path="/tmp", has_lines=0)
            job_b = Job(name="Shoot B", root_path="/tmp", has_lines=0)
            seed.add_all([player, job_a, job_b]); seed.commit()
            seed.refresh(player); seed.refresh(job_a); seed.refresh(job_b)
            pid, jid_a, jid_b = player.id, job_a.id, job_b.id
        finally:
            seed.close()
        c = TestClient(app)
        c.player_id = pid
        c.job_a_id = jid_a
        c.job_b_id = jid_b
        yield c
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _upload(client, pid, faces, monkeypatch, *, method="post", job_id=None,
            filename="ref.jpg"):
    monkeypatch.setattr(references.face_detector, "detect_faces",
                        _fake_detector(faces))
    url = f"/api/players/{pid}/references"
    params = {"job_id": job_id} if job_id is not None else {}
    files = {"file": (filename, b"\xff\xd8imagebytes", "image/jpeg")}
    return getattr(client, method)(url, files=files, params=params)


def test_http_round_trip(client, monkeypatch):
    pid = client.player_id
    res = _upload(client, pid, [_face(det_score=0.9)], monkeypatch)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["player_id"] == pid
    ref_id = body["id"]

    listing = client.get(f"/api/players/{pid}/references").json()
    assert len(listing["items"]) == 1
    assert listing["items"][0]["id"] == ref_id

    img = client.get(f"/api/players/{pid}/references/{ref_id}/image")
    assert img.status_code == 200
    assert img.content == b"\xff\xd8imagebytes"

    res = client.delete(f"/api/players/{pid}/references/{ref_id}")
    assert res.status_code == 200
    assert res.json() == {"deleted": 1}
    assert client.get(f"/api/players/{pid}/references").json()["items"] == []


def test_http_multiple_faces_400(client, monkeypatch):
    res = _upload(client, client.player_id, [_face(), _face()], monkeypatch)
    assert res.status_code == 400
    assert res.json()["detail"]["error"] == "multiple_faces"


@pytest.mark.parametrize("faces,code", [
    ([], "no_face"),
    ([_face(det_score=0.55)], "low_confidence"),
    ([_face(area=0.005)], "face_too_small"),
])
def test_http_quality_rejections_400(client, monkeypatch, faces, code):
    res = _upload(client, client.player_id, faces, monkeypatch)
    assert res.status_code == 400
    assert res.json()["detail"]["error"] == code


def test_http_unknown_player_404(client, monkeypatch):
    res = _upload(client, 99999, [_face()], monkeypatch)
    assert res.status_code == 404


def test_http_unknown_job_404(client, monkeypatch):
    res = _upload(client, client.player_id, [_face()], monkeypatch, job_id=99999)
    assert res.status_code == 404


def test_http_put_replace(client, monkeypatch):
    pid = client.player_id
    _upload(client, pid, [_face()], monkeypatch)
    _upload(client, pid, [_face()], monkeypatch)   # two references now
    assert len(client.get(f"/api/players/{pid}/references").json()["items"]) == 2

    second = _upload(client, pid, [_face()], monkeypatch,
                     method="put", filename="new.jpg").json()
    listing = client.get(f"/api/players/{pid}/references").json()
    assert len(listing["items"]) == 1                  # wiped 2 → set 1 (not appended)
    assert listing["items"][0]["id"] == second["id"]


def test_http_cross_shoot_provenance(client, monkeypatch):
    pid = client.player_id
    _upload(client, pid, [_face()], monkeypatch, job_id=client.job_a_id,
            filename="a.jpg")
    _upload(client, pid, [_face()], monkeypatch, job_id=client.job_b_id,
            filename="b.jpg")
    listing = client.get(f"/api/players/{pid}/references").json()
    assert len(listing["items"]) == 2
    assert {i["captured_job_id"] for i in listing["items"]} == {
        client.job_a_id, client.job_b_id}


# ── Phase B.2: shoot-scoped replace / delete (service) ─────────────────────

def test_replace_shoot_reference_is_scoped(db, tmp_path, monkeypatch):
    """Replacing this shoot's reference keeps exactly one for it and leaves
    every OTHER shoot's reference intact (unlike the player-wide replace)."""
    _mock(monkeypatch, tmp_path, [_face()])
    player = _player(db)
    job_a = _job(db, "Shoot A")
    job_b = _job(db, "Shoot B")
    references.add_reference(db, player.id, b"a", captured_job_id=job_a.id,
                             original_filename="a.jpg")
    references.add_reference(db, player.id, b"b", captured_job_id=job_b.id,
                             original_filename="b.jpg")

    summary = references.replace_shoot_reference(
        db, player.id, job_a.id, b"a2", original_filename="a2.jpg")

    a_refs = db.query(ReferenceFace).filter_by(
        player_id=player.id, captured_job_id=job_a.id).all()
    b_refs = db.query(ReferenceFace).filter_by(
        player_id=player.id, captured_job_id=job_b.id).all()
    assert len(a_refs) == 1 and a_refs[0].id == summary["id"]  # exactly the new one
    assert summary["captured_job_id"] == job_a.id
    assert len(b_refs) == 1                                     # Shoot B untouched


def test_replace_shoot_reference_collapses_within_shoot(db, tmp_path, monkeypatch):
    """If a shoot already had >1 ref, replace collapses to exactly one."""
    _mock(monkeypatch, tmp_path, [_face()])
    player = _player(db)
    job = _job(db)
    references.add_reference(db, player.id, b"x", captured_job_id=job.id,
                             original_filename="x.jpg")
    references.add_reference(db, player.id, b"y", captured_job_id=job.id,
                             original_filename="y.jpg")
    assert db.query(ReferenceFace).filter_by(captured_job_id=job.id).count() == 2
    references.replace_shoot_reference(db, player.id, job.id, b"z",
                                       original_filename="z.jpg")
    assert db.query(ReferenceFace).filter_by(captured_job_id=job.id).count() == 1


def test_replace_shoot_failed_gate_keeps_existing(db, tmp_path, monkeypatch):
    """A failed gate on a shoot-scoped replace leaves this shoot's ref intact."""
    monkeypatch.setattr(references, "REFERENCES_DIR", tmp_path)
    player = _player(db)
    job = _job(db)
    monkeypatch.setattr(references.face_detector, "detect_faces",
                        _fake_detector([_face()]))
    references.add_reference(db, player.id, b"good", captured_job_id=job.id,
                             original_filename="good.jpg")
    good = db.query(ReferenceFace).one()
    good_path = Path(good.image_path)

    monkeypatch.setattr(references.face_detector, "detect_faces",
                        _fake_detector([]))  # no_face
    with pytest.raises(references.ReferenceQualityError):
        references.replace_shoot_reference(db, player.id, job.id, b"bad",
                                           original_filename="bad.jpg")
    assert db.query(ReferenceFace).count() == 1
    assert db.query(ReferenceFace).one().id == good.id
    assert good_path.exists()
    assert not list(tmp_path.glob(".tmp-*"))


def test_replace_shoot_reference_404s(db, tmp_path, monkeypatch):
    _mock(monkeypatch, tmp_path, [_face()])
    player = _player(db)
    job = _job(db)
    with pytest.raises(HTTPException) as e_player:
        references.replace_shoot_reference(db, 99999, job.id, b"x",
                                           original_filename="x.jpg")
    assert e_player.value.status_code == 404
    with pytest.raises(HTTPException) as e_job:
        references.replace_shoot_reference(db, player.id, 99999, b"x",
                                           original_filename="x.jpg")
    assert e_job.value.status_code == 404
    assert db.query(ReferenceFace).count() == 0   # nothing stored on either 404


def test_delete_shoot_references_scoped_and_idempotent(db, tmp_path, monkeypatch):
    _mock(monkeypatch, tmp_path, [_face()])
    player = _player(db)
    job_a = _job(db, "Shoot A")
    job_b = _job(db, "Shoot B")
    references.add_reference(db, player.id, b"a", captured_job_id=job_a.id,
                             original_filename="a.jpg")
    references.add_reference(db, player.id, b"b", captured_job_id=job_b.id,
                             original_filename="b.jpg")
    a_path = Path(db.query(ReferenceFace)
                  .filter_by(captured_job_id=job_a.id).one().image_path)

    assert references.delete_shoot_references(db, player.id, job_a.id) == {"deleted": 1}
    assert db.query(ReferenceFace).filter_by(captured_job_id=job_a.id).count() == 0
    assert db.query(ReferenceFace).filter_by(captured_job_id=job_b.id).count() == 1
    assert not a_path.exists()
    # Idempotent — deleting again is a no-op success.
    assert references.delete_shoot_references(db, player.id, job_a.id) == {"deleted": 0}


def test_delete_shoot_references_unknown_player_404(db):
    with pytest.raises(HTTPException) as exc:
        references.delete_shoot_references(db, 99999, 1)
    assert exc.value.status_code == 404


# ── Phase B.2: shoot-scoped replace / delete (HTTP) ────────────────────────

def test_http_shoot_replace_and_delete(client, monkeypatch):
    pid, jid_a, jid_b = client.player_id, client.job_a_id, client.job_b_id
    monkeypatch.setattr(references.face_detector, "detect_faces",
                        _fake_detector([_face()]))
    files = {"file": ("cap.jpg", b"\xff\xd8imagebytes", "image/jpeg")}

    ra = client.put(f"/api/players/{pid}/references/shoot/{jid_a}", files=files)
    assert ra.status_code == 200, ra.text
    assert ra.json()["captured_job_id"] == jid_a
    rb = client.put(f"/api/players/{pid}/references/shoot/{jid_b}", files=files)
    assert rb.status_code == 200
    assert len(client.get(f"/api/players/{pid}/references").json()["items"]) == 2

    # Re-capture shoot A → still one for A, B intact (total stays 2).
    client.put(f"/api/players/{pid}/references/shoot/{jid_a}", files=files)
    listing = client.get(f"/api/players/{pid}/references").json()["items"]
    assert len(listing) == 2
    assert sum(1 for i in listing if i["captured_job_id"] == jid_a) == 1

    # Delete shoot A's → only B remains; deleting again is idempotent.
    d = client.delete(f"/api/players/{pid}/references/shoot/{jid_a}")
    assert d.status_code == 200 and d.json() == {"deleted": 1}
    remaining = client.get(f"/api/players/{pid}/references").json()["items"]
    assert len(remaining) == 1 and remaining[0]["captured_job_id"] == jid_b
    assert client.delete(
        f"/api/players/{pid}/references/shoot/{jid_a}").json() == {"deleted": 0}


def test_http_shoot_replace_failed_gate_400(client, monkeypatch):
    pid, jid = client.player_id, client.job_a_id
    monkeypatch.setattr(references.face_detector, "detect_faces",
                        _fake_detector([_face(), _face()]))
    files = {"file": ("x.jpg", b"\xff\xd8imagebytes", "image/jpeg")}
    res = client.put(f"/api/players/{pid}/references/shoot/{jid}", files=files)
    assert res.status_code == 400
    assert res.json()["detail"]["error"] == "multiple_faces"
