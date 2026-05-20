# Phase 9 — Hand-off (Wizard rebuild + browse picker + export destination + roster coverage + duplicate-label flag)

Read this in full before starting. Phase 9 has five independent slices that
share a frontend touchpoint (the create-a-job wizard) and a small backend
addition each. Commit each section on its own — they don't depend on each
other beyond Section 1's directory-browser endpoint being reused by Sections
2 and 3.

Run `pytest -v` from `backend/` after each backend change and at the end;
existing tests (258 as of Phase 8) must keep passing. Frontend has no test
runner per project convention — manual acceptance per section.

## Context you need

- The wizard lives at `frontend/src/pages/JobWizard.jsx` (folder + lines +
  subfolder + name, then `POST /api/jobs/peek` → `POST /api/jobs`).
- The job-create endpoint is [`create_job` in `backend/app/api/jobs.py`](backend/app/api/jobs.py),
  POST `/api/jobs`. Returns `{job_id, ...}` then a background task runs
  `_ingest_job`, which calls `ingest_folder` per team and chains into
  `run_pipeline` when `auto_run=true`.
- Roster CSV upload already exists from Phase 6: [`POST /api/jobs/{job_id}/roster`](backend/app/api/roster.py).
  Per-job storage in `RosterEntry`; survives roster re-upload via the
  `Session.roster_team_alias` mapping (Phase 6.1).
- Roster mismatch + folder-suggestions UI lives in
  [`RosterModal.jsx`](frontend/src/components/RosterModal.jsx).
- Export is [`_run_export` in jobs.py](backend/app/api/jobs.py) — currently
  hard-codes `out_root = root.parent / f"{root.name}_sorted"`.
- Flag visibility / `KNOWN_FLAGS` plumbing is in
  [`settings.py`](backend/app/api/settings.py). The validation gate is
  [`_compute_review_readiness`](backend/app/api/sessions.py).

Don't break: ingest, the existing roster modal (upload, mapping section,
mismatches list, reject-as-stray), the Phase 7 parallel ingest + parallel
export + per-stage timing logs, the Phase 8 GPU plumbing, the existing
review flow + flag visibility settings.

