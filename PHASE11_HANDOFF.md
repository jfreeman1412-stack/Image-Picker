# Phase 11 — Hand-off (Cross-team guest-cluster detection)

Read this in full before starting. This phase tackles the "phantom sibling"
problem: two siblings on **different teams** take a buddy photo together,
it's ingested into one sibling's team folder, and the *other* sibling's face
forms a cluster in that folder containing **only the shared buddy shot(s)**
with no solo portrait. Today that phantom cluster either gets nagged with
`no_team_pick` / `no_pano_pick` or is mis-classified as a coach — even though
no coach is involved.

The detection + handling here is grounded in a **validated real example**
(job 18): cluster 1839 in "10U Black" ("Player 1839", 0 solo, both images
shared only with Barrett-Hurkman) matches cluster 2059 = **Elliot-Hurkman**
in "Coach Pitch Rockies" (5 solo portraits) at cosine distance **0.071**.
Same surname, confirmed by face. Elliot is Barrett's sibling, a real player
on another team, who landed in Barrett's buddy shot.

Run `pytest -v` from `backend/` after each backend change; existing tests
(320 as of Phase 10) must stay green.

## What the investigation proved (don't re-derive)

Three candidate discriminators were tested against jobs 17 + 18:

| Signal | Result |
|---|---|
| Age (kid vs adult) | ✗ Unreliable — sibling phantoms read as 26–29; useless in the 23–35 band |
| Co-occurrence count (1 vs many players) | ✗ Every no-solo cluster co-occurs with only 1–2 players, coaches included |
| **Cross-team face match to a solo-having player** | ✓ The reliable one — match ⇒ guest from another team; no match ⇒ leave it (maybe a real coach) |

So: a cheap **structural filter** finds candidates, and an **embedding
cross-match** (reusing Phase 10's centroid machinery) confirms guest vs
"leave alone."

## Context you need

- Centroid helpers already exist in
  [`naming_errors.py`](backend/app/services/naming_errors.py):
  `cluster_centroid`, `centroid_distance`, `_cluster_centroid_from_db`.
  Reuse them.
- `cluster.DEFAULT_EPS = 0.4` is the cosine-distance "same person" cutoff —
  reuse it (same bar the pipeline + Phase 10 use).
- `Face.embedding` = 512-d float32 L2-normalized, `np.frombuffer(..., float32)`.
- A "solo" / single-face image = `COUNT(faces WHERE image_id=X) == 1`.
- Review-readiness gate: [`_compute_review_readiness`](backend/app/api/sessions.py).
- Cluster payload + flags: [`list_clusters`](backend/app/api/clusters.py).
- Roster endpoints live in [`roster.py`](backend/app/api/roster.py) mounted at
  `/api/jobs`.

Don't break: roster modal sections, naming-errors (Phase 10), the Phase 9
run-all gate + 0-faces guard, the review flow, coach detection for real
coaches.

---

## Section 1 — backend detection + gate fix

### New service `app/services/guest_clusters.py`

```python
def structural_candidate_ids(db, session) -> set[int]:
    # Clusters in this session with ZERO single-face images AND every image
    # is shared with another cluster in the same session. Cheap (no
    # embeddings). These are the "no solo, buddy-only" clusters — either a
    # guest sibling or a real coach.

def find_guest_clusters(db, job_id) -> list[dict]:
    # For each non-archived session, take its structural candidates and try
    # to match each candidate's centroid against the centroid of every
    # solo-having cluster in OTHER sessions of the job (distance <= DEFAULT_EPS).
    # A match means the candidate is that player, who belongs to the other
    # team. Returns one row per CONFIRMED guest:
    #   {
    #     "session_id", "session_name", "cluster_id", "image_count",
    #     "guest_name",        # matched cluster's display_label()
    #     "guest_session_id",  # the team they really belong to
    #     "guest_team",        # that session's name
    #     "distance",          # cosine distance of the match (lower = surer)
    #   }
    # Cost control: only loads embeddings when a session actually has
    # structural candidates (usually 0). Compute each other-team
    # solo-cluster centroid once per call and reuse across candidates.
```

Matching detail: a candidate may match more than one other-team cluster —
keep the **closest** (min distance). Only emit a row when distance ≤
DEFAULT_EPS. No match ⇒ not a guest, omit it (leave the cluster as-is; it
may be a real coach).

### Endpoint

```
GET /api/jobs/{job_id}/guest-clusters  ->  {"items": [...]}
```

In `roster.py` (job-scoped, alongside the other aggregators). 404 if job
missing.

### `/clusters` payload — add a `guest_of` field (read-time)

For the requested session, compute its guest map (call a per-session helper;
it does embedding work only if the session has structural candidates). For
each cluster add:

```json
"guest_of": null
// or, for a confirmed guest:
"guest_of": {"name": "Elliot-Hurkman", "team": "Coach Pitch Rockies", "session_id": 240}
```

This lets the cluster grid badge/collapse the card. Clusters that aren't
guests get `guest_of: null` and render exactly as today.

### Review-readiness gate fix (structural, cheap — no embeddings)

In `_compute_review_readiness`, **skip any cluster with zero single-face
images** from the team/pano requirement. Such a cluster can never satisfy a
team/pano pick (those roles come only from single-face photos), so nagging
is pointless — and it's exactly the phantom shape. This fixes the
`no_team_pick` / `no_pano_pick` block for both guest siblings AND real
coaches-with-no-solo, without needing the embedding match in the gate.
(The "missing players" signal already lives in the Phase 9 roster-coverage
report — that's the right place for "this kid has no portrait", not the
mark-reviewed gate.)

### Tests (`backend/tests/test_guest_clusters.py`)

Build clusters with synthetic embeddings (orthogonal basis vectors → easy
same/different faces), and solo vs buddy images via face-count.

- A candidate (0 solo, all-shared) whose centroid matches a solo-having
  cluster in another session at distance < eps → returned with the matched
  name/team/session.
- The same candidate but the only matching cluster is in the SAME session →
  not a guest (must be cross-team).
- A candidate with no cross-team match → omitted (e.g. a real coach).
- A cluster WITH solo images is never a candidate even if it shares buddy
  shots.
- Closest match wins when a candidate is near two other-team clusters.
- Archived sessions excluded (neither source nor match target).
- Gate: a zero-single-face cluster is excluded from
  `_compute_review_readiness` incomplete list; a normal cluster missing
  team/pano still blocks.

---

## Section 2 — frontend: surface guests in the review grid

In [`SessionDetail.jsx`](frontend/src/pages/SessionDetail.jsx): fetch
`GET /api/jobs/{job_id}/guest-clusters` once on load (one job-level call,
like readiness), build a `clusterId -> guest_of` map, and pass each
cluster's `guest_of` down to `ClusterCard`. (Equivalently, read the
`guest_of` field now on the `/clusters` payload — either source works; the
job-level call avoids re-deriving per cluster.)

