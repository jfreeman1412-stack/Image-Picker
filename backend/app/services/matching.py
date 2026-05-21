"""Phase A.3 — reference-photo matching service.
Phase A.4 — extended for pipeline use (roster-scoped index + cluster aggregation).

Given a face embedding (the kind face_pipeline produces during a shoot), return
the best-matching Player above a confidence threshold, scored against stored
reference embeddings. See PHASE_A3_MATCHING_SERVICE.md / PHASE_A4_PIPELINE_INTEGRATION.md.

Similarity is cosine on the L2-normalized 512-d ArcFace embeddings (a dot
product of unit vectors), matching InsightFace. A player's score is the MAX
cosine across ALL of their references (decision 4) — and, for a multi-face
cluster (A.4), the MAX over every (cluster face × that player's references)
pair: "does ANY shot of this cluster match ANY angle the player gave us?".

A.4 split the work so the pipeline can:
  - `load_reference_index(db, player_ids=...)` ONCE per session (optionally
    scoped to a shoot's roster), then
  - `match_against_index(index, query_embeddings)` per cluster (a (k, 512) stack
    of that cluster's face embeddings).
`match_embedding(db, embedding)` is the A.3 single-embedding entrypoint, kept
byte-for-byte compatible (the debug endpoint + A.3 tests depend on it).
"""
from __future__ import annotations

import logging
from typing import NamedTuple

import numpy as np
from sqlalchemy.orm import Session as DbSession

from app.models.db_models import Player, ReferenceFace

logger = logging.getLogger(__name__)

HIGH_THRESHOLD = 0.6     # >= → auto-label (high tier)
LOW_THRESHOLD = 0.4      # >= (and < HIGH) → suggest + flag (low tier)
MIN_MARGIN = 0.05        # top must beat 2nd-best by this when both clear HIGH
TOP_N_CANDIDATES = 5     # how many ranked players the result echoes (debug)

# Tolerance that absorbs float32 round-off (~1e-7) so the documented threshold
# and margin boundaries (e.g. score == 0.6, margin == 0.05) resolve
# deterministically instead of flipping on representation noise. Far below any
# calibration-meaningful difference.
_EPS = 1e-6

_EMBED_DIM = 512


