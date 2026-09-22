"""Bug 1 fix (2026-09-21): un-flag Rule-B-only coach clusters when
every buddy-shot partner is a known player cluster.

Tests two layers:
  A. detect_coaches_evidence — returns the frozenset of rules that
     fired per cluster, so face_pipeline can distinguish "Rule B alone"
     (candidate for veto) from "Rule A/C also fired" (real signal).
  B. _apply_rule_b_partner_veto — the post-pass in face_pipeline that
     inspects buddy-shot partners and un-flags Rule-B-only clusters
     whose partners are 100% known players.

Session 1174 cluster 15312 is the load-bearing reproduction:
  - 4 buddy shots, 0 solo shots
  - Rule B fires (single=0<=3, multi=4>=2)
  - All 4 buddy partners are cluster 15311 — a player (7 imgs incl.
    5 solo, ages median 25.5)
  - Under the current pipeline: 15312 is flagged coach → 15312
    displays as a spurious "COACH" card
  - Under the veto pass: 15312 is un-flagged, correctly identifying
    it as a buddy-only kid.

Regression guards ensure real coaches (Rule A + B together, or Rule
C singleton, or mixed/unknown partners) still get flagged.
"""
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models.db_models import (
    Cluster, Face, Image, Job, Session as SessionModel,
)
from app.services.coach_detection import (
    detect_coaches, detect_coaches_evidence,
)
from app.services.face_pipeline import _apply_rule_b_partner_veto


# ── detect_coaches_evidence layer ──────────────────────────────────────


def _comp(cid, single, multi, ages=None, image_count=None):
    return {
        "cluster_id": cid,
        "ages": ages or [],
        "image_count": image_count if image_count is not None else (single + multi),
        "single_face_count": single,
        "multi_face_count": multi,
    }


def test_evidence_rule_b_only_returns_frozenset_b():
    """Cluster that fires ONLY Rule B: rules_fired == {'B'}."""
    ev = detect_coaches_evidence([_comp(1, 0, 4)], session_median_count=10)
    assert ev[1] == frozenset({"B"})


def test_evidence_rule_a_only_returns_frozenset_a():
    """Cluster that fires ONLY Rule A (age + count + buddy)."""
    ev = detect_coaches_evidence(
        [_comp(1, 1, 1, ages=[40.0, 45.0], image_count=2)],
        session_median_count=8,
    )
    assert ev[1] == frozenset({"A"})


def test_evidence_rule_c_singleton_returns_frozenset_c():
    """Cluster with image_count == 1 fires Rule C."""
    ev = detect_coaches_evidence(
        [_comp(1, 1, 0, image_count=1)], session_median_count=10,
    )
    assert "C" in ev[1]


def test_evidence_multiple_rules_all_recorded():
    """Rule A and Rule B both firing → both in the frozenset."""
    ev = detect_coaches_evidence(
        [_comp(1, 1, 2, ages=[44.0, 45.0, 46.0], image_count=3)],
        session_median_count=8,
    )
    assert ev[1] == frozenset({"A", "B"})


def test_evidence_no_rules_fires_returns_empty_frozenset():
    """Player cluster (no rule fires): empty frozenset."""
    ev = detect_coaches_evidence(
        [_comp(1, 8, 2, ages=[12.0, 12.0])], session_median_count=8,
    )
    assert ev[1] == frozenset()


def test_evidence_matches_detect_coaches_boolean():
    """detect_coaches and detect_coaches_evidence must agree on
    is_coach for every cluster. Load-bearing invariant that the two
    entrypoints stay in lockstep (they share _evaluate_rules internally)."""
    data = [
        _comp(1, 0, 4),                              # Rule B alone
        _comp(2, 8, 0, ages=[12.0]),                 # player, no rule
        _comp(3, 1, 1, ages=[45.0], image_count=2),  # Rule A alone
        _comp(4, 1, 0, image_count=1),               # Rule C alone
    ]
    flags = detect_coaches(data, session_median_count=8)
    ev = detect_coaches_evidence(data, session_median_count=8)
    for cid in (1, 2, 3, 4):
        assert bool(ev[cid]) == flags[cid], f"disagreement on cluster {cid}"


