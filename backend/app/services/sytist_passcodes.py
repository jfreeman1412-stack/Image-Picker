"""Per-job Sytist passcodes (2026-10-07).

When a job has `Job.sytist_passcodes` on, every roster player gets a passcode
for the shoot and the export writes a CSV for Sytist's "import passcodes"
screen in a Preset Passcode Photos gallery. That import (verified against
Sytist 5.8.7, sl-admin/qr/import-non-qr-passcodes.php) works one row per
photo:

  - FILENAME is matched exactly against the stored photo name in the gallery
    and the photo's title becomes the row's PASSCODE. The photos must already
    be uploaded when the CSV is imported.
  - A row with IS_GROUP set makes that photo a group photo whose code is its
    own file name.
  - GROUPS on a kid's row lists the group photos ("; "-separated) the kid also
    sees, e.g. the team composite and panoramic.

Sytist stores uploaded names with spaces turned into underscores, so every
file name written here gets the same treatment. Team composites are built
outside Player Sort and always named `<Team>.jpg` / `<Team>-PANO.jpg` after
the roster's team name, so their rows are written from the roster alone.
"""
from __future__ import annotations

import csv
import json
import re
import secrets
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session as DbSession

from app.models.db_models import Cluster, Job, PlayerMembership, Session
from app.services.roster import normalize_name

# Sytist-style codes: 7 characters, uppercase, no look-alikes (0/O, 1/I/L).
PASSCODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
PASSCODE_LENGTH = 7

CSV_SUFFIX = "_sytist_passcodes.csv"
CSV_HEADER = [
    "FILENAME", "PASSCODE", "SUBJECT_FIRST_NAME", "SUBJECT_LAST_NAME",
    "FIRST_NAME", "LAST_NAME", "EMAIL", "PHONE", "LEADER", "GROUPS", "IS_GROUP",
]
TEAM_PHOTO_EXT = ".jpg"


def generate_passcode(taken: set[str]) -> str:
    while True:
        code = "".join(secrets.choice(PASSCODE_ALPHABET) for _ in range(PASSCODE_LENGTH))
        if code not in taken:
            return code


def family_key_email(email: str | None) -> str | None:
    e = (email or "").strip().lower()
    return f"e:{e}" if "@" in e else None


def family_key_phone(phone: str | None) -> str | None:
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return f"p:{digits}" if len(digits) >= 7 else None


def _family_groups(memberships: list[PlayerMembership]) -> list[list[PlayerMembership]]:
    """Group memberships into families: the same player (on any team), or
    kids sharing a parent email or phone. Groups keep roster order."""
    parent: dict[int, int] = {}

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    first_by_key: dict[str, int] = {}
    for i, m in enumerate(memberships):
        parent[i] = i
        keys = [f"player:{m.player_id}"]
        if not m.is_coach:
            keys += [k for k in (family_key_email(m.parent_email),
                                 family_key_phone(m.parent_phone)) if k]
        for k in keys:
            if k in first_by_key:
                union(first_by_key[k], i)
            else:
                first_by_key[k] = i
    groups: dict[int, list[PlayerMembership]] = {}
    for i, m in enumerate(memberships):
        groups.setdefault(find(i), []).append(m)
    return list(groups.values())


def ensure_job_passcodes(db: DbSession, job_id: int) -> int:
    """Give every membership of this job a passcode. A player on several
    teams in the same shoot shares one code. With the job's family option on
    (the default), siblings sharing a parent email or phone share one code
    too. Codes already sent to Sytist (`passcode_locked`: in an exported CSV
    or from the roster upload) never change; an unsent code can still move to
    the family's code when contact arrives later (e.g. a Sytist sync).
    Coaches are never merged by contact. Returns how many memberships got a
    new or changed code. Caller commits."""
    job = db.query(Job).get(job_id)
    family = bool(job is None or job.sytist_family_passcodes is None
                  or job.sytist_family_passcodes)
    memberships = (
        db.query(PlayerMembership).filter_by(job_id=job_id)
        .order_by(PlayerMembership.id.asc()).all()
    )
    taken = {m.passcode for m in memberships if m.passcode}
    if family:
        groups = _family_groups(memberships)
    else:
        by_player: dict[int, list[PlayerMembership]] = {}
        for m in memberships:
            by_player.setdefault(m.player_id, []).append(m)
        groups = list(by_player.values())
    assigned = 0
    for group in groups:
        locked = [m.passcode for m in group if m.passcode and m.passcode_locked]
        existing = [m.passcode for m in group if m.passcode]
        code = (locked or existing or [None])[0]
        if code is None:
            code = generate_passcode(taken)
            taken.add(code)
        for m in group:
            if m.passcode == code:
                continue
            if m.passcode and (m.passcode_locked or not family):
                continue
            m.passcode = code
            assigned += 1
    db.flush()
    return assigned


