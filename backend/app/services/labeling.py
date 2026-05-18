"""Derive a cluster label from EXIF copyright tags.

When the photographer tagged each subject's name into the copyright field on
ingest, we can auto-name the cluster. Mixed/conflicting tags within a cluster
are a clustering-quality signal worth flagging for review.

Pure functions, no I/O.
"""
from collections import Counter
from typing import List, Optional, Tuple


DOMINANT_FRACTION = 0.60
MIN_OCCURRENCES = 2


def derive_cluster_label(
    faces_with_metadata: List[dict],
) -> Tuple[Optional[str], bool]:
    """Pick the dominant copyright tag for a cluster, if any.

    Args:
        faces_with_metadata: list of {
            "image_id": int,
            "face_count_in_image": int,
            "copyright_tag": str | None,
        }
        Buddy shots (face_count_in_image > 1) are ignored — only single-face
        images contribute, because a buddy photo's copyright field may list
        both names and we can't attribute it cleanly.

    Returns:
        (label_or_None, needs_review_for_ambiguity)
          - If no tags remain after filtering: (None, False)
          - If the most-common tag is >= 60% AND >= 2 occurrences:
              (tag, False)
          - Otherwise ambiguous: (None, True)
    """
    tags = [
        (f.get("copyright_tag") or "").strip()
        for f in faces_with_metadata
        if f.get("face_count_in_image") == 1
    ]
    tags = [t for t in tags if t]  # drop None and empty

    if not tags:
        return None, False

    counts = Counter(tags)
    most_common, count = counts.most_common(1)[0]
    fraction = count / len(tags)

    if count >= MIN_OCCURRENCES and fraction >= DOMINANT_FRACTION:
        # Tie at the top is still ambiguous even if the leader meets the
        # threshold (e.g. 2 of 4 + 2 of 4).
        ties = [t for t, c in counts.items() if c == count]
        if len(ties) > 1:
            return None, True
        return most_common, False

    # No clear winner. Distinguish 'insufficient data' (one unanimous tag but
    # only one sample) from 'genuine disagreement'.
    if len(counts) == 1:
        return None, False
    return None, True
