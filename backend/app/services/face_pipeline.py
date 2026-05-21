"""Pipeline orchestrator — single entrypoint that runs the full sort.

run_pipeline(session_id):
    1. Iterate images for the session, run face_detector on each.
    2. Persist Face rows with embeddings.
    3. Cluster all embeddings → assign cluster_id per face.
    4. Create/update Cluster rows.
    5. Run expression classifier on each face.
    6. For each cluster, build ImageRecord list and call sort_rules.assign_roles.
    7. Persist ImageRole rows.
    8. Run outlier flagging across clusters, write needs_review/review_reason.
    9. Mark session status = 'done'.

This should be runnable as a background task. For v1, FastAPI BackgroundTasks
is fine; agent can move to RQ/Celery later if needed.
"""
import json
import logging
import statistics
import time
from datetime import datetime
from pathlib import Path
import numpy as np

from sqlalchemy.orm import Session as DbSession

from app.models.db_models import Session, Image, Face, Cluster, ImageRole
from app.services import face_detector, cluster as clustering, expression
from app.services.sort_rules import ImageRecord, assign_roles, assign_roles_coach
from app.services.outliers import flag_outliers
from app.services.coach_detection import detect_coaches
from app.services.labeling import derive_cluster_label
from app.services.cluster_matching import match_session_clusters

logger = logging.getLogger(__name__)


def _set_progress(db: DbSession, session: Session, stage: str, current: int, total: int) -> None:
    """Stamp progress on the session row. Committed so a separate read DB
    session in the API path sees the update mid-pipeline. Resets the
    stage-start clock whenever the stage label changes (used for per-stage
    ETA)."""
    if session.progress_stage != stage:
        session.progress_stage_started_at = datetime.utcnow()
    session.progress_stage = stage
    session.progress_current = current
    session.progress_total = total
    db.commit()


def _log_stage(session_name: str, stage: str, start: float) -> float:
    """Emit one `[pipeline] team X: <stage>: T.Ts` line and return a fresh
    monotonic clock for the next stage. WARNING level so uvicorn's default
    filter doesn't swallow it — matches the [ingest]/[export] log style."""
    logger.warning(
        "[pipeline] team %s: %s: %.1fs",
        session_name, stage, time.monotonic() - start,
    )
    return time.monotonic()


