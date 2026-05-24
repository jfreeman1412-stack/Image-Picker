# Phase C.2 — Hand-off (Pre-shoot job: create image-less, attach roster, import images later)

> **Status: DRAFT for review (written 2026-05-24). Do NOT build yet.** Decisions
> were locked with the user before this doc; review the section plan, then build.

Read this in full before starting.

## Why this exists (the gap C.1 left)

C.1 put the **Player roster** upload on `JobDetail`, which assumes a job already
exists. But the only way to make a job is the image-based sort wizard, which
requires a folder of images. **Pre-shoot there are no images yet** — that's the
whole point of loading a roster early (so the capture app has names to shoot
reference photos against). So today there's no way to create the image-less job
the roster needs to hang on. C.2 decouples **job creation** from **image
ingest**.

**The model already supports this** — the coupling is only in two places:
- `POST /api/jobs` (`create_job`, `api/jobs.py:215`) mandates a real existing
  folder, counts team folders, and *immediately* spawns the `_ingest_job`
  background task. It is the **only** caller of `_ingest_job`.
- `JobWizard` is a folder-first state machine with no image-less path.

Everything else is already tolerant: `Job.root_path` is nullable; `JobList`
handles `teams === 0` ("fresh") jobs (`JobList.jsx:99,117,141`); `JobDetail` maps
an empty `job.sessions` fine; and `_ingest_job(job_id, root, has_lines, subfolder,
auto_run)` is a standalone task keyed by `job_id` — nothing forces it to run *at
creation time*. C.2 reuses it verbatim for "import later."

## ⚠️ Decisions — LOCKED 2026-05-24

1. **Separate "New shoot" entry, not wizard surgery** *(Option B).* A new
   lightweight creator makes just the `Job` row; the image wizard and
   `create_job` stay **byte-for-byte untouched** (the hard constraint: don't break
   post-shoot sort jobs).
2. **Full decoupling in one pass:** Part 1 (create image-less job + attach roster)
   **and** Part 2 (import images into that same job later, then sort).
3. **Backend = two new additive endpoints.** `POST /api/jobs/shoot` (image-less
   create) and `POST /api/jobs/{id}/import-images` (reuse `_ingest_job`).
4. **Part 2's frontend reuses `JobWizard` in a guarded "import mode"** (targets an
   existing job via a route param, skips the name/roster steps, calls
   `import-images` at the end) — DRY, and the default wizard flow is untouched.

## Backend design (additive — `create_job`/`_ingest_job` unchanged)

### `POST /api/jobs/shoot` — create an image-less job
```
body: { "name": str, "root_path": str | None }   # root_path optional (future location)
→ 200 { "job_id": int }
```
Creates only the `Job` row: `name`, `root_path` (resolved if given, else null),
`ingest_status="awaiting_images"`, `ingest_total=0`. **No folder validation, no
ingest.** New status string is additive (free-text column; list/detail just pass
it through). `create_job` and the wizard are not touched.

### `POST /api/jobs/{id}/import-images` — bring images in later
```
body: { "root_path": str, "has_lines": bool, "image_subfolder_name": str|None, "auto_run": bool=True }
→ 200 { "job_id": int, "ingest_total": int }    # same shape as create_job
404 job missing · 400 folder missing · 409 if the job already has sessions
```
Validates the folder (`root.exists()/is_dir()`), counts teams
(`_iter_team_folders`), sets `ingest_total` + `ingest_status="pending"`, and spawns
the **existing** `_ingest_job(job_id, root, has_lines, subfolder, auto_run)`. The
**409-if-sessions-exist** guard prevents accidental double-ingest (re-import is a
separate concern, out of scope). The existing `GET /api/jobs/{id}/ingest-status`
already powers the progress UI for this — no new polling endpoint.

## Frontend design

- **Part 1 — "New shoot" creator.** Add a **"+ New shoot"** action on `JobList`
  (beside "+ New job") opening a small modal: a **name** field + an **optional**
  future-folder field (reusing `FolderBrowser`). Submit → `POST /api/jobs/shoot`
  → navigate to `/job/{id}`. There the operator clicks **Player roster** (C.1).
