# Phase C.3 — Hand-off (Image orientation: normalize to upright at import)

> **Status: decisions LOCKED 2026-05-26; build plan ready for pre-build review.**
> Decision 1 (where normalization lives) and Decision 2 (**deprecate copyright —
> face match always wins**, revised 2026-05-26 after the Evoto re-inserted-metadata
> bug) are both confirmed by the user — no open questions remain. This is the
> **first** in the build order of the 2026-05-26 follow-ups, to be **built after the
> upcoming shoot** (the manual metadata strip remains the interim stopgap until
> then). A separate **view-layer-only display
> rotate** for the review UI is appended as a **deferred addendum** (build after the
> shoot, likely skippable once the core fix lands). Read this in full before starting.

## Why this is "C.3"

The **C-series** is desktop sorting-app / pipeline tooling (C.1 roster upload, C.2
pre-shoot job). This is the third: a fix in the **image ingest path** both the
post-shoot wizard and the pre-shoot importer share. (If you'd rather file it as a
pipeline-track doc, the content is identical — only the filename changes.)

## What this fixes

In the full-pipeline test, portrait (vertically-shot) images displayed **sideways**
in the review UI **and** flowed sideways into the exported deliverables. The cause
is diagnosed below. The fix: at import, **physically rotate pixels to upright based
on the EXIF orientation flag**, so the stored working image is genuinely upright
regardless of what any downstream tool or metadata strip does.

---

## Diagnosis (the facts this design rests on)

**Where orientation is decided today — nowhere; it's inherited from the file.**

- **Ingest never touches pixels.** `ingest_folder` (`services/ingest.py:39`)
  records `Image.path` = the original file's resolved path and reads only EXIF
  `DateTimeOriginal` → `capture_time` and `Copyright` → `copyright_tag`. The
  pipeline always reads back the **original files in place**.
- **A portrait shot is usually stored as landscape pixels + an EXIF Orientation
  flag** (e.g. `Orientation=6`, "rotate 90° CW") that viewers are meant to honor.
  Stripping the metadata before import removed that flag, leaving landscape pixels
  with **no instruction to rotate**.
- **Thumbnails ignore the flag regardless.** `images.py:39` opens with Pillow
  `.convert("RGB").thumbnail(...)` — Pillow does **not** auto-apply EXIF
  orientation. So thumbnails render in stored (sideways) orientation even when the
  flag *is* present.
- **`/full` relies on the flag.** `images.py:51` returns `FileResponse(original)`;
  a browser `<img>` honors an EXIF orientation flag if present — but the strip
  removed it, so it renders sideways.
- **Export copies the originals byte-for-byte.** `_copy_one` (`jobs.py:789`) does
  `shutil.copy2` / `shutil.move` of the source file into `To_be_Cropped` /
  `Team Images` / `Pano Images`. **→ sideways reaches the deliverables, not just
  the review UI.** This is the key answer to the scoping question.

**Matching is genuinely orientation-robust — confirmed.** `read_bgr`
(`services/image_io.py:20`) uses `cv2.imread` / Pillow `.convert("RGB")` — neither
applies the EXIF flag — so the matcher has *always* seen the stored (sideways)
pixels and clustered/sorted correctly. Rotating pixels upright at import **will
not change matching behavior**.

**Reference photos are unaffected.** The capture app draws the video frame to a
canvas and re-encodes JPEG (`capture/src/CaptureScreen.jsx:46`), which carries no
EXIF and is already upright. This phase touches only the **shoot images** ingested
on the desktop.

**`capture_time` survives a metadata-free working copy.** The pipeline reads
capture order from `Image.capture_time` in the DB (`face_pipeline.py:420`), which
ingest fills from the **original** file's EXIF. So a working copy that carries no
metadata does not affect capture-order sorting.

---

## The entanglement you must design around (copyright) — and the precedence bug

Your pre-import metadata strip was doing **two** jobs at once:

