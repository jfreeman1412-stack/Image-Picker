# Phase B.6 — Hand-off (Salvage flagged captures: face-selection + low-confidence override in Needs-attention)

> **Status: decisions LOCKED 2026-05-26; DRAFT build plan for review. Do NOT build
> yet.** Surfaced from real offline testing. Builds **third** in the batch
> (**C.3 → B.4 → B.6 → B.5**): more shoot-critical than the walk-up player and
> lower-risk — it's review-flow UI + a contained, additive backend change that
> **does not touch the B.3 sync-queue invariant**. Read this in full before starting.

## Why this exists (the gap offline testing surfaced)

Offline capture worked well, but a real failure mode appeared: a **perfectly good**
reference photo was flagged `multiple_faces` because a bystander's head was in the
background. **Online that's a non-issue** — the volunteer sees the `400` and
re-shoots on the spot. **Offline, the rejection only surfaces at sync time, back at
the office, when the kid is long gone** — so a usable capture becomes a lost
capture. Same story for a `low_confidence` flag on a photo the operator judges fine.

B.6 adds two **operator-driven salvage paths** in the existing B.3 **"Needs
attention"** surface (online, post-sync), so a flagged-but-good capture is never
lost when re-shooting is impossible:

1. **`multiple_faces` → face selection.** Open the capture, see the detected faces
   marked on the image, **tap the player's correct face**, and accept the photo
   using that face — **no re-shoot**.
2. **`low_confidence` → operator override.** A deliberate **"use it anyway"** that
   surfaces *why* it was flagged and lets the operator accept the photo as-is — a
   **conscious, intentional** override, not a casual default.

## What's confirmed in the code

- **Detection already exposes the face boxes.** `detect_faces` (`face_detector.py:216`)
  returns, per face, `bbox: [x, y, w, h]` (ints) + `det_score` + `face_area_ratio`,
  filtered to `det_score ≥ 0.5` (`DET_SCORE_THRESHOLD`). So a detect call on the
  queued bytes yields every tappable box; the endpoint also returns the image
  dimensions so the UI can scale boxes onto the displayed photo.
- **The gate is a pure, unit-tested function** — `evaluate_reference_quality`
  (`references.py:52`), order: `no_face → multiple_faces (2+ faces ≥0.5) →
  low_confidence (single face <0.65 = REF_MIN_DET_SCORE) → face_too_small (<2%
  area)`. Both salvage paths are surgical extensions of this one function.
- **B.3 already retains the bytes** for failed items (the `blobs` store; an item is
  deleted only on a confirmed 200 or an explicit Discard — B.3 losslessness). So the
  resolve action has the photo to work with, with **zero new offline machinery**.
- **`NeedsAttention.jsx` is the home.** It shows `item.lastError` + Re-shoot/Discard,
  branching only on `failed_gone`. B.6 branches the actions on the **error code** —
  which the queue does **not** store yet (the drainer keeps only the message,
  `syncQueue.js:117`); capturing `body.detail.error` is a small client change (§4).

## ⚠️ Decisions — LOCKED 2026-05-26

### Decision 1 — Two salvage capabilities; the other reject reasons are unchanged *(LOCKED)*
- `multiple_faces` → **face selection** (pick the right face).
- `low_confidence` → **deliberate override** (use anyway, reason surfaced).
- `face_too_small`, `no_face`, `failed_gone` → **unchanged** (Re-shoot / Discard).
  Overriding `face_too_small`/`no_face` would store a genuinely unusable embedding —
  out of scope.

### Decision 2 — Separate resolve route + a detect route; the B.3 drain PUT stays frozen *(LOCKED)*
- **`POST /api/players/{player_id}/references/shoot/{job_id}/detect`** — multipart
  file → `{ width, height, faces: [{ index, bbox, det_score, face_area_ratio }] }`.
  Read-only, no DB write. Powers the tappable boxes.
- **`POST /api/players/{player_id}/references/shoot/{job_id}/resolve`** — multipart
  file + Form `selected_bbox` (JSON `[x,y,w,h]`, optional) + `allow_low_confidence`
  (bool, default false). Stores the salvaged/overridden reference via the shared
  core. Explicit operator action.
