# Phase A.2 — Hand-off (Reference photo upload, backend API only)

Read this in full before starting. This is the **second step** of the
mobile/tablet **reference-photo system**. Phase A.1 (shipped, merged to `main`)
built the identity spine: a global `Player` (unique person) + per-shoot
`PlayerMembership`, loaded from the roster CSV. A.2 hangs the **first real
face data** off that spine: a reference photo per player, its 512-d face
embedding, and the upload/quality/list/delete/replace API.

**Phase A.2 builds the backend only:** a `ReferenceFace` record, an upload
endpoint that runs the existing InsightFace detector, automatic quality checks
with clear error messages, on-disk photo storage, and list/delete/replace
endpoints. There is **no** mobile app, **no** matching of shoot faces against
references, **no** pipeline integration, and **no** UI in this phase. Do not
build any of that here — see *Out of scope* at the bottom and respect it.

Run `pytest -v` from `backend/` after each section. Confirm the baseline first:
**352 tests as of Phase A.1** must stay green, alongside the new ones this phase
adds (~24).

---

## ⚠️ Decisions

All three were confirmed with the user in the A.2 kickoff.

1. **Multiple references per player — YES.** A `Player` may have many
   `ReferenceFace` rows (different angles / lighting / sessions). This improves
   future face matching. "Replace" therefore means *wipe this player's
   references and set one fresh* (the wipe-and-set analog of A.1's
   `replace_shoot_memberships`); "delete" removes a single reference by id.
   - **Wipe-and-set over a soft-delete `is_active` flag (confirmed):** the
     hard wipe matches the UX cleanly and keeps A.2 lean. If a use case for
     soft-delete/history emerges later, it's a purely **additive** schema change
     (add a nullable `is_active`/`deleted_at` column) — don't pre-build it now.
2. **One uploaded photo must contain exactly one face — alert otherwise.** The
   user explicitly wants to be **alerted when a photo has two different faces**
   (e.g. a bystander at the check-in table) so the wrong person never gets
   embedded as a reference. The multiplicity check therefore fires at the
   detector's base `DET_SCORE_THRESHOLD` (0.5) floor — *any* second real face,
   not just a confident one, triggers the `multiple_faces` rejection. This is
   intentionally aggressive (a faint background face means "retake"), per the
   user's preference to over-alert rather than risk a poisoned reference.
3. **Provenance key on `ReferenceFace`: `player_id` (required) +
   `captured_job_id` (nullable).** A.1's split-friendly mandate says any data
   attached to a `Player` must be re-partitionable when a `Player` is later
   split, so a reference must record *where it was captured*. The reference
   belongs to the **person** and follows them across shoots; `captured_job_id`
   records the shoot it was taken in so a future split can route it to the right
   kid. The upload endpoint takes `player_id` (+ optional `job_id`).
   - **Why `job_id`, not `membership_id`:** a membership would be richer
     (player+job+team in one), **but membership rows are wiped and recreated on
     every roster re-upload** (`replace_shoot_memberships` deletes then
     re-inserts), so a stored `membership_id` would dangle. Jobs are stable;
     players are upserted (never deleted); memberships are not. `job_id` is the
     stable provenance anchor.
   - **Why not `player_id`-only:** simplest, but stores no capture context and
     violates the split-friendly rule.

Minor calls made for you (flagged, easy to flip):
- **Thresholds:** `REF_MIN_DET_SCORE = 0.65`, `REF_MIN_AREA_RATIO = 0.02` (face
  bbox ≥ 2% of frame area). Proposed starting values — tune against real
  check-in photos in a later pass. They live as module constants so tuning is a
  one-line change.
- **Store the original uploaded bytes** (no re-encode), under the original
  extension (`.jpg`/`.jpeg`/`.png`, sanitized; default `.jpg`). Lossless,
  simple, and lets tests use arbitrary bytes since detection is mocked. (A
  normalized-JPEG/thumbnail derivative is a future nicety — out of scope.)
- **Include a thin image-serving GET** (`.../references/{ref_id}/image` →
  `FileResponse`). It's backend API (not UI), parallels `api/images.py`, and a
  later UI will need it. Cheap to add now.

---

