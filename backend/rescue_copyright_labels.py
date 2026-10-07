"""One-off rescue: de-poison copyright/XMP cluster labels for a SINGLE job.

Why: with copyright present, the labeling stage puts the embedded copyright/XMP
string into Cluster.auto_label, and the matching stage's precedence
(cluster_matching.py:88-96) lets a present-but-conflicting copyright WIN over a
good face match. Evoto re-inserted studio XMP ("…SPORTSLINE PHOTOGRAPHY") on some
exports, so those clusters show the fragment + the read-time duplicate_auto_label
flag.

This does NOT re-detect, re-cluster, re-sort, or touch any image file, Face row,
ReferenceFace, ImageRole, manual_label, or session.reviewed. It only, for THIS job:
  1. nulls Image.copyright_tag (so nothing can re-derive the fragment),
  2. clears the poisoned Cluster.auto_label / auto_label_source (the "labeling"
     step — copyright is now gone, so there is no copyright label), THEN
  3. re-runs matching (match-model-free; reads stored embeddings), whose gap-fill
     now repopulates auto_label from the face match.

Order matters: clearing auto_label BEFORE matching is what lets the gap-fill fire
(cluster_matching.py:88). Running matching with the stale fragment still in
auto_label would keep the fragment (copyright-wins branch).

Result per cluster: high-tier match -> the matched player's name (source "match");
no match -> auto_label None -> display "Player {id}"; manual_label -> unchanged
(it always wins in display_label()).

Usage (from backend/, venv active, backend STOPPED, DB backed up):
    python rescue_copyright_labels.py <job_id>           # DRY RUN — prints, writes nothing
    python rescue_copyright_labels.py <job_id> --apply   # actually commit
"""
import sys

from app.db import SessionLocal
from app.models.db_models import Cluster, Face, Image, Job
from app.services.cluster_matching import match_session_clusters


def main(job_id: int, apply: bool) -> None:
    db = SessionLocal()
    try:
        job = db.query(Job).get(job_id)
        if job is None:
            sys.exit(f"Job {job_id} not found.")
        session_ids = [s.id for s in job.sessions]
        print(f"Job {job_id} ({job.name!r}): {len(session_ids)} team session(s).")
        print(f"Mode: {'APPLY (will commit)' if apply else 'DRY RUN (no writes)'}\n")

        # 1) Null copyright_tag for THIS job's images only (no-op on already-clean
        #    earlier teams). Scoped strictly to this job's sessions.
        nulled = (
            db.query(Image)
            .filter(Image.session_id.in_(session_ids))
            .update({Image.copyright_tag: None}, synchronize_session=False)
        )
        db.flush()
        print(f"copyright_tag nulled on {nulled} image(s) in this job.\n")

        changed = matched = placeholder = manual = 0
        for s in job.sessions:
            before = {c.id: (c.display_label(), c.auto_label_source) for c in s.clusters}
            # 2) Labeling step: copyright is gone -> clear the poisoned auto_label.
            for c in s.clusters:
                c.auto_label = None
                c.auto_label_source = None
            db.flush()
            # 3) Matching step: gap-fill auto_label from the stored face match.
            match_session_clusters(db, s)
            db.flush()

            for c in s.clusters:
                old_label, old_src = before[c.id]
                new_label = c.display_label()
                if c.manual_label:
                    manual += 1
                elif c.auto_label_source == "match":
                    matched += 1
                else:
                    placeholder += 1
                if old_label != new_label:
                    changed += 1
                    print(f"  [{s.name}] cluster {c.id}: "
                          f"{old_label!r} (src={old_src}) -> {new_label!r} "
                          f"(src={c.auto_label_source})")

        print(f"\nSummary: {changed} label(s) changed | "
              f"{matched} now by match | {placeholder} now 'Player {{id}}' | "
              f"{manual} manual (unchanged).")

        if apply:
            db.commit()
            print("\nCommitted. Reload the job in the app to see the corrected labels.")
        else:
            db.rollback()
            print("\nDRY RUN — nothing written. Re-run with --apply to commit.")
    finally:
        db.close()


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].isdigit():
        sys.exit("Usage: python rescue_copyright_labels.py <job_id> [--apply]")
    main(int(sys.argv[1]), apply="--apply" in sys.argv[2:])
