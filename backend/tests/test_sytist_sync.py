"""Sytist family sync (bookings + pre-registration) and the final-folder
passcode CSV. Sytist itself is replaced with a fake source."""
import pytest

from app.models.db_models import PlayerMembership, SytistFamily
from app.services import sytist_sync
from app.services.sytist_db import SytistDbError, load_config, save_config
from app.services.sytist_passcodes import CSV_FILENAME, ensure_job_passcodes

from tests.test_sytist_passcodes import (  # noqa: F401  (ctx is a fixture)
    _cluster, _export, _image, _job, _read_csv, _roster, _session, ctx,
)


def _fam(source, sid, kid, parent, email="", phone=""):
    kf, _, kl = kid.partition(" ")
    pf, _, pl = parent.partition(" ")
    return {"source": source, "source_id": sid,
            "subject_first_name": kf, "subject_last_name": kl,
            "parent_first_name": pf, "parent_last_name": pl,
            "parent_email": email, "parent_phone": phone}


class FakeSource:
    families_out: list = []
    calls: list = []
    fail = False

    def __init__(self, db):
        pass

    def families(self, booking_event_ids, gallery_ids):
        FakeSource.calls.append((booking_event_ids, gallery_ids))
        if FakeSource.fail:
            raise SytistDbError("Couldn't connect to Sytist: timed out")
        return list(FakeSource.families_out)

    def ping(self):
        return {"ok": True}


@pytest.fixture(autouse=True)
def fake_source(monkeypatch):
    FakeSource.families_out = []
    FakeSource.calls = []
    FakeSource.fail = False
    monkeypatch.setattr(sytist_sync, "source_factory", FakeSource)
    yield FakeSource


def _contact(SL, jid):
    db = SL()
    out = {m.player.display_name: m for m in
           db.query(PlayerMembership).filter_by(job_id=jid).all()}
    db.close()
    return out


def test_sync_fills_roster_contact_without_duplicates(ctx, fake_source):
    client, SL, tmp_path = ctx
    db = SL()
    job, _ = _job(db, tmp_path)
    _roster(db, job, [("Ava-Smith", "Tigers"), ("Ben Jones", "Tigers")],
            {("benjones", "tigers"): {"parent_email": "roster@example.com"}})
    jid = job.id
    db.close()
    fake_source.families_out = [
        _fam("booking", "11", "Ava Smith", "Jane Smith", "jane@example.com", "555-1"),
        _fam("preregister", "900", "Ava Smith", "Jane Smith", "other@example.com"),
        _fam("booking", "12", "Ben Jones", "Tom Jones", "tom@example.com", "555-2"),
        _fam("booking", "13", "Cara Lee", "Kim Lee", "kim@example.com"),
    ]
    r = client.put(f"/api/sytist/jobs/{jid}/sources",
                   json={"booking_event_ids": [7], "gallery_ids": [6666]})
    assert r.json()["sources"] == {"booking_event_ids": [7], "gallery_ids": [6666]}
    out = client.post(f"/api/sytist/jobs/{jid}/sync").json()
    assert fake_source.calls == [([7], [6666])]
    assert out["added"] == 4 and out["matched_players"] == 2
    assert [u["player"] for u in out["unmatched"]] == ["Cara Lee"]

    ms = _contact(SL, jid)
    ava = ms["Ava-Smith"]
    # Booking wins over pre-registration; split names come through.
    assert ava.parent_email == "jane@example.com" and ava.parent_phone == "555-1"
    assert (ava.subject_first_name, ava.subject_last_name) == ("Ava", "Smith")
    # Contact from the roster upload is never overwritten.
    assert ms["Ben Jones"].parent_email == "roster@example.com"
    assert ms["Ben Jones"].parent_phone == "555-2"

    # Re-sync with one booking cancelled: no duplicates, the row drops out.
    fake_source.families_out = fake_source.families_out[:3]
    out = client.post(f"/api/sytist/jobs/{jid}/sync").json()
    assert (out["added"], out["updated"], out["removed"]) == (0, 0, 1)
    assert out["unmatched"] == []
    db = SL()
    assert db.query(SytistFamily).filter_by(job_id=jid).count() == 3
    db.close()