# ── _apply_rule_b_partner_veto — the pipeline post-pass ─────────────────


@pytest.fixture
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'veto-test.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    SL = sessionmaker(bind=engine)
    s = SL()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _sess(db, name="T"):
    j = Job(name="J", root_path="/tmp", has_lines=0)
    db.add(j); db.commit(); db.refresh(j)
    s = SessionModel(job_id=j.id, name=name, source_path=f"/tmp/{name}",
                     status="running", created_at=datetime.utcnow())
    db.add(s); db.commit(); db.refresh(s)
    return s


def _img(db, session, filename="img.jpg"):
    i = Image(session_id=session.id, path=f"/tmp/{filename}",
              filename=filename, capture_time=datetime(2026, 1, 1))
    db.add(i); db.commit(); db.refresh(i)
    return i


def _cluster(db, session, *, is_likely_coach=0, manual_coach_override=0,
             auto_label=None, image_count=0):
    c = Cluster(session_id=session.id,
                is_likely_coach=is_likely_coach,
                manual_coach_override=manual_coach_override,
                auto_label=auto_label, image_count=image_count)
    db.add(c); db.commit(); db.refresh(c)
    return c


def _face(db, image, cluster):
    f = Face(image_id=image.id, cluster_id=cluster.id,
             bbox="[0,0,10,10]", det_score=0.9)
    db.add(f); db.commit(); db.refresh(f)
    return f


# The load-bearing test — session 1174 cluster 15312 reproduction.
def test_veto_unflags_buddy_only_kid_all_player_partners(db):
    """The 15312 case: 4 buddy shots, all with a player cluster (15311)
    which has 5 solos + 2 buddies (an unambiguously-a-player pattern
    triggering no coach rules). Post-pass un-flags the buddy-only kid."""
    s = _sess(db)
    # Player cluster: solos only (would never trip Rule B or A).
    player = _cluster(db, s, is_likely_coach=0, auto_label="RealPlayer",
                      image_count=7)
    for i in range(5):
        _face(db, _img(db, s, f"solo_{i}.jpg"), player)

    # Buddy-only kid cluster: only appears in 4 buddy shots with player.
    kid = _cluster(db, s, is_likely_coach=1, image_count=4)  # Rule B → flagged
    for i in range(4):
        buddy_img = _img(db, s, f"buddy_{i}.jpg")
        _face(db, buddy_img, kid)      # kid's face
        _face(db, buddy_img, player)   # player's face — SAME image, 2 faces

    coach_evidence = {kid.id: frozenset({"B"}), player.id: frozenset()}

    _apply_rule_b_partner_veto(db, s.id, coach_evidence)

    db.refresh(kid)
    assert kid.is_likely_coach == 0, (
        "Bug 1 veto didn't fire — buddy-only kid stayed flagged as coach"
    )


def test_veto_does_not_touch_rule_a_or_c_flagged_coach(db):
    """Regression: coach flagged by Rule A (age+count+buddy) OR Rule C
    (singleton) is NOT touched by the veto — the veto only targets
    Rule-B-ONLY firings."""
    s = _sess(db)
    player = _cluster(db, s, is_likely_coach=0, image_count=5)
    for i in range(5):
        _face(db, _img(db, s, f"p{i}.jpg"), player)

    # Coach cluster: fires Rule A (age 45) AND Rule B (composition).
    coach_ab = _cluster(db, s, is_likely_coach=1, image_count=3)
    for i in range(3):
        img = _img(db, s, f"coachAB_{i}.jpg")
        _face(db, img, coach_ab)
        _face(db, img, player)

    # Coach cluster: singleton (Rule C).
    coach_c = _cluster(db, s, is_likely_coach=1, image_count=1)
    _face(db, _img(db, s, "coachC.jpg"), coach_c)

    coach_evidence = {
        player.id: frozenset(),
        coach_ab.id: frozenset({"A", "B"}),   # NOT rule-B-only
        coach_c.id: frozenset({"C"}),          # Rule C
    }
    _apply_rule_b_partner_veto(db, s.id, coach_evidence)

    db.refresh(coach_ab); db.refresh(coach_c)
    assert coach_ab.is_likely_coach == 1, (
        "veto touched a Rule-A+B cluster — should only touch Rule-B-only"
    )
    assert coach_c.is_likely_coach == 1, (
        "veto touched a Rule-C singleton cluster — should only touch Rule-B-only"
    )


