"""Tests for copyright-tag-based cluster auto-labeling."""
from app.services.labeling import derive_cluster_label


def _meta(image_id, face_count_in_image, copyright_tag):
    return {
        "image_id": image_id,
        "face_count_in_image": face_count_in_image,
        "copyright_tag": copyright_tag,
    }


def test_unanimous_label():
    """5 single-face images all tagged 'John Smith' → ('John Smith', False)."""
    data = [_meta(i, 1, "John Smith") for i in range(5)]
    label, ambiguous = derive_cluster_label(data)
    assert label == "John Smith"
    assert ambiguous is False


def test_dominant_80_percent():
    """4 of 5 tagged 'John Smith' → ('John Smith', False) (80% >= 60%)."""
    data = [
        _meta(1, 1, "John Smith"),
        _meta(2, 1, "John Smith"),
        _meta(3, 1, "John Smith"),
        _meta(4, 1, "John Smith"),
        _meta(5, 1, "Mike Jones"),
    ]
    label, ambiguous = derive_cluster_label(data)
    assert label == "John Smith"
    assert ambiguous is False


def test_50_50_tie_ambiguous():
    """2 'John' + 2 'Mike' → (None, True) (50/50)."""
    data = [
        _meta(1, 1, "John"),
        _meta(2, 1, "John"),
        _meta(3, 1, "Mike"),
        _meta(4, 1, "Mike"),
    ]
    label, ambiguous = derive_cluster_label(data)
    assert label is None
    assert ambiguous is True


def test_all_none_returns_none_not_ambiguous():
    """3 single-face images, all None → (None, False)."""
    data = [_meta(i, 1, None) for i in range(3)]
    label, ambiguous = derive_cluster_label(data)
    assert label is None
    assert ambiguous is False


def test_buddy_shots_ignored():
    """Multi-face images don't contribute to the label vote."""
    data = [
        _meta(1, 1, "John"),
        _meta(2, 1, "John"),
        _meta(3, 2, "Mike"),  # buddy — ignored
        _meta(4, 2, "Bob"),   # buddy — ignored
    ]
    label, ambiguous = derive_cluster_label(data)
    assert label == "John"
    assert ambiguous is False


def test_single_occurrence_not_enough():
    """1 single-face image tagged 'John' → (None, False) (needs >= 2)."""
    data = [_meta(1, 1, "John")]
    label, ambiguous = derive_cluster_label(data)
    assert label is None
    assert ambiguous is False


def test_empty_strings_treated_as_missing():
    """Whitespace-only and empty tags don't count."""
    data = [
        _meta(1, 1, ""),
        _meta(2, 1, "   "),
        _meta(3, 1, "John Smith"),
        _meta(4, 1, "John Smith"),
    ]
    label, ambiguous = derive_cluster_label(data)
    assert label == "John Smith"
    assert ambiguous is False


def test_below_dominance_threshold():
    """3 of 6 (50%) is below 60% threshold → ambiguous."""
    data = [
        _meta(1, 1, "John"),
        _meta(2, 1, "John"),
        _meta(3, 1, "John"),
        _meta(4, 1, "Mike"),
        _meta(5, 1, "Bob"),
        _meta(6, 1, "Anna"),
    ]
    label, ambiguous = derive_cluster_label(data)
    assert label is None
    assert ambiguous is True