def test_add_unmatched_family_to_team(ctx, fake_source):
    client, SL, tmp_path = ctx
    db = SL()
    job, _ = _job(db, tmp_path)
    _roster(db, job, [("Ava Smith", "Tigers")])
    jid = job.id
    db.close()
    fake_source.families_out = [
        _fam("booking", "13", "Cara Lee", "Kim Lee", "kim@example.com"),
        _fam("booking", "14", "", "Pat Doe", "pat@example.com"),
    ]
    client.put(f"/api/sytist/jobs/{jid}/sources", json={"booking_event_ids": [7]})
    out = client.post(f"/api/sytist/jobs/{jid}/sync").json()
    by_parent = {u["parent"]: u for u in out["unmatched"]}
    r = client.post(f"/api/sytist/jobs/{jid}/families/{by_parent['Kim Lee']['id']}/add",
                    json={"team": "Tigers"})
    assert r.status_code == 200, r.text
    # No player name on the sign-up: a typed name is required.
    fid = by_parent["Pat Doe"]["id"]
    assert client.post(f"/api/sytist/jobs/{jid}/families/{fid}/add",
                       json={"team": "Lions"}).status_code == 400
    assert client.post(f"/api/sytist/jobs/{jid}/families/{fid}/add",
                       json={"team": "Lions", "name": "Sam Doe"}).status_code == 200
    ms = _contact(SL, jid)
    assert ms["Cara Lee"].parent_email == "kim@example.com" and ms["Cara Lee"].passcode
    assert ms["Sam Doe"].parent_email == "pat@example.com"
    assert ms["Sam Doe"].team_name == "Lions"
    state = client.get(f"/api/sytist/jobs/{jid}").json()
    assert state["unmatched"] == [] and state["families"] == 2


def test_roster_upload_after_sync_picks_up_contact(ctx, fake_source):
    client, SL, tmp_path = ctx
    db = SL()
    job, _ = _job(db, tmp_path)
    jid = job.id
    db.close()
    fake_source.families_out = [_fam("preregister", "5", "Ava Smith", "Jane Smith",
                                     "jane@example.com")]
    client.put(f"/api/sytist/jobs/{jid}/sources", json={"gallery_ids": [1]})
    client.post(f"/api/sytist/jobs/{jid}/sync")
    files = {"file": ("r.csv", b"Ava Smith,Tigers\n", "text/csv")}
    assert client.post(f"/api/players/roster/{jid}", files=files).status_code == 200
    assert _contact(SL, jid)["Ava Smith"].parent_email == "jane@example.com"


def test_sync_errors(ctx, fake_source):
    client, SL, tmp_path = ctx
    db = SL()
    job, _ = _job(db, tmp_path)
    jid = job.id
    db.close()
    assert client.post(f"/api/sytist/jobs/{jid}/sync").status_code == 400
    client.put(f"/api/sytist/jobs/{jid}/sources", json={"booking_event_ids": [7]})
    fake_source.fail = True
    r = client.post(f"/api/sytist/jobs/{jid}/sync")
    assert r.status_code == 502 and "timed out" in r.json()["detail"]["message"]