class ReferenceIndex(NamedTuple):
    """A bulk-loaded, pre-normalized snapshot of the reference library (or a
    roster-scoped subset). Built once, reused across many queries."""
    player_ids: np.ndarray              # (N,) int64 — per-reference owner
    unit_matrix: np.ndarray             # (N, 512) float32, L2-normalized rows
    name_by_player: dict[int, str]      # player_id → display_name


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity of two vectors. For the L2-normalized 512-d ArcFace
    embeddings we store this is a dot product; we normalize defensively so a
    stray non-unit vector still scores correctly. Returns a Python float."""
    a = np.asarray(a, dtype=np.float32)
    b = np.asarray(b, dtype=np.float32)
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def _empty_index() -> ReferenceIndex:
    return ReferenceIndex(
        np.empty((0,), dtype=np.int64),
        np.empty((0, _EMBED_DIM), dtype=np.float32),
        {},
    )


def load_reference_index(
    db: DbSession, *, player_ids: set[int] | None = None,
) -> ReferenceIndex:
    """Bulk-load references into a reusable, pre-normalized index.

    When ``player_ids`` is given, only those players' references are loaded (the
    roster-scoped candidate set, A.4 decision 1); None → every reference
    (global). An empty ``player_ids`` set yields an empty index (no candidates).
    The matrix is L2-normalized here so callers don't repeat it. Mirrors the
    bulk-load + numpy pattern in guest_clusters.py / naming_errors.py.
    """
    q = db.query(ReferenceFace.player_id, ReferenceFace.embedding)
    if player_ids is not None:
        ids = {int(p) for p in player_ids}
        if not ids:
            return _empty_index()
        q = q.filter(ReferenceFace.player_id.in_(ids))
    rows = q.all()
    if not rows:
        return _empty_index()

    pid_arr = np.array([r[0] for r in rows], dtype=np.int64)
    matrix = np.stack([np.frombuffer(r[1], dtype=np.float32) for r in rows])
    # L2-normalize each reference row once (guard a zero vector).
    row_norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    row_norms[row_norms == 0] = 1.0
    unit_matrix = matrix / row_norms

    name_by_player = dict(
        db.query(Player.id, Player.display_name)
        .filter(Player.id.in_({int(p) for p in pid_arr}))
        .all()
    )
    return ReferenceIndex(pid_arr, unit_matrix, name_by_player)


def _none_result(score=None) -> dict:
    return {
        "tier": "none", "reason": "no_match", "needs_review": False,
        "player_id": None, "player_name": None,
        "score": score, "margin": None, "runner_up": None, "candidates": [],
    }


def _build_result(best_by_player: dict[int, float], name_by_player: dict) -> dict:
    """Rank distinct players by score desc, apply the tiered thresholds + the
    minimum-margin rule (with _EPS), and return the documented result dict. The
    tiering logic A.3 used, factored out so both entrypoints produce identical
    dicts."""
    ranked = sorted(best_by_player.items(), key=lambda kv: kv[1], reverse=True)
    top_pid, top_score = ranked[0]
    runner = ranked[1] if len(ranked) >= 2 else None

    candidates = [
        {"player_id": int(pid),
         "player_name": name_by_player.get(pid),
         "score": round(float(s), 4)}
        for pid, s in ranked[:TOP_N_CANDIDATES]
    ]
    margin = (top_score - runner[1]) if runner is not None else None
    runner_up = None
    if runner is not None:
        runner_up = {"player_id": int(runner[0]),
                     "player_name": name_by_player.get(runner[0]),
                     "score": round(float(runner[1]), 4)}
    margin_out = round(float(margin), 4) if margin is not None else None

    # Tiering. _EPS makes the documented boundaries deterministic under float32.
    if top_score >= HIGH_THRESHOLD - _EPS:
        ambiguous = (
            runner is not None
            and runner[1] >= HIGH_THRESHOLD - _EPS
            and (top_score - runner[1]) < MIN_MARGIN - _EPS
        )
        if ambiguous:
            tier, reason, needs_review = "low", "ambiguous_margin", True
        else:
            tier, reason, needs_review = "high", "auto_label", False
    elif top_score >= LOW_THRESHOLD - _EPS:
        tier, reason, needs_review = "low", "low_confidence", True
    else:
        # References exist but none is close enough — report the best score for
        # debugging, but name no player.
        result = _none_result(score=round(float(top_score), 4))
        result["margin"] = margin_out
        result["runner_up"] = runner_up
        result["candidates"] = candidates
        return result

    return {
        "tier": tier,
        "reason": reason,
        "needs_review": needs_review,
        "player_id": int(top_pid),
        "player_name": name_by_player.get(top_pid),
        "score": round(float(top_score), 4),
        "margin": margin_out,
        "runner_up": runner_up,
        "candidates": candidates,
    }


def match_against_index(index: ReferenceIndex, query_embeddings: np.ndarray) -> dict:
    """Score a (k, 512) stack of query embeddings (k >= 1; a 1-D vector is
    accepted too) against a preloaded index and return the A.3 tiered result
    dict.

    Per-player score = MAX cosine over (all k query rows) × (that player's
    reference columns) — generalizes A.3 decision 4 to a multi-face cluster.
    Empty index → tier 'none'.
    """
    q = np.asarray(query_embeddings, dtype=np.float32)
    if q.ndim == 1:
        q = q.reshape(1, -1)
    if index.unit_matrix.shape[0] == 0 or q.shape[0] == 0:
        return _none_result()

    # Defensive L2-normalization of the query rows → dot product == cosine.
    qn = np.linalg.norm(q, axis=1, keepdims=True)
    qn[qn == 0] = 1.0
    unit_q = q / qn

    sims_full = unit_q @ index.unit_matrix.T   # (k, N) per (face, reference) cosine
    sims = sims_full.max(axis=0)               # (N,) best query face per reference

    # MAX over ALL of each player's references (decision 4 — never first-only).
    best_by_player: dict[int, float] = {}
    for pid, s in zip(index.player_ids.tolist(), sims.tolist()):
        if pid not in best_by_player or s > best_by_player[pid]:
            best_by_player[pid] = s

    return _build_result(best_by_player, index.name_by_player)


def match_embedding(db: DbSession, embedding: np.ndarray) -> dict:
    """Match one query embedding against all stored references; return a tiered
    result dict (see PHASE_A3_MATCHING_SERVICE.md for the contract). A.3
    contract, unchanged — now a thin wrapper over the global index."""
    index = load_reference_index(db)
    return match_against_index(index, np.asarray(embedding, dtype=np.float32))
