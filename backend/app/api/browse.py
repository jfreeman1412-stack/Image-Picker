"""Server-side directory browser for the job-folder + export-destination
folder pickers.

Browsers can't expose a native filesystem picker that returns a server-side
path — `<input type=file webkitdirectory>` uploads files, it doesn't give
you the parent path. This LAN tool ingests from UNC shares and exports to
arbitrary paths, so the picker has to be backend-driven. This endpoint
returns the subdirectory list at a given absolute path, plus the parent
path for "go up" navigation. The frontend renders a breadcrumb + clickable
list around it (`FolderBrowser.jsx`).

GET /api/browse?path=<absolute-or-UNC-path>
"""
from __future__ import annotations

import string
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

router = APIRouter()

# Names we never want in the picker — pure noise on Windows.
_HIDDEN_PREFIXES = (".",)
_HIDDEN_EXACT = {"$RECYCLE.BIN", "System Volume Information"}


def _list_drives() -> list[dict]:
    """Synthetic root: every Windows drive letter that's currently mounted.
    Used when `path` is empty so the picker has a sensible starting point."""
    drives: list[dict] = []
    for letter in string.ascii_uppercase:
        root = Path(f"{letter}:\\")
        try:
            if root.exists():
                drives.append({"name": f"{letter}:\\", "is_dir": True})
        except OSError:
            continue
    return drives


def _is_hidden(name: str) -> bool:
    if name in _HIDDEN_EXACT:
        return True
    return any(name.startswith(p) for p in _HIDDEN_PREFIXES)


@router.get("/browse")
def browse(path: str = Query("", description="Absolute or UNC path; empty = drives")):
    """List directories under `path`. Files are filtered out — this is a
    folder picker. Hidden entries (dot-prefixed, $RECYCLE.BIN, System
    Volume Information) are dropped. Sorted case-insensitive.

    Empty/missing path → synthetic root with drive letters. Non-existent
    path → exists=false, entries=[] (the UI handles it inline).
    """
    # Reject path-traversal shenanigans. The LAN-tool threat model already
    # trusts the user, but `..` in a path is almost always a bug, not a
    # feature here — the UI navigates via the parent field, not by string.
    if ".." in path.replace("/", "\\").split("\\"):
        raise HTTPException(400, "path must not contain '..'")
    if path.startswith("~"):
        raise HTTPException(400, "path must be absolute (no '~' shortcuts)")

    raw = path.strip()
    if not raw:
        # Synthetic root: drive letters.
        return {
            "path": "",
            "parent": None,
            "exists": True,
            "is_dir": True,
            "entries": _list_drives(),
        }

    p = Path(raw)
    if not p.exists():
        # Best-guess parent so the UI can still let the user go up.
        try:
            parent = str(p.parent) if str(p.parent) != str(p) else None
        except Exception:  # noqa: BLE001
            parent = None
        return {
            "path": raw,
            "parent": parent,
            "exists": False,
            "is_dir": False,
            "entries": [],
        }

    if not p.is_dir():
        # User pointed at a file. Same shape as missing — UI just shows nothing.
        return {
            "path": raw,
            "parent": str(p.parent),
            "exists": True,
            "is_dir": False,
            "entries": [],
        }

    entries: list[dict] = []
    try:
        for child in p.iterdir():
            try:
                if not child.is_dir():
                    continue
            except OSError:
                # Some pseudo-entries on UNC shares raise on stat. Skip them.
                continue
            if _is_hidden(child.name):
                continue
            entries.append({"name": child.name, "is_dir": True})
    except PermissionError:
        # Surface as an empty listing rather than 500 — the UI can warn.
        return {
            "path": str(p),
            "parent": str(p.parent) if p.parent != p else None,
            "exists": True,
            "is_dir": True,
            "entries": [],
            "permission_denied": True,
        }
    entries.sort(key=lambda e: e["name"].lower())

    parent_path: str | None = str(p.parent)
    if parent_path == str(p):
        # Drive root (`C:\`.parent == `C:\` on Windows). Use empty so the UI
        # navigates back to the drive-letter list.
        parent_path = ""

    return {
        "path": str(p),
        "parent": parent_path,
        "exists": True,
        "is_dir": True,
        "entries": entries,
    }
