"""Dry-run simulator for the 2026-09-08 buddy-split export toggle.

Read-only. NO file I/O, NO DB writes. Reuses the production planning
primitives from app/api/jobs.py so the counts match what _run_export would
actually do:

  - _best_role_map        (same rejected-wins rule)
  - _face_count_map       (same >=2-face definition)
  - _goes_to_buddies      (same role-gated partition decision)
  - _build_rename_plan_for_session  (same rename-mode plan)

For a given job_id, runs the partition twice — with split_buddies OFF and
ON — for both legacy and rename modes, then prints per-session and total
counts of what would land in each output tree. Also prints a few sample
filenames per bucket for eyeball sanity-check.

Usage (from backend/):
    .venv/Scripts/python.exe simulate_buddy_split_export.py <job_id>
    .venv/Scripts/python.exe simulate_buddy_split_export.py <job_id> --rename
"""
import sys
from collections import Counter
from pathlib import Path

from app.db import SessionLocal
from app.models.db_models import Image, Job
from app.api.jobs import (
    _best_role_map, _build_rename_plan_for_session, _exportable_sessions,
    _face_count_map, _goes_to_buddies,
)


def _plan_session(db, session, role_map, face_count_map, *,
                  rename_by_player: bool, split_buddies: bool):
    """Mirror the srcs/roles/image_ids assembly + buddy-split partition
    that _run_export does per session. Returns a list of dicts:
      {image_id, filename, role, face_count, primary_tree, secondary_tree}
    where primary_tree is 'To_be_Cropped' or 'Buddies' and secondary_tree
    is 'Team Images' | 'Pano Images' | None. Rejected images become
    {primary_tree: None, reason: 'rejected'}.
    """
    entries = []

    if rename_by_player:
        plan = _build_rename_plan_for_session(db, session, role_map)
        for src, basename, secondary_subdir, image_id in plan:
            role = (
                "team" if secondary_subdir == "Team Images"
                else "panoramic" if secondary_subdir == "Pano Images"
                else "individual"
            )
            fc = face_count_map.get(image_id, 0)
            to_buddies = _goes_to_buddies(role, fc, split_buddies)
            entries.append({
                "image_id": image_id,
                "filename": Path(src).name,
                "basename": basename,
                "role": role,
                "face_count": fc,
                "primary_tree": "Buddies" if to_buddies else "To_be_Cropped",
                "secondary_tree": secondary_subdir,
            })
    else:
        for image in session.images:
            role = role_map.get(image.id)
            if role == "rejected":
                entries.append({
                    "image_id": image.id,
                    "filename": image.filename,
                    "role": "rejected",
                    "primary_tree": None,
                    "reason": "rejected",
                })
                continue
            if role is None:
                role = "individual"
            fc = face_count_map.get(image.id, 0)
            to_buddies = _goes_to_buddies(role, fc, split_buddies)
            secondary = (
                "Team Images" if role == "team"
                else "Pano Images" if role == "panoramic"
                else None
            )
            entries.append({
                "image_id": image.id,
                "filename": image.filename,
                "role": role,
                "face_count": fc,
                "primary_tree": "Buddies" if to_buddies else "To_be_Cropped",
                "secondary_tree": secondary,
            })
    return entries


def _summarize(entries):
    """Aggregate bucket counts + a few sample filenames per bucket."""
    prim = Counter()
    sec = Counter()
    rejected = 0
    samples = {"To_be_Cropped": [], "Buddies": [],
               "Team Images": [], "Pano Images": []}
    for e in entries:
        if e["primary_tree"] is None:
            rejected += 1
            continue
        prim[e["primary_tree"]] += 1
        if len(samples[e["primary_tree"]]) < 3:
            samples[e["primary_tree"]].append(
                f'{e["filename"]}  (role={e["role"]}, fc={e["face_count"]})'
            )
        if e.get("secondary_tree"):
            sec[e["secondary_tree"]] += 1
            if len(samples[e["secondary_tree"]]) < 3:
                samples[e["secondary_tree"]].append(e["filename"])
    return prim, sec, rejected, samples


def _print_bucket_report(label, entries):
    prim, sec, rejected, samples = _summarize(entries)
    total_primary = sum(prim.values())
    print(f"    {label}")
    print(f"      Primary  (one copy per image):")
    print(f"        To_be_Cropped/  = {prim.get('To_be_Cropped', 0)}")
    print(f"        Buddies/        = {prim.get('Buddies', 0)}")
    print(f"        (total primary  = {total_primary})")
    print(f"      Secondary (extra copies for team/pano):")
    print(f"        Team Images/    = {sec.get('Team Images', 0)}")
    print(f"        Pano Images/    = {sec.get('Pano Images', 0)}")
    print(f"      Rejected skipped  = {rejected}")
    for bucket, lines in samples.items():
        if lines:
            print(f"      sample -> {bucket}/:")
            for ln in lines:
                print(f"           {ln}")