- **Part 2 — "Import images" on a fresh job.** On `JobDetail`, when the job has
  **no sessions**, show an **"Import images"** affordance → navigates to a new
  route `/job/:id/import-images` that renders `JobWizard` in **import mode**: a
  `targetJobId` prop/param makes it skip the name + (Phase-6) roster steps, start
  at the folder step, and on "create" call `POST /api/jobs/{id}/import-images`
  instead of `POST /api/jobs`, then reuse the existing ingest-status polling and
  land back on `/job/{id}`. Default wizard flow (no `targetJobId`) is unchanged.

## Don't break

- `create_job` (`POST /api/jobs`), `_ingest_job`, and the default `JobWizard` flow
  stay byte-for-byte for post-shoot sort jobs. New endpoints/components/mode only.
- No schema change required (`Job.root_path` is already nullable; `ingest_status`
  is free-text). Don't touch `_PHASE2_COLUMNS`.
- All backend tests stay green; current baseline **486** (C.1) plus the new ones.
- `res.ok`-checked fetches; no error body in state (the house rule).

## Build plan (dependency order; each ends green + committed)

### Section 1 — Backend: `POST /api/jobs/shoot`
Add the endpoint in `api/jobs.py`. **Tests (`test_jobs.py`):** `{name}` only →
200 `job_id`, and `GET /api/jobs/{id}` shows it with 0 sessions,
`ingest_status="awaiting_images"`, null/empty root_path; with `root_path` it's
stored; it appears in `GET /api/jobs` as a zero-team job. **No** folder is
required (a nonexistent/blank path must NOT 400).

**Check:** `pytest -v` green; `curl` create-shoot returns a `job_id`.
Commit: `phase C.2 section 1: create image-less shoot job endpoint (+ tests)`

### Section 2 — Frontend: "New shoot" creator
`JobList` "+ New shoot" → modal (name + optional folder via `FolderBrowser`) →
`POST /jobs/shoot` → nav `/job/{id}`. Loading/error states; `res.ok`-checked.

**Manual check:** "New shoot" → name it → land on its JobDetail (0 teams, 0
images) → **Player roster** opens and a roster uploads against it (C.1). `npm run
build` clean.
Commit: `phase C.2 section 2: New shoot entry (image-less job creation)`

### Section 3 — Backend: `POST /api/jobs/{id}/import-images`
Wrap `_ingest_job` for an existing job. **Tests (`test_jobs.py`, which
monkeypatches `jobs.SessionLocal` so background ingest runs against the test DB):**
a temp folder with team subfolders + image files → 200 `ingest_total`, and after
the (synchronous-in-TestClient) ingest the job has sessions/images and
`ingest_status="done"`; 404 unknown job; 400 missing folder; **409 when the job
already has sessions**.

**Check:** `pytest -v` green; `curl` import-images into a shoot job creates teams.
Commit: `phase C.2 section 3: import-images-into-existing-job endpoint (+ tests)`

### Section 4 — Frontend: wizard import-mode + "Import images" affordance
Give `JobWizard` an optional `targetJobId` (via `/job/:id/import-images` route):
skip name/roster, start at folder, call `import-images` on finish, reuse
ingest-status polling. Add the **"Import images"** affordance on `JobDetail` when
the job has no sessions.

**Manual check:** on the image-less shoot from §2, "Import images" → folder →
structure → image-location → ingest runs → teams appear on JobDetail → pipeline
can run. The normal "+ New job" wizard is unchanged. `npm run build` clean.
Commit: `phase C.2 section 4: import-images wizard mode + JobDetail affordance`

### Section 5 — README + acceptance pass
Document the pre-shoot lifecycle (New shoot → Player roster → … → Import images →
sort) in `README.md`'s Rosters/overview area. Run the full acceptance list.
Commit: `phase C.2 section 5: README pre-shoot lifecycle + acceptance`

## Acceptance

1. "New shoot" creates a job with **no images/sessions**, no folder required, and
   lands on its `JobDetail`.
2. The **Player roster** (C.1) uploads against that image-less job.
3. "Import images" on that same job runs the folder→structure→image-location flow
   and ingests teams **into the existing job** (not a new one); the pipeline can
   then run and the job sorts normally.
4. The default image-based wizard and `create_job` are **byte-for-byte unchanged**;
   `pytest -v` fully green incl. new tests; no migration.

## Out of scope (defer)

- **Re-importing / appending images** to a job that already has sessions (the 409
  blocks it for now).
- **Editing a shoot's intended folder** after creation (just create another, or
  set it at import time).
- **Merging the two creators** into one unified entry — kept separate by design
  (Decision 1).
- Any change to C.1's roster behavior.
