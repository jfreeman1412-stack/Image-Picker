# Phase 10 — Hand-off (Cross-team naming-error detection + navigable mismatches)

Read this in full before starting. Two related pieces, both surfaced in the
Roster modal:

1. **Cross-team naming errors** — a player name (`Cluster.auto_label`) that
   appears as a cluster in **2+ different teams** in the same job. Use face
   embeddings to classify each:
   - **same_face** → one player photographed in two folders (mis-foldered):
     recommend moving one into the other.
   - **different_face** → two different kids tagged with the same name,
     because the photographer didn't update the camera's copyright field
     when switching teams: recommend reviewing both teams (the *name* is
     wrong on one cluster, not the folder).
2. **Navigable mismatches** — make the existing Mismatches rows link to the
   source + target team cards and state the recommendation in plain words.

Run `pytest -v` from `backend/` after each backend change and at the end;
existing tests (308 as of the Phase 9 safety guards) must stay green.

## Context you need

- Clustering: [`cluster.py`](backend/app/services/cluster.py) uses DBSCAN with
  cosine distance, **eps = 0.4** (`min_samples=2`). That eps is the
  pipeline's own "same person" cutoff — reuse it so the verdict is
  consistent with how faces were grouped originally. (Add `DEFAULT_EPS = 0.4`
  as a module constant and reference it from both the clusterer default and
  the new service.)
- Face embeddings: `Face.embedding` is a 512-d float32 ArcFace vector stored
  via `np.float32(...).tobytes()`, L2-normalized. Read back with
  `np.frombuffer(f.embedding, dtype=np.float32)`.
- Roster lookups + normalization: [`roster.py`](backend/app/services/roster.py)
  — `normalize_name`, `build_lookup`. Effective team for a session is
  `session_norm_team(session)` (honors the Phase 6.1 alias).
- The existing per-session same-name flag is `duplicate_auto_label`
  (Phase 9 Section 6) — that's *within* one session. Phase 10's detection is
  *across* sessions, which Phase 9 explicitly deferred.
- The Mismatches list + folder-suggestions + coverage UI all live in
  [`RosterModal.jsx`](frontend/src/components/RosterModal.jsx). The
  mismatches aggregator is `GET /api/jobs/{id}/roster-mismatches`
  ([`roster.py` `list_mismatches`](backend/app/api/roster.py)); it already
  returns `source_session_id` / `target_session_id` / `expected_team_name`
  / etc.

Don't break: existing roster modal sections (upload, mapping, mismatches,
coverage), the review flow, flag visibility, the Phase 9 run-all gate.

---

## Section 1 — backend: naming-error detection

### New service: `app/services/naming_errors.py`

```python
def cluster_centroid(embeddings) -> np.ndarray | None
    # mean of the (already L2-normalized) embeddings, re-normalized.
    # None if the cluster has no usable embeddings.

def centroid_distance(a, b) -> float
    # cosine distance between two normalized centroids: 1 - dot(a, b).

def find_cross_team_name_collisions(db, job_id) -> list[dict]
    # 1. Gather every non-archived session's clusters with a non-null
    #    auto_label.
    # 2. Group by normalize_name(auto_label).
    # 3. Keep only groups whose clusters span >= 2 DISTINCT session_ids.
    # 4. For each group, compute each cluster's centroid and the pairwise
    #    centroid distances. verdict = "different_face" if ANY pair exceeds
    #    DEFAULT_EPS (different people share the name), else "same_face".
    # 5. Return one row per group (see shape below).
```

Row shape:

```json
{
  "norm_name": "jameswrzosek",
  "raw_name": "James-Wrzosek",
  "verdict": "different_face",          // or "same_face"
  "max_distance": 0.62,
  "min_distance": 0.62,
  "clusters": [
    {"session_id": 201, "session_name": "12AAA", "session_reviewed": true,
     "cluster_id": 1023, "image_count": 6,
     "roster_team_raw": "12U-White", "in_correct_team": false},
    {"session_id": 204, "session_name": "12U White", "session_reviewed": true,
     "cluster_id": 1099, "image_count": 10,
     "roster_team_raw": "12U-White", "in_correct_team": true}
  ]
}
```

