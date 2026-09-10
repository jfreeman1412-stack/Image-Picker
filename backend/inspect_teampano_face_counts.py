"""Read-only diagnostic: distribution of detected face_count for images
that ended up with role='team' or role='panoramic'.

Motivation: buddy-split spec (2026-09-08) defines buddy as face_count >= 2.
Joey's mental model is that team/pano photos are single-face captures
(each person shot separately, composited later). Verify against real data
across multiple jobs before building the split.

No DB writes. Safe to run alongside the live backend (WAL mode).
"""
import sys
from collections import Counter

from app.db import SessionLocal
from app.models.db_models import Face, ImageRole, Image, Session as DbSess, Job


def run():
    db = SessionLocal()
    try:
        jobs = db.query(Job).order_by(Job.id).all()
        print(f"Total jobs in DB: {len(jobs)}")
        print(f"Non-archived: {sum(1 for j in jobs if not j.archived)}\n")

        # Build per-image face count for ALL images (bulk), then filter to
        # team/pano-role images per job.
        rows = db.execute(
            # SQLAlchemy 2.x-friendly raw text is fine here; the ORM group_by
            # is more ceremony than it's worth for a one-off diagnostic.
            __import__("sqlalchemy").text(
                "SELECT image_id, COUNT(*) FROM faces "
                "WHERE image_id IS NOT NULL "
                "GROUP BY image_id"
            )
        ).all()
        face_count_by_image: dict[int, int] = {iid: n for iid, n in rows}

        # Overall (all jobs) team/pano role counts.
        global_team = Counter()
        global_pano = Counter()

        # Per-job breakdown.
        per_job_rows = []

        for job in jobs:
            if not job.sessions:
                continue
            session_ids = [s.id for s in job.sessions]
            image_ids_in_job = [
                iid for (iid,) in db.query(Image.id).filter(
                    Image.session_id.in_(session_ids)
                ).all()
            ]
            if not image_ids_in_job:
                continue

            # ImageRole join, keeping only team/panoramic.
            role_rows = db.query(
                ImageRole.image_id, ImageRole.role
            ).filter(
                ImageRole.image_id.in_(image_ids_in_job),
                ImageRole.role.in_(("team", "panoramic")),
            ).all()

            team_dist = Counter()
            pano_dist = Counter()
            for iid, role in role_rows:
                fc = face_count_by_image.get(iid, 0)
                if role == "team":
                    team_dist[fc] += 1
                    global_team[fc] += 1
                elif role == "panoramic":
                    pano_dist[fc] += 1
                    global_pano[fc] += 1

            if not team_dist and not pano_dist:
                continue

            per_job_rows.append({
                "job_id": job.id,
                "name": job.name,
                "archived": bool(job.archived),
                "n_sessions": len(job.sessions),
                "team": team_dist,
                "pano": pano_dist,
            })

        # --- Report -----------------------------------------------------

        def fmt_dist(dist: Counter) -> str:
            """0->N, 1->N, 2->N, 3+->N summary."""
            zero = dist.get(0, 0)
            one = dist.get(1, 0)
            multi_by_count = {k: v for k, v in dist.items() if k >= 2}
            multi_total = sum(multi_by_count.values())
            total = zero + one + multi_total
            if total == 0:
                return "(none)"
            parts = [
                f"total={total}",
                f"0f={zero}",
                f"1f={one}",
                f">=2f={multi_total}",
            ]
            if multi_by_count:
                detail = ", ".join(
                    f"{k}f:{v}" for k, v in sorted(multi_by_count.items())
                )
                parts.append(f"[{detail}]")
            return "  ".join(parts)

        print("=" * 78)
        print("PER-JOB team/pano role image face_count distribution")
        print("=" * 78)
        print(f"{'JOB':>4}  {'ARCH':4}  {'SESS':4}  NAME")
        print(f"{'':4}  {'':4}  {'':4}  TEAM: <dist>")
        print(f"{'':4}  {'':4}  {'':4}  PANO: <dist>")
        print("-" * 78)
        for r in per_job_rows:
            arch = "ARCH" if r["archived"] else ""
            print(f"{r['job_id']:>4}  {arch:4}  {r['n_sessions']:>4}  {r['name']}")
            print(f"{'':4}  {'':4}  {'':4}  TEAM: {fmt_dist(r['team'])}")
            print(f"{'':4}  {'':4}  {'':4}  PANO: {fmt_dist(r['pano'])}")

        print()
        print("=" * 78)
        print("GLOBAL (all jobs, incl. archived) team/pano role face_count distribution")
        print("=" * 78)
        print(f"TEAM role: {fmt_dist(global_team)}")
        print(f"PANO role: {fmt_dist(global_pano)}")

        # The load-bearing question: what fraction of team/pano-role images
        # would be swept into the buddy tree under a strict face_count >= 2 rule?
        def pct_multi(dist: Counter) -> str:
            total = sum(dist.values())
            multi = sum(v for k, v in dist.items() if k >= 2)
            if total == 0:
                return "n/a"
            return f"{multi}/{total} ({100 * multi / total:.2f}%)"

        print()
        print("Sweep risk under strict face_count>=2 rule:")
        print(f"  TEAM-role images with >=2 detected faces: {pct_multi(global_team)}")
        print(f"  PANO-role images with >=2 detected faces: {pct_multi(global_pano)}")

    finally:
        db.close()


if __name__ == "__main__":
    run()
    sys.exit(0)