## The data model (what you're building)

### `ReferenceFace` — one reference photo + its embedding for a Player

One row per uploaded reference photo. A `Player` may have many.

- **Belongs to the person, not the shoot.** `player_id` is the hard link (FK to
  `players`, cascade from `Player`). A reference follows the player across all
  future shoots — that's the whole point of A.1's global dedup.
- **`captured_job_id` (nullable FK to `jobs`) is best-effort provenance** — the
  shoot the photo was taken in, recorded so a future "Split Player" can
  re-partition references. It does **not** cascade from `Job`: deleting a shoot
  must **not** delete a person's reference. A dangling `captured_job_id` after a
  job is deleted is acceptable (provenance just goes stale) — the same "orphans
  are fine in A.1/A.2" rule. Do **not** add a `references` relationship to
  `Job`.
- **`embedding`** is the 512-d L2-normalized ArcFace vector from InsightFace,
  stored exactly like `Face.embedding`: `np.float32(...).tobytes()` into a
  `LargeBinary`, read back with `np.frombuffer(..., dtype=np.float32)`.
- **`det_score` / `bbox` / `face_area_ratio`** are carried for debugging and so
  later phases can reason about reference quality without re-detecting.
- **`image_path`** points at the stored file (see storage below).

---

## Context you need

- **InsightFace is already wired — do not touch it.**
  [`services/face_detector.py`](backend/app/services/face_detector.py) exposes
  `detect_faces(image_path: Path) -> list[dict]`. Each dict is:
  `{"bbox": [x,y,w,h], "embedding": np.ndarray float32 (512,) L2-normalized,
  "det_score": float, "age": float|None, "yaw": .., "pitch": ..,
  "face_area_ratio": float|None}`. Faces below the module constant
  `DET_SCORE_THRESHOLD = 0.5` are already filtered out, and `[]` is returned for
  an unreadable image. GPU/CPU selection, the 2 GB VRAM cap, and CUDA DLL wiring
  are all self-contained in `get_detector()` — **A.2 calls `detect_faces` and
  nothing else.** Reuse `DET_SCORE_THRESHOLD` for the multiplicity floor.
- **Embedding (de)serialization** — copy the pipeline's convention verbatim
  (see [`services/face_pipeline.py`](backend/app/services/face_pipeline.py)
  ~line 94): store `det["embedding"].astype(np.float32).tobytes()`, read with
  `np.frombuffer(f.embedding, dtype=np.float32)`. `bbox` is stored as
  `json.dumps([x,y,w,h])`, matching `Face.bbox`.
- **On-disk storage convention** — mirror
  [`api/images.py`](backend/app/api/images.py)'s `THUMB_DIR`: a module-level
  `REFERENCES_DIR = DATA_DIR / "references"` (from `app.db.DATA_DIR`), `mkdir`
  at import. Per-player subdir `REFERENCES_DIR / str(player_id)` created on
  demand. File served via `fastapi.responses.FileResponse`. **Define
  `REFERENCES_DIR` in the service module so tests can monkeypatch it to
  `tmp_path`** (exactly how `test_archive_delete.py` monkeypatches
  `jobs_module.THUMB_DIR`).
- **Migration** — same story as A.1: A.2 adds only a **new table**
  (`reference_faces`), so `Base.metadata.create_all` in `init_db()` is the
  entire migration. **Do NOT touch `_PHASE2_COLUMNS`** in
  [`db.py`](backend/app/db.py) (that's for `ALTER TABLE ADD COLUMN` on existing
  tables; A.2 adds no columns to existing tables).
- **Endpoint + testable-core conventions** — same as A.1. Real logic lives in
  plain functions in `services/references.py` that tests call directly; the HTTP
  routes are thin wrappers that only handle the multipart `UploadFile`. Reuse
  `decode_bytes`? No — that's for text CSVs. Reference uploads are binary; read
  `await file.read()` and pass the raw `bytes` straight through.
- **Test conventions** — same as A.1: **no `conftest.py`**; each test file
  builds its own engine on `tmp_path`. Critically, **monkeypatch
  `detect_faces`** so the suite never loads the 300 MB InsightFace model — this
  is the established pattern (`test_expression.py` monkeypatches
  `_get_fer_detector`; do the same for the face detector here). Quality-check
  logic is split into a **pure function over the detection list** so most tests
  need no mocking at all.

