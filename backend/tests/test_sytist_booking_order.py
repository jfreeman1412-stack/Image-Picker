"""Booking-calendar shoots on the tablets (2026-10-09): they show before any
sign-up is on the roster, and the roster carries each kid's booking slot so
the tablets can sort by booking time."""
from app.services.sytist_db import _booked_at

from tests.test_sytist_passcodes import ctx  # noqa: F401  (fixture)
from tests.test_sytist_sync import _fam, fake_source  # noqa: F401  (fixture)


def _shoot(client, name="Jam Hops"):
    r = client.post("/api/jobs/shoot", json={"name": name, "root_path": None})
    assert r.status_code == 200, r.text
    return r.json()["job_id"]


def _capture_ids(client):
    return {j["id"] for j in client.get("/api/jobs?stage=capture").json()}


def test_booked_at_format():
    assert _booked_at("2026-10-10", "09:30:00") == "2026-10-10 09:30:00"
    assert _booked_at("2026-10-10", "9:30:00") == "2026-10-10 09:30:00"
    assert _booked_at("2026-10-10", None) == "2026-10-10 00:00:00"
    assert _booked_at("0000-00-00", "09:00:00") is None
    assert _booked_at(None, None) is None


def test_linked_shoot_shows_on_tablets_with_empty_roster(ctx, fake_source):
    client, _SL, _tmp = ctx
    plain = _shoot(client, "No roster")
    linked = _shoot(client, "Booking calendar")
    client.put(f"/api/sytist/jobs/{linked}/sources",
               json={"booking_event_ids": [327], "auto_add": True})
    ids = _capture_ids(client)
    assert linked in ids
    assert plain not in ids


def test_roster_carries_earliest_booking_slot(ctx, fake_source):
    client, _SL, _tmp = ctx
    jid = _shoot(client)
    early = _fam("booking", "1", "Ava Smith", "Jane Smith")
    early["booked_at"] = "2026-10-10 09:00:00"
    later = _fam("booking", "2", "Ava Smith", "Jane Smith")
    later["booked_at"] = "2026-10-10 11:30:00"
    ben = _fam("booking", "3", "Ben Jones", "Tom Jones")
    ben["booked_at"] = "2026-10-10 10:15:00"
    walk = _fam("preregister", "9", "Cara Lee", "Kim Lee")
    fake_source.families_out = [later, ben, early, walk]
    client.put(f"/api/sytist/jobs/{jid}/sources",
               json={"booking_event_ids": [327], "auto_add": True})
    assert client.post(f"/api/sytist/jobs/{jid}/sync").status_code == 200
    items = {i["name"]: i for i in client.get(f"/api/players/roster/{jid}").json()["items"]}
    assert items["Ava Smith"]["booked_at"] == "2026-10-10 09:00:00"
    assert items["Ben Jones"]["booked_at"] == "2026-10-10 10:15:00"
    assert items["Cara Lee"]["booked_at"] is None