def run_pipeline(db: DbSession, session_id: int) -> None:
    """Run the full pipeline for one session. Idempotent: clears prior results."""
    session = db.query(Session).get(session_id)
    if session is None:
        raise ValueError(f"Session {session_id} not found")

    session.status = "running"
    session.progress_started_at = datetime.utcnow()
    session.progress_stage_started_at = None  # _set_progress will stamp it
    _set_progress(db, session, "starting", 0, 0)

    # Phase 8: arm a one-shot log on the first detect_faces of this run so
    # we can see at a glance whether CUDA or CPU is actually serving the
    # detection session — catches a silent fallback before we even look
    # at timing numbers.
    face_detector.arm_run_provider_check()

    pipeline_start = time.monotonic()

    try:
        _clear_prior_results(db, session_id)

        # ── Step 1–2: detect faces, persist Face rows ────────────────────────
        stage_start = time.monotonic()
        images = db.query(Image).filter_by(session_id=session_id).all()
        total = len(images)
        _set_progress(db, session, "detecting", 0, total)
        for i, img in enumerate(images, start=1):
            detections = face_detector.detect_faces(Path(img.path))
            for det in detections:
                db.add(Face(
                    image_id=img.id,
                    bbox=json.dumps(det["bbox"]),
                    embedding=det["embedding"].astype(np.float32).tobytes(),
                    det_score=det["det_score"],
                    age=det.get("age"),
                    yaw=det.get("yaw"),
                    pitch=det.get("pitch"),
                    face_area_ratio=det.get("face_area_ratio"),
                ))
            # Commit + progress every few images so the UI updates smoothly
            # without thrashing SQLite on every single image.
            if i % 3 == 0 or i == total:
                _set_progress(db, session, "detecting", i, total)
            else:
                session.progress_current = i
        db.commit()
        stage_start = _log_stage(session.name, "detecting", stage_start)

        # Phase 9 safety guard: if detection produced zero faces across the
        # *entire* session (when there were images to check), don't silently
        # progress to 'done' with zero clusters. The common cause is source
        # files being unreachable (UNC share disconnected, folder renamed
        # post-ingest). Mark 'error' so the UI surfaces it instead of
        # painting the team as successful but empty.
        faces_count = (
            db.query(Face).join(Image)
            .filter(Image.session_id == session_id).count()
        )
        if faces_count == 0 and total > 0:
            logger.error(
                "[pipeline] team %s: 0 faces detected across %d images — "
                "marking session 'error' (verify source images are accessible).",
                session.name, total,
            )
            session.status = "error"
            session.progress_stage = None
            session.progress_current = 0
            session.progress_total = 0
            session.pipeline_finished_at = datetime.utcnow()
            db.commit()
            return


        # ── Step 3–4: cluster all face embeddings ────────────────────────────
        _set_progress(db, session, "clustering", 0, 0)
        faces = (
            db.query(Face)
            .join(Image)
            .filter(Image.session_id == session_id)
            .all()
        )
        if faces:
            embeddings = [
                np.frombuffer(f.embedding, dtype=np.float32) for f in faces
            ]
            labels = clustering.cluster_embeddings(embeddings)

            label_to_cluster: dict[int, Cluster] = {}
            for face, label in zip(faces, labels):
                if label == -1:
                    face.cluster_id = None
                    continue
                cluster_row = label_to_cluster.get(label)
                if cluster_row is None:
                    cluster_row = Cluster(
                        session_id=session_id,
                        needs_review=0,
                        image_count=0,
                    )
                    db.add(cluster_row)
                    db.flush()  # assign id
                    label_to_cluster[label] = cluster_row
                face.cluster_id = cluster_row.id
            db.commit()
        stage_start = _log_stage(session.name, "clustering", stage_start)

        # ── Step 4b: coach detection ────────────────────────────────────────
        _set_progress(db, session, "coach_check", 0, 0)
        db.refresh(session)
        clusters_data = []
        for c in session.clusters:
            faces_in_cluster = db.query(Face).filter_by(cluster_id=c.id).all()
            image_ids_in_cluster = {f.image_id for f in faces_in_cluster}
            # Composition signal: of this cluster's images, how many are
            # single-face (a portrait) vs multi-face (a buddy shot)?
            # face_count is across ALL clusters for that image, not just this.
            single_face_count = 0
            multi_face_count = 0
            for img_id in image_ids_in_cluster:
                total_faces = db.query(Face).filter_by(image_id=img_id).count()
                if total_faces == 1:
                    single_face_count += 1
                else:
                    multi_face_count += 1
            clusters_data.append({
                "cluster_id": c.id,
                "ages": [f.age for f in faces_in_cluster],
                "image_count": len(image_ids_in_cluster),
                "single_face_count": single_face_count,
                "multi_face_count": multi_face_count,
            })
        cluster_sizes = [d["image_count"] for d in clusters_data]
        session_median = int(statistics.median(cluster_sizes)) if cluster_sizes else 0
        coach_flags = detect_coaches(clusters_data, session_median)
        for c in session.clusters:
            c.is_likely_coach = 1 if coach_flags.get(c.id, False) else 0
        db.commit()
        stage_start = _log_stage(session.name, "coach_check", stage_start)

        # ── Step 4c: copyright auto-labeling ────────────────────────────────
        _set_progress(db, session, "labeling", 0, 0)
        for c in session.clusters:
            faces_with_meta = []
            faces_in_cluster = db.query(Face).filter_by(cluster_id=c.id).all()
            seen_images: set[int] = set()
            for f in faces_in_cluster:
                if f.image_id in seen_images:
                    continue
                seen_images.add(f.image_id)
                img = f.image
                face_count_in_image = (
                    db.query(Face).filter_by(image_id=img.id).count()
                )
                faces_with_meta.append({
                    "image_id": img.id,
                    "face_count_in_image": face_count_in_image,
                    "copyright_tag": img.copyright_tag,
                })
            label, ambiguous = derive_cluster_label(faces_with_meta)
            c.auto_label = label
            if label:
                c.auto_label_source = "copyright"
            if ambiguous:
                c.needs_review = 1
                c.review_reason = "ambiguous_copyright"
        db.commit()
        stage_start = _log_stage(session.name, "labeling", stage_start)

        # ── Step 4d: reference matching ──────────────────────────────────────
        # Runs AFTER labeling (so it knows whether copyright is present) and
        # BEFORE sorting (so a confident roster-coach match can flip
        # is_likely_coach in time to feed is_coach_for_sort()). See A.4.
        _set_progress(db, session, "matching", 0, 0)
        match_session_clusters(db, session)
        db.commit()
        stage_start = _log_stage(session.name, "matching", stage_start)

        # ── Step 5: classify expression per face ─────────────────────────────
        total_faces = len(faces)
        _set_progress(db, session, "classifying", 0, total_faces)
        for i, face in enumerate(faces, start=1):
            try:
                bbox = json.loads(face.bbox)
                label, score = expression.classify_expression(
                    Path(face.image.path), bbox
                )
            except Exception as exc:
                logger.warning(
                    "Expression classify failed for face %s: %s", face.id, exc
                )
                label, score = "unknown", 0.0
            face.expression = label
            face.expression_score = score
            if i % 5 == 0 or i == total_faces:
                _set_progress(db, session, "classifying", i, total_faces)
            else:
                session.progress_current = i
        db.commit()
        stage_start = _log_stage(session.name, "classifying", stage_start)

        # ── Step 6–7: assign roles per cluster ───────────────────────────────
        db.refresh(session)
        total_clusters = len(session.clusters)
        _set_progress(db, session, "sorting", 0, total_clusters)
        for i, c in enumerate(session.clusters, start=1):
            _sort_cluster(db, c)
            session.progress_current = i
        db.commit()
        stage_start = _log_stage(session.name, "sorting", stage_start)

        # ── Step 8: outlier flagging across clusters ─────────────────────────
        sizes = {c.id: (c.image_count or 0) for c in session.clusters}
        flags_by_cluster = {f.cluster_id: f for f in flag_outliers(sizes)}
        for c in session.clusters:
            f = flags_by_cluster.get(c.id)
            if f and f.flagged and not c.needs_review:
                c.needs_review = 1
                c.review_reason = f.reason
        db.commit()
        _log_stage(session.name, "flagging", stage_start)

        logger.warning(
            "[pipeline] team %s: TOTAL: %.1fs",
            session.name, time.monotonic() - pipeline_start,
        )

        session.status = "done"
        session.pipeline_finished_at = datetime.utcnow()
        session.progress_stage = None
        session.progress_current = 0
        session.progress_total = 0
        session.progress_stage_started_at = None
        db.commit()

    except Exception:
        session.status = "error"
        session.progress_stage = "error"
        db.commit()
        raise


