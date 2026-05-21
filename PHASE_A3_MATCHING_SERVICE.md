# Phase A.3 — Hand-off (Matching service, backend only)

Read this in full before starting. This is the **third step** of the
mobile/tablet **reference-photo system**. A.1 built the identity spine (`Player`
+ `PlayerMembership`); A.2 hung reference photos + 512-d embeddings off it
(`ReferenceFace`). **A.3 builds the brain that connects a shoot face back to a
player:** given a face embedding (the kind the existing pipeline produces during
a shoot), return the best-matching `Player` above a confidence threshold,
considering all stored reference embeddings.

**Phase A.3 is the matching service in isolation.** It is **its own module**,
and the **only consumers in this phase are its own tests and one debug API
endpoint**. There is **no pipeline integration** — wiring matching into
`face_pipeline.py` is **A.4**, not this phase. Do not touch the pipeline here.

A.3 adds **no new table and no migration** — it is a read-only computation over
the existing `ReferenceFace` (and, for the debug endpoint, `Face`) rows. That
makes this the simplest phase yet: one new service, one new router, one new test
file, plus one line in `main.py`.

Run `pytest -v` from `backend/` after each section. Confirm the baseline first:
**384 tests as of Phase A.2** must stay green, alongside the new ones this phase
adds (~18).

---

## ⚠️ Decisions

All algorithm parameters were specified/confirmed by the user and are baked in
below; the rest are minor calls made for you.

**Specified/confirmed by the user (locked):**

1. **Tiered confidence thresholds (configurable module constants, calibrated
   later):**
   - `HIGH_THRESHOLD = 0.6` → **high** tier: auto-label.
   - `LOW_THRESHOLD = 0.4` → **low** tier: suggest + flag for review.
   - below `LOW_THRESHOLD` → **none**: no match.
2. **Minimum-margin rule:** when the **top two candidates both exceed
   `HIGH_THRESHOLD`**, the best must beat the second-best by at least
   `MIN_MARGIN = 0.05` (configurable). If the margin is smaller, **downgrade to
   the low tier and flag for review** (ambiguous). The margin rule only fires
   when both of the top two clear the high bar — a clean top with a distant
   runner-up is not downgraded.
3. **Similarity metric = cosine similarity on the L2-normalized 512-d
   embeddings** — exactly what InsightFace does internally. Stored embeddings
   are already unit vectors (`normed_embedding`), so cosine reduces to a dot
   product; the implementation still normalizes defensively (guard a zero
   vector) so it's correct even if a non-normalized vector ever arrives.
4. **Per-player aggregation = MAX similarity over ALL of that player's
   references (confirmed).** A player's score is the maximum cosine across
   **every** `ReferenceFace` row they own — "does this face match *any* angle
   they gave us?". MEAN and CENTROID were rejected: both undermine the point of
   multi-reference support (a single off-angle reference would drag a true match
   down, or blur distinct angles into an average face). MAX makes the data
   model's multi-reference capability genuinely useful once the UI exposes it.
   - **Correctness requirement:** the implementation must aggregate across **all**
     of a player's `ReferenceFace` rows, not just one. With today's data (one
     reference per player) MAX-over-all trivially equals that single reference,
     so the code is correct now **and** stays correct unchanged when a player
     gains multiple references later. Implement it as `MAX(sims over the player's
     references)` grouped by `player_id` — never `LIMIT 1` / "first reference".

**Minor calls made for you (flagged, easy to flip):**
- **Candidates for the margin rule are distinct `Player`s, not individual
  references.** We aggregate per player *first* (decision 4), then rank players,
  then apply the margin rule across the top two **players**. This is essential:
  two references *of the same player* being the top two similarities is **not**
  an ambiguity and must never trigger a downgrade.
- **Debug endpoint input = an existing `Face` row id.** A `Face` is exactly a
  pipeline-produced face (the real A.4 input), so `GET /api/matching/face/{id}`
  reads that face's stored embedding and runs matching — the most realistic
  debug surface. The **core function takes a raw `np.ndarray`**, so tests drive
  it with synthetic vectors and never need a real face or model.
