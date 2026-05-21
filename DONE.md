# Phase A.2 — DONE

Implemented `PHASE_A2_REFERENCE_UPLOAD.md` in full. Three sections, three
commits, plus this summary. Branch: `phase-A1-overnight` (continued).

## What got built

The first real face data hanging off the A.1 identity spine: a reference photo
per player, its 512-d InsightFace embedding, automatic quality gates, on-disk
storage, and the upload/list/delete/replace API. **Purely additive** — one new
table, one new service, one new router, one new test file, and a 2-line
registration in `main.py`. `face_detector.py` (the locked InsightFace
integration) was not touched; A.2 only calls `detect_faces`.

### Section 1 — Model (`backend/app/models/db_models.py`)
- **`ReferenceFace`** — one row per uploaded reference photo. Columns:
  `player_id` (FK, hard link), `captured_job_id` (nullable FK — provenance),
  `image_path`, `original_filename`, `embedding` (512-d float32 `.tobytes()`,
  stored exactly like `Face.embedding`), `det_score`, `bbox` (JSON),
  `face_area_ratio`, `created_at`. Index on `player_id`.
- Added `references` relationship to `Player` with
  `cascade="all, delete-orphan"`: deleting a Player removes its reference
  **rows** (file cleanup is the delete/replace service paths' job). Deleting a
  Job does **not** touch references (no relationship from `Job`) — a person's
  reference rightly survives a shoot's deletion; a stale `captured_job_id` is
  accepted.
- No `db.py` change — `create_all` builds `reference_faces` at startup
  (verified: `init_db()` adds the table, leaves all existing tables/data
  intact).

### Section 2 — Quality gate + service (`backend/app/services/references.py`)
- `evaluate_reference_quality(detections)` — **pure** function (no I/O, no
  model) that picks the single reference face or raises `ReferenceQualityError`
  with a stable `code` + clear message. First failure wins:
  `no_face` → `multiple_faces` → `low_confidence` → `face_too_small`. The
  multiplicity check fires at the detector's 0.5 floor, so **any second real
  face triggers `multiple_faces`** (the requested over-alert on bystanders).
  Thresholds: `REF_MIN_DET_SCORE = 0.65`, `REF_MIN_AREA_RATIO = 0.02`.
- `add_reference` — validates player (404) + optional captured job (404), runs
  `face_detector.detect_faces`, enforces the gate (no row / no file on
  failure), then persists row + file at `data/references/{player_id}/{id}{ext}`.
  Appends — multiple references per player allowed. Stores original bytes (no
  re-encode), extension sanitized to jpg/jpeg/png.
- `list_references`, `delete_reference` (removes row **and** file),
  `replace_player_references` (validates the new photo **before** wiping, so a
  failed replace leaves existing references intact).
- `REFERENCES_DIR` is a module-level dir (mkdir at import), monkeypatchable in
  tests — mirrors `api/images.py`'s `THUMB_DIR`.

### Section 3 — API (`backend/app/api/references.py`, registered in `main.py`)
- `POST   /api/players/{player_id}/references` — multipart upload (append);
  optional `?job_id=` provenance; `ReferenceQualityError` → 400
  `{error, message}`.
- `GET    /api/players/{player_id}/references` — list (404 if player missing).
- `PUT    /api/players/{player_id}/references` — replace all with one.
- `DELETE /api/players/{player_id}/references/{ref_id}` — delete one.
- `GET    /api/players/{player_id}/references/{ref_id}/image` — serve the photo
  (`FileResponse`).
- All routes nest under `/{player_id}/references...` — no collision with A.1's
  `/{player_id}` route.

## Test count

- **Baseline (Phase A.1): 352 passed** — confirmed (collection = 352) before
  any change.
- **New this phase: 32** in `backend/tests/test_references.py` (Section 1: 3,
  Section 2: 20, Section 3: 9 — above the hand-off's ~24 estimate because the
  quality gate and rejection paths were parametrized for full coverage).
- **Final: 384 passed**, 0 failed. No existing test modified.

## Commits

```
d7ad6b7 phase A.2 section 3: reference upload/list/delete/replace API
d62de9c phase A.2 section 2: reference quality gate + add/list/delete/replace service
90cf392 phase A.2 section 1: ReferenceFace model
0a67c62 phase A.2 hand-off doc
```

## What to verify in the morning

1. **Tests:** `cd backend && pytest -v` → 384 passed (352 prior + 32 new).
2. **Migration safety:** the real `backend/data/player_sort.db` opens with no
   error and gains exactly one table (`reference_faces`); existing tables/data
   untouched. (Verified via `init_db()` against the live DB.)
3. **`face_detector.py` untouched:** `git diff 0a67c62 HEAD --
   backend/app/services/face_detector.py` is empty. No existing test changed
   (only the new `test_references.py` was added).
4. **Acceptance walk-through (optional, by hand or curl; needs the real
   InsightFace model + a real photo since the tests mock the detector):**
   - Upload a clean single-face photo → file lands in
     `data/references/{player_id}/`, 512-d embedding persisted, summary returns
     `det_score`.
   - Upload a photo with two people → 400 `multiple_faces`; no-face /
     low-confidence / too-small each → 400 with their own message; no row or
     file left behind.
   - Upload twice for one player → two references; `DELETE` removes one (row +
     file); `PUT` replaces all with one (a failed `PUT` keeps the old set).
   - Upload references for the same player against two different `?job_id=`
     shoots → both list under the player, each carrying its `captured_job_id`.

## Tuning / follow-ups (not blockers)

- `REF_MIN_DET_SCORE` (0.65) and `REF_MIN_AREA_RATIO` (0.02) are proposed
  starting values — tune against real check-in photos. One-line constant
  changes in `services/references.py`.
- New service code uses `db.query(...).get(id)` (emits SQLAlchemy
  `LegacyAPIWarning`) to match the existing codebase convention; migrating the
  whole codebase to `Session.get()` is a separate cleanup, out of scope here.

## Out of scope (deferred, as instructed — NOT built)

Face matching / embedding search; pipeline integration; mobile/tablet app + any
UI; image derivatives (thumbnails / re-encode / EXIF stripping); Player
merge/split implementation; orphaned-file GC beyond the delete/replace paths;
`membership_id` provenance (anchored on `captured_job_id` instead — memberships
are unstable across roster re-uploads); any change to `face_detector.py`.