def _clear_prior_results(db: DbSession, session_id: int) -> None:
    """Remove Faces, Clusters, and ImageRoles for a session before re-running."""
    image_ids = [i.id for i in db.query(Image).filter_by(session_id=session_id).all()]
    if image_ids:
        db.query(Face).filter(Face.image_id.in_(image_ids)).delete(synchronize_session=False)
        db.query(ImageRole).filter(ImageRole.image_id.in_(image_ids)).delete(synchronize_session=False)
    db.query(Cluster).filter_by(session_id=session_id).delete(synchronize_session=False)
    db.commit()


def _sort_cluster(db: DbSession, cluster_row: Cluster) -> None:
    """Run sort_rules for one cluster, persist ImageRole rows, update cluster meta.

    Pure persistence of an already-computed sort: callers from the API path use
    this to re-sort just the affected clusters after a reassign or merge.

    Coach clusters take a different path: their faces are filtered to remove
    images shared with non-coach clusters, then assign_roles_coach picks the
    last shot as team.

    Manually-overridden ImageRole rows (manual_override=1) survive re-sorts;
    they are passed into assign_roles as `manual_roles` so the auto-pick
    routes around them.
    """
    manual_rows = (
        db.query(ImageRole)
        .filter_by(cluster_id=cluster_row.id, manual_override=1)
        .all()
    )
    manual_roles = {r.image_id: r.role for r in manual_rows}

    if cluster_row.is_coach_for_sort():
        excluded_image_ids = _filter_coach_cluster_faces(db, cluster_row.id)
        records = _build_image_records_for_cluster(
            db, cluster_row.id, exclude_image_ids=excluded_image_ids,
        )
        result = assign_roles_coach(records, manual_roles=manual_roles)
    else:
        records = _build_image_records_for_cluster(db, cluster_row.id)
        result = assign_roles(records, manual_roles=manual_roles)

    # Wipe only auto rows; preserve manual overrides.
    db.query(ImageRole).filter_by(
        cluster_id=cluster_row.id, manual_override=0,
    ).delete(synchronize_session=False)

    for image_id, role in result.roles.items():
        if image_id in manual_roles:
            continue  # the manual row already has the right role
        db.add(ImageRole(
            image_id=image_id,
            cluster_id=cluster_row.id,
            role=role,
            manual_override=0,
        ))

    cluster_row.needs_review = 1 if result.needs_review else 0
    cluster_row.review_reason = ",".join(result.review_reasons) if result.review_reasons else None
    cluster_row.image_count = len({r.image_id for r in records})


