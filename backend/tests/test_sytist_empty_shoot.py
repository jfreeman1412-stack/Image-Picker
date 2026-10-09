"""A shoot linked to the Sytist booking calendar shows on the tablets before
any sign-up is on its roster, so "Get new sign-ups" can fill it there
(2026-10-09)."""
from tests.test_sytist_passcodes import ctx  # noqa: F401  (fixture)
from tests.test_sytist_sync import fake_source  # noqa: F401  (fixture)


def _shoot(client, name):
    r = client.post("/api/jobs/shoot", json={"name": name, "root_path": None})
    assert r.status_code == 200, r.text
    return r.json()["job_id"]


def test_linked_shoot_shows_on_tablets_with_empty_roster(ctx, fake_source):
    client, _SL, _tmp = ctx
    plain = _shoot(client, "No roster")
    linked = _shoot(client, "Booking calendar")
    client.put(f"/api/sytist/jobs/{linked}/sources",
               json={"booking_event_ids": [327], "auto_add": True})
    ids = {j["id"] for j in client.get("/api/jobs?stage=capture").json()}
    assert linked in ids
    assert plain not in ids