**Don't break:** the existing 352 tests, the A.1 `Player` / `PlayerMembership`
models and their endpoints, and the existing `RosterEntry` system. A.2 is purely
**additive** — one new table, one new service, one new router, one new test
file, plus one line in `main.py` to register the router.

---

## Section 1 — Model

In [`db_models.py`](backend/app/models/db_models.py) add one model and one
relationship. (All needed imports — `Float`, `LargeBinary`, `Index`,
`ForeignKey`, `DateTime`, `String`, `Integer` — are already present from A.1;
no import-line edit is required this time.)

```python
class ReferenceFace(Base):
    """A reference photo + its face embedding for one Player (the person).

    Phase A.2 of the reference-photo system. A Player may have MANY references
    (different angles / lighting). `player_id` is the hard link (cascade from
    Player). `captured_job_id` is best-effort provenance — the shoot the photo
    was taken in — recorded so a future 'Split Player' can re-partition
    references (see PHASE_A1's split-friendly mandate). It is `job_id`, not
    `membership_id`, because memberships are wiped/recreated on every roster
    re-upload while jobs are stable. Embedding is the 512-d L2-normalized
    ArcFace vector, stored like Face.embedding.
    """
    __tablename__ = "reference_faces"
    __table_args__ = (
        Index("ix_reference_faces_player", "player_id"),
    )

    id = Column(Integer, primary_key=True)
    player_id = Column(Integer, ForeignKey("players.id"), nullable=False)
    captured_job_id = Column(Integer, ForeignKey("jobs.id"), nullable=True)  # provenance
    image_path = Column(String, nullable=False)      # data/references/{player_id}/{id}{ext}
    original_filename = Column(String, nullable=True)
    embedding = Column(LargeBinary, nullable=False)  # 512-d float32 .tobytes(), L2-normalized
    det_score = Column(Float, nullable=False)
    bbox = Column(String, nullable=True)             # JSON [x, y, w, h], like Face.bbox
    face_area_ratio = Column(Float, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    player = relationship("Player", back_populates="references")
```

On the existing A.1 `Player` model, add (next to the `memberships`
relationship):

```python
    references = relationship(
        "ReferenceFace", back_populates="player",
        cascade="all, delete-orphan",
    )
```

