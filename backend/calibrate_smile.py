"""Non-destructive smile-threshold calibration (2026-08).

Reads real image paths from the DB (READ-ONLY — no writes, no pipeline, nothing
wiped) and runs InsightFace CPU-only (no GPU contention with the live backend)
to compute services/smile.smile_score for real faces. For each face it also
gets the FER label for cross-reference and saves the face crop named by its
score, so you can open the output folder, sort by filename, and scroll from
neutral (low score) to smiling (high score) to read off the right threshold.

Usage (from backend/, with the venv python):
  python calibrate_smile.py                 # list sessions, then exit
  python calibrate_smile.py <session_id>    # calibrate on that session
  python calibrate_smile.py <session_id> 60 # ...sampling at most 60 images

Output: backend/data/smile_calibration/<session_id>/  (crops + summary.txt)
Tune W_LIFT / W_OPEN / OPEN_BASELINE / SMILE_THRESHOLD in services/smile.py.
"""
import os
import sys

os.environ["CUDA_VISIBLE_DEVICES"] = ""   # force CPU — never contend with the live backend

import cv2
import numpy as np

from app.db import SessionLocal
from app.models.db_models import Session as DbSession, Image
from app.services import expression
from app.services.image_io import read_bgr
from app.services.pose_check import is_acceptable_pose
from app.services.smile import (
    smile_score_from_landmarks, is_smiling, SMILE_THRESHOLD,
    W_LIFT, W_OPEN, OPEN_BASELINE,
)

MAX_IMAGES_DEFAULT = 50


def list_sessions(db):
    rows = (
        db.query(DbSession)
        .order_by(DbSession.id.desc())
        .all()
    )
    print(f"{'id':>5}  {'images':>6}  {'reviewed':>8}  name")
    for s in rows:
        n = db.query(Image).filter_by(session_id=s.id).count()
        if n == 0:
            continue
        print(f"{s.id:>5}  {n:>6}  {('yes' if s.reviewed else 'no'):>8}  {s.name}")
    print("\nPick one with a mix of expressions, ideally NOT a critical reviewed team.")


def calibrate(db, session_id, max_images):
    s = db.query(DbSession).get(session_id)
    if s is None:
        print(f"session {session_id} not found")
        return
    images = db.query(Image).filter_by(session_id=session_id).all()
    if not images:
        print("no images for that session")
        return
    # Even sample across the session so we span its whole capture sequence.
    if len(images) > max_images:
        step = len(images) / max_images
        images = [images[int(i * step)] for i in range(max_images)]

    out_dir = os.path.join("data", "smile_calibration", str(session_id))
    os.makedirs(out_dir, exist_ok=True)

    from insightface.app import FaceAnalysis
    app = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"])
    app.prepare(ctx_id=-1, det_size=(640, 640))

    rows = []
    for img in images:
        bgr = read_bgr(img.path)
        if bgr is None:
            continue
        h, w = bgr.shape[:2]
        area = float(h * w) or 1.0
        for fi, f in enumerate(app.get(bgr)):
            if float(f.det_score) < 0.5:
                continue
            score = smile_score_from_landmarks(
                getattr(f, "landmark_2d_106", None), getattr(f, "kps", None))
            fer, _ = expression.classify_expression(
                img.path, _xywh(f.bbox))
            x1, y1, x2, y2 = f.bbox.astype(int)
            pose = getattr(f, "pose", None)
            yaw = float(pose[1]) if pose is not None else None
            pitch = float(pose[0]) if pose is not None else None
            bw, bh = int(x2 - x1), int(y2 - y1)
            pose_ok = is_acceptable_pose({
                "yaw": yaw, "pitch": pitch, "det_score": float(f.det_score),
                "face_area_ratio": (bw * bh) / area,
            })
            rows.append({
                "score": score, "fer": fer, "pose_ok": pose_ok,
                "img": os.path.basename(img.path), "fi": fi,
            })
            # Save crop named by score so a filename sort == a smile-order scroll.
            crop = bgr[max(0, y1):y2, max(0, x1):x2]
            if crop.size:
                sc = "na" if score is None else f"{score:.2f}"
                pflag = "P" if pose_ok else "x"
                stem = os.path.splitext(os.path.basename(img.path))[0]
                cv2.imwrite(os.path.join(
                    out_dir, f"{sc}_{(fer or 'na')[:3]}_{pflag}_{stem}_f{fi}.jpg"), crop)

    _summarize(rows, out_dir)


def _xywh(bbox):
    x1, y1, x2, y2 = [int(v) for v in bbox]
    return [x1, y1, x2 - x1, y2 - y1]


def _summarize(rows, out_dir):
    scored = [r for r in rows if r["score"] is not None]
    pose_ok = [r for r in scored if r["pose_ok"]]
    lines = []

    def p(msg=""):
        print(msg)
        lines.append(msg)

    p(f"config: W_LIFT={W_LIFT} W_OPEN={W_OPEN} OPEN_BASELINE={OPEN_BASELINE} "
      f"SMILE_THRESHOLD={SMILE_THRESHOLD}")
    p(f"faces: {len(rows)} total, {len(scored)} scored, {len(pose_ok)} pose-acceptable")
    p("\nAmong POSE-ACCEPTABLE faces (the actual pano-candidate population):")
    for label in ("smiling", "serious", "unknown"):
        vals = sorted(r["score"] for r in pose_ok if r["fer"] == label)
        if vals:
            arr = np.array(vals)
            p(f"  FER={label:8} n={len(vals):3}  smile_score "
              f"min={arr.min():.2f} med={np.median(arr):.2f} max={arr.max():.2f}")
    smiling = sorted(r["score"] for r in pose_ok if r["fer"] == "smiling")
    serious = sorted(r["score"] for r in pose_ok if r["fer"] == "serious")
    if smiling and serious:
        # A rough starting threshold: midway between the class medians. This is
        # only a hint — the crops are the real ground truth.
        hint = (np.median(smiling) + np.median(serious)) / 2
        p(f"\nrough threshold hint (midpoint of FER class medians): {hint:.2f}")
        would = sum(1 for v in serious if v >= SMILE_THRESHOLD)
        p(f"at current SMILE_THRESHOLD={SMILE_THRESHOLD}: "
          f"{would}/{len(serious)} FER-serious faces would be called smiling (want low)")
    p(f"\nCrops saved to: {os.path.abspath(out_dir)}")
    p("Open that folder, sort by NAME, scroll low->high score, and set "
      "SMILE_THRESHOLD where your eye sees neutral turn into smiling.")
    # Write the file BEFORE any risky console print so a cp1252 console can
    # never lose the summary.
    with open(os.path.join(out_dir, "summary.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


if __name__ == "__main__":
    db = SessionLocal()
    try:
        if len(sys.argv) < 2:
            list_sessions(db)
        else:
            sid = int(sys.argv[1])
            mx = int(sys.argv[2]) if len(sys.argv) > 2 else MAX_IMAGES_DEFAULT
            calibrate(db, sid, mx)
    finally:
        db.close()
