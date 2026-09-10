"""Read-only export simulation for Phase B validation Tests 3 & 4.

Re-uses the production export PLANNING code paths from app/api/jobs.py
(_best_role_map, _build_rename_plan_for_session, and the legacy-mode
inner loop) WITHOUT any file writes or DB mutations.

Scoped to the two sessions involved in the Phase B copy test:
  - Source session: 9U Black Travel Team (id=664), source cluster 6584
  - Target session: American Legion  (id=666), copy cluster 6825

Test 3: confirm the copy's images appear in the TARGET team's planned
output (BOTH rename and legacy modes), AND that the source still
contains its originals.

Test 4: simulate rejecting one source image — modify the role_map in
memory ONLY (no DB write) — and confirm the rejection excludes it from
the source plan but does NOT touch the target's copy of the photo
(different image_id, independent role_map entry).
"""
from pathlib import Path
from sqlalchemy.orm import Session as DbSession

from app.db import SessionLocal
from app.models.db_models import Cluster, Image, ImageRole, Session as DbSess
from app.api.jobs import (
    _best_role_map, _build_rename_plan_for_session,
)


SOURCE_SESSION_ID = 664
TARGET_SESSION_ID = 666
SOURCE_CLUSTER_ID = 6584
TARGET_CLUSTER_ID = 6825   # the copy


def _legacy_plan(session: DbSess, role_map: dict[int, str]) -> list[dict]:
    """Mirror jobs.py:1268-1286 (the rename_by_player=False branch).
    Produces one entry per image, secondary based on role tag. We don't
    care about path uniqueness/_allocate_dests for the simulation — just
    inclusion + role + filename."""
    plan = []
    for image in session.images:
        role = role_map.get(image.id)
        if role == "rejected":
            plan.append({
                "image_id": image.id, "filename": image.filename,
                "role": role, "included": False, "reason": "rejected",
            })
            continue
        if role is None:
            role = "individual"   # Issue 5 orphan default
        plan.append({
            "image_id": image.id, "filename": image.filename,
            "role": role, "included": True,
            "primary_dir": "To_be_Cropped",
            "secondary_dir": (
                "Team Images" if role == "team" else
                "Pano Images" if role == "panoramic" else None
            ),
        })
    return plan


def _rename_plan(db: DbSession, session: DbSess, role_map: dict[int, str]) -> list[dict]:
    raw = _build_rename_plan_for_session(db, session, role_map)
    return [
        {
            "src_path": Path(src).name,    # filename only for display
            "basename": basename,
            "secondary_dir": secondary,
            "image_id": image_id,
        }
        for src, basename, secondary, image_id in raw
    ]


def _summarize(plan: list[dict], session_name: str, mode: str):
    print(f"\n  -- {mode.upper()} mode, session={session_name} --")
    included = [p for p in plan if mode == "rename" or p.get("included", True)]
    excluded = [p for p in plan if mode == "legacy" and not p.get("included", True)]
    print(f"  {len(included)} included, {len(excluded)} excluded")
    for p in plan:
        if mode == "legacy":
            mark = "[+]" if p.get("included") else "[-]"
            sec = p.get("secondary_dir") or "--"
            print(f"    {mark}  img_id={p['image_id']:>6}  role={p['role']:10}  "
                  f"sec={sec:12}  filename={p['filename']}")
        else:
            sec = p.get("secondary_dir") or "--"
            print(f"    [+]  {p['src_path']:25}  ->  {p['basename']:35}  sec={sec}")