def lock_job_passcodes(db: DbSession, job_id: int) -> None:
    """Mark this job's codes as sent to Sytist so they never change again."""
    db.query(PlayerMembership).filter(
        PlayerMembership.job_id == job_id,
        PlayerMembership.passcode.isnot(None),
    ).update({PlayerMembership.passcode_locked: 1}, synchronize_session=False)


def csv_filename(job_name: str) -> str:
    """The import CSV is named after the job so several jobs' CSVs never get
    mixed up, e.g. 'Spring Soccer' -> 'Spring_Soccer_sytist_passcodes.csv'."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", job_name or "").strip("._")
    return f"{safe or 'job'}{CSV_SUFFIX}"


def sytist_name(name: str) -> str:
    """The file name as Sytist stores it after upload."""
    return name.replace(" ", "_")


def team_photo_names(team_name: str) -> list[str]:
    base = sytist_name(team_name.strip())
    return [f"{base}{TEAM_PHOTO_EXT}", f"{base}-PANO{TEAM_PHOTO_EXT}"]


def _split_name(display_name: str) -> tuple[str, str]:
    """Best-effort first/last from a roster name like 'Eleanor Pederson' or
    the copyright style 'Eleanor-Pederson'."""
    name = display_name.strip()
    if " " in name:
        first, _, last = name.rpartition(" ")
        return first.strip(), last.strip()
    if "-" in name:
        first, _, last = name.partition("-")
        return first.strip(), last.strip()
    return name, ""


class MembershipResolver:
    """Map a cluster to the roster membership whose passcode its photos get.

    The cluster's current label decides (so renaming or moving a player in the
    review UI changes the passcode on the next export); a reference-photo
    match is the fallback when the label isn't a roster name. When the same
    name is on several teams, the membership for the cluster's own team wins.
    """

    def __init__(self, db: DbSession, job_id: int):
        self.memberships = (
            db.query(PlayerMembership).filter_by(job_id=job_id)
            .order_by(PlayerMembership.id.asc()).all()
        )
        self.by_norm_name: dict[str, list[PlayerMembership]] = {}
        self.by_player: dict[int, list[PlayerMembership]] = {}
        for m in self.memberships:
            self.by_norm_name.setdefault(m.player.norm_name, []).append(m)
            self.by_player.setdefault(m.player_id, []).append(m)

    @staticmethod
    def _pick(candidates: list[PlayerMembership], session: Session):
        team_key = normalize_name(session.roster_team_alias or session.name or "")
        for m in candidates:
            if m.norm_team == team_key:
                return m
        return candidates[0]

    def resolve(self, cluster: Cluster, session: Session) -> PlayerMembership | None:
        label = (cluster.manual_label or cluster.auto_label or "").strip()
        if label:
            hits = self.by_norm_name.get(normalize_name(label))
            if hits:
                return self._pick(hits, session)
        if cluster.matched_player_id and not cluster.manual_label:
            hits = self.by_player.get(cluster.matched_player_id)
            if hits:
                return self._pick(hits, session)
        return None


@dataclass
class SytistCsvBuilder:
    """Collects exported files during an export and writes the import CSV.

    `add_file(name, memberships)`: one exported file and the roster players
    shown in it. One player → a row with their passcode. Two or more (a buddy
    shot exported once) → a group photo listed in each player's GROUPS. None →
    counted as unassigned and left out of the CSV.
    """
    db: DbSession
    job_id: int
    upload_ext: str | None = None
    _photo_rows: list[tuple[str, PlayerMembership]] = field(default_factory=list)
    _buddy_groups: dict[int, list[str]] = field(default_factory=dict)
    _group_files: list[str] = field(default_factory=list)
    unassigned: list[str] = field(default_factory=list)
    _manifest: list[list] = field(default_factory=list)
    # Players with no roster team (booking-calendar sign-ups) take the team
    # of the folder their photos were sorted into: player_id -> [teams].
    _folder_teams: dict[int, list[str]] = field(default_factory=dict)

    def upload_name(self, exported_name: str) -> str:
        if self.upload_ext:
            exported_name = Path(exported_name).stem + self.upload_ext
        return sytist_name(exported_name)

    def add_file(self, exported_name: str, memberships, team: str | None = None) -> None:
        name = self.upload_name(exported_name)
        by_player: dict[int, PlayerMembership] = {}
        for m in memberships:
            if m is not None:
                by_player.setdefault(m.player_id, m)
        self._manifest.append([exported_name, sorted(by_player), team])
        if team and team.strip():
            for pid, m in by_player.items():
                if not (m.team_name or "").strip():
                    teams = self._folder_teams.setdefault(pid, [])
                    if team not in teams:
                        teams.append(team)
        if not by_player:
            self.unassigned.append(name)
        elif len(by_player) == 1:
            self._photo_rows.append((name, next(iter(by_player.values()))))
        else:
            if name not in self._group_files:
                self._group_files.append(name)
            for pid in by_player:
                self._buddy_groups.setdefault(pid, []).append(name)

    def manifest_json(self) -> str:
        """Which players each exported file shows, saved on the job so the
        CSV can be rebuilt later from the final (cropped) folder."""
        return json.dumps({"files": self._manifest})

    def write(self, out_dir: Path, job_name: str) -> dict:
        return self.write_to(out_dir / csv_filename(job_name))

    def write_to(self, path: Path) -> dict:
        memberships = (
            self.db.query(PlayerMembership).filter_by(job_id=self.job_id)
            .order_by(PlayerMembership.id.asc()).all()
        )
        teams_by_player: dict[int, list[str]] = {}
        team_names: list[str] = []
        first_membership: dict[int, PlayerMembership] = {}
        for m in memberships:
            first_membership.setdefault(m.player_id, m)
            teams_by_player.setdefault(m.player_id, [])
            # A blank roster team (no team on the sign-up) is filled from the
            # photo folders below; it never makes a team photo of its own.
            teams = [m.team_name] if (m.team_name or "").strip() \
                else self._folder_teams.get(m.player_id, [])
            for team in teams:
                if team not in teams_by_player[m.player_id]:
                    teams_by_player[m.player_id].append(team)
                if team not in team_names:
                    team_names.append(team)

        # Sytist builds one roster entry per passcode from the first row it
        # reads, so every row of a code carries the whole family's teams and
        # group photos (siblings sharing a family code, a kid on two teams).
        players_by_code: dict[str, list[int]] = {}
        for pid, m in first_membership.items():
            if m.passcode:
                players_by_code.setdefault(m.passcode, []).append(pid)

        def family_of(m: PlayerMembership) -> list[int]:
            return players_by_code.get(m.passcode, [m.player_id]) if m.passcode \
                else [m.player_id]

        def teams_for(m: PlayerMembership) -> list[str]:
            teams: list[str] = []
            for pid in family_of(m):
                teams.extend(teams_by_player.get(pid, []))
            return list(dict.fromkeys(teams))

        def groups_for(m: PlayerMembership) -> str:
            names: list[str] = []
            for team in teams_for(m):
                names.extend(team_photo_names(team))
            for pid in family_of(m):
                names.extend(self._buddy_groups.get(pid, []))
            return "; ".join(dict.fromkeys(names))

        def person_row(filename: str, m: PlayerMembership) -> list[str]:
            # Contact goes on every row: Sytist builds the roster entry from
            # whichever row of a passcode it reads first.
            first = m.subject_first_name or ""
            last = m.subject_last_name or ""
            if not first and not last:
                first, last = _split_name(m.player.display_name)
            return [
                filename, m.passcode or "", first, last,
                m.parent_first_name or "", m.parent_last_name or "",
                m.parent_email or "", m.parent_phone or "",
                "; ".join(teams_for(m)),
                groups_for(m), "",
            ]

        rows: list[list[str]] = []
        players_with_photos: set[int] = set()
        for filename, m in self._photo_rows:
            canonical = first_membership.get(m.player_id, m)
            rows.append(person_row(filename, canonical))
            players_with_photos.add(m.player_id)
        # Players with no exported photo still get a roster entry so their
        # family receives a code (and sees team photos).
        no_photo_players = 0
        for pid, m in first_membership.items():
            if pid not in players_with_photos:
                rows.append(person_row("", m))
                no_photo_players += 1
        group_rows = 0
        for team in team_names:
            for name in team_photo_names(team):
                rows.append([name] + [""] * 9 + ["1"])
                group_rows += 1
        for name in self._group_files:
            rows.append([name] + [""] * 9 + ["1"])
            group_rows += 1

        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh, lineterminator="\n")
            writer.writerow(CSV_HEADER)
            writer.writerows(rows)
        lock_job_passcodes(self.db, self.job_id)
        # Sytist matches FILENAME across the whole gallery (sub-galleries
        # included), so two uploaded files with one name would get mixed up.
        uploaded = [n for n, _ in self._photo_rows] + self._group_files + self.unassigned
        for team in team_names:
            uploaded.extend(team_photo_names(team))
        counts = Counter(n.lower() for n in uploaded)
        duplicates = sorted({n for n in uploaded if counts[n.lower()] > 1}, key=str.lower)
        return {
            "path": str(path),
            "duplicate_names": len({n.lower() for n in duplicates}),
            "duplicate_examples": duplicates[:20],
            "players": len(first_membership),
            "photo_rows": len(self._photo_rows),
            "players_without_photos": no_photo_players,
            "group_photos": group_rows,
            "buddy_group_photos": len(self._group_files),
            "unassigned_files": len(self.unassigned),
            "unassigned_examples": self.unassigned[:20],
        }


# ── Build the CSV from the final folder (2026-10-08) ─────────────────────
# The export CSV names files as exported. After cropping, the files that go
# to Sytist can differ in type (cropped shots become PNG) or be culled, so
# this rebuilds the CSV from what's actually in the final folder: each file
# is matched to the last export by base name, whatever its extension.

IMAGE_EXTS = {".jpg", ".jpeg", ".png"}


def _stem_key(name: str) -> str:
    return sytist_name(Path(name).stem).lower()


def build_csv_from_folder(db: DbSession, job_id: int, folder: Path,
                          manifest_json: str | None) -> dict:
    if not manifest_json:
        raise ValueError("Export this job with Sytist passcodes on first.")
    try:
        manifest = json.loads(manifest_json).get("files") or []
    except (ValueError, TypeError, AttributeError):
        raise ValueError("The saved export list is unreadable. Export again.")
    if not folder.is_dir():
        raise FileNotFoundError(f"Folder not found: {folder}")

    by_stem: dict[str, tuple[list[int], str | None]] = {}
    for entry in manifest:
        exported_name, player_ids = entry[0], entry[1]
        team = entry[2] if len(entry) > 2 else None
        by_stem.setdefault(_stem_key(exported_name), (list(player_ids), team))

    first_membership: dict[int, PlayerMembership] = {}
    team_files: set[str] = set()
    for m in (db.query(PlayerMembership).filter_by(job_id=job_id)
              .order_by(PlayerMembership.id.asc()).all()):
        first_membership.setdefault(m.player_id, m)
        if (m.team_name or "").strip():
            team_files.update(n.lower() for n in team_photo_names(m.team_name))
    for _, (_, team) in by_stem.items():
        if team and team.strip():
            team_files.update(n.lower() for n in team_photo_names(team))

    builder = SytistCsvBuilder(db, job_id)
    found_stems: set[str] = set()
    team_found: set[str] = set()
    not_in_export: list[str] = []
    files = sorted(p for p in folder.rglob("*")
                   if p.is_file() and p.suffix.lower() in IMAGE_EXTS)
    for p in files:
        uploaded = sytist_name(p.name)
        if uploaded.lower() in team_files:
            team_found.add(uploaded.lower())
            continue
        key = _stem_key(p.name)
        if key not in by_stem:
            not_in_export.append(uploaded)
            continue
        found_stems.add(key)
        player_ids, team = by_stem[key]
        builder.add_file(p.name, [first_membership.get(pid) for pid in player_ids], team)

    job = db.query(Job).get(job_id)
    stats = builder.write_to(folder / csv_filename(job.name if job else ""))
    stats.update({
        "folder": str(folder),
        "image_files": len(files),
        "not_in_export": len(not_in_export),
        "not_in_export_examples": not_in_export[:20],
        "exported_not_in_folder": len(set(by_stem) - found_stems),
        "team_photos_in_folder": len(team_found),
        "team_photos_missing": sorted(team_files - team_found)[:40],
    })
    return stats
