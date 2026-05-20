"""Tests for the server-side directory browser used by the wizard's
folder picker and the export modal's destination picker."""
import sys

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api.browse import browse
from app.main import app


def test_lists_subdirs_only(tmp_path):
    (tmp_path / "Alpha").mkdir()
    (tmp_path / "Beta").mkdir()
    (tmp_path / "afile.txt").write_text("x")
    res = browse(path=str(tmp_path))
    names = [e["name"] for e in res["entries"]]
    assert names == ["Alpha", "Beta"]                      # files filtered, sorted
    for e in res["entries"]:
        assert e["is_dir"] is True
    assert res["exists"] is True
    assert res["is_dir"] is True


def test_sorted_case_insensitive(tmp_path):
    for n in ("apple", "Banana", "cherry"):
        (tmp_path / n).mkdir()
    res = browse(path=str(tmp_path))
    assert [e["name"] for e in res["entries"]] == ["apple", "Banana", "cherry"]


def test_hidden_entries_filtered(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".venv").mkdir()
    (tmp_path / "$RECYCLE.BIN").mkdir()
    (tmp_path / "System Volume Information").mkdir()
    (tmp_path / "Visible").mkdir()
    res = browse(path=str(tmp_path))
    assert [e["name"] for e in res["entries"]] == ["Visible"]


def test_missing_path_returns_exists_false(tmp_path):
    res = browse(path=str(tmp_path / "does-not-exist"))
    assert res["exists"] is False
    assert res["entries"] == []
    # Best-guess parent so the UI can still let the user navigate up.
    assert res["parent"] == str(tmp_path)


def test_file_path_returns_exists_true_is_dir_false(tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("hi")
    res = browse(path=str(f))
    assert res["exists"] is True
    assert res["is_dir"] is False
    assert res["entries"] == []
    assert res["parent"] == str(tmp_path)


def test_parent_field_walks_up(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    res = browse(path=str(sub))
    assert res["parent"] == str(tmp_path)


def test_empty_path_returns_synthetic_drive_root_on_windows():
    res = browse(path="")
    assert res["path"] == ""
    assert res["parent"] is None
    if sys.platform == "win32":
        # At least one drive letter should be present on a real Windows box.
        assert any(e["name"].endswith(":\\") for e in res["entries"])


def test_dot_dot_rejected():
    with pytest.raises(HTTPException) as exc:
        browse(path="C:\\Users\\..\\Windows")
    assert exc.value.status_code == 400


def test_dot_dot_rejected_unix_style():
    with pytest.raises(HTTPException) as exc:
        browse(path="C:/Users/../Windows")
    assert exc.value.status_code == 400


def test_tilde_rejected():
    with pytest.raises(HTTPException) as exc:
        browse(path="~/Documents")
    assert exc.value.status_code == 400


def test_http_round_trip(tmp_path):
    (tmp_path / "OnlyChild").mkdir()
    client = TestClient(app)
    res = client.get("/api/browse", params={"path": str(tmp_path)})
    assert res.status_code == 200
    body = res.json()
    assert body["exists"] is True
    assert [e["name"] for e in body["entries"]] == ["OnlyChild"]


def test_http_round_trip_404_path_returns_exists_false(tmp_path):
    client = TestClient(app)
    res = client.get("/api/browse", params={"path": str(tmp_path / "nope")})
    assert res.status_code == 200          # endpoint returns 200 with exists=false
    assert res.json()["exists"] is False