def _filter_coach_cluster_faces(db: DbSession, cluster_id: int) -> set[int]:
    """Return image_ids in the coach's cluster that should be excluded.

    An image is excluded from a coach's cluster when another face in that same
    image belongs to a non-coach cluster — the buddy/team-pose shot stays with
    the player, not the coach. Returns the set of image_ids to exclude (the
    caller drops them; the Face rows themselves aren't reassigned, just
    skipped during sort/role assignment for this cluster).
    """
    faces_in_cluster = db.query(Face).filter_by(cluster_id=cluster_id).all()
    image_ids = {f.image_id for f in faces_in_cluster}
    if not image_ids:
        return set()

    other_faces = (
        db.query(Face)
        .filter(Face.image_id.in_(image_ids), Face.cluster_id != cluster_id)
        .all()
    )
    excluded: set[int] = set()
    for f in other_faces:
        if f.cluster_id is None:
            continue
        other_cluster = db.query(Cluster).get(f.cluster_id)
        if other_cluster is not None and not other_cluster.is_coach_for_sort():
            excluded.add(f.image_id)
    return excluded


def _build_image_records_for_cluster(
    db: DbSession,
    cluster_id: int,
    exclude_image_ids: set[int] | None = None,
) -> list[ImageRecord]:
    """Helper: collect ImageRecord list for a cluster.

    face_count comes from the total faces in the source image (across all
    clusters), so a buddy photo with two players reads as face_count=2 even
    when only one face belongs to this cluster. expression is the expression of
    the face that belongs to THIS cluster — the same image in another player's
    cluster can carry a different expression.

    exclude_image_ids lets the coach path drop images that belong to a
    non-coach cluster.
    """
    exclude_image_ids = exclude_image_ids or set()
    faces_in_cluster = db.query(Face).filter_by(cluster_id=cluster_id).all()
    records: list[ImageRecord] = []
    seen_images: set[int] = set()
    for f in faces_in_cluster:
        if f.image_id in seen_images or f.image_id in exclude_image_ids:
            continue
        seen_images.add(f.image_id)
        img = f.image
        total_faces_in_image = db.query(Face).filter_by(image_id=img.id).count()
        ts = img.capture_time.timestamp() if img.capture_time else 0.0
        records.append(ImageRecord(
            image_id=img.id,
            capture_time=ts,
            face_count=total_faces_in_image,
            expression=f.expression or "unknown",
            yaw=f.yaw,
            pitch=f.pitch,
            det_score=f.det_score,
            face_area_ratio=f.face_area_ratio,
        ))
    return records
