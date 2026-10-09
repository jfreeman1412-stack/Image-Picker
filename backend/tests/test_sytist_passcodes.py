"""Per-job Sytist passcodes: roster passcode/contact storage, passcode
generation, and the Preset Passcode Photos import CSV written on export."""
import csv
import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api import jobs as jobs_module
from app.db import Base, get_db
from app.main import app
from app.models.db_models import (
    Cluster, Image, ImageRole, Job, Player, PlayerMembership,
    Session as DbSessionModel,
)
from app.services.players import parse_mapped_contacts, replace_shoot_memberships
from app.services.sytist_passcodes import (
    PASSCODE_ALPHABET, csv_filename, PASSCODE_LENGTH, ensure_job_passcodes,
    team_photo_names,
)


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'sytist-test.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    SL = sessionmaker(bind=engine)

    def _override():
        s = SL()
        try:
            yield s
        finally:
            s.close()

    monkeypatch.setattr(jobs_module, "SessionLocal", SL)
    app.dependency_overrides[get_db] = _override
    try:
        yield TestClient(app), SL, tmp_path
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _job(db, tmp_path, *, sytist=1):
    root = tmp_path / "Shoot"
    root.mkdir(exist_ok=True)
    job = Job(name="Shoot", root_path=str(root.resolve()), has_lines=0,
              created_at=datetime.utcnow(), sytist_passcodes=sytist)
    db.add(job); db.commit(); db.refresh(job)
    return job, root


def _session(db, job, root, name):
    d = root / name
    d.mkdir(exist_ok=True)
    s = DbSessionModel(job_id=job.id, name=name, source_path=str(d.resolve()),
                       status="done", created_at=datetime.utcnow())
    db.add(s); db.commit(); db.refresh(s)
    return s, d


def _cluster(db, sess, label):
    c = Cluster(session_id=sess.id, image_count=0, manual_label=label)
    db.add(c); db.commit(); db.refresh(c)
    return c


def _image(db, sess, folder, filename, claims):
    """claims = [(cluster, role), ...]"""
    fp = folder / filename
    fp.write_bytes(b"img")
    img = Image(session_id=sess.id, path=str(fp.resolve()), filename=filename)
    db.add(img); db.commit(); db.refresh(img)
    for cluster, role in claims:
        db.add(ImageRole(image_id=img.id, cluster_id=cluster.id, role=role))
    db.commit()
    return img


def _roster(db, job, rows, contacts=None):
    replace_shoot_memberships(db, job.id, rows, contacts)
    db.commit()


