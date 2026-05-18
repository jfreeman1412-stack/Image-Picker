"""Cluster face embeddings into one-cluster-per-player.

We use DBSCAN with cosine distance because:
  - Number of players (clusters) is unknown ahead of time.
  - DBSCAN handles noise (e.g. accidental crowd faces) by labeling them -1.
  - Cosine distance is the standard metric for normalized ArcFace embeddings.

Default params are tuned for InsightFace `buffalo_l` normed_embeddings:
  eps=0.4 (cosine distance threshold)
  min_samples=2 (a cluster needs at least 2 faces; singletons → noise)

The agent should expose these as session-level config so they can be tuned
per studio/lighting.
"""
from typing import List, Tuple
import numpy as np

from sklearn.cluster import DBSCAN


def cluster_embeddings(
    embeddings: List[np.ndarray],
    eps: float = 0.4,
    min_samples: int = 2,
) -> List[int]:
    """Assign each embedding a cluster label.

    Args:
        embeddings: list of shape-(512,) float32 vectors. Assumed L2-normalized.
        eps: cosine distance threshold.
        min_samples: minimum cluster size.

    Returns:
        list[int] same length as `embeddings`. -1 means noise (no cluster).
    """
    if not embeddings:
        return []
    X = np.stack(embeddings)
    labels = DBSCAN(eps=eps, min_samples=min_samples, metric="cosine").fit_predict(X)
    return labels.tolist()


def relabel_after_merge(labels: List[int], merge_pairs: List[Tuple[int, int]]) -> List[int]:
    """Merge cluster labels (a, b) → a. Helper for the merge-clusters endpoint."""
    mapping = {i: i for i in set(labels)}
    for a, b in merge_pairs:
        mapping[b] = mapping.get(a, a)
    return [mapping.get(l, l) for l in labels]