- **The B.2 drain endpoint `PUT .../references/shoot/{job_id}` stays byte-for-byte**
  — the B.3 drain target is untouched. Only the shared **service core**
  (`replace_shoot_reference`) and the pure gate gain **optional kwargs** (defaults
  preserve today's behavior), so existing callers/tests are unaffected.
- *Rejected — extending the drain PUT with optional params:* smaller surface but
  mutates the exact endpoint the B.3 drain depends on; keeping it frozen is safer.

### Decision 3 — Resolution is an ONLINE office action; the offline queue/drainer is untouched *(LOCKED)*
Detect + resolve hit the network and happen at the office, operating on the
**already-retained** queued bytes. On a 200, the resolve removes the queue item and
marks the player synced — reusing the existing Discard / `markRosterSynced`
plumbing. **The offline capture/enqueue/drain path and its FIFO/idempotent invariant
do not change.** This is why B.6 is materially lower-risk than B.5. (Offline, the
salvage actions are disabled with a "connect to resolve" hint; Re-shoot and Discard
still work offline as today — bytes are never lost.)

### Decision 4 — Record how a reference was accepted (provenance column) *(RECOMMENDED — default include; droppable at review)*
Add a nullable `accepted_via` column to `ReferenceFace` (`normal` | `face_select` |
`low_conf_override`) via the idempotent `_PHASE2_COLUMNS` ALTER pattern (`db.py`).
Cheap, aligns with A.1's split-friendly/provenance mandate, and lets the desktop
later badge an operator-accepted low-quality reference. Trivially droppable if you'd
rather not touch the schema — the low `det_score` already makes an override
self-evident.

## How the gate extension works (the heart of the backend change)

`evaluate_reference_quality(detections, *, selected_bbox=None, allow_low_confidence=False)`:

- `faces = [d for d in detections if det_score ≥ 0.5]`; if empty → `no_face`
  (unchanged).
- **If `selected_bbox` is given:** pick the face with the highest **IoU** against it
  (skip the multiplicity check); if no face overlaps acceptably → new code
  `selected_face_not_found` (defensive; identical bytes detect identically, so this
  is rare). Use that face.
- **Else:** if `len(faces) > 1` → `multiple_faces` (unchanged); else `face = faces[0]`.
- If `face.det_score < 0.65` **and not** `allow_low_confidence` → `low_confidence`.
- If area `< 2%` → `face_too_small` (**always** enforced).
- Return the chosen face.

**Default behavior is byte-for-byte identical** (no kwargs → today's logic), so A.2's
gate tests stay green. A salvaged photo whose chosen face is *also* low-confidence
returns `low_confidence` — the UI then offers "use anyway" inline (sends both
`selected_bbox` + `allow_low_confidence`), keeping every accept an intentional step.

## Don't break

- **The B.3 drain PUT + the offline queue/drainer are byte-for-byte unchanged**
  (Decision 2/3). No new queue op-type, no FIFO/idempotency change. Resolution is an
  additive online surface over retained bytes.
- **A.2's gate stays behavior-compatible** — `evaluate_reference_quality` and
  `replace_shoot_reference` only gain optional kwargs with today's defaults; existing
  callers and `test_references.py` pass unchanged.
- **Losslessness holds** — a flagged item's bytes are removed only on a confirmed
  resolve 200 or an explicit Discard. A failed/cancelled resolve leaves the item.
- **Backend tests stay green** — record the live `pytest -v` baseline and keep it
  green plus B.6's new tests. The capture app stays test-free; B.6's verification is
  the on-device checklist (§7).
- **No CORS change** — detect/resolve are same-origin via the existing proxy.

## Build plan (dependency order; each ends green + committed)

### Section 1 — Backend: extend the pure gate (+ tests)
Add the optional kwargs to `evaluate_reference_quality` per "How the gate extension
works", plus a small pure `_iou(boxA, boxB)` helper. No I/O.

**Tests (`test_references.py`):** with `selected_bbox` matching one of two faces →
returns that face, no `multiple_faces`; a `selected_bbox` matching nothing →
`selected_face_not_found`; `allow_low_confidence=True` on a sub-0.65 single face →
returns it (no raise); `allow_low_confidence` does **not** bypass `face_too_small`;
**default call (no kwargs) is unchanged** for every existing case.

**Check:** `pytest -v` green.
Commit: `phase B.6 section 1: gate extension for face-select + low-confidence override (+ tests)`

### Section 2 — Backend: the `/detect` route (+ tests)
Add `POST …/references/shoot/{job_id}/detect`: write the upload to a temp file
(reuse `_write_temp`), run `detect_faces`, return `{ width, height, faces:[{index,
bbox, det_score, face_area_ratio}] }`; clean up the temp file. 404 if player/job
missing (parity with the family).

**Tests:** an image with two faces → two boxes + dims; an image with none → empty
`faces`; unknown player/job → 404. (Monkeypatch `face_detector.detect_faces` per the
A.2 test pattern so the model never loads.)

**Check:** `pytest -v` green.
Commit: `phase B.6 section 2: reference face-detect route (boxes + dims) (+ tests)`

### Section 3 — Backend: the `/resolve` route + core threading (+ optional column)
Thread `selected_bbox` / `allow_low_confidence` through `replace_shoot_reference`
into the gate; store the chosen face's embedding/bbox/det_score as a normal
reference (validate the NEW photo before any wipe, exactly like B.2). Add the
`/resolve` route wiring the Form params in; translate `selected_face_not_found` to a
clear 400. *(If Decision 4 kept:)* set `accepted_via` accordingly and add the
`_PHASE2_COLUMNS` entry.

**Tests:** resolve with `selected_bbox` on a two-face image → 200, exactly one ref
stored (the chosen face), `captured_job_id` set; resolve with
`allow_low_confidence=true` on a low-confidence image → 200, ref stored with the low
`det_score`; resolve still rejects `face_too_small`; the existing B.2 PUT path is
unchanged (regression); idempotent — a second resolve replaces, one ref remains.

**Check:** `pytest -v` green; `curl` resolve with a bbox stores a ref.
Commit: `phase B.6 section 3: reference resolve route (face-select + low-conf override) (+ tests)`

### Section 4 — Client: capture the failure CODE onto the queue item
In `syncQueue.js`, when routing a `400`, also store `failReason = body?.detail?.error`
(the code: `multiple_faces` / `low_confidence` / `face_too_small` / `no_face`)
alongside `lastError`. No schema change (queue records are open). `App` already
passes failed items to `NeedsAttention`; they now carry the code.

**Manual check:** force an offline `multiple_faces` and a `low_confidence` capture;
on sync, inspect the queue records in DevTools → each has the correct `failReason`.

Commit: `phase B.6 section 4: record sync-failure code on queue items`

### Section 5 — Client: the "Use anyway" path (low_confidence)
In `NeedsAttention`, for a `low_confidence` item show a **"Use anyway"** action that
expands to show the server's reason and a confirm ("Accept this photo despite low
detection confidence? A poor reference can weaken matching for this player."). On
confirm: load bytes (`getCaptureBytes`), `POST …/resolve` with
`allow_low_confidence=true`; on 200 → remove the item + mark the player synced
(reuse the Discard / `markRosterSynced` plumbing); on failure → show a message, keep
the item. Disabled offline with a "connect to resolve" hint.

**Manual check:** a low_confidence item → "Use anyway" shows the reason and requires
the confirm (not one-tap) → accepts → badge flips green; `GET …/references` shows one
ref with the low `det_score`. Re-shoot/Discard still present and working.

Commit: `phase B.6 section 5: low-confidence "use anyway" override in Needs-attention`

### Section 6 — Client: the "Select face" path (multiple_faces)
For a `multiple_faces` item, a **"Select face"** action opens a face-selection view
(small new component or inline): load bytes → render the photo → `POST …/detect` →
draw each face as a **tappable box** scaled from `width/height` to the displayed
image → operator taps the player's face → `POST …/resolve` with that `selected_bbox`
→ on 200 resolve (remove + mark synced). If resolve returns `low_confidence` (the
chosen face is also weak), offer **"use anyway"** inline (resend with
`allow_low_confidence=true`). Disabled offline with the same hint.

**Manual check (with a real background-head capture):** open it → boxes appear over
the faces → tap the kid → accepts that face → green; the stored ref's bbox is the
chosen face (spot-check via `GET …/references`). Tapping the *background* face and
accepting stores *that* face (proves selection is honored).

Commit: `phase B.6 section 6: multiple-faces face-selection salvage in Needs-attention`

### Section 7 — README + adversarial offline checklist
Update `capture/README.md`: the two salvage paths in Needs-attention, that they're
**online office actions** on retained bytes (offline-disabled), and the checklist
below. Note that overriding accepts a possibly-weaker reference by the operator's
deliberate choice.

**Checklist additions:**
1. Offline capture with a bystander in frame → `multiple_faces` at sync → Select
   face → tap the player → synced green, no re-shoot.
2. Offline low-light capture → `low_confidence` at sync → Use anyway (reason shown,
   confirm required) → synced; the ref carries the low `det_score`.
3. Chosen face is itself low-confidence → resolve returns `low_confidence` → inline
   "use anyway" → synced.
4. `face_too_small` / `no_face` / `failed_gone` → still Re-shoot/Discard only (no
   salvage offered).
5. Attempt a salvage **offline** → blocked with "connect to resolve"; the item and
   its bytes are untouched.
6. Resolve twice (double-tap / retry) → exactly one ref; item removed once
   (idempotent, scoped-replace).

**Manual check:** a teammate reproduces both salvage paths from the README on a real
flagged capture and confirms the salvaged references are usable by desktop matching.

Commit: `phase B.6 section 7: README + salvage adversarial checklist`

---

## Edge cases — and how B.6 handles each

| Edge case | Handling |
| --- | --- |
| Bystander head → `multiple_faces` on a good photo | Select face → tap the player → resolve stores that face; no re-shoot (§6). |
| Operator judges a `low_confidence` photo fine | Deliberate "use anyway" with the reason surfaced + confirm → stored as-is (§5). |
| Chosen face is also low-confidence | Resolve returns `low_confidence`; inline "use anyway" accepts it (§6). |
| Player's face is below the 0.5 detect floor | It isn't returned as a box (detect filters <0.5) → not selectable → fall back to Re-shoot/Discard (genuinely bad capture). |
| Salvage attempted offline | Disabled with "connect to resolve"; bytes retained, nothing lost (Decision 3). |
| Resolve interrupted / retried | Scoped-replace is idempotent; the item is removed only on a confirmed 200 → at worst it re-sends (B.3 model). |
| `face_too_small` / `no_face` | Not salvageable (unusable embedding) → Re-shoot/Discard, unchanged (Decision 1). |
| A **walk-up** capture (B.5) is flagged | Out of scope here (walk-ups don't exist until B.5); **B.5 composes its create-then-store step with this resolve path** — noted in both docs. |

## Conventions (match existing code)

- Backend: testable-core + thin HTTP wrapper; reuse `_write_temp` / `detect_faces` /
  `replace_shoot_reference`; pure-function gate driven by tests; monkeypatch the
  detector in tests (A.2 precedent). Additive only; B.2 endpoint frozen.
- Capture app: Vite + React 18, JSX, hooks; reuse B.3's `db.js` accessors
  (`getCaptureBytes`, `removeQueueItem`, `markRosterSynced`); `res.ok`-checked
  `fetch`, error bodies via `body?.detail?.message || body?.detail?.error`, no error
  object in state.
- Single-line commit per section (`phase B.6 section N: …`). `pytest -v` green after
  every backend section.
- Verification = pytest (backend) + the on-device checklist (§7), per B.1/B.2/B.3.

## Acceptance

1. A `multiple_faces` capture can be **opened, its faces shown, the player's face
   tapped, and accepted** using that face — no re-shoot — landing as a normal synced
   reference.
2. A `low_confidence` capture can be accepted via a **deliberate "use anyway"** that
   surfaces the reason and requires an explicit confirm.
3. Both resolutions are **online actions on B.3's retained bytes**; the **drain PUT,
   offline queue, and FIFO/idempotent invariant are byte-for-byte unchanged**;
   nothing is ever lost.
4. `evaluate_reference_quality` / `replace_shoot_reference` **default behavior is
   unchanged** (A.2 tests green); `/detect` + `/resolve` are additive; `pytest -v`
   green incl. new tests.
5. `face_too_small` / `no_face` / `failed_gone` are unchanged (Re-shoot/Discard).
6. Salvaged/overridden references are valid and usable by desktop matching *(and
   carry `accepted_via` provenance if Decision 4 is kept)*.

## Out of scope (defer — do NOT build in B.6)

- **Overriding `face_too_small` or `no_face`** — they yield unusable embeddings.
- **Client-side face detection** — detection stays server-only (B.1/B.2/B.3
  precedent); the `/detect` round-trip is an online office action.
- **Cropping / editing the photo** — selection picks an existing detected face only.
- **Resolving a flagged *walk-up* capture** — until B.5 composes its create step with
  the resolve path (cross-item note).
- **Batch / bulk resolve** — one item at a time.
- **A desktop-side salvage UI** — B.6 lives in the capture app's Needs-attention.
