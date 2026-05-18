"""Tiny pure ETA estimator shared by ingest/pipeline/export progress.

Linear projection: rate = done / elapsed, remaining = (total - done) / rate.
Returns None when there isn't enough signal yet (nothing done, no elapsed
time, or already complete) — the UI shows "estimating…" in that case.
"""
from typing import Optional


def eta_seconds(done: int, total: int, elapsed_seconds: float) -> Optional[int]:
    if total <= 0 or done <= 0 or elapsed_seconds <= 0:
        return None
    if done >= total:
        return 0
    rate = done / elapsed_seconds          # items per second
    if rate <= 0:
        return None
    remaining = (total - done) / rate
    return int(round(remaining))