def _read_csv(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _export(client, job_id, **extra):
    body = {"mode": "copy", "overwrite": True, **extra}
    r = client.post(f"/api/jobs/{job_id}/export", json=body)
    assert r.status_code == 200, r.text
    status = client.get(f"/api/jobs/{job_id}/export-status").json()
    assert status["status"] == "done", status
    return status["result"]


# ── passcodes + roster storage ──────────────────────────────────────────────


def test_ensure_job_passcodes_unique_shared_and_stable(ctx):
    _, SL, tmp_path = ctx
    db = SL()
    job, _ = _job(db, tmp_path)
    _roster(db, job, [("Ava Smith", "Tigers"), ("Ben Jones", "Tigers"),
                      ("Ava Smith", "Lions")])
    assert ensure_job_passcodes(db, job.id) == 3
    db.commit()
    ms = db.query(PlayerMembership).filter_by(job_id=job.id).all()
    codes = {m.player.display_name: set() for m in ms}
    for m in ms:
        assert len(m.passcode) == PASSCODE_LENGTH
        assert set(m.passcode) <= set(PASSCODE_ALPHABET)
        codes[m.player.display_name].add(m.passcode)
    assert len(codes["Ava Smith"]) == 1          # one code across both teams
    assert codes["Ava Smith"] != codes["Ben Jones"]
    before = {m.id: m.passcode for m in ms}
    assert ensure_job_passcodes(db, job.id) == 0
    assert {m.id: m.passcode for m in ms} == before
    db.close()


def test_reupload_keeps_passcodes_and_contacts(ctx):
    _, SL, tmp_path = ctx
    db = SL()
    job, _ = _job(db, tmp_path)
    _roster(db, job, [("Ava Smith", "Tigers")],
            {("avasmith", "tigers"): {"parent_email": "mom@example.com"}})
    ensure_job_passcodes(db, job.id); db.commit()
    code = db.query(PlayerMembership).filter_by(job_id=job.id).one().passcode
    # Re-upload without contact columns: code + email survive.
    _roster(db, job, [("Ava Smith", "Tigers"), ("Ben Jones", "Tigers")])
    ava = (db.query(PlayerMembership).join(Player)
           .filter(PlayerMembership.job_id == job.id,
                   Player.norm_name == "avasmith").one())
    assert ava.passcode == code
    assert ava.parent_email == "mom@example.com"
    # A passcode in the upload wins.
    _roster(db, job, [("Ava Smith", "Tigers")],
            {("avasmith", "tigers"): {"passcode": "NEWCODE"}})
    ava = db.query(PlayerMembership).filter_by(job_id=job.id).one()
    assert ava.passcode == "NEWCODE"
    assert ava.parent_email == "mom@example.com"
    db.close()


def test_parse_mapped_contacts_split_mode():
    text = ("First,Last,Team,Parent First,Parent Last,Email,Phone,Code\n"
            "Ava,Smith,Tigers,Jane,Smith,jane@example.com,555-1111,\n"
            "Ben,Jones,Tigers,,,,,BEN1234\n")
    mapping = {
        "has_header": True, "name_mode": "split",
        "first_name_column": "First", "last_name_column": "Last",
        "team_column": "Team",
        "parent_first_name_column": "Parent First",
        "parent_last_name_column": "Parent Last",
        "parent_email_column": "Email", "parent_phone_column": "Phone",
        "passcode_column": "Code",
    }
    out = parse_mapped_contacts(text, mapping)
    assert out[("avasmith", "tigers")] == {
        "subject_first_name": "Ava", "subject_last_name": "Smith",
        "parent_first_name": "Jane", "parent_last_name": "Smith",
        "parent_email": "jane@example.com", "parent_phone": "555-1111",
    }
    assert out[("benjones", "tigers")]["passcode"] == "BEN1234"


def test_mapped_upload_stores_contacts_and_assigns_codes(ctx):
    client, SL, tmp_path = ctx
    db = SL()
    job, _ = _job(db, tmp_path)
    jid = job.id
    db.close()
    text = "Player,Team,Email\nAva Smith,Tigers,jane@example.com\n"
    mapping = {"has_header": True, "name_mode": "full",
               "name_column": "Player", "team_column": "Team",
               "parent_email_column": "Email"}
    r = client.post(f"/api/players/roster/{jid}/mapped",
                    files={"file": ("r.csv", text, "text/csv")},
                    data={"mapping": json.dumps(mapping)})
    assert r.status_code == 200, r.text
    items = client.get(f"/api/players/roster/{jid}").json()["items"]
    assert items[0]["parent_email"] == "jane@example.com"
    assert items[0]["passcode"]  # option is on → code assigned at upload


def test_toggle_endpoint(ctx):
    client, SL, tmp_path = ctx
    db = SL()
    job, _ = _job(db, tmp_path, sytist=0)
    _roster(db, job, [("Ava Smith", "Tigers")])
    jid = job.id
    db.close()
    assert client.get(f"/api/jobs/{jid}").json()["sytist_passcodes"] is False
    r = client.post(f"/api/jobs/{jid}/sytist-passcodes", json={"enabled": True})
    assert r.json() == {"sytist_passcodes": True, "sytist_family_passcodes": True,
                        "passcodes_assigned": 1}
    assert client.get(f"/api/jobs/{jid}").json()["sytist_passcodes"] is True
    r = client.post(f"/api/jobs/{jid}/sytist-passcodes",
                    json={"enabled": True, "family": False})
    assert r.json()["sytist_family_passcodes"] is False
    assert client.get(f"/api/jobs/{jid}").json()["sytist_family_passcodes"] is False


# ── export CSV ──────────────────────────────────────────────────────────────


def _two_kid_job(SL, tmp_path, *, sytist=1):
    db = SL()
    job, root = _job(db, tmp_path, sytist=sytist)
    _roster(db, job, [("Ava Smith", "Tigers 10U"), ("Ben Jones", "Tigers 10U")],
            {("avasmith", "tigers10u"): {"parent_email": "jane@example.com",
                                          "parent_first_name": "Jane"}})
    sess, folder = _session(db, job, root, "Tigers 10U")
    ava = _cluster(db, sess, "Ava Smith")
    ben = _cluster(db, sess, "Ben Jones")
    stranger = _cluster(db, sess, "Not On Roster")
    _image(db, sess, folder, "IMG_0001.jpg", [(ava, "individual")])
    _image(db, sess, folder, "IMG_0002.jpg", [(ava, "individual")])
    _image(db, sess, folder, "IMG_0003.jpg", [(ben, "individual")])
    _image(db, sess, folder, "IMG_0004.jpg", [(ava, "buddy"), (ben, "buddy")])
    _image(db, sess, folder, "IMG_0005.jpg", [(stranger, "individual")])
    _image(db, sess, folder, "IMG_0006.jpg", [(ava, "rejected")])
    jid = job.id
    db.close()
    return jid, root


def _codes(SL, jid):
    db = SL()
    out = {m.player.display_name: m.passcode
           for m in db.query(PlayerMembership).filter_by(job_id=jid)}
    db.close()
    return out


def test_export_off_writes_no_csv(ctx):
    client, SL, tmp_path = ctx
    jid, root = _two_kid_job(SL, tmp_path, sytist=0)
    result = _export(client, jid)
    assert "sytist_csv" not in result
    assert not (root.parent / "Shoot_sorted" / csv_filename("Shoot")).exists()


def test_export_writes_import_csv_legacy_names(ctx):
    client, SL, tmp_path = ctx
    jid, root = _two_kid_job(SL, tmp_path)
    result = _export(client, jid)
    info = result["sytist_csv"]
    rows = _read_csv(info["path"])
    codes = _codes(SL, jid)
    by_file = {r["FILENAME"]: r for r in rows}

    team_group = "Tigers_10U.jpg; Tigers_10U-PANO.jpg"
    for f in ("IMG_0001.jpg", "IMG_0002.jpg"):
        r = by_file[f]
        assert r["PASSCODE"] == codes["Ava Smith"]
        assert (r["SUBJECT_FIRST_NAME"], r["SUBJECT_LAST_NAME"]) == ("Ava", "Smith")
        assert r["EMAIL"] == "jane@example.com" and r["FIRST_NAME"] == "Jane"
        assert r["GROUPS"] == team_group + "; IMG_0004.jpg"
        assert r["IS_GROUP"] == ""
    assert by_file["IMG_0003.jpg"]["PASSCODE"] == codes["Ben Jones"]
    assert by_file["IMG_0003.jpg"]["GROUPS"] == team_group + "; IMG_0004.jpg"
    # Buddy shot exported once → one group photo both kids list.
    assert by_file["IMG_0004.jpg"]["IS_GROUP"] == "1"
    assert by_file["IMG_0004.jpg"]["PASSCODE"] == ""
    # Team composites from the roster's team name (spaces → underscores).
    assert by_file["Tigers_10U.jpg"]["IS_GROUP"] == "1"
    assert by_file["Tigers_10U-PANO.jpg"]["IS_GROUP"] == "1"
    # Unmatched cluster and rejected photo stay out.
    assert "IMG_0005.jpg" not in by_file and "IMG_0006.jpg" not in by_file
    assert info["unassigned_files"] == 1
    assert info["unassigned_examples"] == ["IMG_0005.jpg"]
    assert info["photo_rows"] == 3 and info["players"] == 2


def test_export_upload_ext_and_player_without_photos(ctx):
    client, SL, tmp_path = ctx
    db = SL()
    job, root = _job(db, tmp_path)
    _roster(db, job, [("Ava Smith", "Tigers"), ("Cara Lee", "Tigers")])
    sess, folder = _session(db, job, root, "Tigers")
    ava = _cluster(db, sess, "Ava Smith")
    _image(db, sess, folder, "IMG 0001.jpg", [(ava, "individual")])
    jid = job.id
    db.close()
    rows = _read_csv(_export(client, jid, sytist_upload_ext=".png")["sytist_csv"]["path"])
    by_file = {r["FILENAME"]: r for r in rows}
    assert "IMG_0001.png" in by_file          # new extension, spaces → _
    no_photo = [r for r in rows if r["FILENAME"] == ""]
    assert len(no_photo) == 1 and no_photo[0]["SUBJECT_FIRST_NAME"] == "Cara"
    assert no_photo[0]["PASSCODE"] and no_photo[0]["GROUPS"] == "Tigers.jpg; Tigers-PANO.jpg"


def test_export_rename_mode_gives_each_buddy_copy_its_kid(ctx):
    client, SL, tmp_path = ctx
    jid, root = _two_kid_job(SL, tmp_path)
    rows = _read_csv(_export(client, jid, rename_by_player=True)["sytist_csv"]["path"])
    codes = _codes(SL, jid)
    by_file = {r["FILENAME"]: r for r in rows}
    assert by_file["Ava_Smith_001.jpg"]["PASSCODE"] == codes["Ava Smith"]
    # The buddy shot is copied once per kid, each copy under that kid's code.
    assert by_file["Ava_Smith_003.jpg"]["PASSCODE"] == codes["Ava Smith"]
    assert by_file["Ben_Jones_002.jpg"]["PASSCODE"] == codes["Ben Jones"]
    assert not [r for r in rows if r["IS_GROUP"] == "1"
                and r["FILENAME"].startswith(("Ava", "Ben"))]
    assert by_file["Ava_Smith_001.jpg"]["GROUPS"] == "Tigers_10U.jpg; Tigers_10U-PANO.jpg"


def test_moved_photo_follows_new_cluster_label(ctx):
    """Relabelling a cluster in review changes whose code its photos get."""
    client, SL, tmp_path = ctx
    jid, _ = _two_kid_job(SL, tmp_path)
    db = SL()
    c = db.query(Cluster).filter_by(manual_label="Not On Roster").one()
    c.manual_label = "Ben Jones"
    db.commit(); db.close()
    rows = _read_csv(_export(client, jid)["sytist_csv"]["path"])
    by_file = {r["FILENAME"]: r for r in rows}
    assert by_file["IMG_0005.jpg"]["PASSCODE"] == _codes(SL, jid)["Ben Jones"]


def test_team_photo_names():
    assert team_photo_names("3rd-4th Peterson") == [
        "3rd-4th_Peterson.jpg", "3rd-4th_Peterson-PANO.jpg"]


def test_bad_upload_ext_rejected(ctx):
    client, SL, tmp_path = ctx
    jid, _ = _two_kid_job(SL, tmp_path)
    r = client.post(f"/api/jobs/{jid}/export",
                    json={"mode": "copy", "sytist_upload_ext": ".exe"})
    assert r.status_code == 400


def test_csv_named_after_job():
    assert csv_filename("Spring Soccer 2026") == "Spring_Soccer_2026_sytist_passcodes.csv"
    assert csv_filename("Lions/Tigers: U10") == "Lions_Tigers_U10_sytist_passcodes.csv"
    assert csv_filename("") == "job_sytist_passcodes.csv"


def test_export_flags_same_file_name_on_two_teams(ctx):
    client, SL, tmp_path = ctx
    db = SL()
    job, root = _job(db, tmp_path)
    _roster(db, job, [("Ava Smith", "Tigers"), ("Ben Jones", "Lions")])
    for team, kid in (("Tigers", "Ava Smith"), ("Lions", "Ben Jones")):
        sess, folder = _session(db, job, root, team)
        _image(db, sess, folder, "IMG_0001.jpg", [(_cluster(db, sess, kid), "individual")])
    _image(db, sess, folder, "IMG_0002.jpg", [(_cluster(db, sess, "Ben Jones"), "individual")])
    jid = job.id
    db.close()
    info = _export(client, jid)["sytist_csv"]
    assert info["path"].endswith("Shoot_sytist_passcodes.csv")
    assert info["duplicate_names"] == 1
    assert info["duplicate_examples"] == ["IMG_0001.jpg"]


# ── family passcodes (2026-10-09) ───────────────────────────────────────────


def _by_name(db, job):
    return {m.player.display_name: m
            for m in db.query(PlayerMembership).filter_by(job_id=job.id)}


def test_family_passcode_shared_by_email_or_phone(ctx):
    _, SL, tmp_path = ctx
    db = SL()
    job, _ = _job(db, tmp_path)
    _roster(db, job,
            [("Ava Smith", "Tigers"), ("Max Smith", "Lions"), ("Zoe Smith", "Lions"),
             ("Ben Jones", "Tigers"), ("Coach-Jane Smith", "Tigers")],
            {("avasmith", "tigers"): {"parent_email": "Jane@Example.com"},
             ("maxsmith", "lions"): {"parent_email": "jane@example.com ",
                                     "parent_phone": "(763) 555-0101"},
             ("zoesmith", "lions"): {"parent_phone": "1-763-555-0101"},
             ("benjones", "tigers"): {"parent_email": "bob@example.com"},
             ("coachjanesmith", "tigers"): {"parent_email": "jane@example.com"}})
    ensure_job_passcodes(db, job.id); db.commit()
    ms = _by_name(db, job)
    family = ms["Ava Smith"].passcode
    assert ms["Max Smith"].passcode == family      # same email, other team
    assert ms["Zoe Smith"].passcode == family      # phone links her to Max
    assert ms["Ben Jones"].passcode != family
    assert ms["Coach-Jane Smith"].passcode != family  # coaches never merged
    db.close()


def test_family_option_off_keeps_one_code_per_kid(ctx):
    _, SL, tmp_path = ctx
    db = SL()
    job, _ = _job(db, tmp_path)
    job.sytist_family_passcodes = 0; db.commit()
    _roster(db, job, [("Ava Smith", "Tigers"), ("Max Smith", "Lions")],
            {("avasmith", "tigers"): {"parent_email": "jane@example.com"},
             ("maxsmith", "lions"): {"parent_email": "jane@example.com"}})
    ensure_job_passcodes(db, job.id); db.commit()
    ms = _by_name(db, job)
    assert ms["Ava Smith"].passcode != ms["Max Smith"].passcode
    db.close()


def test_late_contact_merges_unsent_codes_but_never_sent_ones(ctx):
    _, SL, tmp_path = ctx
    db = SL()
    job, _ = _job(db, tmp_path)
    _roster(db, job, [("Ava Smith", "Tigers"), ("Max Smith", "Lions"),
                      ("Kim Lee", "Tigers"), ("Lou Lee", "Lions")])
    ensure_job_passcodes(db, job.id); db.commit()
    ms = _by_name(db, job)
    assert ms["Ava Smith"].passcode != ms["Max Smith"].passcode
    ava_code = ms["Ava Smith"].passcode
    # Contact arrives later (e.g. a Sytist sync): not sent yet → merged.
    ms["Ava Smith"].parent_email = ms["Max Smith"].parent_email = "jane@example.com"
    db.commit()
    assert ensure_job_passcodes(db, job.id) == 1
    db.commit()
    assert ms["Max Smith"].passcode == ava_code
    # Once sent to Sytist, codes never change, even if contact now links them.
    kim, lou = ms["Kim Lee"].passcode, ms["Lou Lee"].passcode
    for m in ms.values():
        m.passcode_locked = 1
    ms["Kim Lee"].parent_phone = ms["Lou Lee"].parent_phone = "763-555-0199"
    db.commit()
    assert ensure_job_passcodes(db, job.id) == 0
    assert (ms["Kim Lee"].passcode, ms["Lou Lee"].passcode) == (kim, lou)
    db.close()


def test_new_sibling_after_export_gets_family_code(ctx):
    client, SL, tmp_path = ctx
    jid, _ = _two_kid_job(SL, tmp_path)
    _export(client, jid)
    db = SL()
    job = db.query(Job).get(jid)
    ms = _by_name(db, job)
    assert all(m.passcode_locked for m in ms.values())
    ava_code = ms["Ava Smith"].passcode
    replace_shoot_memberships(
        db, jid, [("Ava Smith", "Tigers 10U"), ("Ben Jones", "Tigers 10U"),
                  ("Sam Smith", "Lions")],
        {("samsmith", "lions"): {"parent_email": "JANE@example.com"}})
    db.commit()
    ensure_job_passcodes(db, jid); db.commit()
    ms = _by_name(db, job)
    assert ms["Ava Smith"].passcode == ava_code    # kept across the re-upload
    assert ms["Sam Smith"].passcode == ava_code
    db.close()


def test_roster_passcode_is_locked():
    text = "Player,Team,Code\nAva Smith,Tigers,ABC1234\nBen Jones,Tigers,\n"
    out = parse_mapped_contacts(text, {
        "has_header": True, "name_mode": "full", "name_column": "Player",
        "team_column": "Team", "passcode_column": "Code"})
    assert out[("avasmith", "tigers")] == {"passcode": "ABC1234", "passcode_locked": 1}


def test_family_csv_rows_carry_whole_family_groups(ctx):
    client, SL, tmp_path = ctx
    db = SL()
    job, root = _job(db, tmp_path)
    _roster(db, job, [("Ava Smith", "Tigers"), ("Max Smith", "Lions"),
                      ("Ben Jones", "Lions")],
            {("avasmith", "tigers"): {"parent_email": "jane@example.com"},
             ("maxsmith", "lions"): {"parent_email": "jane@example.com"}})
    s1, f1 = _session(db, job, root, "Tigers")
    s2, f2 = _session(db, job, root, "Lions")
    ava = _cluster(db, s1, "Ava Smith")
    max_ = _cluster(db, s2, "Max Smith")
    ben = _cluster(db, s2, "Ben Jones")
    _image(db, s1, f1, "A1.jpg", [(ava, "individual")])
    _image(db, s2, f2, "M1.jpg", [(max_, "individual")])
    _image(db, s2, f2, "MB.jpg", [(max_, "buddy"), (ben, "buddy")])
    jid = job.id
    db.close()
    rows = _read_csv(_export(client, jid)["sytist_csv"]["path"])
    by_file = {r["FILENAME"]: r for r in rows}
    family = by_file["A1.jpg"]["PASSCODE"]
    assert by_file["M1.jpg"]["PASSCODE"] == family
    expected = "Tigers.jpg; Tigers-PANO.jpg; Lions.jpg; Lions-PANO.jpg; MB.jpg"
    assert by_file["A1.jpg"]["GROUPS"] == expected
    assert by_file["M1.jpg"]["GROUPS"] == expected
    assert by_file["A1.jpg"]["LEADER"] == "Tigers; Lions"
    # Ben's own family is just Ben.
    assert by_file["MB.jpg"]["IS_GROUP"] == "1"
    ben_rows = [r for r in rows if r["SUBJECT_FIRST_NAME"] == "Ben"]
    assert ben_rows and all(r["PASSCODE"] != family for r in ben_rows)
