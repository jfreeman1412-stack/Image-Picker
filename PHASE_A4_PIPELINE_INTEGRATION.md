# Phase A.4 — Hand-off (Matching → pipeline integration, backend only)

Read this in full before starting. This is the **fourth step** of the
mobile/tablet **reference-photo system**. A.1 built the identity spine
(`Player` + `PlayerMembership`); A.2 hung reference photos + 512-d embeddings
off it (`ReferenceFace`); A.3 built the matching brain
(`services/matching.py` — given an embedding, return the best-matching
`Player`). **A.4 wires that brain into the shoot pipeline:** during a sort,
match each cluster's faces against the reference library, label confident
matches, flag uncertain ones, and feed confident coach matches into coach
detection.

**Phase A.4 is backend only.** Like A.1–A.3, there is **no React work** — the
agent implements Python + tests overnight, `pytest -v` green after each section.
The new match data is **persisted and exposed via the existing
`GET /api/sessions/{id}/clusters` API**, but the existing review UI does **not**
consume the new fields — building the cluster-card confirm/reject/override UI is
**A.5**, a separate phase. Nothing the current frontend already reads may change.

Run `pytest -v` from `backend/` after each section. Confirm the baseline first:
**403 tests as of Phase A.3** must stay green, alongside the new ones this phase
adds (~35).

This phase modifies the locked-feeling `face_pipeline.py` (adding one stage) and
the labeling stage (one line). That is **expected and in scope** — A.3's "don't
touch the pipeline" rule was scoped to A.3. A.4 *is* the pipeline-integration
phase. `face_detector.py` stays untouched (we consume stored embeddings; no
re-detection, no model load in the matching stage).

---

## ⚠️ Decisions

All four high-impact forks were decided by the user and are **locked** below.
Sub-decisions ("minor calls") are flagged and easy to flip.

### Decision 1 — Candidate scope: roster-scoped, with global fallback (LOCKED)

