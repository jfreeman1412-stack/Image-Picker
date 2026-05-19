# Hand-off to Claude Code

Read this first if you're an agent picking up this repo.

## What's done

- Project structure: backend (FastAPI + SQLAlchemy + SQLite) + frontend (React/Vite).
- Database models in `backend/app/models/db_models.py` matching the schema in `ARCHITECTURE.md`.
- All API routes are stubbed with working request/response shapes:
  - `backend/app/api/sessions.py` — session CRUD, pipeline kickoff
  - `backend/app/api/clusters.py` — list, reassign, merge, new, rename
  - `backend/app/api/images.py` — thumbnail and full-res serving
- **`backend/app/services/sort_rules.py` is complete and unit-tested.** Eight tests pass in `backend/tests/test_sort_rules.py`. Do not modify the rules without updating tests — these encode Joey's exact sorting logic.
- **`backend/app/services/outliers.py` is complete and unit-tested.** Three tests pass in `backend/tests/test_outliers.py`.
- Frontend pages and the cluster card UI are wired up and ready to display data once the backend produces it.

## What needs implementation (in priority order)

### 1. `backend/app/services/ingest.py` → `_read_exif()`

Wire up `exifread` to read `DateTimeOriginal` and `Copyright` from each image. Straightforward — pseudocode is in the docstring. **Test:** run `POST /api/sessions` against a folder of real photos and verify `image.capture_time` is populated in SQLite.

### 2. `backend/app/services/face_detector.py`

Install `insightface` and `onnxruntime` (already in `requirements.txt`). Implement `get_detector()` and `detect_faces()` per the docstrings. The first model load downloads ~300MB to `~/.insightface/`; that's expected. **Test:** call `detect_faces()` on a single image and verify you get back a list of dicts with `bbox`, `embedding` (shape (512,)), and `det_score`.

### 3. `backend/app/services/cluster.py` → `cluster_embeddings()`

Implement with `sklearn.cluster.DBSCAN`. The implementation is in the docstring. **Test:** feed in embeddings from ~20 photos of 3 different people and verify you get 3 distinct cluster labels (no -1 noise on clean inputs).

### 4. `backend/app/services/expression.py` → `classify_expression()`

Pick a smile/serious classifier and implement. Suggested: a pretrained FER model that returns 7 emotions, collapsed to {smiling=happy, serious=anything-else}. **Test:** classify obvious smiling vs neutral crops and verify they come out correctly.

### 5. `backend/app/services/face_pipeline.py` → fill in TODOs

The orchestrator. All the `TODO(agent)` blocks have pseudocode in the comments. **Test:** create a session against a real folder, call `POST /api/sessions/{id}/run`, wait for status=done, then check `GET /api/sessions/{id}/clusters` returns clustered images with roles.

### 6. `backend/app/api/clusters.py` → re-sort after reassign/merge

Currently the reassign and merge endpoints update `cluster_id` on Faces but don't re-run sort_rules for affected clusters. Add a helper that re-runs `assign_roles()` + outlier flagging for a single cluster after mutations.

### 7. `backend/app/api/images.py` → thumbnail caching

Currently serves the full file for thumbnails. Add PIL-based thumbnailing to 256px max edge, cache to `backend/data/thumbs/{image_id}.jpg`.

## Phase 5 (Tier 1 review-speed bundle) — done, with one deferral

Delivered per `PHASE5_HANDOFF.md`:

- **Modal arrow-key nav** (1.1): `ImageModal` now takes `images`/`index`/
  `clusterId`/`onIndexChange`; `←`/`→` move within the cluster and clamp at
  the ends (no wrap); title bar shows `Image N of M · filename` + the current
  role tag; ESC still closes; prev/next images are preloaded.
- **Single-key roles in the modal** (1.2): `T/P/I/B/X` set the role and
  auto-advance, `U` clears the override, `Shift+key` sets without advancing.
  A `? keys` button + `?` key toggle a shortcut legend. `SessionDetail` keeps
  modal state as `{cluster_id, index}` and re-derives the image list from the
  freshly-loaded `clusters` by `cluster_id` after each role change, so the
  load() refetch can't desync the modal. The `R` shortcut is suppressed while
  the modal is open (modal owns the keyboard then).
- **Per-cluster complete/incomplete state** (1.3): `SessionDetail` passes an
  `incomplete` bool + `missing` list to each `ClusterCard` from the existing
  review-readiness set. Complete = green left rail + tint + "✓ complete" chip;
  incomplete = amber left rail + "▲ needs …" chip. Rendered via a `::before`
  rail so it doesn't fight the `.review` border or `.drop-hover` shadow, and
  withheld until the first readiness fetch lands (no green-everything flash).

**Deferred — grid-selection single-key roles.** The wishlist's stretch idea of
selecting a thumbnail on the grid (without opening the modal) and assigning
roles by key was intentionally not built: it needs a new selection model
(focus/outline state, arrow traversal across cards, not colliding with native
drag) that the handoff explicitly allowed deferring rather than half-building.
The modal is the primary fast-review flow and fully covers the key bindings.

**Backend `complete` field — intentionally skipped (no backend change).** The
optional `/clusters` `complete` boolean was not added: `_compute_review_
readiness()` already computes exactly player=team+pano / coach=team, and
`SessionDetail` already fetches it. A duplicate field would only add a second
code path that could drift from the validation gate. So Phase 5 is
frontend-only; the 172 backend tests are untouched and still green.

## Phase 4 known limitation: wizard assumes one JPG-location pattern per job

The walkthrough sets one `image_subfolder_name` (or None) and applies it to every
team folder in the job. If teams have inconsistent structures — some have JPGs in
the team folder root, some in `exports/`, some in `JPG/` — the current wizard
can't capture that.

Workarounds today:
- Normalize folder structure before importing (preferred).
- Import each line separately if patterns differ across lines.

Future improvement: per-team override in the wizard, or "first folder containing
JPGs" auto-detection per team. The `create_job` endpoint already collects skipped
teams in `skipped_teams` when the chosen subfolder is missing, so partial recovery
is possible.

## Convention reminders

- Pre-print discovery overwrites existing file (matches Sportsline dashboard convention).
- Windows paths: use `pathlib.Path`, not f-string concatenation.
- Single-line git commit messages on Windows CMD.
- Run tests with `cd backend && python -m pytest -v` from the backend directory.

## How to know it works end-to-end

1. Drop ~50 photos from a real Sportsline shoot into a folder (5–6 different players, ~8 photos each, mix of single-face and a buddy shot or two).
2. Start backend (`uvicorn app.main:app --reload --port 8020`) and frontend (`npm run dev`).
3. In the UI, create a session pointing at that folder, click Run.
4. After processing, the cluster grid should show roughly one card per player, each with a TEAM badge on the last smiling single-face shot and a PANO badge on the second-to-last serious single-face shot.
5. Drag-reassign an image and confirm it moves clusters and the destination re-sorts correctly.