In [`ClusterCard.jsx`](frontend/src/components/ClusterCard.jsx), when
`guest_of` is set:

- Render a clear banner instead of the normal name/coach pill:
  **"Guest: Elliot-Hurkman — belongs to Coach Pitch Rockies (not this team)"**
  with a link to that team's session page.
- **Suppress** the COACH pill and the team/pano completeness chip on guest
  cards (they're not a member of this team — those controls are noise).
- Visually de-emphasize: a `.cluster-card.guest` style (muted/!), and
  ideally sort guest cards to the end of the grid (pass an `isGuest` hint
  up to `SessionDetail`'s sort, or just style in place).
- Keep the thumbnails visible (the user may still want to confirm), but the
  message is "this is someone else's kid in a buddy shot — handle on their
  real team."

Manual acceptance:
- Open job 18 → 10U Black. Cluster 1839 shows the guest banner naming
  Elliot-Hurkman / Coach Pitch Rockies, no coach pill, no team/pano nag.
- Cluster 1831 (no cross-team match) is unchanged — still a normal card
  (could be a real coach), and no longer blocks "Mark reviewed" because it
  has zero solo images.

---

## Conventions (unchanged)

- Reuse `DEFAULT_EPS` + the `naming_errors` centroid helpers — don't invent
  new thresholds or duplicate the math.
- Read-time only — persist nothing; detection reflects current cross-team
  state (it can't be precomputed per-session since it needs every team
  processed).
- Resilient fetch on the frontend (check `res.ok`).
- Single-line commits; `pytest -v` green (~333 after this phase, ~13 new).
- Cost: only load embeddings when a session has structural candidates;
  document that a future optimization is a stored per-cluster centroid
  column if job-scale matching gets slow.

## Acceptance

1. `GET /api/jobs/{id}/guest-clusters` returns confirmed cross-team guests
   with matched name/team/distance; job 18 returns the Elliot-Hurkman row
   for 10U Black cluster 1839.
2. A zero-single-face cluster no longer blocks "Mark reviewed & next".
3. In the review grid, a confirmed guest card shows the "belongs to <team>"
   banner, with the coach pill and team/pano chip suppressed; non-guests
   are unchanged.
4. `pytest -v` passes — existing 320 untouched, Phase 11 tests added.

## Out of scope (defer)

- Auto-moving/auto-deleting the guest cluster — Phase 11 only detects +
  surfaces; the user decides (consistent with every prior phase). A
  one-click "reject as not-on-this-team" could be a follow-up.
- A stored centroid column for performance — only if matching gets slow at
  scale.
- The ambiguous no-match candidates (possible real coaches with no solo) —
  left to existing coach detection; not reclassified here.