def test_veto_does_not_fire_when_a_partner_is_a_coach(db):
    """Rule-B cluster whose buddy-shot partner is ALSO a coach (auto or
    manual) is NOT vetoed. Real coaches often buddy with each other
    (coach + assistant coach); this pattern should stay flagged."""
    s = _sess(db)
    # Two coach clusters, buddying with each other.
    coach_a = _cluster(db, s, is_likely_coach=1, image_count=3)
    coach_b = _cluster(db, s, is_likely_coach=1, image_count=3)
    for i in range(3):
        img = _img(db, s, f"pair_{i}.jpg")
        _face(db, img, coach_a)
        _face(db, img, coach_b)

    coach_evidence = {
        coach_a.id: frozenset({"B"}),  # Rule B alone
        coach_b.id: frozenset({"B"}),  # Rule B alone
    }
    _apply_rule_b_partner_veto(db, s.id, coach_evidence)

    db.refresh(coach_a); db.refresh(coach_b)
    # Neither vetoes because each other is a (pre-veto) coach.
    assert coach_a.is_likely_coach == 1
    assert coach_b.is_likely_coach == 1


def test_veto_does_not_fire_on_orphan_partner_face(db):
    """A buddy shot where the OTHER face has no cluster (cluster_id
    NULL — legacy or degenerate case) blocks the veto: we can't confirm
    'known player', so we err on the safe side and keep the flag."""
    s = _sess(db)
    kid = _cluster(db, s, is_likely_coach=1, image_count=2)
    buddy_img = _img(db, s, "b1.jpg")
    _face(db, buddy_img, kid)
    # A second face on the same image with cluster_id=None (orphan).
    orphan_face = Face(image_id=buddy_img.id, cluster_id=None,
                       bbox="[10,10,20,20]", det_score=0.9)
    db.add(orphan_face); db.commit()

    # Add a second buddy image to satisfy multi>=2 in evidence (just for
    # realism — Rule B needs multi>=2; here evidence is provided directly).
    b2 = _img(db, s, "b2.jpg")
    _face(db, b2, kid)
    o2 = Face(image_id=b2.id, cluster_id=None,
              bbox="[10,10,20,20]", det_score=0.9)
    db.add(o2); db.commit()

    coach_evidence = {kid.id: frozenset({"B"})}
    _apply_rule_b_partner_veto(db, s.id, coach_evidence)

    db.refresh(kid)
    assert kid.is_likely_coach == 1, (
        "veto fired despite orphan partner — should be conservative "
        "when partner cluster identity is unknown"
    )


def test_veto_respects_partner_manual_override_coach(db):
    """Partner has manual_coach_override=1 (operator pinned them as
    coach). Even if is_likely_coach happens to be 0, the veto must
    treat them as a coach (not a player) and NOT fire."""
    s = _sess(db)
    # Partner: auto flag OFF but manual override says COACH.
    partner = _cluster(
        db, s, is_likely_coach=0, manual_coach_override=1,
        image_count=3,
    )
    for i in range(3):
        _face(db, _img(db, s, f"p_{i}.jpg"), partner)

    kid = _cluster(db, s, is_likely_coach=1, image_count=2)
    for i in range(2):
        img = _img(db, s, f"buddy_{i}.jpg")
        _face(db, img, kid)
        _face(db, img, partner)

    coach_evidence = {kid.id: frozenset({"B"}), partner.id: frozenset()}
    _apply_rule_b_partner_veto(db, s.id, coach_evidence)

    db.refresh(kid)
    assert kid.is_likely_coach == 1, (
        "veto ignored partner's manual_coach_override=1 — should treat "
        "manually-pinned coaches as coaches for veto partner check"
    )