Locked-in decisions from brainstorm (don't re-litigate):

- **Roster CSV uploaded during the wizard** is purely a frontend reorder:
  the file is stashed in component state, then the existing
  `POST /api/jobs/{id}/roster` is called automatically right after the job
  is created. No new endpoint, no atomic create-with-roster combo.
- **No new wizard-side "team↔folder matching"** — the existing Phase 6.1
  folder-suggestion + alias-mapping system handles this *after* pipeline
  runs, exactly as today. The wizard does not need to ask the user about
  this at create time.
- **Folder browser is server-side**: a backend endpoint returns subdirs
  at a given path; the frontend renders a breadcrumb + clickable list.
  Works for UNC paths (`\\192.168.1.134\...`). One reusable component is
  used by both the wizard's "choose job folder" step and the export
  modal's "choose destination" affordance.
- **`duplicate_auto_label` flag is "both-but-optional"**: surfaces as a
  `▲ duplicate_auto_label` chip on every affected cluster card AND blocks
  "Mark reviewed & next" when the flag is currently visible per the
  flag-visibility settings (Phase 3). User can hide the flag globally to
  switch off both effects in one toggle — keeps a single source of truth.

---

## Section 1 — `GET /api/browse` server-side directory listing

A new endpoint that returns subdirectories under a given path, plus the
parent path (for "go up" navigation). Reused by Sections 2 and 3.

### Endpoint

```
GET /api/browse?path=<absolute-or-UNC-path>
```

Response:

```json
{
  "path": "C:\\Users\\Sportsline\\OneDrive\\Photos",
  "parent": "C:\\Users\\Sportsline\\OneDrive",
  "exists": true,
  "is_dir": true,
  "entries": [
    {"name": "2026", "is_dir": true},
    {"name": "Archive", "is_dir": true}
  ]
}
```

- If `path` is empty or `/`, return a synthetic root that lists Windows
  drive letters (call `string.ascii_uppercase` + `:\\`, keep only those
  where `Path("X:\\").exists()`).
- If `path` doesn't exist, return `{exists: false, entries: [], parent: <best-guess-parent>}` with status 200 (the UI handles it gracefully).
- Return only directories in `entries` (drop files — the picker is for
  folder selection only). Sort alphabetically, case-insensitive.
- Hide names starting with `.` and the Windows-special `$RECYCLE.BIN` /
  `System Volume Information` — avoid junk noise in the picker.
- Accept UNC paths (`\\server\share\...`) — `pathlib.Path` handles them.
- **Security guard**: reject `path` that starts with `~` or contains
  `..\\` after normalization. Allow any absolute path (the LAN-tool
  threat model already trusts the user; this is just to keep silly
  traversal mistakes out).

### File layout

New file: `app/api/browse.py` with its own router; mount in `main.py` at
prefix `/api`. Tests in `tests/test_browse.py`.

### Tests

- Listing a real `tmp_path` returns its subdirs only (files filtered out).
- `path=""` returns drive letters on Windows; one synthetic root entry
  per existing drive.
- A non-existent path returns `exists: false` and entries `[]` (not 404).
- Path with `..` after normalization → 400.
- Hidden dirs (`.git`, `.venv`) and `$RECYCLE.BIN` are filtered.
- Sorted case-insensitively (`Apple` before `banana`).

---

## Section 2 — `destination_path` on `ExportJobRequest`

Make the export destination user-selectable instead of always
`<root>_sorted`.

### Backend change

```python
class ExportJobRequest(BaseModel):
    mode: str = "copy"
    overwrite: bool = True
    destination_path: str | None = None   # NEW: absolute path; None = legacy default
```

In `_run_export`:

```python
if payload.destination_path:
    out_root = Path(payload.destination_path)
else:
    out_root = root.parent / f"{root.name}_sorted"   # legacy default preserved
```

The existing `_clear_out_root` (Phase 7 follow-up) handles the
skip-if-empty / rename-and-defer behavior on whatever path we point at.

### Frontend change

`ExportModal.jsx` gets a destination input. Default value is
`<root>_sorted` so users who don't care never have to touch it. Click
"Browse" to open the same `FolderBrowser` modal from Section 4.

### Tests (extend `test_export.py`)

- Passing `destination_path` causes files to land at that path; the
  legacy `<root>_sorted` path is NOT created.
- Omitting `destination_path` preserves the legacy default behavior
  (existing `test_overwrite_true_replaces` still passes).
- A destination on a different drive works.

---

## Section 3 — Wizard rewrite (frontend, no backend change)

Reorder steps to: **name → roster choice → roster upload (if yes) →
folder browse → lines y/n → /peek verify → subfolder pick (if needed) →
create + auto-run**.

### State + flow

```jsx
const [step, setStep] = useState("name");
const [name, setName] = useState("");
const [useRoster, setUseRoster] = useState(null);   // true | false | null (not picked)
const [rosterFile, setRosterFile] = useState(null); // File object stashed locally
const [folder, setFolder] = useState("");
const [hasLines, setHasLines] = useState(null);
const [peekResult, setPeekResult] = useState(null);
const [subfolder, setSubfolder] = useState(null);
const [creating, setCreating] = useState(false);
```

After "Create job" is clicked:

1. POST `/api/jobs` with `{name, folder, has_lines, image_subfolder_name, auto_run: true}`. Get back `job_id`.
2. If `rosterFile` is set: POST `/api/jobs/{job_id}/roster` with `multipart/form-data { file }`. Surface any warnings inline.
3. `nav(/job/${job_id})`.

If step 2 fails, the job is already created — show a non-blocking toast:
"Job created but roster upload failed (HTTP X). You can re-upload from
the job page." Don't roll back the job creation.

### Folder picker step

Two affordances side-by-side:
- **[Browse…] button** — opens the `FolderBrowser` modal from Section 4.
- **Text input** — fallback for power users / pasting a full path.

When the user picks a folder in the browser, the chosen path populates
the text input. They can edit it freely from there.

### Step UI

Reuse the existing wizard's step-styling pattern (`.wizard-steps` ul
in styles.css). Add a step indicator at top so the user knows where they
are in the flow. Each step has a Continue button that's disabled until
the step's input is valid.

### Frontend files touched

- Rewrite `frontend/src/pages/JobWizard.jsx` (most of the file).
- New `frontend/src/components/FolderBrowser.jsx` — see Section 4.

### Manual acceptance

1. Start a new job, type a name, click "Yes" for roster, upload the
   real Princeton CSV, browse to a folder, pick has_lines=no, /peek
   shows the team folders, no subfolder needed, click Create. After
   ~2s the user is on `/job/<new-id>` and the "Roster · N" header
   button on that page works (roster is already uploaded).
2. Same flow but click "No" for roster — wizard skips the upload step
   and the job page's button reads "Upload roster" as before.
3. With an invalid folder path: /peek returns an error; user can fix.
4. The existing flag-visibility settings, drag-and-drop, R shortcut,
   modal arrow + role keys all still work.

---

## Section 4 — Reusable `FolderBrowser` component

A modal that wraps the `/api/browse` endpoint. Used by both the wizard
(Section 3) and the export modal (Section 2).

### Props

```jsx
<FolderBrowser
  initialPath={...}            // optional starting path; defaults to ""
  title="Choose job folder"
  onPick={(path) => ...}       // user clicked "Use this folder"
  onClose={() => ...}
/>
```