1. **Killing the orientation flag** → the sideways bug (what C.3's core fixes).
2. **Suppressing the `Copyright` tag** → which was masking a **real precedence
   bug** in the labeling pipeline:
   - `face_pipeline.py:202-228` sets `Cluster.auto_label` from `copyright_tag`
     when present.
   - `cluster_matching.py:88-96`: on a high-confidence face match, **if a copyright
     label is present and disagrees, copyright WINS** and the cluster is flagged
     `match_label_conflict`. **This precedence is backwards** for where the project
     is going (face-match + capture app as the source of truth) — copyright should
     never override a match.
   - The read-time **`needs_duplicate_auto_label`** flag (`clusters.py:111`,
     `roster_check.py:26`) fires when **multiple clusters share the same
     `auto_label`** — exactly what happens when one studio copyright string lands on
     every cluster.

**The real-world trigger (observed during the test import):** copyright was cleared
**in-camera**, but **Evoto's export re-inserted copyright/XMP** — e.g.
`</dc:rights> <dc:creator> <rdf:Bag> <rdf:li>SPORTSLINE PHOTOGRAPHY`. That studio
fragment, present on many exported images, became the `auto_label` for many clusters
and — per the backwards precedence — **beat good face matches**, and the shared
string tripped `needs_duplicate_auto_label` across all of them. **Exported metadata
overriding the matcher is the bug.** Decision 2 (revised) fixes it at the root so no
exported metadata can poison labels and you never hand-strip again.

---

## ⚠️ Decisions

### Decision 1 — Normalize orientation at import, into an upright working copy *(LOCKED 2026-05-26)*

At ingest, for each image whose EXIF Orientation flag is **not** "normal", write an
**upright working copy** (pixels physically rotated, metadata dropped) into a
managed directory and point `Image.path` at the copy. Images with no flag / a
normal flag are already upright and keep `Image.path = original` (no copy, no
re-encode). The originals are **never modified** — they remain the untouched source
of truth on the share.

- **Why a copy, not in-place:** ingest references the user's source files in place;
  rewriting them would mutate the customer's originals. A managed copy keeps the
  fix entirely inside the app's data dir.
- **Why only when the flag is non-normal:** the vast majority of landscape frames
  are Orientation=1 (or no flag) and are already upright — re-encoding them would
  burn time, ~double disk, and slightly degrade JPEG quality for no gain. We touch
  only the images that are actually rotated.
- **Downstream, everything self-fixes** because they all read `Image.path`:
  thumbnails (`images.py:39`), `/full` (`images.py:51`), the matcher
  (`image_io.read_bgr`), and **export** (`_copy_one`) — deliverables become upright.
- **Export `move` mode note:** for a normalized (rotated) image, `Image.path` is
  the copy, so `move` moves the **copy** and leaves the original in place (rather
  than relocating the source). That's a benign, arguably-better change; call it out
  in the export README line.

### Decision 2 — Deprecate copyright labeling: a face match ALWAYS wins; copyright is a last-resort hint, never an override; clean placeholder otherwise *(LOCKED 2026-05-26 — revised; broader than the original "suppress for roster jobs")*

**Intent (confirmed):** the project is moving to **face-match + the capture app** as
the source of truth for labels and is **deprecating copyright-based labeling**.
Copyright must **never override a face match**. Three coordinated changes:

1. **A high-confidence face match always wins.** Flip the precedence in
   `cluster_matching.py:85-96`: on a high-tier match set `auto_label = matched
   name`, `auto_label_source = "match"` **unconditionally**, regardless of any
   copyright `auto_label`. **Delete** the "copyright wins + `match_label_conflict`"
   branch. A residual/exported copyright can no longer hijack a correct match.
2. **No match → clean placeholder, never a metadata fragment.** When there is no
   high-tier match and no usable copyright, leave `auto_label = None` so
   `display_label()` shows **"Player {id}"** (its existing fallback) — an explicitly
   *unassigned* cluster, not a garbage string.
3. **Copyright is at most a last-resort hint.** For **roster/reference jobs, stop
   reading copyright entirely** (the original Decision 2 — gate `copyright_tag` to
   `None` at ingest when the job has a roster), so it never enters the pipeline. For
   **classic no-roster jobs**, copyright may still fill the label **only when there
   is no match** — never overriding one.

- **Signal for the ingest gate:** a job has a roster (≥1 `PlayerMembership`) ⇒
  reference workflow ⇒ skip copyright. No roster ⇒ classic ⇒ read it (no-match
  fallback only). Same "has a roster" test `cluster_matching.py:49-55` already uses.
- **This is the permanent fix.** Once a match can't be overridden and copyright
  isn't read for roster jobs, **no exported metadata (Evoto or otherwise) can poison
  labels** and you **never manually strip metadata again** — the whole goal. The
  `needs_duplicate_auto_label` mislabels disappear because the studio string never
  becomes an `auto_label`.
- *Rejected — a slim pre-import copyright-only strip:* still a manual step, and it
  wouldn't fix the backwards precedence (the real bug).

> **Decided 2026-05-26 — KEEP the copyright labeling stage (Step 4c) as the no-match
> fallback; do NOT remove it.** With the precedence flipped, copyright can never
> override a face match, so it's invisible for every reference/roster job and the
> poisoning problem is solved. The fallback only matters for jobs with **no
> reference photos**, where it avoids labeling every cluster "Player {id}" — it costs
> the face-match workflow nothing. Revisit removal only if classic no-reference jobs
> are fully retired.

---

## Don't break

- **Originals are never mutated.** Normalization only ever *writes copies* into the
  app data dir and *reads* originals. A failed/aborted ingest must leave source
  files byte-for-byte.
- **Matching must not change.** The matcher already ignores orientation; the only
  observable change is that it now reads an upright copy for rotated images. Spot-
  check that cluster assignments for the test shoot are identical before/after.
- **The match-precedence flip is deliberate** (Decision 2): `cluster_matching` now
  makes a face match **always** win over copyright — an intentional behavior change,
  so the existing test that encodes the old precedence
  (`test_cluster_matching.py:129`, asserting `auto_label_source != "match"` on
  conflict) is **updated, not preserved**. Classic no-roster copyright labeling
  survives **only as a no-match fallback**; `derive_cluster_label` and the labeling
  stage are otherwise untouched.
- **`capture_time` still comes from the original EXIF** at ingest — don't read it
  from the metadata-free copy.
- **No schema change.** `Image.path` already holds an arbitrary path; pointing it
  at a managed copy needs no migration. Don't add to `_PHASE2_COLUMNS`.
- **Backend tests stay green.** Record the current `pytest -v` baseline (C.2 was
  486 + its own; confirm the live number) and keep it green plus C.3's new tests.
- **Already-imported jobs are not retroactively fixed** — the test job's sideways
  images stay sideways (re-import is blocked by C.2's 409). Verification uses a
  **fresh** import. Acceptable; noted in Out of scope.

---

## Build plan (dependency order; each ends green + committed)

### Section 1 — Orientation core (pure helper + tests)
New `backend/app/services/image_normalize.py`:
- `needs_upright(path) -> bool` — read the EXIF Orientation tag
  (`PILImage.open(path).getexif().get(0x0112, 1)`); `True` for values 2–8.
- `write_upright_copy(src: Path, dst_dir: Path) -> Path` — open, apply
  `ImageOps.exif_transpose(img)`, save to `dst_dir/{src.name}` **without** EXIF
  (Pillow drops it on re-save by default; JPEG at `quality=95`, keep source
  format). Returns the copy path. Pure file-in/file-out; tests drive it directly.

**Tests (`test_image_normalize.py`):** build a small in-memory JPEG, write it with
EXIF `Orientation=6` (via `piexif` or a raw exif dict), assert `needs_upright` is
True; after `write_upright_copy` the output's width/height are **swapped**
(physically rotated) and `getexif().get(0x0112)` is absent/1; an Orientation=1
image → `needs_upright` False; a PNG with no flag → `needs_upright` False.

**Check:** `pytest -v` green.
Commit: `phase C.3 section 1: orientation normalization core (+ tests)`

### Section 2 — Wire normalization into ingest
In `ingest_folder` (`services/ingest.py:39`): after the EXIF pass, for each path
call `needs_upright`; if True, `write_upright_copy` into
`DATA_DIR/normalized/{session_id}/` and use that path for `Image.path`; else use
the original. Reuse the existing `ThreadPoolExecutor` (the work is I/O + decode).
`capture_time` / `copyright_tag` still read from the **original** (per Decision 2,
`copyright_tag` is gated — see Section 3). `mkdir(parents=True, exist_ok=True)` the
per-session normalized dir.

**Tests (`test_ingest.py`):** a folder with one Orientation=6 JPEG + one normal
JPEG → after ingest, the rotated one's `Image.path` is under `normalized/` and is
upright, the normal one's `Image.path` is the original; `capture_time` is read for
both; no source file was modified (compare bytes/mtime).

**Check:** `pytest -v` green.
Commit: `phase C.3 section 2: normalize orientation to upright at ingest (+ tests)`

### Section 3 — Deprecate copyright override (Decision 2): match always wins + suppress for roster jobs + clean placeholder
Two coordinated changes:
- **Ingest gate (suppress for roster jobs):** gate `copyright_tag` population by
  whether the job has a roster. `_ingest_job` computes `read_copyright =
  db.query(PlayerMembership).filter_by(job_id=job_id).first() is None` once and
  passes it to `ingest_folder`; when False, store `copyright_tag = None`.
- **Precedence flip (`cluster_matching.py:85-96`):** on a high-tier match set
  `auto_label = name`, `auto_label_source = "match"` **unconditionally**; **delete**
  the copyright-wins / `match_label_conflict` branch. No match → leave `auto_label`
  as-is (copyright hint for non-roster jobs, else `None` → `display_label()` =
  "Player {id}").

**Tests:** roster job with embedded copyright → `copyright_tag` is `None`, clusters
labeled by **match** or **"Player {id}"**, and **no `needs_duplicate_auto_label`**;
a high-tier match with a *conflicting* copyright present → `auto_label` = match
name, source `match`, **no `match_label_conflict`** (this **updates**
`test_cluster_matching.py:129`, which encoded the now-reversed precedence — an
intended change); no match + no copyright → `display_label()` returns "Player {id}";
a no-roster job with copyright + no match → copyright still used (classic fallback).

**Check:** `pytest -v` green (incl. the updated precedence tests).
Commit: `phase C.3 section 3: face match wins over copyright + suppress copyright for roster jobs (+ tests)`

### Section 4 — README + end-to-end visual verification
Document in `README.md`: orientation is normalized to upright at import (no more
manual strip needed); the move-mode note; the roster-job copyright behavior. Then
the acceptance pass below.

Commit: `phase C.3 section 4: README + orientation acceptance`

---

## Deferred addendum — display-only rotate in the review UI (build AFTER the shoot)

> **Status: deferred — NOT part of the core C.3 build above, and explicitly built
> after the shoot, not now.** This is a **view-layer-only** convenience in the
> desktop **`frontend/`** review UI, entirely separate from the upright-pixels
> pipeline fix. It exists only because, with orientation stripped, the review
> screen shows portrait images sideways, which makes verifying matches hard.

**Hard boundary: view-layer only.** This must **not** touch ingest, export, the
matcher, the stored bytes, or the backend in any way — it is purely how the already-
served image is *displayed*. Implementation is a CSS `transform: rotate(...)` on the
displayed `<img>` in **`ImageModal.jsx:121`** (the full-size, keyboard-driven match-
verification viewer — the screen where sideways hurts most) and optionally the
thumbnail strip in **`ClusterCard.jsx`**. Because it's a transform on the served
image, it changes nothing downstream.

### The two flavors (weighed)

- **Manual per-image rotate *(recommended)*.** A rotate-90° control (a button +
  a keyboard shortcut, e.g. `r`, alongside the existing role keys) that steps the
  displayed image through 0/90/180/270°. Hold the rotation in the viewer's state,
  keyed by `image_id` (session-local, or `localStorage` to persist across a review
  session) — **no backend, no persisted column** (that would cross the view-layer
  boundary). Dead simple, works on **any** sideways image regardless of *why* it's
  sideways, and needs no orientation signal at all.
- **Auto-rotate the display from detected orientation.** Needs an orientation signal,
  and both sources are poor here:
  - *From EXIF orientation* — but the strip removed it (the whole problem); and if
    EXIF **is** present, C.3's core upright fix already makes the image upright, so
    auto-from-EXIF is **redundant** with the core fix.
  - *From face-landmark inference* (e.g. eye-line angle) — a **new computation** that
    belongs in the pipeline, not the view layer, so it **crosses the "view-layer
    only" boundary** and is scope-creep.
  → **Auto is not worth building:** redundant where it would work, out-of-layer
  where it wouldn't.

### Is it still needed once C.3's core (upright pixels) lands?

**Largely no — for new work, the core fix makes this unnecessary.** `ImageModal`
reads the same `full_url` / `thumb_url` that the core fix makes upright, so once
upright-pixels ships **the review display is correct with zero view-layer work.**
The manual rotate's remaining value is narrow:

- **Interim:** bridges the gap *before* C.3 core ships.
- **Insurance:** the only lasting use is **already-imported sideways jobs** that C.3
  core doesn't retroactively fix (re-import is 409-blocked — e.g. the existing test
  job), plus a manual override for any odd one-off rotation.

**Recommendation:** treat it as **optional, lowest-priority, build-after-the-shoot**,
and **very likely skip it entirely once C.3 core is in** — build it only if there are
legacy already-imported sideways jobs that must stay reviewable without re-importing.
If built, it's a single tiny frontend section:

> **Section A (deferred) — viewer rotate control.** Add a rotate-90° control +
> shortcut to `ImageModal` (and optionally `ClusterCard` thumbs) applying a CSS
> transform; persist per-`image_id` rotation in `localStorage`. **Manual check:** a
> sideways image rotates upright in the viewer with the control/shortcut and the
> rotation sticks while navigating; export and the cluster data are untouched.
> Commit: `phase C.3 section A: display-only rotate in the review viewer`

---

## Acceptance

1. A **fresh** import of a shoot containing portrait (EXIF-rotated) JPEGs shows
   **upright** thumbnails and `/full` images in the review UI.
2. Exporting that job produces **upright** files in `To_be_Cropped` / `Team Images`
   / `Pano Images` (open a known-portrait deliverable and confirm).
3. The **same clusters / same matches** result as before normalization (matching is
   unaffected) — spot-check the test shoot's sorting is identical.
4. Source files on the share are **byte-for-byte unchanged**; only copies under
   `DATA_DIR/normalized/` are new.
5. A face match **always wins over copyright** (the `cluster_matching` precedence is
   flipped). With a roster attached, ingested images carry **no copyright label**;
   clusters are labeled by **face match** or a clean **"Player {id}"** placeholder
   (never a metadata fragment), and the `needs_duplicate_auto_label` mislabels from
   re-inserted studio XMP (the Evoto case) are **gone**. A no-roster job still uses
   copyright **only as a no-match fallback** (classic workflow intact).
6. `pytest -v` green incl. new + updated precedence tests; no migration.

## Out of scope (defer)

- **Retroactively normalizing already-imported jobs** (the 409 blocks re-import;
  re-create the job to re-ingest if ever needed).
- **Uniform metadata-free deliverables** — C.3 drops metadata only on the rotated
  *copies*; upright originals are exported as-is. If you later want all delivered
  files metadata-clean, that's a separate export-time strip.
- **Lossless JPEG rotation** (jpegtran-style). C.3 re-encodes rotated images at
  q95; the quality delta is negligible for these and the code stays simple.
- **RAW formats** — unchanged from current ingest (`SUPPORTED_EXTS` only).