- **Thresholds live as module constants** (mirrors A.2's `REF_MIN_DET_SCORE`).
  Promoting them to the `Setting` key/value store for live calibration without a
  redeploy is a possible future enhancement — **out of scope** here.
- **Scope is global: all stored references across all players/shoots** (per the
  user's "considering all stored reference embeddings"). Restricting candidates
  to a shoot's roster (`PlayerMembership` for the job) would sharpen precision
  but belongs to **A.4** (the pipeline knows the job). Design `match_embedding`
  so an optional candidate filter is an additive change later — don't build it
  now.

---

## What you're building (no data model)

A.3 adds **no model**. It reads:
- **`ReferenceFace.embedding`** — every stored reference, across all players.
  This is the candidate set.
- **`ReferenceFace.player_id`** + **`Player.display_name`** — to attribute a
  match to a person.
- **`Face.embedding`** — only the debug endpoint, to fetch a query embedding by
  face id.

Embeddings are read back with the locked convention from A.2 / the pipeline:
`np.frombuffer(blob, dtype=np.float32)` → shape `(512,)`, L2-normalized.

---

## Context you need

- **Embedding (de)serialization** — `np.frombuffer(row.embedding,
  dtype=np.float32)` yields the `(512,)` float32 unit vector. Both
  `ReferenceFace.embedding` and `Face.embedding` are `LargeBinary` written as
  `.astype(np.float32).tobytes()`. Do not re-detect or re-encode — A.3 only
  consumes stored vectors.
- **Cosine on normalized vectors** — for unit vectors, cosine similarity is the
  dot product. Vectorize: stack all reference embeddings into a matrix `M`
  (N×512) once, then `sims = M @ q` is the per-reference similarity vector. This
  mirrors the bulk-load + numpy pattern already in
  [`services/guest_clusters.py`](backend/app/services/guest_clusters.py) and
  [`services/naming_errors.py`](backend/app/services/naming_errors.py)
  (`np.frombuffer`, centroids, cosine).
- **Where things go** — service in `backend/app/services/matching.py`; router in
  `backend/app/api/matching.py` with one `router = APIRouter()`, mounted in
  [`main.py`](backend/app/main.py) with a prefix (like every other router).
  Testable-core-plus-thin-HTTP-wrapper split, exactly as A.1/A.2: the real logic
  is a plain function tests call directly; the route is a thin wrapper that only
  resolves the `face_id` → embedding.
- **Test conventions** — same as A.1/A.2: **no `conftest.py`**; each test file
  builds its own engine on `tmp_path`. The matching core needs **no model and no
  mocking** — tests construct synthetic unit vectors with known cosine
  similarities (see the test section for the 2-D-in-512-D trick). Seed
  `Player` + `ReferenceFace` rows directly.

**Don't break:** the existing 384 tests, the A.1/A.2 models and endpoints. A.3
is purely **additive** — new service, new router, new test file, one line in
`main.py`. **Do not modify `face_detector.py` or `face_pipeline.py`** (pipeline
integration is A.4).

---

## Section 1 — Matching core (`backend/app/services/matching.py`)

### Constants

```python
HIGH_THRESHOLD = 0.6     # >= → auto-label (high tier)
LOW_THRESHOLD = 0.4      # >= (and < HIGH) → suggest + flag (low tier)
MIN_MARGIN = 0.05        # top must beat 2nd-best by this when both clear HIGH
TOP_N_CANDIDATES = 5     # how many ranked players the result echoes (debug)
```

### Cosine + reference index

```python
def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity of two vectors. For the L2-normalized 512-d ArcFace
    embeddings we store this is a dot product; we normalize defensively so a
    stray non-unit vector still scores correctly. Returns a Python float."""
    # divide each by its norm (guard zero), then dot.

def _load_reference_index(db):
    """Bulk-load every stored reference: returns (player_ids: np.ndarray (N,),
    matrix: np.ndarray (N, 512) float32, name_by_player: dict[int, str]).
    Empty arrays + empty dict when there are no references. Separated from
    match_embedding so A.4 can later cache/reuse it across many queries."""
```

### The matcher (the heart of the phase)

```python
def match_embedding(db, embedding: np.ndarray) -> dict:
    """Match one query embedding against all stored references and return a
    tiered result. Pure-ish: reads the DB for references, no writes, no model.

    Algorithm:
      1. Load the reference index. No references → tier 'none'.
      2. sims = M @ normalize(query)  → per-reference cosine.
      3. Per player, score = MAX(sims over that player's references)  [decision 4].
      4. Rank players by score desc → top, runner_up (next DISTINCT player).
      5. Tiering:
           top.score >= HIGH:
             runner_up exists AND runner_up.score >= HIGH
                 AND (top.score - runner_up.score) < MIN_MARGIN
               → tier 'low',  reason 'ambiguous_margin', needs_review True
             else
               → tier 'high', reason 'auto_label',       needs_review False
           elif top.score >= LOW:
               → tier 'low',  reason 'low_confidence',    needs_review True
           else:
               → tier 'none', reason 'no_match',          needs_review False
    """
```

Result dict (the contract A.4 will consume; the debug API returns it verbatim):

```python
{
    "tier": "high" | "low" | "none",
    "reason": "auto_label" | "ambiguous_margin" | "low_confidence" | "no_match",
    "needs_review": bool,
    "player_id": int | None,          # best player (None only for 'none')
    "player_name": str | None,        # Player.display_name
    "score": float | None,            # best cosine, rounded (e.g. 4 dp)
    "margin": float | None,           # top - runner_up, None if < 2 players
    "runner_up": {"player_id", "player_name", "score"} | None,
    "candidates": [                   # top-N players by score, for debugging
        {"player_id", "player_name", "score"}, ...
    ],
}
```

> Notes: `score` for `tier == "none"` may still report the best (sub-LOW) score
> for debugging, but `player_id`/`player_name` are `None` there — a 'none' result
> names no player. Round scores to 4 dp for readable output; keep full precision
> for the comparisons themselves. Convert numpy floats/ints to Python types in
> the dict so it's JSON-serializable.

### Tests for Section 1 (`backend/tests/test_matching.py`)

Build synthetic **unit** vectors with exact cosine similarities using a 2-D
plane embedded in 512-D: a query `q = e0` (1 in dim 0, else 0) and a reference
`r(θ) = cos θ · e0 + sin θ · e1` has `cosine(q, r) = cos θ`. So
`r = [s, sqrt(1-s²), 0, …]` has `cosine(q, r) = s` for a target similarity `s`.
A small `_unit(*coords)` helper makes these. Seed `Player` + `ReferenceFace`
rows (embedding = `vec.astype(np.float32).tobytes()`).

Cosine (pure):
- identical vectors → 1.0; orthogonal → 0.0; a constructed `s=0.7` pair → 0.7.

Tiering / margin / aggregation:
- **No references** → tier `none`, `player_id` None.
- **Clean high:** query == player A's only reference (sim ~1.0), B far away
  → tier `high`, reason `auto_label`, `player_id == A`, `needs_review` False.
- **High with distant runner-up:** A at 0.7, B at 0.3 (B < HIGH) → `high`
  (margin rule does **not** fire because B is below HIGH).
- **Ambiguous downgrade:** A at 0.70, B at 0.69 (both ≥ HIGH, margin 0.01 <
  0.05) → tier `low`, reason `ambiguous_margin`, `needs_review` True,
  `player_id == A` (still the suggestion), `runner_up == B`.
- **Margin boundary:** A at 0.70, B at 0.65 (margin exactly 0.05) → `high`
  (≥ margin passes); A at 0.70, B at 0.66 (margin 0.04) → `low`/ambiguous.
- **Low tier:** best at 0.5 (≥ LOW, < HIGH) → tier `low`, reason
  `low_confidence`, `needs_review` True.
- **None:** best at 0.3 (< LOW) → tier `none`.
- **Threshold boundaries:** best exactly 0.6 → `high`; exactly 0.4 → `low`.
- **MAX aggregation:** player A has two references at sim 0.5 and 0.7 to the
  query → A's score is 0.7 (the max), not the mean.
- **Same-player top-two is NOT ambiguous:** player A has two references at 0.70
  and 0.69, player B's best is 0.30 → tier `high` (the 0.69 is A's own second
  reference, collapsed into A by MAX; the runner-up *player* is B at 0.30, so no
  margin downgrade).

Commit: `phase A.3 section 1: cosine matching service with tiered thresholds + margin rule`

---

## Section 2 — Debug API endpoint (`backend/app/api/matching.py`)

New file with `router = APIRouter()`. Register in
[`main.py`](backend/app/main.py): add `matching` to the
`from app.api import (...)` line and
`app.include_router(matching.router, prefix="/api/matching", tags=["matching"])`.
This is the only edit to an existing file in this phase.

Endpoint:

```
GET /api/matching/face/{face_id}   match an existing shoot face's embedding against all references
```

- `GET /api/matching/face/{face_id}`: 404 if the `Face` doesn't exist. Read
  `np.frombuffer(face.embedding, dtype=np.float32)`, call `match_embedding`,
  return the result dict. Optionally include a `"thresholds"` block
  (`{"high", "low", "margin"}`) in the response so calibration is legible from
  the wire — handy while tuning, cheap to add.

> This endpoint exists for manual inspection / calibration only. It is **not**
> consumed by the pipeline in A.3.

### Tests for Section 2

Use a `client` fixture like A.1/A.2 — `dependency_overrides[get_db]`,
`TestClient(app)`. Seed (in the same DB the client hits): a `Player` with a
`ReferenceFace`, plus an `Image` + a `Face` whose embedding is a known unit
vector. (No detector/model needed — embeddings are written directly.)

- `GET /api/matching/face/{id}` for a face equal to the player's reference →
  200, tier `high`, correct `player_id`.
- A face far from every reference → 200, tier `none`.
- Unknown `face_id` → 404.

Commit: `phase A.3 section 2: matching debug API endpoint`

---

## Conventions (match the existing code)

- Read embeddings with `np.frombuffer(..., dtype=np.float32)`; never re-detect.
- Vectorize with a single reference matrix and `M @ q`; bulk-load once per call
  (`_load_reference_index`) — same style as `guest_clusters.py` /
  `naming_errors.py`.
- Thresholds/margin are module constants at the top of `matching.py` (calibrate
  later by editing one line).
- Testable-core-plus-thin-HTTP-wrapper split; plain-dict responses;
  `{"candidates": [...]}` lists inside the result.
- New test file builds its own engine on `tmp_path` (no shared `conftest.py`);
  synthetic unit vectors, no model, no mocking.
- Single-line commit messages per section (`phase A.3 section N: ...`).
- `pytest -v` green after every section: 384 existing + new (~18 → ~402).

## Acceptance

1. `match_embedding` returns the documented result dict for every tier; no new
   table is created and `init_db()` is unchanged (A.3 adds no migration).
2. A query equal to a player's reference returns tier `high` with that player;
   a query below `LOW_THRESHOLD` returns tier `none`.
3. The margin rule downgrades an ambiguous top-two (both ≥ HIGH, gap <
   `MIN_MARGIN`) to tier `low` + `needs_review`, while a same-player top-two does
   **not** downgrade (MAX aggregation collapses it to one player first).
4. Similarity is cosine on the L2-normalized 512-d embeddings (dot product of
   unit vectors), matching InsightFace.
5. `GET /api/matching/face/{face_id}` returns the match result for a real face
   and 404s for an unknown id.
6. The existing 384 tests still pass; `pytest -v` is fully green; nothing in
   `face_detector.py` / `face_pipeline.py` changed.

## Out of scope (defer — do NOT build in A.3)

- **Pipeline integration** — wiring matching into `face_pipeline.py` (auto-label
  high-tier faces, queue low-tier for review) is **A.4**. A.3's only consumers
  are its tests and the debug endpoint.
- **Roster-scoped candidate filtering** — restricting candidates to a shoot's
  `PlayerMembership` set. Sharper precision, but it's A.4's job (the pipeline
  knows the job id). Keep `match_embedding` global; design it so an optional
  filter is an additive change.
- **Approximate-nearest-neighbour indexes / caching** — a linear scan over all
  references is fine at this scale. A persistent ANN index (faiss, etc.) and
  cross-query index caching are future performance work.
- **Writing match results anywhere** — A.3 computes and returns; it does not
  persist matches, set `Cluster.auto_label`, or create review items.
- **Threshold UI / live calibration via the Setting store.** Constants only.
- **Re-detection / embedding generation.** A.3 consumes stored embeddings;
  generating them stays in the locked `face_detector.py`.
- **Any change to `face_detector.py` or `face_pipeline.py`.**
```
