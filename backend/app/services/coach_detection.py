"""Flag clusters as likely coaches.

Two complementary auto-detection signals (logical OR — either alone suffices):

  1. Age + low photo count (original) — catches coaches who DO have a few
     solo photos with visible age cues.
  2. Photo composition (Phase 4.5, new) — catches coaches whose primary
     signal is "appears in buddy shots, has few/no individual portraits."
     More reliable than FER age estimation, but requires the cluster to have
     at least 2 buddy shots.

Manual override (`Cluster.manual_coach_override`, resolved via
`Cluster.is_coach_for_sort()`) always wins over whatever this sets — this
only writes the auto signal `is_likely_coach`.

Known trade-off (rule 1): a coach who happens to get a lot of solo shots
(>= half the session median) won't be flagged by age. Rule 2 covers most of
those (coaches still show up in buddy shots); the rest the user overrides
manually.
"""
import statistics
from typing import List


AGE_THRESHOLD = 25
MIN_COUNT_FLOOR = 3
COUNT_FRACTION_OF_MEDIAN = 0.5

# Rule 2 (composition) thresholds.
MAX_SINGLE_FACE_FOR_COACH = 3      # a player has 6-12 solo portraits
MIN_MULTI_FACE_FOR_COACH = 2       # confirm they're actually in buddy shots
COMPOSITION_COUNT_FRACTION = 0.7   # total must be well under session median


def detect_coaches(
    clusters_data: List[dict],
    session_median_count: int,
) -> dict[int, bool]:
    """Return {cluster_id: is_coach_bool}.

    clusters_data: each dict has:
      - cluster_id: int
      - ages: list[float]            (None entries ignored)
      - image_count: int            (total images in cluster)
      - single_face_count: int      (images whose total face count == 1)
      - multi_face_count: int       (images whose total face count >= 2)
        single_face_count / multi_face_count are optional — when absent the
        composition rule simply can't fire (back-compat with old callers/tests).

    A cluster is a coach if EITHER:
      A) median(ages) >= 25 AND image_count <= max(3, session_median * 0.5)
      B) single_face_count <= 3 AND multi_face_count >= 2
         AND image_count < session_median * 0.7
    """
    count_threshold = max(MIN_COUNT_FLOOR, session_median_count * COUNT_FRACTION_OF_MEDIAN)
    composition_total_cap = session_median_count * COMPOSITION_COUNT_FRACTION

    out: dict[int, bool] = {}
    for c in clusters_data:
        cid = c["cluster_id"]
        count = c["image_count"]

        # Rule A — age + low count
        ages = [a for a in c.get("ages", []) if a is not None]
        rule_a = bool(ages) and statistics.median(ages) >= AGE_THRESHOLD \
            and count <= count_threshold

        # Rule B — photo composition
        single = c.get("single_face_count")
        multi = c.get("multi_face_count")
        rule_b = (
            single is not None and multi is not None
            and single <= MAX_SINGLE_FACE_FOR_COACH
            and multi >= MIN_MULTI_FACE_FOR_COACH
            and count < composition_total_cap
        )

        out[cid] = bool(rule_a or rule_b)
    return out