def _run_one_mode(db, to_export, role_map, face_count_map, *,
                  rename_by_player: bool, split_buddies: bool, mode_label: str):
    print("-" * 78)
    print(f"MODE: {mode_label}")
    print("-" * 78)
    total_prim = Counter()
    total_sec = Counter()
    total_rejected = 0
    per_session_lines = []
    for session in to_export:
        entries = _plan_session(
            db, session, role_map, face_count_map,
            rename_by_player=rename_by_player,
            split_buddies=split_buddies,
        )
        prim, sec, rej, _ = _summarize(entries)
        total_prim.update(prim)
        total_sec.update(sec)
        total_rejected += rej
        per_session_lines.append(
            f"    {session.name:40}"
            f"  TBC={prim.get('To_be_Cropped', 0):>4}"
            f"  Bud={prim.get('Buddies', 0):>4}"
            f"  T={sec.get('Team Images', 0):>3}"
            f"  P={sec.get('Pano Images', 0):>3}"
            f"  rej={rej:>3}"
        )
    print("  PER SESSION:")
    for ln in per_session_lines:
        print(ln)
    print()
    print("  TOTAL for this mode:")
    print(f"    To_be_Cropped/  = {total_prim.get('To_be_Cropped', 0)}")
    print(f"    Buddies/        = {total_prim.get('Buddies', 0)}")
    print(f"    Team Images/    = {total_sec.get('Team Images', 0)}")
    print(f"    Pano Images/    = {total_sec.get('Pano Images', 0)}")
    print(f"    Rejected skipped = {total_rejected}")
    print()
    return total_prim, total_sec, total_rejected


def run(job_id: int, rename_by_player: bool):
    db = SessionLocal()
    try:
        job = db.query(Job).get(job_id)
        if job is None:
            print(f"ERROR: job {job_id} not found")
            return

        print("=" * 78)
        print(f"JOB {job_id}: {job.name}")
        print("=" * 78)

        to_export, skipped = _exportable_sessions(job)
        print(f"Exportable sessions: {len(to_export)}"
              f"  (skipped: {len(skipped)})")
        for s in skipped:
            print(f"  skipped: {s}")

        if not to_export:
            print("No exportable sessions.")
            return

        # Match the production hoist: one role_map + one face_count_map
        # for the whole job.
        all_ids = [img.id for s in to_export for img in s.images]
        role_map = _best_role_map(db, all_ids)
        face_count_map = _face_count_map(db, all_ids)

        print(f"Images across exportable sessions: {len(all_ids)}")
        rejected_count = sum(1 for iid in all_ids if role_map.get(iid) == "rejected")
        print(f"Globally rejected (would skip): {rejected_count}")
        multi_face = sum(1 for c in face_count_map.values() if c >= 2)
        print(f"Images with >=2 detected faces: {multi_face}")
        print()

        # Sample a few known individual/buddy roles that would flip.
        would_flip = [
            iid for iid in all_ids
            if role_map.get(iid) in ("individual", "buddy", None)
            and face_count_map.get(iid, 0) >= 2
        ]
        # Cross-check with None role — orphans default to individual in the
        # legacy path, so they DO flip when face_count >=2. Confirm both
        # modes report the same total count for the same job.
        print(f"Individual/buddy-role images with fc>=2 (buddy candidates): "
              f"{len(would_flip)}")
        print()

        label_mode = "rename-by-player" if rename_by_player else "legacy"
        _run_one_mode(
            db, to_export, role_map, face_count_map,
            rename_by_player=rename_by_player, split_buddies=False,
            mode_label=f"{label_mode}, split_buddies=OFF (control — should match today)",
        )
        _run_one_mode(
            db, to_export, role_map, face_count_map,
            rename_by_player=rename_by_player, split_buddies=True,
            mode_label=f"{label_mode}, split_buddies=ON",
        )

        # Delta report — the whole point of the sanity-check.
        print("=" * 78)
        print("DELTA (ON vs OFF, same mode):")
        print("=" * 78)
        off_prim = Counter()
        on_prim = Counter()
        for session in to_export:
            off_entries = _plan_session(db, session, role_map, face_count_map,
                                        rename_by_player=rename_by_player,
                                        split_buddies=False)
            on_entries = _plan_session(db, session, role_map, face_count_map,
                                       rename_by_player=rename_by_player,
                                       split_buddies=True)
            for e in off_entries:
                if e["primary_tree"]:
                    off_prim[e["primary_tree"]] += 1
            for e in on_entries:
                if e["primary_tree"]:
                    on_prim[e["primary_tree"]] += 1

        print(f"  To_be_Cropped: {off_prim.get('To_be_Cropped', 0)}  ->  "
              f"{on_prim.get('To_be_Cropped', 0)}  "
              f"(diff {on_prim.get('To_be_Cropped', 0) - off_prim.get('To_be_Cropped', 0):+d})")
        print(f"  Buddies:       {off_prim.get('Buddies', 0)}  ->  "
              f"{on_prim.get('Buddies', 0)}  "
              f"(diff {on_prim.get('Buddies', 0) - off_prim.get('Buddies', 0):+d})")

        # Invariants for the reader's confidence.
        print()
        print("Invariants:")
        off_total = sum(off_prim.values())
        on_total = sum(on_prim.values())
        print(f"  primary-copy total unchanged ON vs OFF:  "
              f"{off_total} vs {on_total}  "
              f"-> {'OK' if off_total == on_total else 'MISMATCH'}")
        # OFF must land nothing in Buddies (byte-identical guarantee).
        buddy_leak_off = off_prim.get("Buddies", 0)
        print(f"  Buddies/ empty when toggle OFF:          "
              f"{buddy_leak_off}  -> {'OK' if buddy_leak_off == 0 else 'LEAK'}")
        # No image appears in both trees simultaneously (each image has
        # exactly one primary_tree in each plan).
        print(f"  no image duplicated across trees (single primary per image "
              f"per plan by construction): OK")

    finally:
        db.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python simulate_buddy_split_export.py <job_id> [--rename]")
        sys.exit(2)
    job_id = int(sys.argv[1])
    rename = "--rename" in sys.argv[2:]
    run(job_id, rename_by_player=rename)