### Behavior

- On open: GET `/api/browse?path=<initialPath>`. If `initialPath` is
  empty, the response is the synthetic drive-letter root.
- Renders:
  - Breadcrumb at top (clickable parents).
  - Editable text input showing the current path (typing Enter
    re-fetches at that path).
  - Subdirectory list below (click name → drill in; click "(use this
    folder)" → invoke `onPick(currentPath)` and close).
  - "↑ Up" button → fetches `parent` from the last response.
- Resilient-fetch pattern from Phase 5: check `res.ok`, never let an
  error body land in component state, show a friendly inline error
  on fetch failure.
- ESC closes; backdrop click closes; modal-panel styling from the
  existing roster/export modals.

### File

`frontend/src/components/FolderBrowser.jsx`.

### Manual acceptance

- Open the picker from the wizard. Navigate up to drives, back into
  the OneDrive root, into a real team-shoot folder. "Use this folder"
  populates the wizard's path input.
- Open the picker from the export modal. Pick a destination on a
  different drive. Export lands there (verified by Section 2 test).

---

## Section 5 — `GET /api/sessions/{id}/roster-coverage`

A per-team report: which roster players were photographed, which were
missed, which clusters look "extra" (auto-labeled to someone not on the
expected roster), which clusters are unidentified (no auto-label).

### Endpoint

```
GET /api/sessions/{session_id}/roster-coverage
```

Response:

```json
{
  "expected_team_norm": "10ublacksoftball",
  "expected_team_raw":  "10U-Black-Softball",
  "expected_players": [
    {"raw_name": "Eleanor-Pederson", "norm_name": "eleanorpederson"},
    {"raw_name": "June-Wampach",     "norm_name": "junewampach"}
  ],
  "present_players": [
    {"raw_name": "Eleanor-Pederson", "cluster_id": 1023, "image_count": 7}
  ],
  "missing_players": [
    {"raw_name": "June-Wampach"}
  ],
  "extra_clusters": [
    {"cluster_id": 1099, "label": "Mystery-Player", "image_count": 4,
     "roster_team_raw": "11UA-Baseball"}   // null if not on roster at all
  ],
  "unidentified_clusters": [
    {"cluster_id": 1100, "image_count": 3}   // auto_label was None / blank
  ]
}
```

### Resolution rules

1. The session's effective team (via `session_norm_team` — folder name
   OR the Phase 6.1 alias).
2. `expected_players` = every `RosterEntry` whose `norm_team` equals
   the session's effective team norm.
3. For each cluster in the session:
   - If `auto_label` is None/blank → `unidentified_clusters`.
   - Else look up `normalize_name(auto_label)` in the roster.
     - Match found AND the roster row's team == session's team →
       cluster goes in `present_players` with the roster's `raw_name`.
     - Match found BUT the roster row's team ≠ session's team →
       `extra_clusters` with `roster_team_raw` set.
     - No match anywhere in the roster → `extra_clusters` with
       `roster_team_raw` null.
4. `missing_players` = `expected_players` minus the norm_names that
   ended up in `present_players`.

### When the job has no roster

Return `{expected_players: [], present_players: [], missing_players:
[], extra_clusters: [], unidentified_clusters: []}` plus
`expected_team_raw: session.name`. The UI should just hide the section
when `expected_players` is empty.

### UI integration

- **In the `RosterModal`** — add a new section: "Coverage by team".
  One collapsible row per team showing `N of M players found, K
  missing, J unidentified clusters`. Expanding lists names.
- **On the job-page team card** — small chip "▲ 2 missing" when
  `missing_players.length > 0`, click-to-open-the-roster-modal-at-that-team.

Both surface the same data; the modal is the deep view, the chip is
the quick scan.

### Tests (`backend/tests/test_roster_coverage.py`)

- Empty roster → all-empty response.
- Every expected player has a matching cluster → `missing_players ==
  []`, `present_players` size matches expected.
- Roster has 12 players, pipeline produced 10 clusters all matching →
  2 missing.
- A cluster with `auto_label` matching a player on a different team
  shows up in `extra_clusters` with `roster_team_raw` set.
- A cluster with `auto_label` matching no roster row → `extra_clusters`
  with `roster_team_raw=None`.
- A cluster with `auto_label=None` → `unidentified_clusters`.
- Coverage uses `session_norm_team` (so a folder-aliased session
  reports against the aliased CSV team, not the folder name).
- Archived clusters / sessions excluded.

---

## Section 6 — `duplicate_auto_label` flag

Cross-cluster flag: two or more clusters in the same session share the
same normalized `auto_label`. Computed at read-time in the `/clusters`
payload (no DB write), gated by `KNOWN_FLAGS` visibility.

