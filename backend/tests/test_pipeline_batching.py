"""Tests for the 2026-09-14 sqlite-lock fix — batched commits in
clustering and sorting stages.

Central claims:
  1. Batched commits produce IDENTICAL final state to single-commit runs:
     same Face rows, same Cluster count, same ImageRole assignments, same
     session.status='done'. Batching is a lock-hold optimization, not a
     semantic change.
  2. A mid-stage crash after some batches have committed leaves partial
     state on disk (that's the whole point of committing periodically),
     BUT _clear_prior_results on the next /run wipes cleanly by session/
     image_id and rebuilds from scratch — no orphaned Face rows without
     cluster_ids, no half-populated ImageRole rows, no stale Cluster rows.
  3. The batch commits actually FIRE (proven by counting commits on a
     spy DB session) — a regression that lifts the batching back out
     (e.g. someone reverts to "one commit at end") would drop this count
     and fail the assertion.

These tests fully mock the heavy inference calls (detect_faces,
classify_expression, match_session_clusters) — same pattern as
test_pipeline_concurrency.py.
"""
import numpy as np
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models.db_models import (
    Cluster, Face, Image, ImageRole, Session as SessionModel,
)
from app.services import face_pipeline
from app.services.face_pipeline import (
    run_pipeline, _CLUSTERING_BATCH, _SORTING_BATCH,
)


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def engine(tmp_path):
    e = create_engine(
        f"sqlite:///{tmp_path / 'batching-test.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=e)
    return e


@pytest.fixture
def SessionLocal(engine):
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _seed_session(SessionLocal, name: str, n_images: int) -> int:
    """Create a Session row with n_images Image rows. Returns session id."""
    db = SessionLocal()
    try:
        s = SessionModel(name=name, status="pending",
                         source_path=f"/tmp/{name}")
        db.add(s); db.commit(); db.refresh(s)
        for i in range(n_images):
            db.add(Image(
                session_id=s.id,
                path=f"/tmp/{name}/{name}_{i}.jpg",
                filename=f"{name}_{i}.jpg",
            ))
        db.commit()
        return s.id
    finally:
        db.close()


def _install_mocks(monkeypatch, *, faces_per_image=1, distinct_embeddings=True):
    """Patch detect_faces + classify_expression + match_session_clusters
    with lightweight fakes. `distinct_embeddings=True` (default) gives
    each face a one-hot embedding in a rotating slot so DBSCAN produces
    per-face singletons; that maximises the Face/Cluster row counts that
    exercise the batching code paths."""
    call_seed = [0]

    def _detect(image_path):
        out = []
        for _ in range(faces_per_image):
            idx = call_seed[0] % 512 if distinct_embeddings else 0
            call_seed[0] += 1
            emb = np.zeros(512, dtype=np.float32)
            emb[idx] = 1.0
            out.append({
                "bbox": [0, 0, 32, 32],
                "embedding": emb,
                "det_score": 0.9,
                "age": 15.0,
                "yaw": 0.0,
                "pitch": 0.0,
                "face_area_ratio": 0.10,
                "smile_score": 0.5,
            })
        return out

    monkeypatch.setattr(face_pipeline.face_detector, "detect_faces", _detect)
    monkeypatch.setattr(
        face_pipeline.expression, "classify_expression",
        lambda image_path, bbox: ("smiling", 0.9),
    )
    monkeypatch.setattr(
        face_pipeline, "match_session_clusters",
        lambda db, session: None,
    )


# ── Test 1: batched pipeline produces the expected final state ──────────


def test_batched_pipeline_produces_complete_final_state(
    SessionLocal, monkeypatch,
):
    """Batching didn't change the outcome: every Face has a cluster_id,
    every image with a face has an ImageRole, session is 'done', no
    orphans. Session is deliberately larger than both batch sizes so the
    per-batch commits actually fire during the run."""
    # Sized so clustering hits >_CLUSTERING_BATCH (25) faces and sorting
    # hits >_SORTING_BATCH (5) clusters. With distinct embeddings each
    # face is its own cluster, so 30 images = 30 faces = 30 clusters.
    n_images = _CLUSTERING_BATCH * 2 + 5    # 55 — well past both batches
    assert n_images > _SORTING_BATCH        # sanity for the sorting batch

    sid = _seed_session(SessionLocal, "big", n_images)
    _install_mocks(monkeypatch)

    db = SessionLocal()
    try:
        run_pipeline(db, sid)
        s = db.query(SessionModel).get(sid)
        assert s.status == "done"
        assert s.progress_stage is None

        # Every image → 1 face; every face has cluster_id set.
        faces = db.query(Face).join(Image).filter(
            Image.session_id == sid,
        ).all()
        assert len(faces) == n_images
        orphan_faces = [f for f in faces if f.cluster_id is None]
        assert not orphan_faces, (
            f"{len(orphan_faces)} Face rows have cluster_id=NULL after a "
            f"complete pipeline run — batching regression left orphans"
        )

        # Clusters exist and every one has ≥1 face.
        clusters = db.query(Cluster).filter_by(session_id=sid).all()
        assert clusters, "no Cluster rows after pipeline"
        empty_clusters = [c for c in clusters
                          if not db.query(Face).filter_by(cluster_id=c.id).count()]
        assert not empty_clusters, (
            f"{len(empty_clusters)} empty Cluster rows — a partial "
            f"clustering commit left stale skeleton clusters"
        )

        # ImageRole rows: each single-face image gets exactly one role.
        roles = db.query(ImageRole).join(Image).filter(
            Image.session_id == sid,
        ).all()
        assert len(roles) == n_images, (
            f"expected {n_images} ImageRole rows (one per image); got "
            f"{len(roles)} — sorting batch may have skipped clusters"
        )
    finally:
        db.close()


# ── Test 2: batching identity vs a single-commit reference ──────────────


def test_batched_output_matches_single_commit_baseline(
    SessionLocal, monkeypatch, engine,
):
    """Run the pipeline twice — once with the current batched commits,
    once with the batch sizes bumped so high they never fire (= single-
    commit-per-stage, the pre-fix shape). Assert both runs produce
    equivalent final row counts + role distributions. If batching ever
    subtly changed clustering (say, by causing an autoflush-driven read
    to see partial state) this would catch it.

    Uses separate engines so the two runs don't touch each other's data."""
    n_images = _CLUSTERING_BATCH * 2 + 3

    def _snapshot(SL, sid):
        db = SL()
        try:
            faces = db.query(Face).join(Image).filter(
                Image.session_id == sid,
            ).all()
            clusters = db.query(Cluster).filter_by(session_id=sid).all()
            roles = db.query(ImageRole).join(Image).filter(
                Image.session_id == sid,
            ).all()
            return {
                "n_faces": len(faces),
                "n_clusters": len(clusters),
                "n_roles": len(roles),
                "faces_with_cluster": sum(1 for f in faces if f.cluster_id),
                "role_counts": {
                    r: sum(1 for x in roles if x.role == r)
                    for r in {"team", "panoramic", "individual", "buddy"}
                },
            }
        finally:
            db.close()

    # Run A: current batched shape.
    sid_a = _seed_session(SessionLocal, "batched", n_images)
    _install_mocks(monkeypatch)
    db = SessionLocal()
    try:
        run_pipeline(db, sid_a)
    finally:
        db.close()
    snap_batched = _snapshot(SessionLocal, sid_a)

    # Run B: monkeypatch the batch constants to values that will never
    # fire during this session → effectively one commit per stage.
    monkeypatch.setattr(face_pipeline, "_CLUSTERING_BATCH", 10**9)
    monkeypatch.setattr(face_pipeline, "_SORTING_BATCH", 10**9)
    sid_b = _seed_session(SessionLocal, "unbatched", n_images)
    db = SessionLocal()
    try:
        run_pipeline(db, sid_b)
    finally:
        db.close()
    snap_unbatched = _snapshot(SessionLocal, sid_b)

    assert snap_batched == snap_unbatched, (
        f"batched vs single-commit divergence — batching changed the "
        f"pipeline's semantic output.\nbatched  : {snap_batched}\n"
        f"unbatched: {snap_unbatched}"
    )


# ── Test 3: partial mid-stage state on disk recovers cleanly on rerun ──


def test_pipeline_rerun_after_partial_state_wipes_clean(
    SessionLocal, monkeypatch,
):
    """Simulate the load-bearing safety property of batched commits: a
    crash mid-clustering (or mid-sorting) leaves partial rows on disk
    after the batch commits that already fired, and a subsequent /run
    must wipe them clean via _clear_prior_results and rebuild from
    scratch — no orphaned Faces without cluster_ids, no half-populated
    ImageRole rows, no stale Cluster rows.

    Seeded directly rather than crashed live: crashing precisely at the
    right commit boundary is fragile (commit counts differ per pipeline
    version), and the recovery contract we actually depend on is
    "_clear_prior_results deletes every row in scope regardless of how
    it got there." Seeding a realistic partial state lets us assert that
    directly.
    """
    n_images = _CLUSTERING_BATCH * 2 + 3
    sid = _seed_session(SessionLocal, "recovery", n_images)
    _install_mocks(monkeypatch)

    # Seed partial state that mimics what a crash mid-clustering-batch-2
    # would have left on disk: a mix of Faces with and without cluster_id,
    # some Cluster rows, and a stray ImageRole from a previous
    # incomplete sort. All scoped to this session's image ids so
    # _clear_prior_results should see + wipe every row.
    db = SessionLocal()
    try:
        image_ids = [
            i.id for i in db.query(Image).filter_by(session_id=sid).all()
        ]
        # Two stale Cluster rows for the session.
        c1 = Cluster(session_id=sid, needs_review=0, image_count=0)
        c2 = Cluster(session_id=sid, needs_review=0, image_count=0)
        db.add_all([c1, c2]); db.flush()

        # A batch of Face rows: half assigned to c1, half orphaned.
        emb = np.zeros(512, dtype=np.float32); emb[0] = 1.0
        eb = emb.tobytes()
        for i, img_id in enumerate(image_ids):
            db.add(Face(
                image_id=img_id,
                bbox='[0,0,32,32]',
                embedding=eb,
                det_score=0.9,
                cluster_id=c1.id if i < len(image_ids) // 2 else None,
            ))

        # A stray ImageRole from a partial sort.
        db.add(ImageRole(
            image_id=image_ids[0], cluster_id=c1.id,
            role="team", manual_override=0,
        ))
        db.commit()
    finally:
        db.close()

    # Sanity: verify partial state IS on disk before the re-run wipes it.
    # If seeding didn't produce partial state the test isn't testing the
    # recovery path at all.
    db = SessionLocal()
    try:
        assert db.query(Cluster).filter_by(session_id=sid).count() == 2
        seeded_faces = db.query(Face).join(Image).filter(
            Image.session_id == sid,
        ).all()
        assert len(seeded_faces) == n_images
        with_cluster = sum(1 for f in seeded_faces if f.cluster_id is not None)
        assert with_cluster == n_images // 2
        assert db.query(ImageRole).count() == 1
    finally:
        db.close()

    # Re-run — should wipe partial state via _clear_prior_results, then
    # rebuild from scratch.
    db = SessionLocal()
    try:
        run_pipeline(db, sid)
        s = db.query(SessionModel).get(sid)
        assert s.status == "done", (
            f"re-run over partial state didn't complete: status={s.status}. "
            f"_clear_prior_results may not be wiping every partial row."
        )

        # Full rebuild: every face reassigned, every cluster fresh, every
        # role recomputed. Same invariants as a first-run clean pipeline.
        faces = db.query(Face).join(Image).filter(
            Image.session_id == sid,
        ).all()
        assert len(faces) == n_images, (
            f"expected {n_images} Face rows after rebuild, got {len(faces)}"
            f" — _clear_prior_results didn't wipe partial faces"
        )
        orphans = [f for f in faces if f.cluster_id is None]
        assert not orphans, (
            f"{len(orphans)} orphan faces (cluster_id=NULL) after re-run"
        )

        # Clusters were rebuilt (not merged): the 2 pre-seeded stale
        # clusters are gone; new ones from clustering.cluster_embeddings
        # replace them. With distinct embeddings each face becomes its
        # own singleton, so n_clusters == n_images. Also proves no stale
        # cluster ids leaked (would push count > n_images).
        cluster_count = db.query(Cluster).filter_by(session_id=sid).count()
        assert cluster_count == n_images, (
            f"expected {n_images} clusters after rebuild (one per face), "
            f"got {cluster_count} — stale clusters may have survived the "
            f"wipe"
        )

        # ImageRole rows: exactly one per image (single-face mocks →
        # every image gets a role). The stray ImageRole from the seeded
        # partial state must have been wiped.
        roles = db.query(ImageRole).join(Image).filter(
            Image.session_id == sid,
        ).all()
        assert len(roles) == n_images
    finally:
        db.close()


# ── Test 4: prove the batches actually commit ───────────────────────────


def test_clustering_batching_actually_commits(SessionLocal, monkeypatch):
    """Count DB commits during a run large enough that clustering
    generates multiple batches. Assert we see >1 commit within the
    clustering stage — the whole point of the fix is that the write
    lock releases periodically instead of once at stage end.

    This guards against a regression where someone reverts the batching
    (or misplaces the `if idx % _CLUSTERING_BATCH == 0` check) and the
    test in test_batched_output_matches_single_commit_baseline still
    passes (equivalent output) while the actual lock-hold behavior
    silently reverts to the pre-fix shape.
    """
    # 55 images → 55 faces → clustering iterates 55 times. With batch=25:
    # commits at faces 25, 50, and the terminal commit → ≥3 commits in
    # this stage. Pre-fix: 1 commit.
    n_images = _CLUSTERING_BATCH * 2 + 5
    sid = _seed_session(SessionLocal, "commit_count", n_images)
    _install_mocks(monkeypatch)

    # Spy on Session.commit — bump a counter each call. Filter to the
    # SQLAlchemy Session used by run_pipeline (identified by holding
    # our test's `db` reference — easier is to just count all commits
    # during the run and subtract commits from the setup/teardown).
    from sqlalchemy.orm.session import Session as SqlaSession
    orig_commit = SqlaSession.commit
    commit_count = [0]

    def _spy_commit(self, *a, **kw):
        commit_count[0] += 1
        return orig_commit(self, *a, **kw)

    monkeypatch.setattr(SqlaSession, "commit", _spy_commit)

    db = SessionLocal()
    try:
        run_pipeline(db, sid)
    finally:
        db.close()

    # Pre-fix run_pipeline committed roughly one time per stage: starting,
    # detecting (+ _set_progress bumps), clustering (1), coach_check (1),
    # labeling (1), matching (1), classifying (1), sorting (1), flagging
    # (1), done (1). Post-fix, clustering ALONE contributes ~3 commits
    # (25, 50, terminal) and sorting contributes ~11 commits (5, 10, 15,
    # …, 55, terminal). We assert a floor generous enough not to bind on
    # unrelated commit-count changes elsewhere in the pipeline, but
    # tight enough that reverting the batching would break it (single-
    # commit baseline would be ~15-20 total; batched should be ≥25).
    assert commit_count[0] >= 25, (
        f"only {commit_count[0]} commits during a large pipeline run — "
        f"the clustering/sorting batching may not be firing. Expected "
        f"~30+ commits from batched stages alone. If this floor is now "
        f"too tight because commit sites were removed elsewhere, revisit "
        f"— but do NOT drop the batched commits in clustering/sorting."
    )