def run():
    db = SessionLocal()
    try:
        src_sess = db.query(DbSess).get(SOURCE_SESSION_ID)
        tgt_sess = db.query(DbSess).get(TARGET_SESSION_ID)
        if src_sess is None or tgt_sess is None:
            print(f"ERROR: session not found (src={src_sess}, tgt={tgt_sess})")
            return

        print(f"Source session: {src_sess.name!r} (id={SOURCE_SESSION_ID})")
        print(f"Target session: {tgt_sess.name!r} (id={TARGET_SESSION_ID})")

        # Identify the two clusters' image_ids via ImageRole (load-bearing
        # post-Phase-B membership relation — what export already uses).
        src_image_ids = {r.image_id for r in
                         db.query(ImageRole).filter_by(cluster_id=SOURCE_CLUSTER_ID).all()}
        tgt_image_ids = {r.image_id for r in
                         db.query(ImageRole).filter_by(cluster_id=TARGET_CLUSTER_ID).all()}
        print(f"\nSource cluster {SOURCE_CLUSTER_ID} image_ids: {sorted(src_image_ids)}")
        print(f"Target cluster {TARGET_CLUSTER_ID} image_ids: {sorted(tgt_image_ids)}")
        print(f"Disjoint (option-beta image-row duplication): "
              f"{src_image_ids.isdisjoint(tgt_image_ids)}")

        # Same path values though (point-in-time snapshot):
        src_paths = {i.path for i in
                     db.query(Image).filter(Image.id.in_(src_image_ids)).all()}
        tgt_paths = {i.path for i in
                     db.query(Image).filter(Image.id.in_(tgt_image_ids)).all()}
        print(f"Source paths: {len(src_paths)}, target paths: {len(tgt_paths)}, "
              f"overlap: {len(src_paths & tgt_paths)}")

        # Build the same role_map the production export uses.
        all_ids = (
            [img.id for img in src_sess.images]
            + [img.id for img in tgt_sess.images]
        )
        role_map = _best_role_map(db, all_ids)

        # =================================================================
        # TEST 3 — copy lands in BOTH sessions, BOTH modes
        # =================================================================
        print("\n" + "=" * 78)
        print("TEST 3 — copy lands in BOTH sessions, BOTH modes (no rejection)")
        print("=" * 78)

        # Legacy mode: filter the per-session plans down to JUST the cluster's
        # image_ids so the output is readable. Both clusters' images should
        # be in their respective session's plan.
        for session, focus_ids, label, cluster_id in [
            (src_sess, src_image_ids, "SOURCE", SOURCE_CLUSTER_ID),
            (tgt_sess, tgt_image_ids, "TARGET (COPY)", TARGET_CLUSTER_ID),
        ]:
            plan = _legacy_plan(session, role_map)
            focused = [p for p in plan if p["image_id"] in focus_ids]
            _summarize(focused, f"{label} session={session.name} cluster={cluster_id}",
                       "legacy")

        # Rename mode is per-session, per-cluster — _build_rename_plan_for_session
        # returns the WHOLE session, but the basenames give away which cluster
        # produced each entry (display_label-derived). We'll filter the rename
        # plan to lines whose src_path matches one of our cluster's image paths.
        for session, focus_ids, label, cluster_id in [
            (src_sess, src_image_ids, "SOURCE", SOURCE_CLUSTER_ID),
            (tgt_sess, tgt_image_ids, "TARGET (COPY)", TARGET_CLUSTER_ID),
        ]:
            focus_filenames = {
                db.query(Image).get(iid).filename for iid in focus_ids
            }
            full_plan = _rename_plan(db, session, role_map)
            focused = [p for p in full_plan if p["src_path"] in focus_filenames]
            _summarize(focused, f"{label} session={session.name} cluster={cluster_id}",
                       "rename")

        # =================================================================
        # TEST 4 — independence: reject 1 source image, target keeps copy
        # =================================================================
        print("\n" + "=" * 78)
        print("TEST 4 — independence under reject (in-memory role_map only, NO DB writes)")
        print("=" * 78)

        # Pick one source image to reject and find its corresponding target copy.
        # Match by path (the point-in-time snapshot preserves paths).
        target_path_to_id = {
            db.query(Image).get(iid).path: iid for iid in tgt_image_ids
        }
        sample_src_id = sorted(src_image_ids)[0]
        sample_src = db.query(Image).get(sample_src_id)
        sample_tgt_id = target_path_to_id.get(sample_src.path)
        print(f"Will reject source image_id={sample_src_id}  path={Path(sample_src.path).name}")
        print(f"Its target-side copy: image_id={sample_tgt_id}  (same path, different id)")

        # Simulate the rejection by mutating role_map in-memory ONLY.
        simulated_role_map = dict(role_map)
        simulated_role_map[sample_src_id] = "rejected"
        # NB: we do NOT set simulated_role_map[sample_tgt_id]. That's the
        # whole point — different image_ids → independent role_map entries.

        print("\n  EXPECTED: source plan EXCLUDES image_id={}; target plan "
              "STILL INCLUDES image_id={}".format(sample_src_id, sample_tgt_id))

        # Re-run BOTH modes for BOTH sessions with the simulated rejection.
        for session, focus_ids, label, cluster_id in [
            (src_sess, src_image_ids, "SOURCE", SOURCE_CLUSTER_ID),
            (tgt_sess, tgt_image_ids, "TARGET (COPY)", TARGET_CLUSTER_ID),
        ]:
            plan = _legacy_plan(session, simulated_role_map)
            focused = [p for p in plan if p["image_id"] in focus_ids]
            _summarize(focused, f"{label} session={session.name} cluster={cluster_id}",
                       "legacy")

        for session, focus_ids, label, cluster_id in [
            (src_sess, src_image_ids, "SOURCE", SOURCE_CLUSTER_ID),
            (tgt_sess, tgt_image_ids, "TARGET (COPY)", TARGET_CLUSTER_ID),
        ]:
            focus_filenames = {
                db.query(Image).get(iid).filename for iid in focus_ids
            }
            full_plan = _rename_plan(db, session, simulated_role_map)
            focused = [p for p in full_plan if p["src_path"] in focus_filenames]
            _summarize(focused, f"{label} session={session.name} cluster={cluster_id}",
                       "rename")

        # =================================================================
        # VERDICT
        # =================================================================
        print("\n" + "=" * 78)
        print("VERDICT")
        print("=" * 78)
        # Test 3
        plan_src_legacy_t3 = _legacy_plan(src_sess, role_map)
        plan_tgt_legacy_t3 = _legacy_plan(tgt_sess, role_map)
        src_legacy_inc = {p["image_id"] for p in plan_src_legacy_t3 if p.get("included")}
        tgt_legacy_inc = {p["image_id"] for p in plan_tgt_legacy_t3 if p.get("included")}
        t3_legacy_src = src_image_ids.issubset(src_legacy_inc)
        t3_legacy_tgt = tgt_image_ids.issubset(tgt_legacy_inc)

        plan_src_rename_t3 = _rename_plan(db, src_sess, role_map)
        plan_tgt_rename_t3 = _rename_plan(db, tgt_sess, role_map)
        src_rename_filenames = {p["src_path"] for p in plan_src_rename_t3}
        tgt_rename_filenames = {p["src_path"] for p in plan_tgt_rename_t3}
        src_focus_fn = {db.query(Image).get(i).filename for i in src_image_ids}
        tgt_focus_fn = {db.query(Image).get(i).filename for i in tgt_image_ids}
        t3_rename_src = src_focus_fn.issubset(src_rename_filenames)
        t3_rename_tgt = tgt_focus_fn.issubset(tgt_rename_filenames)

        print(f"  Test 3 legacy — source has all {len(src_image_ids)} cluster images: "
              f"{'PASS' if t3_legacy_src else 'FAIL'}")
        print(f"  Test 3 legacy — target has all {len(tgt_image_ids)} cluster images: "
              f"{'PASS' if t3_legacy_tgt else 'FAIL'}")
        print(f"  Test 3 rename — source has all {len(src_image_ids)} cluster filenames: "
              f"{'PASS' if t3_rename_src else 'FAIL'}")
        print(f"  Test 3 rename — target has all {len(tgt_image_ids)} cluster filenames: "
              f"{'PASS' if t3_rename_tgt else 'FAIL'}")

        # Test 4
        plan_src_legacy_t4 = _legacy_plan(src_sess, simulated_role_map)
        plan_tgt_legacy_t4 = _legacy_plan(tgt_sess, simulated_role_map)
        src_inc_t4 = {p["image_id"] for p in plan_src_legacy_t4 if p.get("included")}
        tgt_inc_t4 = {p["image_id"] for p in plan_tgt_legacy_t4 if p.get("included")}
        t4_source_excludes = sample_src_id not in src_inc_t4
        t4_target_includes = sample_tgt_id in tgt_inc_t4

        plan_src_rename_t4 = _rename_plan(db, src_sess, simulated_role_map)
        plan_tgt_rename_t4 = _rename_plan(db, tgt_sess, simulated_role_map)
        src_fn_t4 = {p["src_path"] for p in plan_src_rename_t4}
        tgt_fn_t4 = {p["src_path"] for p in plan_tgt_rename_t4}
        rejected_filename = sample_src.filename
        t4_rename_src_excludes = rejected_filename not in src_fn_t4
        t4_rename_tgt_includes = rejected_filename in tgt_fn_t4

        print(f"  Test 4 legacy — source EXCLUDES rejected image_id={sample_src_id}: "
              f"{'PASS' if t4_source_excludes else 'FAIL'}")
        print(f"  Test 4 legacy — target STILL INCLUDES copy image_id={sample_tgt_id}: "
              f"{'PASS' if t4_target_includes else 'FAIL'}")
        print(f"  Test 4 rename — source EXCLUDES rejected filename: "
              f"{'PASS' if t4_rename_src_excludes else 'FAIL'}")
        print(f"  Test 4 rename — target STILL INCLUDES same filename via copy: "
              f"{'PASS' if t4_rename_tgt_includes else 'FAIL'}")

        all_pass = all([
            t3_legacy_src, t3_legacy_tgt, t3_rename_src, t3_rename_tgt,
            t4_source_excludes, t4_target_includes,
            t4_rename_src_excludes, t4_rename_tgt_includes,
        ])
        print(f"\n  OVERALL: {'ALL PASS' if all_pass else 'FAILED — see above'}")

    finally:
        db.close()


if __name__ == "__main__":
    run()