def test_veto_respects_partner_manual_override_player(db):
    """Partner has manual_coach_override=-1 (operator pinned player).
    Even if is_likely_coach happens to be 1, the veto must treat them
    as a player and DO fire (all partners are players → un-flag)."""
    s = _sess(db)
    # Partner: auto flag ON but manual override says PLAYER.
    partner = _cluster(
        db, s, is_likely_coach=1, manual_coach_override=-1,
        image_count=3,
    )
    for i in range(3):
        _face(db, _img(db, s, f"p_{i}.jpg"), partner)

    kid = _cluster(db, s, is_likely_coach=1, image_count=2)
    for i in range(2):
        img = _img(db, s, f"buddy_{i}.jpg")
        _face(db, img, kid)
        _face(db, img, partner)

    coach_evidence = {
        kid.id: frozenset({"B"}),
        # Partner's evidence irrelevant — we read manual override.
        partner.id: frozenset({"B"}),
    }
    _apply_rule_b_partner_veto(db, s.id, coach_evidence)

    db.refresh(kid)
    assert kid.is_likely_coach == 0, (
        "veto ignored partner's manual_coach_override=-1 — should treat "
        "manually-pinned players as players for veto partner check"
    )


def test_veto_noop_when_cluster_has_no_buddy_shots(db):
    """A Rule-B-flagged cluster with only solo shots (impossible per
    Rule B's definition, but defensive against future rule tweaks) is
    not vetoed — there are no buddy partners to check."""
    s = _sess(db)
    c = _cluster(db, s, is_likely_coach=1, image_count=3)
    for i in range(3):
        _face(db, _img(db, s, f"solo_{i}.jpg"), c)

    coach_evidence = {c.id: frozenset({"B"})}
    _apply_rule_b_partner_veto(db, s.id, coach_evidence)

    db.refresh(c)
    assert c.is_likely_coach == 1


def test_veto_noop_when_no_clusters_in_session(db):
    """Empty session — helper is defensive, doesn't crash."""
    s = _sess(db)
    _apply_rule_b_partner_veto(db, s.id, {})
    # No crash, nothing to check.


def test_veto_mixed_partners_one_coach_one_player_does_not_fire(db):
    """Kid's 2 buddy shots: one partners with a player, one with a
    coach. Not 100% player-partners → don't veto. Regression guard for
    the 'ALL partners must be players' invariant."""
    s = _sess(db)
    real_player = _cluster(db, s, is_likely_coach=0, image_count=5)
    for i in range(5):
        _face(db, _img(db, s, f"pl_{i}.jpg"), real_player)

    real_coach = _cluster(db, s, is_likely_coach=1, image_count=3)
    for i in range(3):
        _face(db, _img(db, s, f"co_{i}.jpg"), real_coach)

    kid = _cluster(db, s, is_likely_coach=1, image_count=2)
    # Buddy 1: kid + player
    b1 = _img(db, s, "b1.jpg")
    _face(db, b1, kid); _face(db, b1, real_player)
    # Buddy 2: kid + coach
    b2 = _img(db, s, "b2.jpg")
    _face(db, b2, kid); _face(db, b2, real_coach)

    coach_evidence = {
        kid.id: frozenset({"B"}),
        real_player.id: frozenset(),
        real_coach.id: frozenset({"C"}),
    }
    _apply_rule_b_partner_veto(db, s.id, coach_evidence)

    db.refresh(kid)
    assert kid.is_likely_coach == 1, (
        "veto fired despite one buddy partner being a coach — invariant "
        "'ALL partners must be players' regressed"
    )