Match candidates are restricted to players rostered for **this session's job**
(`PlayerMembership.player_id` where `job_id == session.job_id` — this is the
whole shoot, **all teams**, not just this session's team). If the job has **no
roster** (no `PlayerMembership` rows for the job, or `session.job_id` is `None`),
fall back to a **global** match across all references.

- **Rationale (user):** best precision when rosters exist (~85% of shoots),
  graceful degradation when they don't, and no silent failure mode.
- **Requirement — log the fallback at run time.** When falling back to global,
  emit a clear `WARNING` line so it's obvious to anyone watching logs:
  ```
  [matching] No roster for job 7 — falling back to global match across 142 references.
  ```
- **Requirement — the API must distinguish the two.** Every matched cluster
  carries a `match_scope` of `"roster"` or `"global_fallback"` so A.5 can badge
  global-fallback matches differently (icon/tooltip — A.5's call; A.4 only
  guarantees the distinction is in the data).
- **Minor call (flagged):** if a roster *exists* but none of its rostered
  players have references, the scoped index is empty → all clusters resolve to
  `tier "none"` with `match_scope "roster"`. We do **not** fall back to global in
  that case — falling back would pull in *other* shoots' references and defeat
  the precision the roster scope buys. Fallback fires only on *no roster at all*.
  Easy to flip to "fall back when the scoped index is empty" if the user prefers.
- **Manual escape hatch (in A.4 scope).** The conservative default above means a
  rostered cluster that the roster scope can't match never *automatically* tries
  the global library. A.4 ships a **read-only** per-cluster endpoint so a user
  can deliberately ask for a global match (the data layer for A.5's eventual
  "Try global match" button) — see Section 5. It changes no cluster state.

### Decision 2 — Label precedence: copyright wins; match fills gaps + flags conflict (LOCKED)

`display_label()` resolution is **unchanged**: `manual_label` >
`auto_label` (copyright) > `Player {id}`. The matching stage interacts with
`auto_label` (the existing copyright field) as follows:

| Situation | Behavior |
|---|---|
| No copyright tag + **high**-tier match | **Gap-fill:** write the matched player's name into `auto_label`. The unchanged `display_label()` then shows it for free. Set `auto_label_source = "match"`. **No flag** — this is the clean, expected steady state. |
| Copyright present + high-tier match **agrees** (normalized names equal) | Leave `auto_label` (copyright) as-is. **No flag** — silent happy path. |
| Copyright present + high-tier match **disagrees** | Keep copyright as `auto_label` (copyright wins). Add review flag **`match_label_conflict`**. |
| **low**-tier match (any) | **Never** touches `auto_label`. Add review flag **`low_confidence_match`** (it's a suggestion needing human confirm). |
| **none** | No label change, no flag. |
| `manual_label` set (user renamed) | Wins over everything, as today. |

- **Rationale (user):** copyright tagging is a **transitional fallback** that
  will be phased out as the reference workflow proves itself. This rule lets us
  *see* disagreements during the trust-building period without silently
  overriding either signal. Once shoots stop populating copyright, this rule
  **naturally degrades to "match wins by default in the absence of copyright"**
  with no code change. If we later decide matching is accurate enough to
  override copyright outright, that's a one-line follow-up.
- **CRITICAL — missing copyright is NOT an error or warning.** A cluster with no
  copyright but a high-tier match auto-labels **cleanly** with the matched name
  (no flag, no nag). The future state is *no copyright at all*; support it from
  day one. Do not add any "copyright missing" flag or readiness block.
- **Requirement:** add `match_label_conflict` to `KNOWN_FLAGS`
  (`api/settings.py`) so it rides the existing Phase 4.5 flag-visibility system.

### Decision 3 — Coach signal: a confident match to a roster-coach sets `is_likely_coach` (LOCKED)

When a cluster gets a **high**-tier match to a player whose
`PlayerMembership.is_coach == 1` for **this job**, set the cluster's
`is_likely_coach = 1`.

- **Rationale (user):** the roster is authoritative ground truth (the league
  explicitly marked these as coaches in the CSV), and a high-tier match means
  we're confident the cluster *is* that roster entry. This catches coaches the
  age/composition heuristic misses (young assistants, buddy-heavy coaches).
- **Independent, additive signal.** The roster-coach signal and the existing
  age (rule A) + composition (rule B) heuristics are **independent** — *any one*
  firing sets `is_likely_coach = 1`. We are adding a **third** coach signal, not
  replacing the existing two. **Promote-only:** a match never *clears*
  `is_likely_coach`.
- **`manual_coach_override` still wins over everything**, exactly as today (the
  Phase 4.5 dropdown remains the escape hatch for a wrong call).
- **`is_coach_for_sort()` does not change** — it already routes
  `is_likely_coach == 1` to `assign_roles_coach`. The wiring is purely upstream:
  in the matching stage, set `is_likely_coach = 1` when the matched player is a
  roster-coach. Because the matching stage runs **before** sorting (see Section
  3), the promoted coach state feeds the sort dispatch in the same run.
- **Minor call (flagged):** coach promotion applies **only in roster scope**.
  In global fallback there is no `PlayerMembership` for this job, so the matched
  player's coach status for this shoot is unknown → we skip promotion. (A player
  who is a coach in some *other* job tells us nothing about this one.)

### Decision 4 — Scope: backend only; expose match data, defer UI to A.5 (LOCKED)

- **Rationale (user):** backend phases have shipped clean overnight by staying
  focused and fully `pytest`-coverable. A.4 is already the most consequential
  backend phase yet (pipeline modification, schema additions, three coach
  signals interacting, label precedence with conflict flagging, validation-gate
  extension). Adding UI doubles the surface area and breaks the pattern.
- **The existing UI must keep working unchanged.** `display_label()` keeps its
  current resolution. New match fields populate the DB and the
  `GET /clusters` response, but the existing review UI does not read them —
  that's A.5.
- **New API fields** (in each cluster object of `GET /clusters`, additive):
  `matched_player_id`, `matched_player_name`, `match_confidence`, `match_tier`,
  `match_scope`, plus a `match_team_mismatch` flag spliced into the existing
  read-time review-reason machinery. **Do not change** any field the existing
  frontend already reads.
- **New read-only endpoint** `POST /api/clusters/{cluster_id}/global-match-suggest`
  (Section 5): returns the top-N **global** (roster-ignoring) matches for a
  cluster as a suggestion, marked `match_scope: "global_fallback"`. It is the
  data layer for A.5's "Try global match" button. It **mutates nothing** — the
  user accepts a suggestion separately via the existing rename
  (`manual_label`) / role mechanisms, which is A.5's flow.
- **Validation-gate extension (matched name vs. roster team).** A high-tier
  match to a player who is rostered for a **different team** than this session is
  a strong "something's wrong" signal (mis-clustered, or a wrong-team photo).
  A.4 surfaces it as the **`match_team_mismatch`** flag and, because A.4 ships
  with no UI, must make the problem legible without one:
  - **Log it at run time** with a full sentence, e.g.
    `[matching] Cluster 12 (Smith): high match to Smith (0.83), but Smith is rostered for team(s) '11U-Black-SB', not this session's team '12U-Orange-SB'.`
  - **Carry the data** to the API (`matched_player_name`, the matched player's
    roster team, the session's team) so A.5 can render the same sentence:
    *"Cluster Smith matched to player on team 11U-Black-SB, but this session is
    for team 12U-Orange-SB."*

---

## Data model (one migration: new `clusters` columns)

A.4 adds **no new table** — it adds columns to `clusters` and reuses the
established idempotent `ALTER TABLE` migration in
[`db.py`](backend/app/db.py) (`_PHASE2_COLUMNS` → `_migrate_phase2`). Add these
to both the `Cluster` model ([`db_models.py`](backend/app/models/db_models.py))
**and** the `"clusters"` list in `_PHASE2_COLUMNS` so existing
`player_sort.db` files migrate at startup:

| Column | Type | Meaning |
|---|---|---|
| `matched_player_id` | `INTEGER` (FK `players.id`, nullable) | Best matched player for high/low tier; `NULL` for none/unmatched. |
| `match_confidence` | `FLOAT` (nullable) | The match score (cosine), rounded; `NULL` when no references were in scope. |
| `match_tier` | `VARCHAR` (nullable) | `"high"` \| `"low"` \| `"none"` \| `NULL` (never matched, e.g. empty cluster). |
| `match_scope` | `VARCHAR` (nullable) | `"roster"` \| `"global_fallback"` — which candidate set produced the match. |
| `auto_label_source` | `VARCHAR` (nullable) | `"copyright"` \| `"match"` — origin of the current `auto_label`, so A.5 can badge it. `NULL` for legacy/unlabeled clusters. |

> `matched_player_name` is **not stored** — derive it by joining `Player` at read
> time (the matched player's `display_name`), mirroring `name_by_player` in
> `matching.py`. Keeps the denormalized name from going stale if a player is
> renamed/merged.

> **Idempotency note:** `run_pipeline._clear_prior_results` deletes and recreates
> all `Cluster` rows each full run, so these columns reset naturally on re-run —
> matching recomputes from scratch every pipeline run, exactly like `auto_label`
> and `is_likely_coach` do today. (See Out-of-scope re: incremental re-matching.)

---

## Context you need

- **Embeddings are already stored** — `Face.embedding` and
  `ReferenceFace.embedding` are 512-d float32, L2-normalized, read back with
  `np.frombuffer(blob, dtype=np.float32)`. The matching stage **does not load the
  InsightFace model** — it's pure numpy over stored vectors, so it's cheap and
  fully testable without the 300 MB model.
- **A.3 designed for exactly this.** `matching._load_reference_index` was split
  out "so A.4 can later cache/reuse it across many queries," and `match_embedding`
  was specified so "an optional candidate filter is an additive change later."
  A.4 cashes both in: add a `player_ids` filter to the index loader, load the
  index **once per session**, and reuse it across every cluster.
- **Per-player aggregation = MAX (A.3 decision 4).** A cluster has many faces.
  Generalize A.3's rule: a player's score for a cluster is the **MAX cosine over
  (every face in the cluster) × (every reference that player owns)** — "does any
  shot of this cluster match any reference of this player?". The margin rule
  still compares the top two **distinct players**.
- **The pipeline orchestrator** is [`face_pipeline.run_pipeline`](backend/app/services/face_pipeline.py).
  Current stages: `detecting → clustering → coach_check → labeling → classifying
  → sorting → flagging`, each stamped via `_set_progress` and timed via
  `_log_stage`. The matching stage slots in **after `labeling`** (so it knows
  whether copyright is present) and **before `classifying`/`sorting`** (so the
  coach promotion feeds `is_coach_for_sort()` during sort dispatch).
- **Coach detection** is [`coach_detection.detect_coaches`](backend/app/services/coach_detection.py),
  run in the `coach_check` stage; it writes `is_likely_coach`. The matching stage
  runs *after* it and OR-promotes (`is_likely_coach = 1`) — never clears.
- **Read-time flag pattern.** `roster_mismatch` and `duplicate_auto_label` are
  computed **at read time** in [`clusters.list_clusters`](backend/app/api/clusters.py)
  (never stored on `Cluster.review_reason`), because the roster can change
  without re-running the pipeline. `match_team_mismatch` follows the **same
  pattern** — the *match* (`matched_player_id`) is stored by the pipeline, but the
  *team-mismatch* is recomputed at read time from the current roster. Helpers
  live in [`roster_check.py`](backend/app/services/roster_check.py).
- **`normalize_name`** ([`services/roster.py`](backend/app/services/roster.py)) is
  the canonical name normalizer used everywhere for label/roster comparison —
  use it for the copyright-vs-match conflict check (Decision 2) and team
  comparisons.
- **The readiness gate** is `_compute_review_readiness` in
  [`sessions.py`](backend/app/api/sessions.py). `duplicate_auto_label` blocks
  "Mark reviewed" *when visible*; A.4 extends the gate with `match_team_mismatch`
  the same way (Section 4).
- **Test conventions:** no `conftest.py`; each test file builds its own engine on
  `tmp_path`. Pipeline tests (see
  [`test_pipeline_errors.py`](backend/tests/test_pipeline_errors.py)) monkeypatch
  `face_pipeline.face_detector.detect_faces`. The matching-stage core is built
  **model-free**: tests seed `Face`/`Cluster`/`Player`/`ReferenceFace`/
  `PlayerMembership` rows and call the stage function directly — no detector, no
  mocking, synthetic unit vectors (reuse A.3's 2-D-in-512-D `_unit` trick).

**Don't break:** the 403 existing tests; `display_label()`'s resolution order;
any field the current frontend reads from `GET /clusters`; `match_embedding`'s
signature + the `GET /api/matching/face/{id}` debug endpoint (A.3); the existing
coach heuristic, the `roster_mismatch`/`duplicate_auto_label` flows, drag-and-drop
reassign, the review gate, and `face_detector.py`.

---

## Section 1 — Matching-core extensions (scope + cluster aggregation)

Extend `services/matching.py` **additively** — `match_embedding(db, embedding)`
and its result-dict contract must remain byte-for-byte compatible (A.3's 19
tests + the debug endpoint depend on it). No pipeline code yet.

### Reference index: optional roster filter + pre-normalization

```python
def load_reference_index(db, *, player_ids: set[int] | None = None):
    """Bulk-load references into a reusable index. When player_ids is given,
    only those players' references are loaded (the roster-scoped candidate set);
    None → every reference (global). Pre-normalizes the matrix so callers don't
    repeat it. Returns an index object: (player_ids (N,), unit_matrix (N,512),
    name_by_player). Empty when no references match the filter.

    A.4 loads this ONCE per session and reuses it across all clusters."""
```
- Implement by adding `if player_ids is not None: q = q.filter(ReferenceFace.player_id.in_(player_ids))` to the existing bulk load, and folding the L2-normalization that currently lives inside `match_embedding` into the index build (do it once).
- Keep the existing private `_load_reference_index` working, or rename internal callers — your call, but **don't** change `match_embedding`'s public behavior.

### Score a query matrix against an index (the generalization)

```python
def match_against_index(index, query_embeddings: np.ndarray) -> dict:
    """Score a (k, 512) stack of query embeddings (k >= 1) against a preloaded
    index and return the SAME tiered result dict as A.3's match_embedding.

    Per-player score = MAX cosine over (all k query rows) × (that player's
    reference columns)  [generalizes A.3 decision 4 to a multi-face cluster].
    Then: rank distinct players, apply HIGH/LOW thresholds + the MIN_MARGIN
    rule across the top two players, with the _EPS tolerance — all unchanged
    from A.3. Empty index → tier 'none'."""
```
- Vectorize: normalize the `k` query rows, `sims = unit_query @ index.unit_matrix.T` → `(k, N)`; for each player take the max over their reference columns and over all `k` rows → one score per player. Reuse A.3's exact thresholds (`HIGH_THRESHOLD`, `LOW_THRESHOLD`, `MIN_MARGIN`, `_EPS`) and the result-dict shape.
- **Refactor the tiering block out of `match_embedding` into a shared helper** (e.g. `_tier_from_best_by_player(best_by_player, name_by_player)`) so both entrypoints produce identical dicts. This is the cleanest way to keep A.3's contract exact.

### Preserve the A.3 entrypoint

```python
def match_embedding(db, embedding: np.ndarray) -> dict:
    """A.3 contract, unchanged. Now a thin wrapper:
    match_against_index(load_reference_index(db), embedding.reshape(1, -1))."""
```

### Tests for Section 1 (`backend/tests/test_matching.py`, extend)

- `load_reference_index(db, player_ids={A})` excludes B's references; `None` loads all.
- **Cluster MAX aggregation:** a cluster of two faces, one at sim 0.5 and one at 0.7 to player A's reference → A's score is **0.7** (max over faces), tier `high`.
- **Multi-face × multi-reference MAX:** 2 faces × 2 references → score is the single max of the 4 cosines.
- `match_against_index` reproduces every A.3 tier/boundary for `k == 1` (parametrize against the existing single-embedding cases).
- **A.3 regression:** `match_embedding` returns the identical dict it did before (its existing tests must stay green untouched).
- Empty index (filtered to a player with no references) → tier `none`.

Commit: `phase A.4 section 1: cluster-level + roster-scoped matching core`

---

## Section 2 — The matching pipeline stage (`services/cluster_matching.py`)

A new module holding the **pipeline-facing** logic, kept separate from the pure
scorer (`matching.py`) so it's seed-and-call testable without running detection,
clustering, or expression. This is the heart of A.4.

```python
def match_session_clusters(db, session) -> None:
    """Match every cluster in a session against the reference library and
    persist results. Mutates Cluster rows; caller commits.

    1. Candidate scope (Decision 1):
         roster_player_ids = {pm.player_id for pm in PlayerMembership(job_id=session.job_id)}
         coach_player_ids  = {pm.player_id for pm if pm.is_coach}
       - roster rows exist        → scope='roster',  load_reference_index(db, player_ids=roster_player_ids)
       - no roster (or job_id None)→ scope='global_fallback', load_reference_index(db),
                                      LOG the fallback line with the global ref count,
                                      coach_player_ids = set()   # unknown in fallback
    2. For each cluster with >=1 face:
         embs = [np.frombuffer(f.embedding, np.float32) for f in cluster faces]
         r = match_against_index(index, np.stack(embs))
         cluster.match_scope = scope
         cluster.match_tier  = r['tier']
         cluster.matched_player_id = r['player_id']     # None for 'none'
         cluster.match_confidence  = r['score']
         if r['tier'] == 'high':
             if not (cluster.auto_label or '').strip():
                 cluster.auto_label = r['player_name']; cluster.auto_label_source = 'match'   # gap-fill, no flag
             elif normalize_name(cluster.auto_label) != normalize_name(r['player_name']):
                 cluster.review_reason = _add_flag(cluster.review_reason, 'match_label_conflict')
             # (agree → no change, no flag)
             if r['player_id'] in coach_player_ids:
                 cluster.is_likely_coach = 1            # promote-only; manual override still wins
             _log_team_mismatch_if_any(...)             # run-time legibility (Decision 4)
         elif r['tier'] == 'low':
             cluster.review_reason = _add_flag(cluster.review_reason, 'low_confidence_match')
         # 'none' → nothing
    """
```

Notes:
- `_add_flag` appends a code to the comma-joined `review_reason`, deduped —
  same shape as `roster_check.add_roster_flag`. (Reuse/extract a shared helper.)
- The team-mismatch **flag** is computed read-time (Section 4); here we only
  **log** the full sentence at run time for a high-tier match whose matched
  player's `PlayerMembership` teams for this job don't include
  `session_norm_team(session)`. We have the roster in hand here, so the log is
  free; do not store the flag.
- Empty cluster (no faces) → leave all match columns `NULL`/untouched, `continue`.
- Roster scope but empty index (no references for any rostered player) → every
  cluster gets `match_tier='none'`, `match_scope='roster'`, no fallback
  (Decision 1 minor call).

### Tests for Section 2 (`backend/tests/test_cluster_matching.py`)

Seed rows directly (no model). Helper builds a `Job` + `Session` + `Image`s +
`Face`s (embeddings = synthetic unit vectors) + `Player`s + `ReferenceFace`s +
`PlayerMembership`s, then calls `match_session_clusters` and asserts on the
`Cluster` rows.

- **Gap-fill, no copyright:** cluster matches player A (high), `auto_label` was
  empty → `auto_label == "A"`, `auto_label_source == "match"`, `matched_player_id == A`,
  `match_tier == "high"`, **no** `match_label_conflict`, **no** review flag.
- **Agree:** `auto_label == "A"` (copyright) and high match to A → `auto_label`
  unchanged, `auto_label_source` stays `"copyright"`, **no** flag.
- **Conflict:** `auto_label == "A"` (copyright) and high match to **B** →
  `auto_label` still `"A"`, `review_reason` contains `match_label_conflict`,
  `matched_player_id == B`.
- **Low tier:** best score in `[0.4, 0.6)` → `match_tier == "low"`,
  `low_confidence_match` flag, `auto_label` untouched, `is_likely_coach` untouched.
- **None:** best score `< 0.4` → `matched_player_id` None, `match_tier == "none"`,
  no flag, no label change.
- **Coach promotion:** high match to a player with `PlayerMembership.is_coach=1`
  for this job → `is_likely_coach == 1`. Independent of the heuristic (seed a
  cluster the heuristic would NOT flag and confirm matching alone promotes it).
- **Coach promotion is high-only:** a low match to a roster-coach does **not**
  promote.
- **Roster scope filters candidates:** player C (rostered on a *different job*)
  has a reference nearly identical to the query, but is excluded → the cluster
  matches the rostered player or resolves to none, never C. `match_scope == "roster"`.
- **Global fallback:** session with `job_id=None` (or a job with no memberships)
  → `match_scope == "global_fallback"`, fallback log emitted (assert via
  `caplog`), and no coach promotion even if the matched player is a coach
  elsewhere.
- **MAX over a multi-face cluster** end-to-end (one weak + one strong face →
  high).
- **Empty-scoped-index stays roster/none** (roster exists, players have no
  references) — no fallback.
- **Re-run idempotency:** calling the stage twice yields the same columns (no
  duplicated flags).

Commit: `phase A.4 section 2: matching pipeline stage (roster scope, coach signal, conflict flag)`

---

## Section 3 — Wire the stage into `face_pipeline`

Insert a `matching` stage into `run_pipeline`, **after `labeling`** and
**before `classifying`** (it must precede `sorting` so the coach promotion is
visible to `is_coach_for_sort()` during sort dispatch — placing it before
`classifying` is simplest and order-safe).

```python
# ── Step 4d: reference matching ──────────────────────────────────────
_set_progress(db, session, "matching", 0, 0)
from app.services.cluster_matching import match_session_clusters
match_session_clusters(db, session)
db.commit()
stage_start = _log_stage(session.name, "matching", stage_start)
```

Also: in the existing **labeling** stage, where a non-`None` copyright label is
assigned (`c.auto_label = label`), add `c.auto_label_source = "copyright"` so the
origin marker is set for copyright-labeled clusters too. (One line; the only
edit to existing pipeline logic besides inserting the stage.)

Add `"matching"` to the progress-stage docstring/comment on `Session.progress_stage`.

### Tests for Section 3 (`backend/tests/test_pipeline_errors.py` or a new `test_pipeline_matching.py`)

Follow the existing pipeline-test pattern (monkeypatch
`face_pipeline.face_detector.detect_faces` to return synthetic detections with
known embeddings; monkeypatch `cluster.cluster_embeddings` and
`expression.classify_expression` to deterministic stubs so no model loads).

- **Stage runs in order:** monkeypatch `cluster_matching.match_session_clusters`
  to record invocation; assert it's called once, after labeling set `auto_label`
  and before `_sort_cluster`. (A spy list + asserting relative call order, or
  asserting `progress_stage` transitions.)
- **End-to-end smoke:** a tiny session where detection yields embeddings that
  match a seeded reference → after `run_pipeline`, the cluster has
  `matched_player_id` set and `auto_label` gap-filled; `session.status == "done"`.
- **Zero-faces guard still holds:** the existing zero-faces tests stay green
  (matching stage is never reached when the pipeline bails early).

Commit: `phase A.4 section 3: wire matching stage into pipeline`

---

## Section 4 — Expose match data + the `match_team_mismatch` validation gate

### KNOWN_FLAGS (`api/settings.py`)

Add all three new flags (keep the in-sync comment accurate):
```python
"match_label_conflict",   # high match disagrees with the copyright auto_label
"low_confidence_match",   # low-tier match — a suggestion needing human confirm
"match_team_mismatch",    # high match to a player rostered for a DIFFERENT team
```
`match_label_conflict` and `low_confidence_match` are **stored** on
`review_reason` by the pipeline (Section 2); `match_team_mismatch` is
**read-time** (next).

### Read-time team-mismatch (`services/roster_check.py`)

Mirror the `roster_mismatch` / `duplicate_auto_label` pattern:
```python
MATCH_TEAM_MISMATCH = "match_team_mismatch"

def cluster_match_team_mismatch(cluster, session_norm_team, membership_teams_by_player) -> bool:
    """True iff this cluster has a HIGH match to a player who is rostered for
    this job but NOT for this session's team. membership_teams_by_player:
    {player_id: {norm_team, ...}} for this job. Abstain (False) when the matched
    player isn't rostered for this job at all (e.g. global fallback)."""
    if cluster.match_tier != "high" or cluster.matched_player_id is None:
        return False
    teams = membership_teams_by_player.get(cluster.matched_player_id)
    if not teams:
        return False
    return session_norm_team not in teams

def add_match_team_mismatch_flag(review_reason, is_mismatch):  # same shape as add_roster_flag
    ...
```

### `GET /clusters` (`api/clusters.py`)

- Build `membership_teams_by_player` once for the session's job (one query).
- Build a `player_name_by_id` lookup for the matched players (one query).
- For each cluster, **add** (without touching existing fields) a `match` block:
  ```python
  "match": {
      "player_id": c.matched_player_id,
      "player_name": player_name_by_id.get(c.matched_player_id),
      "confidence": c.match_confidence,
      "tier": c.match_tier,
      "scope": c.match_scope,                       # 'roster' | 'global_fallback'
      "roster_team": <matched player's raw team for this job, or None>,
      "team_mismatch": <bool from cluster_match_team_mismatch>,
  }
  ```
  (Also acceptable: flat top-level `matched_player_id` etc. — but a nested
  `match` block keeps the new surface area obviously separable for A.5. Either
  way, **add only**; change nothing existing.)
- Splice `match_team_mismatch` into `combined_reason` at read time (alongside the
  existing `add_roster_flag` / `add_duplicate_label_flag` calls), then through
  `filter_visible_reasons` as usual.

### Readiness gate (`api/sessions.py`)

Extend `_compute_review_readiness` to block on `match_team_mismatch` **when
visible**, exactly as `duplicate_auto_label` does: compute the per-cluster
mismatch (reuse the helper + the job's membership lookup), and when the flag is
visible per `get_flag_visibility_map`, add `match_team_mismatch` to that
cluster's `missing` list. Toggling the flag off makes it informational-only
(no block) — same affordance as `duplicate_auto_label`.

### Tests for Section 4

- `cluster_match_team_mismatch`: high match to a same-team player → False;
  high match to a same-job different-team player → True; low/none → False;
  matched player not rostered for the job (fallback) → False.
- `GET /clusters` returns the `match` block with correct values; **all existing
  fields unchanged** (assert the pre-A.4 keys still present with same meaning).
- `match_team_mismatch` appears in `visible_review_reasons` when visible, absent
  when hidden.
- Readiness: a session with a visible `match_team_mismatch` cluster is **not**
  ready; hiding the flag makes it ready (other roles permitting). Existing
  readiness tests stay green.
- `match_label_conflict` / `low_confidence_match` flow through visibility
  filtering like any other stored flag.

Commit: `phase A.4 section 4: expose match data + team-mismatch validation gate`

---

## Section 5 — Global-match suggestion endpoint (read-only escape hatch)

A per-cluster, **read-only** endpoint that runs a *global* match (ignoring roster
scope) and returns the top-N suggestions. This is the conservative-default escape
hatch from Decision 1: when the roster scope can't match a cluster, a user can
deliberately consult the whole reference library. It is the **data layer only** —
A.5 adds the "Try global match" button + the acceptance flow.

Home: [`api/cluster_move.py`](backend/app/api/cluster_move.py) — it already owns
the `/api/clusters/{cluster_id}/...` prefix (`POST /{cluster_id}/move`). Add the
route there (or a sibling router mounted at the same prefix; do **not** change
the prefix the existing `/move` route lives under).

```
POST /api/clusters/{cluster_id}/global-match-suggest
```

```python
@router.post("/{cluster_id}/global-match-suggest")
def global_match_suggest(cluster_id, db=Depends(get_db)):
    """Read-only: top-N GLOBAL matches for a cluster, ignoring roster scope.
    Mutates nothing. 404 if the cluster doesn't exist."""
    cluster = db.query(Cluster).get(cluster_id)
    if cluster is None:
        raise HTTPException(404, "Cluster not found")
    embs = [np.frombuffer(f.embedding, np.float32)
            for f in db.query(Face).filter_by(cluster_id=cluster_id)]
    index = matching.load_reference_index(db)              # GLOBAL — no player_ids
    result = matching.match_against_index(index, np.stack(embs)) if embs \
             else matching._none_result()
    result["scope"] = "global_fallback"                    # always — this is a global query
    result["thresholds"] = {                               # echo, like the debug endpoint
        "high": matching.HIGH_THRESHOLD,
        "low": matching.LOW_THRESHOLD,
        "margin": matching.MIN_MARGIN,
    }
    return result
```

- Reuses Section 1's `load_reference_index` (global) + `match_against_index`
  (cluster MAX aggregation). The result dict's `candidates` list **is** the
  top-N (A.3's `TOP_N_CANDIDATES`).
- Always tag `scope: "global_fallback"` so the UI can present it as
  *"Global match — verify this is the right person."*
- **No writes.** This endpoint never sets `matched_player_id`, `auto_label`,
  `is_likely_coach`, or any review flag. Acceptance is a separate, explicit user
  action (rename / set-role) — A.5.
- Empty cluster (no faces) → return a clean `none` result, not a 500.

### Tests for Section 5 (`backend/tests/test_global_match_suggest.py`)

- A cluster whose faces match a reference for a player **outside** this job's
  roster → high-tier suggestion naming that player, `scope == "global_fallback"`,
  `candidates` populated. (This is the case roster scope deliberately *won't*
  match in the pipeline — proving the escape hatch reaches further.)
- The call leaves the `Cluster` row unchanged (re-read: `matched_player_id`,
  `auto_label`, `is_likely_coach`, `review_reason` all as before).
- Unknown `cluster_id` → 404. Empty cluster → 200 with a `none` result.

Commit: `phase A.4 section 5: read-only global-match suggestion endpoint`

---

## Conventions (match the existing code)

- Read embeddings with `np.frombuffer(..., dtype=np.float32)`; never re-detect,
  never load the InsightFace model in the matching path.
- Reuse A.3's thresholds/`_EPS` and result-dict contract verbatim; refactor the
  tiering into a shared helper rather than duplicating it.
- Roster scope = `PlayerMembership` for `session.job_id` (the whole shoot).
  Team-level comparison uses `normalize_name` / `session_norm_team`.
- Read-time flags (`match_team_mismatch`) computed in `/clusters`, never stored;
  pipeline-time flags (`match_label_conflict`, `low_confidence_match`) stored on
  `review_reason`. Same split the codebase already uses.
- Migration via the `_PHASE2_COLUMNS` ALTER-TABLE mechanism + the model; no new
  table, no Alembic.
- Testable cores: the matching stage is a plain function over seeded rows;
  synthetic unit vectors, no model, no mocking. New test file builds its own
  engine on `tmp_path`; no shared `conftest.py`.
- Single-line commit messages per section (`phase A.4 section N: ...`); `pytest -v`
  green after each (403 existing + new ~35 → ~438).
- WARNING-level pipeline logs in the `[matching] ...` style (matches
  `[pipeline]` / `[ingest]` / `[export]`).

## Acceptance

1. New `clusters` columns exist and migrate onto an existing DB at startup;
   `Cluster` model + `_PHASE2_COLUMNS` agree; no new table.
2. **Roster scope:** a session whose job has a roster matches only against that
   job's rostered players (`match_scope == "roster"`); a session with no roster
   falls back to global (`match_scope == "global_fallback"`) **and logs** the
   fallback line with the reference count.
3. **Label precedence:** no-copyright + high match → clean gap-fill label (no
   flag); copyright + agreeing match → unchanged, no flag; copyright +
   disagreeing match → copyright kept + `match_label_conflict`; `manual_label`
   beats both; **missing copyright is never flagged**.
4. **Coach signal:** a high match to a roster-coach sets `is_likely_coach=1`
   independently of the heuristic; `manual_coach_override` still wins;
   `is_coach_for_sort()` unchanged; promotion is high-tier + roster-scope only.
5. **Validation gate:** a high match to a different-team rostered player produces
   `match_team_mismatch`, blocks "Mark reviewed" when visible, and logs the full
   human-readable sentence at run time.
6. **Matching stage** runs after labeling and before sorting; `progress_stage`
   shows `"matching"`; the zero-faces guard still short-circuits before it.
7. **A.3 untouched:** `match_embedding` + `GET /api/matching/face/{id}` behave
   identically; their tests pass unmodified. `face_detector.py` unchanged.
8. **Existing UI contract intact:** `display_label()` resolution unchanged; every
   pre-A.4 `GET /clusters` field unchanged; new fields are purely additive.
9. **Global-match escape hatch:** `POST /api/clusters/{cluster_id}/global-match-suggest`
   returns top-N global suggestions marked `scope: "global_fallback"`, mutates no
   cluster state, and 404s on an unknown cluster.
10. The 403 existing tests still pass; `pytest -v` fully green.

## Out of scope (defer — do NOT build in A.4)

- **All frontend work** — the cluster-card matched-name display, confidence,
  roster-vs-fallback badge, the "Try global match" button, and the
  confirm/reject/override controls are **A.5**. A.4 only persists + exposes the
  data (including the read-only `global-match-suggest` endpoint that backs the
  button).
- **Incremental re-matching on reassign / merge / new-cluster.** `_sort_cluster`
  (the edit path) does not recompute matching, exactly as it doesn't recompute
  `auto_label` or the coach heuristic today. Matching recomputes only on a full
  `run_pipeline`. Re-matching an edited cluster is a later enhancement.
- **Demoting the coach signal** (clearing `is_likely_coach` when a match says
  "player, not coach"). A.4 is promote-only; bidirectional correction is a
  separate decision.
- **Match wins over copyright** (silent override). The locked rule is
  copyright-wins-with-conflict-flag; flipping it later is a one-liner.
- **Falling back to global when a roster exists but has no references** (Decision
  1 minor call) — currently stays roster/none.
- **ANN indexes / cross-session caching / persisting per-face matches.** A linear
  scan with a once-per-session index is fine at this scale; the only persistence
  is the per-cluster columns above.
- **Threshold tuning / live calibration via the `Setting` store.** Thresholds
  stay A.3's module constants.
- **Any change to `face_detector.py`** or to `match_embedding`'s contract.