- `roster_team_raw` per cluster: the team the roster says this *name* belongs
  to (None if the name isn't on the roster). `in_correct_team`:
  `session_norm_team(session) == normalize_name(roster_team_raw)`.
- Sort rows: `different_face` first (more urgent), then by raw_name.
- `raw_name`: prefer a cluster's `display_label()`; fall back to the
  auto_label.

### Endpoint

```
GET /api/jobs/{job_id}/naming-errors  ->  {"items": [...]}
```

Per-session (non-archived) only. If the job has no roster the detection
still runs (cross-team duplicate auto_labels are suspicious regardless of
roster), but `roster_team_raw` will be None for every cluster — that's fine.

### Tests (`backend/tests/test_naming_errors.py`)

Build clusters with synthetic embeddings (np arrays → `.tobytes()`):

- Two clusters, same auto_label, **same** embeddings (distance ~0) in two
  sessions → one row, verdict `same_face`.
- Two clusters, same auto_label, **orthogonal/opposite** embeddings
  (distance > 0.4) in two sessions → verdict `different_face`.
- Same auto_label but both clusters in the **same** session → NOT returned
  (that's the within-session `duplicate_auto_label` case, not cross-team).
- A name in only one session → not returned.
- `in_correct_team` is True for the cluster whose session matches the
  roster team, False for the other.
- Three clusters, two matching faces + one stranger → verdict
  `different_face` (any mismatched pair is enough).
- `cluster_centroid` re-normalizes (norm ~1.0); returns None for a cluster
  with no embeddings.
- Archived sessions excluded.
- No roster → still detects collisions, `roster_team_raw` all None.

---

## Section 2 — frontend: surface in RosterModal

### New "Possible naming errors" section

Above the existing Mismatches list (it's higher-signal). Fetch
`GET /api/jobs/{job.id}/naming-errors` in the modal's `load()`. One row per
item:

```
James-Wrzosek  ⚠ different faces (likely a naming error)
  12AAA (6 imgs) ·  ↗            ← link to /session/201
  12U White (10 imgs) · ✓ correct team ·  ↗   ← link to /session/204
  The same name is on two different kids — the photographer probably didn't
  update the copyright field when switching teams. Open both and rename the
  wrong one.
```

For `same_face` verdict:

```
Jack-Smith  ✓ same face in two folders
  10U Black (5 imgs) ·  ↗
  10U Orange (3 imgs) ·  ↗
  Same player photographed in two folders — use the Mismatches actions to
  move one into the other.
```

- Each team is a `<Link to={/session/${session_id}}>` (react-router is
  already in use). Clicking closes the modal and navigates.
- Mark the cluster whose `in_correct_team` is true with a small "✓ correct
  team" tag.
- Surface `session_reviewed` with the same "moving will un-review" caution
  the mismatches rows use, where relevant.

### Navigable mismatches (the ask-#1 polish)

In the existing Mismatches rows, render `source_session_name` and
`expected_team_name` as links to `/session/{source_session_id}` and
`/session/{target_session_id}` respectively (when those ids are present),
and add a one-line plain recommendation:
*"<player> should be on <expected_team> (roster); currently in
<source_team>. Recommended: move."*

Keep the existing Merge / Create / Reject buttons.

### Frontend files

- `RosterModal.jsx` — add the naming-errors fetch + section, and the
  link/recommendation polish on mismatch rows.
- `react-router-dom`'s `Link` (or `useNavigate` + onClose) — already a dep.

Manual acceptance (no frontend test runner):
- A job with a known mis-named player shows a `different_face` row linking
  to both teams.
- A genuine mis-foldered player (same face) shows a `same_face` row.
- Mismatch rows are now clickable through to the team cards.

---

## Conventions (unchanged)

- Reuse `DEFAULT_EPS` from `cluster.py` — don't invent a new threshold.
- No new frontend deps.
- Resilient fetch (check `res.ok`, never store error bodies) — Phase 5 rule.
- Single-line git commit messages; `pytest -v` green at the end (~320 tests
  after this phase, ~12 new).
- Read-time only — naming-error detection computes on request, persists
  nothing.

## Acceptance

1. `GET /api/jobs/{id}/naming-errors` returns cross-team same-name groups
   with a `same_face` / `different_face` verdict driven by centroid cosine
   distance against eps=0.4.
2. The Roster modal shows a "Possible naming errors" section; each row links
   to the involved team cards and states the recommendation.
3. Existing Mismatches rows are now click-through to the source + target
   teams with a plain-words recommendation.
4. `pytest -v` passes — existing 308 untouched, Phase 10 tests added.

## Out of scope (defer)

- Auto-renaming the wrong cluster — Phase 10 only *detects + navigates*; the
  user fixes the name via the existing rename UI (manual-approval principle).
- Using embeddings to auto-suggest the *correct* name for a mis-named
  cluster (would need a cross-job face identity index — separate feature).
