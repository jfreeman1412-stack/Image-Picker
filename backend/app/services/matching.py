"""Phase A.3 — reference-photo matching service.

Given a face embedding (the kind face_pipeline produces during a shoot), return
the best-matching Player above a confidence threshold, scored against ALL stored
reference embeddings. Read-only: no model, no writes, no pipeline integration
(that's A.4). See PHASE_A3_MATCHING_SERVICE.md.

Similarity is cosine on the L2-normalized 512-d ArcFace embeddings (a dot
product of unit vectors), matching InsightFace. A player's score is the MAX
cosine across ALL of their references (decision 4) — robust to pose; one good
reference is enough, and the rule stays correct unchanged once a player has
many references.
"""
from __future__ import annotations

import logging

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


def _load_reference_index(db: DbSession):
    """Bulk-load every stored reference. Returns
    (player_ids (N,), matrix (N, 512) float32, name_by_player dict).
    Empty arrays + empty dict when there are no references. Separated from
    match_embedding so A.4 can later cache/reuse it across many queries."""
    rows = db.query(ReferenceFace.player_id, ReferenceFace.embedding).all()
    if not rows:
        return (np.empty((0,), dtype=np.int64),
                np.empty((0, _EMBED_DIM), dtype=np.float32), {})
    player_ids = np.array([r[0] for r in rows], dtype=np.int64)
    matrix = np.stack([np.frombuffer(r[1], dtype=np.float32) for r in rows])
    name_by_player = dict(
        db.query(Player.id, Player.display_name)
        .filter(Player.id.in_({int(p) for p in player_ids}))
        .all()
    )
    return player_ids, matrix, name_by_player


def _none_result(score=None) -> dict:
    return {
        "tier": "none", "reason": "no_match", "needs_review": False,
        "player_id": None, "player_name": None,
        "score": score, "margin": None, "runner_up": None, "candidates": [],
    }


def match_embedding(db: DbSession, embedding: np.ndarray) -> dict:
    """Match one query embedding against all stored references; return a tiered
    result dict (see PHASE_A3_MATCHING_SERVICE.md for the contract)."""
    query = np.asarray(embedding, dtype=np.float32)
    player_ids, matrix, name_by_player = _load_reference_index(db)
    if matrix.shape[0] == 0:
        return _none_result()

    # Defensive L2-normalization of both sides → dot product == cosine.
    row_norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    row_norms[row_norms == 0] = 1.0
    unit_matrix = matrix / row_norms
    qn = float(np.linalg.norm(query))
    unit_query = query / qn if qn else query
    sims = unit_matrix @ unit_query  # (N,) per-reference cosine

    # MAX over ALL of each player's references (decision 4 — never first-only).
    best_by_player: dict[int, float] = {}
    for pid, s in zip(player_ids.tolist(), sims.tolist()):
        if pid not in best_by_player or s > best_by_player[pid]:
            best_by_player[pid] = s

    # Rank distinct players by score desc.
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
