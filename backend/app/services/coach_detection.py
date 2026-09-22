"""Flag clusters as likely coaches.

Three complementary auto-detection signals (logical OR — any one suffices):

  A. Age + low photo count + buddy presence — catches coaches who DO have
     a few solo photos with visible age cues AND also show up in a group
     photo. The buddy-presence guard (added 2026-09-21) stops a real
     player with a small solo sequence from tripping this rule on the
     back of a noisy InsightFace age estimate.
  B. Photo composition — catches coaches whose primary signal is "appears in
     buddy shots, has few/no individual portraits." More reliable than FER
     age estimation; requires the cluster to have at least 2 buddy shots.
  C. Singleton heuristic (Issue 5) — `image_count == 1`. By workflow rule,
     every player gets multiple shots (individual + team + pano); the only
     person who ever gets a single shot is a coach. So in practice a 1-image
     cluster IS a coach. The off-process kid-arrived-late case (a real
     singleton that's a player) is handled by the manual override dropdown.

Phase 4.6: rule B originally had a third condition,
`image_count < session_median * 0.7`. It was meant to stop a player with a
short session from being flagged, but in practice it caught legitimate
coaches: the real "Nash-Hauer" coach cluster had 2 single + 5 buddy = 7
images, and 7 < 9*0.7 (6.3) is false, so the coach was MISSED. The combined
(few single) + (multiple buddy) signal is already strong on its own —
players always have a real individual-portrait sequence (6-12 solo shots),
so they never look like this. The total-count gate is dropped. The rare
genuine false positive (a player who only got 2-3 solo shots AND has 2+
buddy shots) is handled by the manual coach dropdown (Phase 4.5 §3).

Issue 5 (Rule C): added after we discovered that 1-shot coaches were
silently disappearing from the cluster grid. DBSCAN with min_samples=2
labels lone faces -1 (noise); face_pipeline now promotes those noise faces
to singleton Cluster rows, and Rule C tags them as coach so they route to
both To_be_Cropped and Team Images via assign_roles_coach. Trivially
satisfies Issue 2's planned `single_face_count >= 1` gate.

Manual override (`Cluster.manual_coach_override`, resolved via
`Cluster.is_coach_for_sort()`) always wins over whatever this sets — this
only writes the auto signal `is_likely_coach`.

Known trade-off (rule A): a coach who happens to get a lot of solo shots
(>= half the session median) won't be flagged by age. Rule B covers most of
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
        single_face_count / multi_face_count are optional — when absent,
        rules A and B can't fire (only Rule C — singleton — remains).
        Face_pipeline.py always supplies both in production, so this
        back-compat only affects callers/tests written before F-A
        (2026-09-21).

    A cluster is a coach if ANY of:
      A) median(ages) >= 25 AND image_count <= max(3, session_median * 0.5)
         AND multi_face_count >= 1
      B) single_face_count <= 3 AND multi_face_count >= 2
      C) image_count == 1 (singleton — Issue 5)
    """
    count_threshold = max(MIN_COUNT_FLOOR, session_median_count * COUNT_FRACTION_OF_MEDIAN)

    out: dict[int, bool] = {}
    for c in clusters_data:
        cid = c["cluster_id"]
        count = c["image_count"]
        # multi is used by Rule A's F-A buddy guard AND Rule B. Read once.
        multi = c.get("multi_face_count")

        # Rule A — age + low count + must appear in a buddy shot.
        # 2026-09-21 (F-A, session 1123 mislabel): added the multi>=1 guard.
        # Pre-fix, a real player with a few solo shots and a noisy age
        # estimate crossing 25 was flagged: cluster 14706 had 3 solo, 0
        # buddy, per-face ages [24, 26, 26] → median 26, count 3 tripped
        # the threshold. InsightFace's age head is ±5-8y noisy on kids, so
        # a 25-27 median from adolescents is common. The rule's docstring
        # ("catches coaches who DO have a few solo photos") implicitly
        # assumed the coach also appears in buddy shots — otherwise why
        # are they in the shoot? Encoded that assumption: Rule A only
        # fires when the cluster has at least one buddy shot. Real coaches
        # with a few solo shots still show up in group photos and hit A;
        # kids with 0 buddy shots and borderline ages no longer trip.
        # Rules B and C are unaffected — B independently catches coaches
        # who mostly appear in buddy shots regardless of age; C catches
        # singletons regardless of composition (see [[clustering-singleton-edge]]).
        ages = [a for a in c.get("ages", []) if a is not None]
        rule_a = (
            bool(ages)
            and statistics.median(ages) >= AGE_THRESHOLD
            and count <= count_threshold
            and multi is not None and multi >= 1
        )

        # Rule B — photo composition (no total-count gate; see module docstring)
        single = c.get("single_face_count")
        rule_b = (
            single is not None and multi is not None
            and single <= MAX_SINGLE_FACE_FOR_COACH
            and multi >= MIN_MULTI_FACE_FOR_COACH
        )

        # Rule C — singleton heuristic (Issue 5). Players always get multiple
        # shots; a 1-image cluster IS a coach in our workflow. Operator
        # override handles the off-process kid case.
        rule_c = count == 1

        out[cid] = bool(rule_a or rule_b or rule_c)
    return out


def detect_coaches_evidence(
    clusters_data: List[dict],
    session_median_count: int,
) -> dict[int, frozenset[str]]:
    """2026-09-21 Bug 1 support: same rule evaluation as detect_coaches,
    but returns the SET of rule names that fired for each cluster (a
    subset of {"A", "B", "C"}). An empty frozenset means no rule fired
    (cluster is not a coach).

    Kept as a separate function so `detect_coaches` retains its
    {cluster_id: bool} back-compat return shape — no churn to the
    existing 26-test coach_detection suite. The two share
    implementation via the internal `_evaluate_rules` helper below.

    face_pipeline's Bug-1 partner-veto pass reads this to distinguish
    "Rule B alone fired" (candidate for veto) from "Rule A or C fired
    too" (real coach signal — leave alone).
    """
    count_threshold = max(MIN_COUNT_FLOOR, session_median_count * COUNT_FRACTION_OF_MEDIAN)
    out: dict[int, frozenset[str]] = {}
    for c in clusters_data:
        out[c["cluster_id"]] = _evaluate_rules(c, count_threshold)
    return out


def _evaluate_rules(cluster: dict, count_threshold: float) -> frozenset[str]:
    """Return the frozenset of rule names ({"A", "B", "C"} subset) that
    fire on the given cluster. Same logic as the loop body of
    detect_coaches — kept separate so both entrypoints stay in lockstep."""
    count = cluster["image_count"]
    ages = [a for a in cluster.get("ages", []) if a is not None]
    multi = cluster.get("multi_face_count")
    single = cluster.get("single_face_count")

    rule_a = (
        bool(ages)
        and statistics.median(ages) >= AGE_THRESHOLD
        and count <= count_threshold
        and multi is not None and multi >= 1
    )
    rule_b = (
        single is not None and multi is not None
        and single <= MAX_SINGLE_FACE_FOR_COACH
        and multi >= MIN_MULTI_FACE_FOR_COACH
    )
    rule_c = count == 1

    fired: set[str] = set()
    if rule_a: fired.add("A")
    if rule_b: fired.add("B")
    if rule_c: fired.add("C")
    return frozenset(fired)
