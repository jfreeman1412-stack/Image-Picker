# Phase A.3 — DONE

Implemented `PHASE_A3_MATCHING_SERVICE.md` in full. Two sections, two commits,
plus this summary. Branch: `phase-A1-overnight` (continued).

## What got built

The matching brain of the reference-photo system: given a face embedding (the
kind `face_pipeline` produces during a shoot), return the best-matching `Player`
above a confidence threshold, scored against **all** stored reference
embeddings. **Purely additive and read-only** — one new service, one new router,
one new test file, and a 2-line registration in `main.py`. **No new table, no
migration** (A.3 computes over existing `ReferenceFace`/`Face` rows). The locked
`face_detector.py` and `face_pipeline.py` were not touched — there is **no
pipeline integration** (that's A.4); the only consumers are the tests and the
debug endpoint.

### Section 1 — Matching core (`backend/app/services/matching.py`)
- **Tunable module constants:** `HIGH_THRESHOLD = 0.6`, `LOW_THRESHOLD = 0.4`,
  `MIN_MARGIN = 0.05`, `TOP_N_CANDIDATES = 5`.
- `cosine_similarity(a, b)` — cosine on the vectors, normalizing defensively
  (zero-vector guarded) so it's a dot product for the unit ArcFace embeddings
  but still correct for a stray non-unit vector.
- `_load_reference_index(db)` — bulk-loads every reference into a player-id
  array + an (N, 512) matrix + a name map (the `guest_clusters`/`naming_errors`
  pattern). Separated so A.4 can later cache/reuse it.
- `match_embedding(db, embedding)` — the heart:
  1. vectorized `sims = unit_matrix @ unit_query`;
  2. **MAX over ALL of each player's references** (decision 4 — never
     first-only), so the rule is correct today (one ref/player) and unchanged
     when a player gains many;
  3. rank distinct **players**;
  4. tier via thresholds + the margin rule (margin compares the top two
     **players**, so two refs of the same player never read as ambiguous).
- Returns the documented result dict: `tier` (`high`/`low`/`none`), `reason`
  (`auto_label`/`ambiguous_margin`/`low_confidence`/`no_match`), `needs_review`,
  `player_id`, `player_name`, `score`, `margin`, `runner_up`, `candidates`.
- **Float-boundary determinism:** comparisons use a `_EPS = 1e-6` tolerance so
  the documented knife-edge cases (`score == 0.6` → high, `score == 0.4` → low,
  `margin == 0.05` → high) resolve deterministically instead of flipping on
  float32 round-off / the fact that `0.7 - 0.65 != 0.05` in IEEE floats. `_EPS`
  is far below any calibration-meaningful difference. (This is the one
  refinement beyond the hand-off's literal pseudocode — see note below.)

### Section 2 — Debug API (`backend/app/api/matching.py`, registered in `main.py`)
- `GET /api/matching/face/{face_id}` — reads the stored `Face.embedding`
  (`np.frombuffer(..., float32)`), runs `match_embedding`, returns the result
  dict plus a `thresholds` echo (`{high, low, margin}`) for legible calibration.
  404 for an unknown face. Manual-inspection only; not wired into the pipeline.

## Test count

- **Baseline (Phase A.2): 384 passed** — confirmed (collection = 384) before any
  change.
- **New this phase: 19** in `backend/tests/test_matching.py` (Section 1: 16 —
  cosine + every tier + both margin boundaries + both threshold boundaries + MAX
  aggregation + same-player-not-ambiguous; Section 2: 3 — HTTP high / none / 404).
  Slightly above the ~18 estimate.
- **Final: 403 passed**, 0 failed. No existing test modified.

## Commits

```
30744b9 phase A.3 section 2: matching debug API endpoint
1c69eb6 phase A.3 section 1: cosine matching service with tiered thresholds + margin rule
8e89ecf phase A.3 hand-off doc
```

## What to verify in the morning

1. **Tests:** `cd backend && pytest -v` → 403 passed (384 prior + 19 new).
2. **No migration:** A.3 added no table — `git diff 8e89ecf HEAD --
   backend/app/db.py backend/app/models/db_models.py` is empty; `init_db()` is
   unchanged.
3. **Locked files untouched:** `git diff 8e89ecf HEAD --
   backend/app/services/face_detector.py backend/app/services/face_pipeline.py`
   is empty. No existing test changed (only the new `test_matching.py`).
4. **Behaviour spot-check (optional, by hand or curl; needs a real `Face` row
   and `ReferenceFace` rows in the live DB):**
   - `GET /api/matching/face/{id}` for a face that matches a reference →
     `tier: "high"`, the right `player_id`, and a `thresholds` block.
   - A face far from every reference → `tier: "none"`.
   - The result dict shape matches the A.4 contract (tier / reason /
     needs_review / player_id / score / margin / runner_up / candidates).

## Decision / refinement notes (not blockers)

- **`_EPS = 1e-6` tolerance in comparisons.** The hand-off's pseudocode used bare
  `>=` / `<`. Implemented literally, the documented boundary tests are flaky:
  IEEE floats make `0.7 - 0.65 = 0.04999999…` (so an "exactly 0.05" margin would
  wrongly downgrade) and float32(0.6) drifts by ~1e-7 (so an "exactly 0.6" score
  could fall below HIGH). A tiny `_EPS` absorbs that noise and makes the
  documented boundaries hold exactly. It changes nothing at calibration scale.
- **Thresholds are module constants**, as specified — calibrate later by editing
  one line. Promoting them to the `Setting` store (live tuning) remains a future
  option, out of scope.
- New code uses `db.query(Face).get(id)` (emits `LegacyAPIWarning`) to match the
  existing codebase convention.

## Out of scope (deferred, as instructed — NOT built)

Pipeline integration (auto-labelling high-tier faces / queuing low-tier for
review) — that's **A.4**, and `face_pipeline.py` is untouched; roster-scoped
candidate filtering (designed so it's an additive change later, but global for
now); ANN indexes / cross-query caching; persisting match results anywhere;
threshold UI / live calibration; re-detection / embedding generation; any change
to `face_detector.py` or `face_pipeline.py`.