def test_export_syncs_first_and_survives_sytist_down(ctx, fake_source):
    client, SL, tmp_path = ctx
    db = SL()
    job, root = _job(db, tmp_path)
    _roster(db, job, [("Ava Smith", "Tigers")])
    sess, folder = _session(db, job, root, "Tigers")
    _image(db, sess, folder, "IMG_0001.jpg", [(_cluster(db, sess, "Ava Smith"), "individual")])
    jid = job.id
    db.close()
    client.put(f"/api/sytist/jobs/{jid}/sources", json={"booking_event_ids": [7]})
    fake_source.families_out = [_fam("booking", "1", "Ava Smith", "Jane Smith",
                                     "jane@example.com")]
    info = _export(client, jid)["sytist_csv"]
    assert info["sync"]["matched_players"] == 1
    assert _read_csv(info["path"])[0]["EMAIL"] == "jane@example.com"
    fake_source.fail = True
    info = _export(client, jid)["sytist_csv"]
    assert "timed out" in info["sync"]["error"]
    assert _read_csv(info["path"])[0]["EMAIL"] == "jane@example.com"


def test_csv_from_final_folder(ctx):
    client, SL, tmp_path = ctx
    db = SL()
    job, root = _job(db, tmp_path)
    _roster(db, job, [("Ava Smith", "Tigers 10U"), ("Ben Jones", "Tigers 10U")])
    sess, folder = _session(db, job, root, "Tigers 10U")
    ava, ben = _cluster(db, sess, "Ava Smith"), _cluster(db, sess, "Ben Jones")
    _image(db, sess, folder, "IMG 0001.jpg", [(ava, "individual")])
    _image(db, sess, folder, "IMG_0002.jpg", [(ava, "individual")])
    _image(db, sess, folder, "IMG_0003.jpg", [(ben, "individual")])
    _image(db, sess, folder, "IMG_0004.jpg", [(ava, "buddy"), (ben, "buddy")])
    jid = job.id
    db.close()

    r = client.post(f"/api/sytist/jobs/{jid}/csv-from-folder",
                    json={"folder": str(tmp_path / "Final")})
    assert r.status_code == 400          # no export yet
    _export(client, jid)

    final = tmp_path / "Final"
    (final / "sub").mkdir(parents=True)
    for name in ("IMG 0001.png", "IMG_0004.png", "Tigers_10U.jpg", "Stray.png"):
        (final / name).write_bytes(b"x")
    (final / "sub" / "IMG_0003.png").write_bytes(b"x")   # IMG_0002 was culled
    r = client.post(f"/api/sytist/jobs/{jid}/csv-from-folder", json={"folder": f'"{final}"'})
    assert r.status_code == 200, r.text
    info = r.json()
    db = SL()
    codes = {m.player.display_name: m.passcode
             for m in db.query(PlayerMembership).filter_by(job_id=jid)}
    db.close()
    by_file = {row["FILENAME"]: row for row in _read_csv(final / CSV_FILENAME)}
    assert by_file["IMG_0001.png"]["PASSCODE"] == codes["Ava Smith"]
    assert by_file["IMG_0003.png"]["PASSCODE"] == codes["Ben Jones"]
    assert by_file["IMG_0004.png"]["IS_GROUP"] == "1"
    assert "IMG_0004.png" in by_file["IMG_0001.png"]["GROUPS"]
    assert "IMG_0002.png" not in by_file and "IMG_0002.jpg" not in by_file
    assert by_file["Tigers_10U.jpg"]["IS_GROUP"] == "1"
    assert info["not_in_export_examples"] == ["Stray.png"]
    assert info["exported_not_in_folder"] == 1
    assert info["team_photos_missing"] == ["tigers_10u-pano.jpg"]


def test_settings_keep_password(ctx):
    client, SL, _ = ctx
    r = client.put("/api/sytist/settings", json={
        "host": "db.example", "user": "player_sort", "password": "pw1"})
    assert r.json() == {"host": "db.example", "port": 3306, "user": "player_sort",
                        "database": "sportsline", "password_set": True,
                        "configured": True}
    client.put("/api/sytist/settings", json={"host": "db2.example", "password": ""})
    db = SL()
    cfg = load_config(db)
    db.close()
    assert cfg["host"] == "db2.example" and cfg["password"] == "pw1"
    assert "password" not in client.get("/api/sytist/settings").json()
