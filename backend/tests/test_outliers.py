"""Tests for outlier flagging."""
from app.services.outliers import flag_outliers


def test_typical_session_no_outliers():
    sizes = {1: 8, 2: 7, 3: 9, 4: 8, 5: 8}
    flags = {f.cluster_id: f for f in flag_outliers(sizes)}
    assert all(not f.flagged for f in flags.values())


def test_high_outlier_flagged():
    """Median 8, cluster 5 has 20 → flagged outlier_high (20 > 8*1.5=12)."""
    sizes = {1: 8, 2: 8, 3: 8, 4: 8, 5: 20}
    flags = {f.cluster_id: f for f in flag_outliers(sizes)}
    assert flags[5].flagged
    assert flags[5].reason == "outlier_high"
    assert not flags[1].flagged


def test_low_outlier_flagged():
    """Median 8, cluster 5 has 2 → flagged outlier_low (2 < 8*0.5=4)."""
    sizes = {1: 8, 2: 8, 3: 8, 4: 8, 5: 2}
    flags = {f.cluster_id: f for f in flag_outliers(sizes)}
    assert flags[5].flagged
    assert flags[5].reason == "outlier_low"


def test_single_cluster_no_flag():
    """One cluster — can't be an outlier against itself."""
    flags = flag_outliers({1: 100})
    assert not flags[0].flagged
