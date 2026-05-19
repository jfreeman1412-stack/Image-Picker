# Phase 6 — Hand-off to Claude Code (Roster cross-check + cross-session moves)

Read this in full before starting. This phase adds a roster-based safety net:
upload the CSV that already drives copyright tagging, and the app flags any
cluster whose copyright-derived name says it belongs on a different team. The
user then approves the move (or rejects the cluster as a stray). **No
automatic moves**, ever — every cross-team change is a deliberate click.

This is roughly half backend (a new table, a few endpoints, the cross-session
move primitive) and half frontend (a job-level "Roster Mismatches" panel, a
new flag on cluster cards, a roster upload affordance). Run `pytest -v` from
`backend/` after each backend touch and at the end; existing tests must keep
passing and this phase should add its own.

## Context you need (most of the wiring already exists)

A lot of this is plumbing on top of already-strong primitives — read these
before designing anything new:

- **Copyright is already extracted at ingest.** [Image.copyright_tag](backend/app/models/db_models.py#L73)
  is populated by [`_read_exif` in ingest.py](backend/app/services/ingest.py#L57)
  (JPEG/TIFF via exifread; PNG metadata path also exists). Missing tags → `NULL`,
  which is fine.
- **Per-cluster name is already derived.** [Cluster.auto_label](backend/app/models/db_models.py#L109)
  is set by [`derive_cluster_label` in labeling.py](backend/app/services/labeling.py)
  using a 60% dominance + ≥2 occurrences rule on single-face images only
  (buddy shots ignored, since their copyright can list two names). Ambiguous
  clusters → `auto_label = NULL` — that's our natural "abstain" signal.
- **Sessions are team-scoped and name-equals-folder.** Session.name is the team
  folder name (e.g. "Iron Pigs", "SB-12U White"). Job.sessions is the team set
  for a roster scope.
- **Flag visibility is already a first-class system.** `visible_review_reasons`
  on the `/clusters` payload is filtered by per-flag toggles in `Settings`. A
  new `roster_mismatch` flag plugs into the same machinery (see
  [filter_visible_reasons + get_flag_visibility_map in settings.py](backend/app/api/settings.py)).
- **Within-session reassign + resort already works.** [reassign in clusters.py](backend/app/api/clusters.py#L96)
  moves a single `Face` row between clusters in the same session and re-runs
  `_sort_cluster` + `_resort_session_outliers`. Phase 6's cross-session move
  is the same idea but moves a whole `Cluster` (with its `Face`s and
  `ImageRole`s) into another session.
- **Manual overrides survive in-session resorts.** [`_sort_cluster`](backend/app/services/face_pipeline.py#L244)
  preserves `ImageRole.manual_override=1` rows. Phase 6 deliberately does NOT
  preserve them through a cross-session merge — see Section 3.
- **Reviewed pill is per-session.** Flipping `Session.reviewed` is done by
  [`set_reviewed` in sessions.py](backend/app/api/sessions.py#L179) — it clears
  `reviewed_at` when unmarking. The cross-session move auto-unreviews the
  target (Section 3) using the same field semantics, in the same DB
  transaction.

Don't break: drag-and-drop reassignment, the `R` shortcut, validation gate,
flag visibility settings, the modal review flow (arrows + T/P/I/B/X/U/Shift
keys), the per-team `reviewed` pill, set-role / clear-role-override /
review-readiness endpoints.

---

## Section 1 — Roster ingest + storage

The user uploads the same CSV they used to write copyright tags (so cross-
referencing is a strict-exact match after normalization — no fuzzy matching).
The roster is **per-job** because team naming repeats across jobs.

### Data model

Add a new table `RosterEntry`:

| column          | type     | notes                                           |
|-----------------|----------|-------------------------------------------------|
| `id`            | int PK   |                                                 |
| `job_id`        | FK jobs  | indexed; `ON DELETE CASCADE` via relationship   |
| `raw_name`      | string   | as it appears in the CSV (for display)          |
| `norm_name`     | string   | normalized lookup key (see below)               |
| `team_name`     | string   | the team folder/Session.name as it appears      |
| `norm_team`     | string   | normalized team key (matches `norm(session.name)`)|

- Index `(job_id, norm_name)` — that's the hot lookup path.
- Auto-migrate via the existing `_PHASEX_COLUMNS` / table-create pattern in
  [app/db.py](backend/app/db.py). Add a `roster_entries` create + indexes block.
- `Job` gets a relationship `roster_entries = relationship("RosterEntry",
  cascade="all, delete-orphan")` so deleting a job cleans up.

### Normalization

One pure function `normalize_name(s: str) -> str` (in a new
`backend/app/services/roster.py`):

- Lowercase.
- Strip every non-alphanumeric character (handles `-`, `_`, `.`, spaces,
  middle-initial periods, accents → just remove them after `unicodedata.NFKD`).
- Empty → returns `""`.

Apply the same function to `norm_name` (from CSV) AND `norm(Cluster.auto_label)`
on lookup, AND to `norm_team` (from CSV) vs `norm(Session.name)` on the
mismatch check. **Same source CSV → strict equality wins; no Levenshtein.**

### Endpoints

```
POST   /api/jobs/{job_id}/roster        upload (multipart CSV) — replaces existing
GET    /api/jobs/{job_id}/roster        list current roster entries
DELETE /api/jobs/{job_id}/roster        clear the roster for this job
```

POST behavior:
- Accept `multipart/form-data` with a `file` field. Read with the stdlib `csv`
  module (UTF-8, fall back to cp1252 if decode fails).
- **No header row.** Exactly two positional columns:
  - col 0 = player name as `First-Last` (or multi-hyphen compounds like
    `Brooks-Cruz-Carter`, `Lincoln-St-Marie`, `Aiden-Lira-Campos`). This
    string equals what's burned into the copyright EXIF tag for that player's
    photos — that's the whole basis for strict-equality matching.
  - col 1 = team name (e.g. `10U-Black-Softball`, `Majors-1-Baseball`).
  - "Coach" rows are real rows in the same shape with a placeholder code in
    col 0 (e.g. `Coach-10UBlack-SB,10U-Black-Softball`). Don't special-case
    them in the parser — they're regular roster rows; the photographer
    embeds the same `Coach-XXX-YY` code in the copyright tag for those shots,
    so the existing normalization + lookup path covers them.
- Trim whitespace per cell. Skip rows where either column is empty after
  trimming. Skip rows whose column count is not exactly 2 (counted as
  `entries_skipped` with a per-row reason in logs, not in the response).
  Case quirks in the source data (e.g. `carter-Johanson`, `Dublin-McDOnnell`)
  are intentional — `normalize_name` handles them.
- Atomic replace: delete all existing `roster_entries` for the job, insert the
  new set in one transaction, commit. If parsing throws (malformed CSV /
  decode failure), abort the transaction and return 400 with the offending
  line number.
- After insert, compute `distinct_teams` from the new rows and compare with
  the job's `Session.name` set (normalized): any team in the CSV that doesn't
  match any session, AND any session that doesn't appear in the roster, goes
  into `warnings` (informational — does NOT fail the upload).
- Same `norm_name` appearing on multiple teams in the same CSV → store all
  rows but flag with `duplicate_name_warnings: [name, ...]`. The lookup must
  treat such names as ambiguous and abstain rather than picking one
  arbitrarily.
- Response: `{ entries_loaded: int, entries_skipped: int, distinct_teams:
  int, warnings: { unmatched_csv_teams: [...], sessions_missing_from_roster:
  [...], duplicate_names: [...] } }`.

### Tests (`backend/tests/test_roster.py`)

- `normalize_name` handles hyphens (single and multi), middle initials,
  spacing, casing (`carter-Johanson` → `carterjohanson`), and unicode (NFKD).
- Upload happy path: real 2-column CSV (use the Princeton sample shape:
  `Eleanor-Pederson,10U-Black-Softball`) — N entries → N stored,
  `distinct_teams` correct.
- Upload replaces, doesn't append (re-upload with 5 rows after a 10-row
  upload leaves 5 in the DB).
- Empty rows, rows with empty col 0 OR col 1, and rows with ≠ 2 columns are
  skipped and counted in `entries_skipped`.
- A name appearing on two different teams produces `duplicate_names`
  warning AND `lookup_expected_team` returns None for that name (abstain).
- A team column in the CSV that doesn't normalize-match any
  `Session.name` for the job appears in `unmatched_csv_teams` (warning only,
  not a failure).
- Multi-hyphen names round-trip (`Brooks-Cruz-Carter` → stored raw,
  normalized to `brookscruzcarter`, lookup matches a cluster whose
  `auto_label` is the same string).
- DELETE clears + GET returns empty.
- Malformed CSV (bad encoding / unterminated quote) → 400 with line number,
  no partial state.

---

## Section 2 — Roster mismatch detection (per-cluster flag)

For each cluster whose session belongs to a job with a roster, decide:

1. If `Cluster.auto_label` is `NULL` → **abstain** (already ambiguous; no
   flag). This is the graceful no-op for "no copyright / mixed copyright".
2. Otherwise look up `normalize_name(auto_label)` in the job's roster:
   - **No match in roster** → abstain. (Could be a coach, a guest player, or
     a roster typo — we don't want to scream about it.)
   - **Multiple matches in roster** (same normalized name on more than one
     team) → abstain. This was flagged at upload as a `duplicate_names`
     warning; the user can fix the CSV or accept the ambiguity.
   - **Single match → expected_team_name.** Compare
     `normalize_name(expected_team_name)` to `normalize_name(session.name)`.
     If different → flag.

The flag has a fixed reason string: `roster_mismatch`. It plugs straight
into the existing `Cluster.review_reason` + `visible_review_reasons` machinery
(comma-joined). Re-use the existing flag-visibility settings page so the user
can hide the flag globally if they want — register `roster_mismatch` in the
flag map in [app/api/settings.py](backend/app/api/settings.py) alongside the
other reasons.

### Where the check runs

This is read-time, not write-time. Don't bake the result into the DB — the
roster can be uploaded / cleared / re-uploaded at any time, and re-running the
pipeline shouldn't be required to re-evaluate flags.

Two integration points:

- **`/clusters` payload** (and any cluster-payload helper that fills
  `visible_review_reasons`): if the session's job has a roster, add
  `roster_mismatch` to `review_reason` for matching clusters before
  `filter_visible_reasons` runs. Cache the job's roster as a `{norm_name:
  norm_team}` dict per request to avoid 30× DB hits.
- **Review-readiness**: optional but recommended — let a `roster_mismatch`
  count as "needs attention" the same way an unreviewed flag does. Reuse the
  existing `clusterNeedsAttention` frontend predicate; on the backend, the
  flag already shows up in `visible_review_reasons` so no change is needed in
  `_compute_review_readiness` (it intentionally only tracks team/pano
  completeness — that's still the right bar for "blocks Mark reviewed?").

### Tests (`backend/tests/test_roster_match.py`)

- Cluster with `auto_label=NULL` → no flag.
- Cluster with `auto_label` that doesn't match any roster row → no flag.
- Cluster whose roster team == session team → no flag.
- Cluster whose roster team != session team → `roster_mismatch` appears in
  `review_reason`.
- `filter_visible_reasons` correctly hides `roster_mismatch` when its
  visibility flag is off.

---

## Section 3 — Cross-session cluster move (the new primitive)

This is the only genuinely new mutation in Phase 6. The existing per-image
reassign moves one `Face` row between clusters in the same session; this
moves an entire `Cluster` (with its `Face`s and decisions about its
`ImageRole`s) into a different session. There is currently no UI affordance
for cross-session moves — Phase 6 adds the endpoint AND the panel that drives
it (Section 4).

### Endpoint

```
POST /api/clusters/{cluster_id}/move
Body: { "target_session_id": int, "mode": "merge" | "create" }
```

Constraints / preconditions:
- Source cluster must exist and belong to a session whose `job_id == target
  session's job_id` (no moving across jobs).
- For `mode=merge`: a target cluster must be specified implicitly via "best
  match" — match by `normalize_name(target.auto_label or target.manual_label)
  == normalize_name(source.auto_label or roster_expected_name)`. If no single
  unambiguous target cluster exists in the target session, return 409 with a
  helpful error; the user should pick `mode=create` instead.
- Target session must not be archived; source can be anything.

### `mode = "create"` semantics

- Flip `Cluster.session_id` on the source row to `target_session_id`.
- `Face` rows ride along automatically (they FK to cluster_id, not
  session_id). No changes needed there.
- **Preserve** source cluster's `manual_label`, `manual_coach_override`, and
  every `ImageRole` (including `manual_override=1` rows) — the cluster is
  intact, just relocated.
- Optionally: if source cluster's `auto_label` is `NULL` but the roster has a
  match for at least some images, set `auto_label` to the roster name. Skip
  if it complicates.
- Re-run `_sort_cluster(db, cluster)` in its new context (different session,
  different sibling clusters).
- `_resort_session_outliers(db, source_session_id)` AND
  `_resort_session_outliers(db, target_session_id)` — both sessions changed
  shape.
- If `target_session.reviewed == 1`: set `target_session.reviewed = 0` and
  `target_session.reviewed_at = NULL`. (Source session's `reviewed` flag is
  intentionally untouched — losing a stray cluster doesn't invalidate
  decisions about what stayed.)

### `mode = "merge"` semantics

- Re-parent all `Face` rows: `Face.cluster_id = target_cluster_id` for every
  face whose `cluster_id == source.id`.
- **Drop ALL source `ImageRole` rows entirely** (including
  `manual_override=1`). The user explicitly decided this: a "TEAM" pick made
  in the wrong team's context doesn't have authority in the new context. The
  next step re-derives roles cleanly.
- **Drop ALL target `ImageRole` rows too**, then delete the source `Cluster`
  row. Now the target cluster has a fresh image set with no `ImageRole` rows.
- Run `_sort_cluster(db, target_cluster)` — it'll re-derive TEAM / PANO /
  individual / buddy from scratch on the combined image set. (This is the
  user's "reevaluate that cluster" behavior verbatim.)
- `_resort_session_outliers` on both sessions, same as `create`.
- If `target_session.reviewed == 1`: un-review as in `create`.

### Response

```json
{
  "status": "moved",
  "mode": "merge" | "create",
  "source_cluster_id": 123,
  "target_cluster_id": 456,            // same as source for "create"; target's for "merge"
  "target_session_id": 78,
  "target_unreviewed": true | false    // true if we just cleared a reviewed pill
}
```

### Tests (`backend/tests/test_cluster_move.py`)

- `mode=create`: cluster relocates, faces follow, manual roles preserved,
  both sessions resorted, source.image_count drops to 0 (and source is
  cleaned up if you want — or left as an empty cluster, your call; match the
  rest of the app's behavior on empty clusters).
- `mode=create` un-reviews target when target was reviewed; leaves source
  reviewed flag alone.
- `mode=merge`: faces re-parent, source cluster deleted, ImageRole rows
  cleared on both sides, `_sort_cluster` produces fresh role assignments,
  manual_override=1 rows from BOTH sides are gone.
- `mode=merge` 404 if no ambiguous match; 409 if multiple candidate clusters
  in target.
- Cross-job moves rejected.
- After move, `/clusters` for the target session lists the cluster correctly
  with the new image_count; source session no longer lists it.

---

## Section 4 — Job-level "Roster Mismatches" panel (frontend)

The workflow is inherently cross-session ("I want to see EVERY misplaced
player across this whole job"), so the surface for this lives on the job page,
not the team page. A per-cluster `▲ roster_mismatch` chip on the team page is
nice for context, but the primary review surface is one panel listing all of
them.

### New endpoint (read aggregator)

```
GET /api/jobs/{job_id}/roster-mismatches
```

Returns one row per flagged cluster:

```json
{
  "items": [
    {
      "source_session_id": 184,
      "source_session_name": "SB-12U Blue",
      "source_cluster_id": 1023,
      "source_cluster_label": "Jack Smith",     // display_label()
      "source_image_count": 7,
      "expected_team_name": "Iron Pigs",         // raw team from roster row
      "target_session_id": 172,                  // null if no Session matches the team name
      "target_cluster_id": 845,                  // null if no same-name cluster in target session
      "target_session_reviewed": true            // so the UI can warn before the move
    }
  ]
}
```

This is purely derived — no new table. Build it by joining `roster_entries`
against every cluster across the job's sessions. If the roster's team name
doesn't resolve to an actual `Session.name` in the same job, `target_session_id`
is `null` and the row is informational only (no move possible — surface as
"team not found").

### Panel UX

New page or modal accessible from the job page header: a button "Roster
mismatches (N)" next to "Run pipeline / Export / Archive". N is the
`items.length`. If no roster is uploaded yet, the button is replaced by an
"Upload roster" affordance (Section 1's POST endpoint).

Each row shows: source team → expected team, the cluster's display label, the
image count, and one or two action buttons:

| Condition | Buttons |
|----|----|
| `target_session_id` and `target_cluster_id` both present | **Merge into existing cluster in <Expected Team>** *(highlighted default)* · **Create new cluster in <Expected Team>** · **Reject as stray** |
| `target_session_id` present, no `target_cluster_id` | **Create new cluster in <Expected Team>** *(highlighted default)* · **Reject as stray** |
| `target_session_id` is `null` (team not in job) | Read-only row: "Expected team *<X>* is not in this job. Verify the roster." |

Behaviors:
- Each action POSTs to the relevant endpoint (`/move` for the first two,
  `/set-role` per-image with `role=rejected` for "Reject as stray").
- Each action is **explicit** — no batch "fix all" button (per the user's
  rule: every cross-team change is approved individually).
- If `target_session_reviewed` is true, surface a one-line warning in the
  row: "Iron Pigs is marked reviewed — moving this cluster will un-review
  it." Do not block.
- After a successful action, refetch `/roster-mismatches` and re-render. The
  row disappears (or remains if the action didn't resolve the mismatch, e.g.
  "Reject as stray" leaves images on the wrong team but flagged differently).

### Reject as stray (no new endpoint)

"Reject as stray" sets `role=rejected` (with `manual_override=1`) on every
image in the source cluster, via the existing `POST /api/sessions/{id}/set-
role` endpoint called per image. That keeps the cluster in place but excludes
its images from the team's exports — using the existing rejected-role
plumbing rather than adding a new "rejected cluster" concept.

### Frontend file plan

- New: `frontend/src/pages/RosterMismatches.jsx` (the panel). Plain React
  state + fetch, matching the existing page style. No new deps.
- New: `frontend/src/components/RosterUpload.jsx` — file input + POST. Show
  the `entries_loaded` count on success.
- Edit: the existing `frontend/src/pages/JobDetail.jsx` (or equivalent) to
  add the "Roster mismatches (N)" header button + Upload affordance.
- Edit: `frontend/src/components/ClusterCard.jsx` — no rendering change
  required if `roster_mismatch` flows through `visible_review_reasons`
  (existing review-badge map renders any reason); add a friendlier display
  label for the reason text if desired.

---

## Section 5 — Wiring + conventions

- Register `roster_mismatch` in the flag-visibility map in
  [app/api/settings.py](backend/app/api/settings.py) so the user can hide it
  globally via the existing settings page.
- No frontend dependencies — plain React state, matching existing patterns.
- Roster ingest validates input + atomically replaces; never partial state.
- The cross-session move is atomic per call (single DB transaction). If
  anything fails, the move is rolled back and neither session changes.
- Single-line git commit messages on Windows.
- `pytest -v` from `backend/` stays green and adds Phase 6 tests
  (test_roster.py, test_roster_match.py, test_cluster_move.py).

## Conventions reminder

- Same-source CSV = strict normalized-exact match. No fuzzy matching, no ML.
- Manual-only moves. No "auto-resolve all mismatches" button at any layer.
- Graceful no-op: no roster → no flags; no copyright tag on a cluster →
  cluster abstains. Phase 6 must never make the app worse when this data is
  missing.
- Reuse `_sort_cluster` and `_resort_session_outliers` after any move. They
  already encode the right post-mutation behavior.

## Acceptance

1. Upload a roster CSV via the job page; success modal shows entries loaded
   + distinct teams. Re-upload replaces cleanly.
2. A cluster whose `auto_label` matches a roster row for a different team
   shows a `roster_mismatch` flag on its cluster card; one whose roster team
   matches its session does not; a cluster with no `auto_label` does not.
3. The job-level "Roster mismatches (N)" panel lists every flagged cluster
   across all sessions in the job, with per-row source/expected/count/actions.
4. "Merge into existing cluster" moves all `Face` rows to the target cluster,
   drops both sides' `ImageRole` rows, re-runs `_sort_cluster`, and the new
   merged cluster has freshly-derived TEAM/PANO/individual/buddy assignments.
5. "Create new cluster" relocates the source cluster intact into the target
   session — `manual_label`, manual roles, manual coach override all
   preserved.
6. If the target session was `reviewed`, after any move its reviewed pill is
   cleared and the user is expected to re-review it (no popup, no extra step).
7. "Reject as stray" sets every image in the source cluster to
   `role=rejected` via the existing endpoint — cluster stays in source
   session, images exclude from export.
8. Drag-and-drop reassignment, the `R` shortcut, the validation gate, flag
   visibility settings, and Phase 5's modal review flow (arrows + T/P/I/B/X/
   U/Shift + `?` help) all still work.
9. `pytest -v` passes — existing tests untouched, Phase 6 tests added.

## Out of scope (deferred — note in HANDOFF.md when done)

- Fuzzy name matching (the same-CSV invariant makes it unnecessary; revisit
  only if rosters diverge from copyright in practice).
- Roster-driven cluster *naming* — e.g. auto-renaming `Cluster.manual_label`
  to the roster name on import. Tempting but it's a different feature (auto-
  apply vs. manual-approve) and conflicts with the "manual moves only" rule.
- Batch "approve all mismatches that are unambiguous". Same reason: every
  cross-team change is a click.
- Roster-vs-copyright *consistency* warnings (e.g. "the roster has Jack Smith
  on Iron Pigs, but no Iron Pigs photos tagged Jack Smith"). Useful but
  separate.