**Cascade semantics:** deleting a `Player` cascades to its `ReferenceFace`
**rows** (ORM-level). It does **not** delete the **files** on disk — ORM cascade
doesn't know about the filesystem. A.2 normally never deletes Players (orphan
Players are accepted, per A.1), so this is a non-issue in practice; the
`delete_reference` / `replace_player_references` service paths are responsible
for removing files when they remove rows. Orphaned-file GC after a raw
`db.delete(player)` is **out of scope** (note it, don't build it). Deleting a
`Job` does **not** touch `reference_faces` (no relationship from `Job`).

No `db.py` change is required — `create_all` builds the new table at startup.

### Tests for Section 1 (`backend/tests/test_references.py`)

- `reference_faces` is created by `Base.metadata.create_all` (fixture inserts a
  `Player` + a `ReferenceFace` and reads it back; embedding round-trips via
  `np.frombuffer`).
- `Player` → `references` cascade: `db.delete(player)` removes its
  `ReferenceFace` rows (file cleanup not asserted here — that's the service's
  job, covered in Section 2).
- A `ReferenceFace` with `captured_job_id=None` is valid (provenance optional).

Commit: `phase A.2 section 1: ReferenceFace model`

---

## Section 2 — Quality checks + reference service

New file `backend/app/services/references.py`.

### Constants + error type

```python
from app.services.face_detector import DET_SCORE_THRESHOLD  # 0.5 — multiplicity floor

REF_MIN_DET_SCORE = 0.65     # single-face confidence gate (≥ DET_SCORE_THRESHOLD)
REF_MIN_AREA_RATIO = 0.02    # face bbox must be ≥ 2% of the frame
REFERENCES_DIR = DATA_DIR / "references"
REFERENCES_DIR.mkdir(parents=True, exist_ok=True)


class ReferenceQualityError(ValueError):
    """A reference photo failed an automatic quality gate. Carries a stable
    `code` (for the client) plus a human message. The HTTP layer maps this to
    400 with detail={"error": code, "message": str}."""
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
```

### The quality gate (pure function — the heart of the phase)

```python
def evaluate_reference_quality(detections: list[dict]) -> dict:
    """Pick the single reference face from a detection list, or raise
    ReferenceQualityError with a clear code+message. Pure — no I/O, no model —
    so it's unit-tested directly with hand-built dicts.

    Order matters; first failure wins:
      no_face        — zero faces (also covers an unreadable image, which
                       detect_faces returns as [])
      multiple_faces — 2+ faces at the 0.5 detector floor (ANY second real
                       face → alert; the user wants to over-catch bystanders)
      low_confidence — the single face is below REF_MIN_DET_SCORE
      face_too_small — the single face's area ratio is below REF_MIN_AREA_RATIO
    Returns the chosen detection dict on success.
    """
    if not detections:
        raise ReferenceQualityError(
            "no_face",
            "No face was detected in the photo (or the file isn't a readable "
            "image). Retake with the player's face clearly in frame.")
    if len(detections) > 1:
        raise ReferenceQualityError(
            "multiple_faces",
            f"{len(detections)} faces detected. A reference photo must show "
            "exactly one person — make sure no one else is in frame.")
    face = detections[0]
    if face["det_score"] < REF_MIN_DET_SCORE:
        raise ReferenceQualityError(
            "low_confidence",
            "Face detection confidence is too low. Retake in better lighting, "
            "facing the camera.")
    if (face.get("face_area_ratio") or 0.0) < REF_MIN_AREA_RATIO:
        raise ReferenceQualityError(
            "face_too_small",
            "The face is too small in the frame. Move closer and retake.")
    return face
```

### Add / list / delete / replace (testable cores)

All take `db` and commit themselves (caller = thin HTTP wrapper). All raise
`HTTPException(404, ...)` for a missing player/reference, matching A.1.

```python
def add_reference(db, player_id, data: bytes, *,
                  captured_job_id=None, original_filename=None) -> dict:
    """Validate the player (404), optionally the captured job (404), run the
    detector on the bytes, enforce evaluate_reference_quality (→ raises
    ReferenceQualityError on a failed gate), then persist the row + file.
    Returns a summary dict. Allows multiple references per player (appends)."""
    # 1. player must exist; if captured_job_id given, that job must exist.
    # 2. write `data` to a temp file under REFERENCES_DIR (e.g. NamedTemporaryFile
    #    or "<uuid>.tmp"); detect_faces needs a real Path.
    # 3. face = evaluate_reference_quality(face_detector.detect_faces(tmp))
    #    — on ReferenceQualityError, delete the temp file and re-raise (the HTTP
    #      layer turns it into a 400). NO row, NO kept file on a failed gate.
    # 4. create ReferenceFace(player_id, captured_job_id,
    #       embedding=face["embedding"].astype(np.float32).tobytes(),
    #       det_score=face["det_score"], bbox=json.dumps(face["bbox"]),
    #       face_area_ratio=face.get("face_area_ratio"),
    #       original_filename=original_filename, image_path="");  db.flush()  # id
    # 5. final = REFERENCES_DIR / str(player_id) / f"{ref.id}{ext}";
    #    move temp → final; ref.image_path = str(final); db.commit()
    # ext: sanitized suffix of original_filename limited to .jpg/.jpeg/.png,
    #      default ".jpg".
    # returns {"id", "player_id", "captured_job_id", "det_score",
    #          "face_area_ratio", "image_path"}

def list_references(db, player_id) -> dict:
    """404 if player missing. Returns {"player_id", "items": [{"id",
    "captured_job_id", "det_score", "face_area_ratio", "created_at"}...]}
    ordered by id. (No embedding bytes in the response.)"""

def delete_reference(db, player_id, ref_id) -> dict:
    """404 if the reference doesn't exist OR isn't this player's. Remove the
    file (best-effort; missing file is fine) then the row. Returns {"deleted": 1}."""

def replace_player_references(db, player_id, data: bytes, *,
                              captured_job_id=None, original_filename=None) -> dict:
    """Wipe ALL of this player's references (rows + files), then add one fresh
    via the same validate→detect→gate→store path. 404 if player missing. If the
    new photo fails a quality gate, raise BEFORE wiping (validate the new one
    first so a bad upload doesn't destroy good existing references). Returns the
    add_reference summary."""
```

> **Important ordering in `replace_player_references`:** run the detector +
> quality gate on the *new* photo **first**; only wipe the existing references
> once the new one is known-good. A failed replace must leave the old
> references intact.

### Tests for Section 2

Quality gate (pure, no mocking — hand-built detection dicts):
- `[]` → `no_face`.
- two faces (both `det_score` ≥ 0.5) → `multiple_faces` (the "two different
  faces" alert the user asked for).
- one face `det_score=0.55` → `low_confidence`.
- one face `det_score=0.9, face_area_ratio=0.005` → `face_too_small`.
- one good face (`det_score=0.9, face_area_ratio=0.2`) → returned unchanged.

Service (monkeypatch `references.face_detector.detect_faces`, monkeypatch
`references.REFERENCES_DIR = tmp_path`; raw bytes are fine since detection is
mocked):
- Happy path: one good face → row created, file exists at
  `REFERENCES_DIR/{player_id}/{id}.jpg`, stored `embedding` round-trips
  (`np.frombuffer` equals the mock's vector), `det_score`/`face_area_ratio`
  stored, summary correct.
- **Multiple allowed:** two successful uploads for one player → two rows, two
  files.
- Each failed gate (`no_face`, `multiple_faces`, `low_confidence`,
  `face_too_small`) → raises `ReferenceQualityError` with the right `code`, and
  leaves **no row and no leftover temp file**.
- Unknown player → `HTTPException(404)`; unknown `captured_job_id` →
  `HTTPException(404)`; `captured_job_id=None` is accepted and stored null.
- **Cross-shoot provenance:** add two references for the same player with two
  different `captured_job_id`s → both listed under the player.
- `delete_reference` removes the row **and** the file; deleting another player's
  ref id → 404.
- `replace_player_references` wipes existing (rows + files) and leaves exactly
  one; a replace whose new photo fails the gate raises and leaves the old
  references untouched.
- `Player` cascade: `db.delete(player)` removes its `ReferenceFace` rows.

Commit: `phase A.2 section 2: reference quality gate + add/list/delete/replace service`

---

## Section 3 — API endpoints

New file `backend/app/api/references.py` with `router = APIRouter()`. Register
it in [`main.py`](backend/app/main.py): add `references` to the
`from app.api import (...)` line and
`app.include_router(references.router, prefix="/api/players", tags=["references"])`.
This is the only edit to an existing file in this phase.

All routes are nested under `/{player_id}/references...`, so **none collide with
A.1's dynamic `/{player_id}` route** (different segment counts — no
static-vs-dynamic ambiguity to worry about this time).

Endpoints:

```
POST   /api/players/{player_id}/references            upload (multipart) — appends a reference
GET    /api/players/{player_id}/references            list this player's references
PUT    /api/players/{player_id}/references            replace ALL of this player's references with one
DELETE /api/players/{player_id}/references/{ref_id}   delete one reference
GET    /api/players/{player_id}/references/{ref_id}/image   serve the stored photo (FileResponse)
```

- `POST`: read `await file.read()` (binary). Optional `job_id` as a query param
  (`job_id: int | None = None`) → passed as `captured_job_id`. Call
  `add_reference`. Translate `ReferenceQualityError` →
  `HTTPException(400, detail={"error": exc.code, "message": str(exc)})`. Returns
  the Section-2 summary.
- `PUT`: same multipart shape; calls `replace_player_references`; same 400
  translation.
- `GET` (list): 404 if player missing; returns the `list_references` envelope.
- `DELETE`: calls `delete_reference` (404 if not this player's ref);
  `{"deleted": 1}`.
- `GET .../image`: 404 if the reference isn't this player's or the file is
  missing on disk; else `FileResponse(image_path)`.

### Tests for Section 3

Use a `client` fixture like A.1's `test_players.py` — `dependency_overrides[get_db]`,
`TestClient(app)`, seed a Player (and a Job, for the `job_id` provenance param).
**Monkeypatch `detect_faces` and `REFERENCES_DIR`** for the HTTP tests too.

- POST multipart (good-face mock) → 200 + summary; `GET` list reflects it;
  `GET .../image` returns 200; `DELETE` → `{"deleted": 1}`; follow-up list is
  empty.
- POST with a two-face mock → **400** with `detail.error == "multiple_faces"`.
- POST with a no-face mock → 400 `no_face`; low-confidence and too-small mocks →
  400 with their codes.
- POST to an unknown player id → 404; `?job_id=` unknown → 404.
- `PUT` replace: upload one, PUT a new good one → list shows exactly the new one.
- Cross-shoot: POST two references with different `?job_id=` → list shows both,
  each carrying its `captured_job_id` (proves provenance end-to-end over HTTP).

Commit: `phase A.2 section 3: reference upload/list/delete/replace API`

---

## Conventions (match the existing code)

- Call `face_detector.detect_faces` for detection+embedding; never construct
  InsightFace yourself. Reuse `DET_SCORE_THRESHOLD`.
- Serialize embeddings with `.astype(np.float32).tobytes()`; deserialize with
  `np.frombuffer(..., dtype=np.float32)`. `bbox` as `json.dumps`.
- Testable-core-plus-thin-HTTP-wrapper split (the `add_reference` /
  `replace_player_references` pattern); plain-dict responses; `{"items": [...]}`
  envelopes for lists.
- Storage dir is a module-level `REFERENCES_DIR` (monkeypatchable in tests),
  `mkdir` at import — mirror `api/images.py`'s `THUMB_DIR`.
- New test file builds its own engine on `tmp_path` (no shared `conftest.py`);
  **monkeypatch `detect_faces`** so the model never loads; quality-gate tests
  call the pure function directly.
- Single-line commit messages per section (`phase A.2 section N: ...`).
- `pytest -v` green after every section: 352 existing + new (~24 → ~376).

## Acceptance

1. `reference_faces` exists after `init_db()`; existing `player_sort.db` files
   open with **no** migration error and **no** data loss (only the new table is
   added; `_PHASE2_COLUMNS` untouched).
2. The A.1 `Player`/`PlayerMembership` and the Phase 6 `RosterEntry` models and
   their endpoints/tests are unchanged; all 352 prior tests still pass.
3. Uploading a clean single-face photo for a player stores the file under
   `data/references/{player_id}/`, computes + persists the 512-d embedding, and
   returns a summary with `det_score`.
4. A photo with **two faces** is rejected with `multiple_faces`; no-face,
   low-confidence, and too-small photos are each rejected with their own clear
   message; **no row or file is left behind** on a rejection.
5. A player can hold **multiple** references; `DELETE` removes one (row + file);
   `PUT` replaces all with one (and a failed `PUT` leaves the old set intact).
6. References list by player regardless of `captured_job_id`, and each row
   carries its provenance `captured_job_id`; `pytest -v` is fully green.

## Out of scope (defer — do NOT build in A.2)

- **Matching / search.** No "find the player for this shoot face", no
  embedding similarity queries, no nearest-reference lookup. A.2 only *stores*
  embeddings.
- **Pipeline integration.** The sort pipeline does not consult references yet.
- **The mobile/tablet capture app and any UI** (no React).
- **Image derivatives** — no reference thumbnails, no re-encode/normalize, no
  EXIF stripping. Store the original bytes; serving is a raw `FileResponse`.
- **Player merge/split implementation** and **orphaned-file garbage collection**
  (beyond removing files on the explicit delete/replace paths). Honor the
  forward constraint: provenance (`captured_job_id`) is recorded **now** so a
  later split *can* re-partition references — but the split itself is a later
  phase.
- **`membership_id` provenance / linking references to teams.** `captured_job_id`
  is the anchor (memberships are unstable across roster re-uploads).
- **Touching `face_detector.py` / GPU config.** Call `detect_faces`; change
  nothing in it.
```
