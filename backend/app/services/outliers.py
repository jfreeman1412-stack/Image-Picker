"""Outlier detection for cluster sizes.

A cluster is flagged when its image count is statistically unusual relative to
the rest of the session. Defaults match what Joey described: median ≈ 8, flag
when something looks like 20.

We use 1.5x median as the high threshold and 0.5x median as the low threshold.
These are tunable; the agent should expose them via session config.
"""
from dataclasses import dataclass
from typing import Optional
import statistics


@dataclass
class OutlierFlag:
    cluster_id: int
    flagged: bool
    reason: Optional[str]   # "outlier_high" | "outlier_low" | None


def flag_outliers(
    cluster_sizes: dict[int, int],
    high_multiplier: float = 1.5,
    low_multiplier: float = 0.5,
) -> list[OutlierFlag]:
    """Return per-cluster flags based on size distribution.

    Args:
        cluster_sizes: {cluster_id: image_count}
        high_multiplier: count > median * this → flagged outlier_high
        low_multiplier:  count < median * this → flagged outlier_low

    Returns:
        list[OutlierFlag], one per cluster.
    """
    if not cluster_sizes:
        return []
    counts = list(cluster_sizes.values())
    if len(counts) < 2:
        # Single cluster — nothing to compare against.
        return [OutlierFlag(cid, False, None) for cid in cluster_sizes]

    median = statistics.median(counts)
    high_thresh = median * high_multiplier
    low_thresh = median * low_multiplier

    out = []
    for cid, n in cluster_sizes.items():
        if n > high_thresh:
            out.append(OutlierFlag(cid, True, "outlier_high"))
        elif n < low_thresh:
            out.append(OutlierFlag(cid, True, "outlier_low"))
        else:
            out.append(OutlierFlag(cid, False, None))
    return out
