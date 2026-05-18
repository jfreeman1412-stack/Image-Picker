"""Flag clusters as likely coaches.

Two complementary auto-detection signals (logical OR — either alone suffices):

  1. Age + low photo count (original) — catches coaches who DO have a few
     solo photos with visible age cues.
  2. Photo composition — catches coaches whose primary signal is "appears in
     buddy shots, has few/no individual portraits." More reliable than FER
     age estimation; requires the cluster to have at least 2 buddy shots.

Phase 4.6: rule 2 originally had a third condition,
`image_count < session_median * 0.7`. It was meant to stop a player with a
short session from being flagged, but in practice it caught legitimate
coaches: the real "Nash-Hauer" coach cluster had 2 single + 5 buddy = 7
images, and 7 < 9*0.7 (6.3) is false, so the coach was MISSED. The combined
(few single) + (multiple buddy) signal is already strong on its own —
players always have a real individual-portrait sequence (6-12 solo shots),
so they never look like this. The total-count gate is dropped. The rare
genuine false positive (a player who only got 2-3 solo shots AND has 2+
buddy shots) is handled by the manual coach dropdown (Phase 4.5 §3).

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
    """
    count_threshold = max(MIN_COUNT_FLOOR, session_median_count * COUNT_FRACTION_OF_MEDIAN)

    out: dict[int, bool] = {}
    for c in clusters_data:
        cid = c["cluster_id"]
        count = c["image_count"]

        # Rule A — age + low count
        ages = [a for a in c.get("ages", []) if a is not None]
        rule_a = bool(ages) and statistics.median(ages) >= AGE_THRESHOLD \
            and count <= count_threshold

        # Rule B — photo composition (no total-count gate; see module docstring)
        single = c.get("single_face_count")
        multi = c.get("multi_face_count")
        rule_b = (
            single is not None and multi is not None
            and single <= MAX_SINGLE_FACE_FOR_COACH
            and multi >= MIN_MULTI_FACE_FOR_COACH
        )

        out[cid] = bool(rule_a or rule_b)
    return out