### Register the flag

Add `"duplicate_auto_label"` to `KNOWN_FLAGS` in
`backend/app/api/settings.py`.

### Compute in `/clusters`

In `list_clusters` (clusters.py), after `roster_lookup` is built and
before the per-cluster loop:

```python
norm_label_groups: dict[str, list[int]] = {}
for c in s.clusters:
    key = normalize_name(c.auto_label)
    if key:
        norm_label_groups.setdefault(key, []).append(c.id)
duplicate_cluster_ids = {
    cid for cids in norm_label_groups.values() if len(cids) > 1
    for cid in cids
}
```

In the per-cluster loop, splice `"duplicate_auto_label"` into the
`combined_reason` string the same way `roster_mismatch` is spliced
(via a small helper `add_duplicate_label_flag` in `roster_check.py`,
mirroring `add_roster_flag`). Never persist to `Cluster.review_reason`.

### Block "Mark reviewed & next" when visible

Extend `_compute_review_readiness` in `sessions.py`. After the existing
team/pano completeness checks, also add to `incomplete_clusters` every
cluster in `duplicate_cluster_ids` IF the `duplicate_auto_label` flag's
visibility is true in `get_flag_visibility_map(db)`. Missing reason for
those rows: `["duplicate_auto_label"]`.

User can hide the flag in settings → it stops both showing as a chip
*and* blocking review. Single toggle, single behavior.

### Tests (`backend/tests/test_duplicate_label.py`)

- Two clusters in one session with identical `auto_label` → both have
  `duplicate_auto_label` in their `visible_review_reasons`.
- A cluster whose `auto_label` is unique in its session → no flag.
- `auto_label = None` → no flag (we don't group on missing labels).
- Three clusters sharing the same label → all three flagged.
- Two clusters in *different* sessions sharing a label → no flag
  (it's per-session).
- Hiding the flag via flag-visibility settings → it doesn't appear
  in `visible_review_reasons` AND the review-readiness check passes.
- Showing the flag with two duplicate clusters → review-readiness
  blocks with `incomplete_clusters` containing both.
- The flag is NOT written to `Cluster.review_reason` in the DB
  (read-time only, like `roster_mismatch`).

---

## Conventions (unchanged)

- No new frontend deps — plain React, matching the existing modals.
- Single-line git commit messages.
- `pytest -v` from `backend/` stays green; aim for ~290+ tests at end
  of Phase 9 (~32 new across Sections 1, 2, 5, 6).
- Mirror Phase 5's resilient-fetch pattern in any new frontend fetches.
- Respect typing-guard / modifier-key conventions if you add new
  keyboard handlers (none required by this spec).
- `roster_team_alias` from Phase 6.1 is the source of truth for "which
  CSV team does this folder map to" — anywhere you compare a folder
  against the roster, go through `session_norm_team(session)`, not the
  folder name directly.

## Acceptance

1. **Browse endpoint**: `GET /api/browse?path=C:\` returns drive letter
   directories; nested paths return their subdirs; missing paths return
   `exists: false`; `..` traversal → 400.
2. **Export destination**: ExportModal has a "Destination" input
   defaulting to `<root>_sorted`. Browse picks any folder. Export lands
   there; legacy default still works when destination is unchanged.
3. **Wizard**: new flow goes Name → Roster Y/N + upload → Folder
   (browse or paste) → Lines → /peek verify → Subfolder → Create. After
   create, roster is uploaded automatically (if one was picked). User
   lands on the job page with the roster already in place.
4. **FolderBrowser**: drives → folders → subfolders, breadcrumb works,
   ESC closes, picking a folder fires `onPick`.
5. **Roster coverage**: opening the Roster modal shows a "Coverage by
   team" section. Each row lists missing players' names. Team cards on
   the job page show "▲ N missing" chips.
6. **`duplicate_auto_label` flag**: two clusters with the same name
   both show the chip on their cards. Trying to "Mark reviewed & next"
   on that team is blocked with the new reason in the gate modal.
   Hiding the flag in settings clears both behaviors.
7. `pytest -v` passes — existing 258 tests untouched, Phase 9 tests
   added for Sections 1, 2, 5, 6.

## Out of scope (defer to a follow-up phase)

- Cross-team duplicate detection (same `auto_label` in *different*
  sessions of one job). Useful but a different shape of problem from
  within-session duplication.
- A native filesystem picker via Electron / native shell — same reason
  we built the server-side browser; not worth the runtime change yet.
- Roster coverage history / per-session "last-coverage-snapshot" so
  you can spot which photos disappeared between two pipeline runs.
- FER-on-GPU (Phase 8 follow-up — waiting on real-size pipeline timing
  data before deciding).
