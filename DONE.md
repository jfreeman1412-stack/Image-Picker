# Phase A.4 — DONE

Implemented `PHASE_A4_PIPELINE_INTEGRATION.md` in full. Five sections, five
commits, plus this summary. Branch: **`phase-A4-overnight`** (cut from `main` at
the A.4 hand-off doc commit `0af7209`).

> **Heads-up:** a `git stash` entry (`WIP on main: 0af7209`) holds your earlier
> uncommitted edits to `backend/app/api/jobs.py` and
> `frontend/src/pages/JobWizard.jsx`. A.4 did **not** touch it — `git stash pop`
> when you want them back.

## What got built

A.4 wires the A.3 matching brain into the shoot pipeline: during a sort, each
cluster's faces are matched against the reference library, confident matches are
labeled, uncertain ones flagged, and confident roster-coach matches feed coach
detection. **Backend only** — match data is persisted and exposed via the
existing `GET /clusters` API; no React (that's A.5). `face_detector.py` is
untouched, and the matching stage loads **no model** (pure numpy over stored
embeddings). All four locked decisions honored.

### Section 1 — Matching-core extensions (`services/matching.py`)
- `load_reference_index(db, *, player_ids=None)` → a reusable, pre-normalized
  `ReferenceIndex` (player_ids / unit_matrix / name_by_player); optional roster
  filter (empty set → empty index). Loaded once per session and reused.
- `match_against_index(index, query_embeddings)` → scores a `(k, 512)` cluster
  stack; per-player score = **MAX cosine over (faces × that player's
  references)** (A.3 decision 4 generalized to multi-face). Shared `_build_result`
  tiering helper.
- `match_embedding(db, embedding)` preserved byte-for-byte as a thin wrapper —
  A.3's 19 tests + `GET /api/matching/face/{id}` unchanged.

### Section 2 — Matching pipeline stage (`services/cluster_matching.py`, new)
- `match_session_clusters(db, session)`: roster scope from
  `PlayerMembership(job_id=session.job_id)` (all teams), else global fallback
  (logs `[matching] No roster for job N — falling back to global match across M
  references.`). Per cluster: store `match_scope` / `match_tier` /
  `matched_player_id` / `match_confidence`; **high** → gap-fill `auto_label`
  (source `"match"`) when no copyright, or `match_label_conflict` when copyright
  disagrees (copyright wins), promote `is_likely_coach` for a roster-coach,
  and log the team-mismatch sentence; **low** → `low_confidence_match` flag, no
  label change; **none** → nothing. Empty cluster left untouched.

### Section 3 — Pipeline wiring (`services/face_pipeline.py`)
- New `matching` stage inserted **after `labeling`, before `classifying`** (so
  coach promotion reaches `is_coach_for_sort()` during sorting). Labeling now
  stamps `auto_label_source = "copyright"` when it sets a copyright label.
  `progress_stage` doc updated; the zero-faces guard still short-circuits first.

### Section 4 — Expose match data + validation gate
- `KNOWN_FLAGS` gains `match_label_conflict`, `low_confidence_match`,
  `match_team_mismatch`.
- `roster_check.cluster_match_team_mismatch` + `add_match_team_mismatch_flag` —
  read-time (high-tier match to a same-job, different-team player), mirroring
  `roster_mismatch`/`duplicate_auto_label`.
- `GET /clusters` adds an additive `match` block (`player_id`, `player_name`,
  `confidence`, `tier`, `scope`, `roster_team`, `team_mismatch`) and splices the
  read-time flag — **no existing field changed**.
- `_compute_review_readiness` blocks "Mark reviewed" on a **visible**
  `match_team_mismatch` (toggle off → informational), like `duplicate_auto_label`.

### Section 5 — Global-match suggestion endpoint (`api/cluster_move.py`)
- `POST /api/clusters/{cluster_id}/global-match-suggest`: read-only top-N global
  matches (ignores roster scope), always tagged `scope: "global_fallback"`,
  echoes thresholds. **Mutates nothing.** 404 unknown cluster; empty cluster →
  clean `none`.

## Schema (one migration, no new table)

Five nullable columns added to `clusters` (model + the idempotent
`_PHASE2_COLUMNS` ALTER mechanism in `db.py`): `matched_player_id`,
`match_confidence`, `match_tier`, `match_scope`, `auto_label_source`. Verified
the ALTER path adds them to a pre-A.4 `clusters` table (`MIGRATED_OK`).
`matched_player_name` is derived at read time (not stored).

## Test count

- **Baseline (Phase A.3): 403 passed** — confirmed before any change.
- **New this phase: 43** — Section 1: 6 (in `test_matching.py`); Section 2: 15
  (`test_cluster_matching.py`); Section 3: 3 (`test_pipeline_matching.py`);
  Section 4: 15 (`test_match_exposure.py`); Section 5: 4
  (`test_global_match_suggest.py`).
- **Final: 446 passed**, 0 failed. The only existing test file modified is
  `test_matching.py` (extended per the hand-off — purely additive, no A.3 test
  changed). `pytest -v` green after every section.

## Commits

```
76d4b1f phase A.4 section 5: read-only global-match suggestion endpoint
d05267e phase A.4 section 4: expose match data + team-mismatch validation gate
5ae0352 phase A.4 section 3: wire matching stage into pipeline
e68c293 phase A.4 section 2: matching pipeline stage (roster scope, coach signal, conflict flag)
8d0d6b1 phase A.4 section 1: cluster-level + roster-scoped matching core
```

## What to verify in the morning

1. **Tests:** `cd backend && ./.venv/Scripts/python.exe -m pytest -q` → 446
   passed (403 prior + 43 new).
2. **A.3 untouched:** `git diff main..HEAD -- backend/app/services/face_detector.py`
   is empty; `match_embedding` + the debug endpoint behave identically (their
   tests pass unmodified).
3. **No existing test rewritten:** `git diff --name-only main..HEAD -- backend/tests/`
   lists only the four new files + `test_matching.py` (extended, not edited).
4. **Migration:** new `clusters` columns appear on your existing
   `backend/data/player_sort.db` after the next `uvicorn` start (additive ALTERs;
   delete the DB to start fresh if preferred).
5. **Behaviour spot-check (optional, needs real references):** run a shoot whose
   job has a roster + reference photos →
   - no-copyright clusters auto-label with the matched name (`auto_label_source`
     = `"match"`); copyright disagreements show `match_label_conflict`;
   - `GET /sessions/{id}/clusters` carries the `match` block; a different-team
     high match shows `match_team_mismatch` and blocks "Mark reviewed";
   - a roster-coach match flips the cluster to coach;
   - `POST /clusters/{id}/global-match-suggest` returns global suggestions and
     changes nothing.

## Decision / nuance notes (not blockers)

- **Gap-fill writes the matched name into `auto_label`** (origin recorded in
  `auto_label_source`), so the unchanged `display_label()` surfaces it with no UI
  change — the mechanism that reconciles "match fills the gap" (decision 2) with
  "`display_label()` unchanged / UI doesn't read new fields" (decision 4).
- **Coach promotion is high-tier + roster-scope only**, promote-only;
  `manual_coach_override` and `is_coach_for_sort()` are untouched.
- **`match_team_mismatch` is read-time** (recomputed from the current roster, like
  `roster_mismatch`); the match itself is stored. It blocks readiness only when
  visible. Team-mismatch is asserted for high-tier matches only.
- **`db.query(...).get(id)`** used for new code to match the existing convention
  (emits the same pre-existing `LegacyAPIWarning` as the rest of the codebase).

## Out of scope (deferred, as instructed — NOT built)

All frontend / A.5 work (cluster-card match display, badges, the "Try global
match" button + acceptance flow — A.4 only provides the API); incremental
re-matching on reassign/merge (matching runs only on a full `run_pipeline`, like
`auto_label`/coach today); demoting the coach signal; match-overrides-copyright;
global fallback when a roster exists but has no references; ANN indexes /
cross-session caching / persisting per-face matches; threshold UI; any change to
`face_detector.py` or `match_embedding`'s contract.

## Smoke-test findings (real-data verification — pre-merge, not in tests)

A.4 was exercised against one real shoot (110 individual portraits, 15 players,
filenames as ground truth; CPU detection — no CUDA on this laptop). Integration
ran end-to-end (`status=done`; the matching stage slots in after labeling /
before sorting), and the decision logic behaved exactly as specified: held-out
same-session accuracy 15/15 (conf 0.84–0.96); agreement fired no flag; a
deliberately mislabeled reference fired `match_label_conflict` with copyright
winning the label; gap-fill populated `auto_label` + `auto_label_source='match'`
on a no-copyright cluster. Three findings to record before this is trusted:

1. **The A.2 reference-quality gate rejects shoot photos.** Every face in the
   shoot is far below the gate's `REF_MIN_AREA_RATIO = 0.02` — max observed face
   area was **0.0075** of the frame (these are full-body poses), and a real
   upload returned `400 face_too_small`. **Reference photos must be dedicated
   close-up captures (the phone/tablet check-in flow), not pulled from shoot
   folders.**

2. **Cross-capture accuracy is the key untested unknown.** All numbers above are
   the *same-session ceiling* — reference and query are different frames of the
   same shoot, minutes apart (same-person cosine ≈ 0.78–0.94 vs ≤ 0.32 for
   different children, so the 0.6/0.4 thresholds sit cleanly in the gap). The
   realistic case — a close-up reference matched against a shoot face on a
   different day/lighting/distance — will score lower and has **not** been
   measured. Necessary, not sufficient; validating it needs real close-up
   references.

3. **On current real data, matching is redundant with copyright.** Every cluster
   that matched confidently already carried a correct copyright `auto_label`
   (agreement), and every no-copyright cluster was a degraded background fragment
   that was unmatchable (all tier `none`, top score ≤ 0.38). So gap-fill adds no
   value *today*; A.4's value is the **future no-copyright state** (plus conflict
   detection in the interim) — exactly the transitional rationale behind
   Decision 2.
