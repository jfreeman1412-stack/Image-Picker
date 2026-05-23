# Phase B.2 — Hand-off (Roster check-in: pick a shoot, pick a player, capture)

> **Status: decisions LOCKED 2026-05-23; building section-by-section.** This doc
> reflects the locked plan after two rounds of review. The headline refinement
> from review: **the captured-✓ status is SHOOT-SPECIFIC, not cross-shoot** —
> each player needs their own reference photo *for each shoot* — which reshaped
> Decision 1's filter and replaced Decision 2's blanket-`PUT` with a new
> shoot-scoped replace endpoint, and added a shoot-scoped delete (Decision 6).

Read this in full before starting. This is the **second step** of the
**B-series** mobile capture app. **B.1** (merged to `main`, `6b75794`) proved the
scary on-device parts: rear-camera preview, the framing oval, capture-to-JPEG,
and a real multipart upload to the A.2 reference endpoint — all hardcoded to a
single test player via `VITE_REF_PLAYER_ID`. **B.2 turns that prototype into a
usable check-in tool:** a volunteer picks a *shoot* from a dropdown, sees the
shoot's *roster*, filters/searches to the player in front of them, taps the
name, and the existing capture flow uploads as **that** player. The hardcoded
env id goes away; the player is chosen in the UI.

Everything novel about the camera is already done and **must be reused, not
rewritten** — B.2 wraps B.1's `useCamera` hook and capture state machine with a
selection layer in front of it. The new work is comparatively ordinary React: a
shoot dropdown, a filterable roster list, a live shoot-specific ✓ indicator, a
"remove this shoot's photo" path, and the plumbing that carries the chosen
player + shoot into the upload.

**Backend footprint (deliberately scoped):** the B-series had been backend-free;
B.2 adds **three small additive endpoints**, all leaving A.1/A.2 byte-for-byte
unchanged — a read-only shoot-scoped status endpoint (Section 1), and a
shoot-scoped reference **replace** + **delete** pair (Section 5). Each ships with
A-series pytest tests and a green suite. **The capture app itself stays
test-free** (same as B.1 — no JS test runner); its sections are verified by an
**on-device manual check**.

---

## ⚠️ Decisions — LOCKED 2026-05-23

All decisions below are settled. Rejected alternatives are kept for the record.

### Decision 1 — Shoot-scoped per-player reference status *(LOCKED: Option C, shoot-scoped)*

**The need:** the roster screen shows a ✓ badge per player meaning "already has
a reference photo **for this shoot**," and a "needs photo only" toggle that
filters to players *without* one. We need this for **every** player on screen
load without one HTTP call per player.

**What the existing API offered:** nothing returns reference status in bulk —
`GET /api/players/roster/{job_id}`, `GET /api/players`, and
`GET /api/players/{id}` all omit it; only the per-player
`GET /api/players/{id}/references` exists (the N-calls problem). So it was N
calls vs. a backend addition. The data is one cheap indexed query server-side.

> **Shoot-specific (not cross-shoot) — the key correction from review.** A
> reference belongs to the global `Player`, but for *check-in completeness* each
> shoot needs its **own** photo. So ✓ means "this player has a reference whose
> `captured_job_id == this shoot`." A reference from a *prior* shoot, or one with
> no provenance (`captured_job_id IS NULL`, e.g. a B.1 upload), does **NOT**
> satisfy this shoot and must not show ✓. This is why Decision 4 (always send
> `?job_id=`) is load-bearing, not cosmetic.

**LOCKED — Option C:** a new additive read-only endpoint
`GET /api/players/roster/{job_id}/reference-status` →
`{"player_ids_with_references": [int, …]}`, **filtered on
`captured_job_id == job_id`**. The roster screen makes two cheap calls on
shoot-select (roster + status) and merges them into a `Set` for the badges.
Implemented in Section 1:
```python
@router.get("/roster/{job_id}/reference-status")
def shoot_reference_status(job_id: int, db: DbSession = Depends(get_db)):
    if db.query(Job).get(job_id) is None:
        raise HTTPException(404, "Job not found")
    rows = (db.query(ReferenceFace.player_id)
            .filter(ReferenceFace.captured_job_id == job_id)   # this shoot only
            .distinct().all())
    return {"player_ids_with_references": [pid for (pid,) in rows]}
```
- *Rejected — Option A (N per-player calls, zero backend):* a 150-player shoot =
  150 requests per roster open. There's precedent for "N small calls"
  (`RosterModal.jsx:65`) but it's capped at "~30 … cheap" and flags batching at
  larger scale. Workable fallback, not chosen.
- *Rejected — Option B (augment the A.1 roster endpoint with `has_reference`):*
  mutates an A.1 endpoint's response shape, crossing the A.1/A.2 separation.
- *Rejected — Option D (generic bulk `?player_ids=`):* over-engineered.

### Decision 2 — Shoot-scoped replace endpoint for capture *(LOCKED; supersedes the original blanket-`PUT` proposal)*

B.2's model is **one reference per (player, shoot)** with a binary ✓, and retake
**replaces** that shoot's photo. The original proposal — A.2's existing
`PUT /api/players/{id}/references` (`replace_player_references`) — **wipes ALL of
a player's references regardless of shoot**, so capturing a kid at shoot B would
destroy their shoot-A reference. **Wrong for shoot-specific status.** A.2's
`POST` (append) is the opposite problem — retaking accumulates multiple refs for
the same shoot.

> **Data-model implication (worked through in review): no model change.**
> `ReferenceFace.captured_job_id` already exists (`db_models.py:248`) and
> references are a one-to-many off `Player`; two refs for two shoots already
> coexist (`test_references.py:251-257`). "One per (player, shoot)" is enforced
> in the **capture path**, NOT a DB constraint — a `UNIQUE(player_id,
> captured_job_id)` would forbid the *multiple references per shoot* A.2
> deliberately allows (`db_models.py:232`, `test_multiple_references_allowed`)
> and `captured_job_id` is nullable. So the schema is untouched (no migration).

**LOCKED:** add a new additive **shoot-scoped replace** endpoint
`PUT /api/players/{player_id}/references/shoot/{job_id}` (Section 5). It
**validates the new photo (quality gate) → wipes only refs `WHERE
player_id=P AND captured_job_id=J` → stores the new one with
`captured_job_id=J`**, atomically in one request. Atomic matters on flaky gym
wifi; it makes "retake" mean "replace *this shoot's* photo" while every other
shoot's reference is untouched, and (like A.2's existing replace) a failed gate
leaves the existing shoot reference intact. A.2's existing `POST`/`PUT`/`DELETE`
routes and their tests stay **byte-for-byte unchanged** — B.2 does not use them.
- *Rejected — client `POST`-then-`DELETE`:* same end state with no new write
  endpoint, but non-atomic (a failed `DELETE` after a successful `POST` strands
  a second ref) and 3+ round-trips per capture over the tunnel.
- *Rejected — additive `POST` per shoot:* simplest backend, but retake
  *accumulates* (no true replace) — a bad shot lingers for that shoot.

### Decision 3 — Remove `VITE_REF_PLAYER_ID` *(LOCKED)*

The player is chosen in the UI, so the hardcoded env id is obsolete. Drop it
from `App.jsx`, `.env`, `.env.example`, and the README, deleting the "no player
set" fallback branch. (We do **not** add a new env var to preselect a shoot
either — shoot selection stays UI-driven.)

### Decision 4 — Send `?job_id=` on upload — now load-bearing *(LOCKED)*

B.1 omitted `?job_id=`. B.2 always knows the selected shoot, and `captured_job_id`
is the **scope key** for both shoot-specific status (Decision 1) and scoped
replace/delete (Decisions 2 & 6) — not merely provenance. Every capture targets
the shoot-scoped endpoint, which sets `captured_job_id = job_id`.

### Decision 5 — Routerless: lift state into `App` *(LOCKED)*

Three screens (shoot picker → roster → capture), driven by `App`-level state
(`view`, `selectedJob`, `selectedPlayer`). The roster's **filter state (team /
search / needs-photo) and the captured-✓ `Set` live in `App`**, not the roster
component — so they survive the round-trip to the capture screen and back, and
"return preserving filters" + live ✓ updates fall out for free. No
`react-router`.

### Decision 6 — Shoot-scoped delete (Remove photo) *(LOCKED — added in review)*

Real workflow: a volunteer takes a bad shot (their own kid squirming) and needs
to **remove it entirely**, not just retake — leaving the player **un-✓ for this
shoot**. B.2 adds a delete path:
- **Backend (Section 5, shares the scoped wipe with Decision 2):**
  `DELETE /api/players/{player_id}/references/shoot/{job_id}` — deletes only refs
  `WHERE player_id=P AND captured_job_id=J`, never touches other shoots,
  **idempotent** (returns `{"deleted": 0}` if none), 404 only if the player row
  is missing. +pytest test.
- **UI (Section 7):** a "Remove photo" action on the capture screen for an
  already-✓ player, behind a **confirmation step** so a good photo can't be
  fat-fingered away. The ✓ badge **clears live** (remove from
  `referencedPlayerIds` — no re-fetch, mirroring the live-✓-on-capture pattern),
  and the player reappears if the needs-photo filter is on.

Minor calls made for you (flagged, easy to flip):
- **Roster rows are per-membership, ✓ is per-player+shoot.** The roster endpoint
  returns one item per membership; a player on two teams in one shoot appears
  twice, and both rows share the same `player_id`, so once captured both show ✓.
- **Coaches participate normally** — `is_coach` rows show a "Coach" tag and obey
  the filters like anyone else (a coach is a distinct `Player` and can have a
  reference).
- **Dropdown lists all non-archived shoots** (`GET /api/jobs` already excludes
  archived). Filtering to *recent/active* shoots is a future refinement, out of
  scope.
- **Matching is unaffected.** A.4's matcher loads **all** of a roster player's
  references regardless of capturing shoot (`matching.py:88-93`, filters only by
  `player_id`). B.2's shoot-scoped capture/status does **not** change matching;
  scoping matching itself to a shoot's own reference is a separate A-series
  change if ever wanted.

---

## Context you need

### The endpoints B.2 consumes

Existing (shipped, unchanged):
- **`GET /api/jobs`** (`api/jobs.py:285`) — the shoot dropdown's source. A **bare
  JSON array** (not an `{items}` envelope) of `{id, name, …, archived}`, newest
  first, **archived excluded by default**. B.2 needs only `id` + `name`.
- **`GET /api/players/roster/{job_id}`** (`api/players.py:53`) — the roster.
  `{memberships_loaded, items: [{player_id, name, team, is_coach}],
  distinct_teams}`, ordered by membership id. **404** if the job is missing. The
  team-filter dropdown is built from the distinct `team` values in `items`.

New in B.2 (additive; A.1/A.2 otherwise frozen):
- **`GET /api/players/roster/{job_id}/reference-status`** — **Section 1, DONE.**
  `{"player_ids_with_references": [int, …]}`, shoot-scoped
  (`captured_job_id == job_id`). 404 if the job is missing.
- **`PUT /api/players/{player_id}/references/shoot/{job_id}`** — **Section 5.**
  Shoot-scoped replace (validate → wipe this shoot's refs → store one).
  Multipart, **field `file`**. 200 with `{id, player_id, captured_job_id,
  det_score, face_area_ratio, image_path}`. 400 on a failed gate (existing shoot
  ref kept); 404 if player/job missing.
- **`DELETE /api/players/{player_id}/references/shoot/{job_id}`** — **Section 5.**
  Shoot-scoped delete → `{"deleted": n}` (idempotent). 404 only if player missing.

B.2 does **not** use A.2's `POST`/`PUT /{id}/references` or
`DELETE /{id}/references/{ref_id}` — the shoot-scoped pair replaces them for this
workflow.

**Failure responses to surface (reuse B.1's handling):** the quality gate returns
**400** `detail = {error, message}` (codes `no_face`, `multiple_faces`,
`low_confidence`, `face_too_small` — `services/references.py`); show `message`
verbatim with a **Retake**. **404** (player/job gone) → "that shoot/player is
gone — go back and reselect," not a config banner. The error-reading idiom is
already in `capture/src/App.jsx:79`:
```js
if (!res.ok) {
  const body = await res.json().catch(() => ({}));
  const code = body?.detail?.error;   // 400 → {error, message}; 404 → string
}
```

### B.1 building blocks to reuse (don't rewrite)

- **`capture/src/useCamera.js`** — acquires the rear camera once, tears down on
  unmount, maps `getUserMedia` failures to friendly messages. B.2 mounts the
  capture screen (and thus the camera) **only when a player is selected**, so the
  camera isn't held open while browsing the roster. Reuse as-is.
- **`capture/src/App.jsx`** capture state machine
  (`live → review → uploading → success|error → live`) + the capture-to-JPEG /
  object-URL lifecycle. B.2 refactors the single screen into screen components
  but keeps this machine intact for the capture screen.
- **`capture/src/styles.css`** dark mobile-first tokens (`--accent`, `--success`,
  `--warn`, `--text-muted`, …) + the full-bleed camera layout. New roster/picker
  screens use the same tokens but **scroll** (relax `html,body{overflow:hidden}`
  per-screen — Section 4).
- **`capture/vite.config.js`** — port 8022, `strictPort`, same-origin
  `/api → :8020` proxy, `allowedHosts: true`. **Unchanged.** The new endpoints are
  hit through the same proxy: same-origin, **no CORS, no `main.py` change.**

### Seed recipe — a multi-team test roster (needed from Section 2 on)

You need a shoot whose roster spans 3 teams with a mix of captured and
not-yet-captured players, so the team filter, search, toggle, and ✓ badges have
something to exercise on-device.

1. **Create a shoot (Job).** The roster needs **no images or sessions** — make a
   job in the editor wizard pointing at any folder, or `POST /api/jobs` with a
   throwaway `root_path`. Read its `id` from `GET /api/jobs`.
2. **Upload `test-roster.csv`** (3 teams, a coach each, ~4 players each):
   ```
   Ava-Nguyen,Lions
   Mason-Reyes,Lions
   Sofia-Petrov,Lions
   Liam-OConnor,Lions
   Coach-Lions,Lions
   Ethan-Kim,Tigers
   Maya-Johnson,Tigers
   Noah-Alvarez,Tigers
   Coach-Tigers,Tigers
   Zoe-Martin,Bears
   Lucas-Brandt,Bears
   Harper-Singh,Bears
   Olivia-Day,Bears
   Coach-Bears,Bears
   ```
   ```powershell
   curl.exe -F "file=@test-roster.csv" http://localhost:8020/api/players/roster/<JOB_ID>
   ```
3. **Pre-capture a few** (once Sections build) so ✓ / "needs photo only" have a
   mix. **Reset for re-test:** `DELETE /api/players/{id}/references/shoot/{job_id}`
   (the Section-5 endpoint) clears a player's ✓ for that shoot.

The README (Section 8) captures this so on-device testing is repeatable.

---

## Don't break

- **A.1 and A.2 stay frozen.** B.2's only backend additions are the three new
  routes (Sections 1 & 5) + their tests. Do **not** edit the existing functions
  in `services/references.py`, the existing `references.py`/`players.py`/`jobs.py`
  routes, `main.py`, or the CORS regex. Add new functions/routes alongside.
- **All backend tests stay green.** Add tests for each new endpoint; keep
  `pytest -v` fully green (currently **449** + the new ones).
- **Don't touch `frontend/`.** The editor app is independent.
- **Reuse B.1's camera + capture machine.** Don't re-implement `getUserMedia`,
  the oval, or capture-to-JPEG.
- **Keep the same-origin proxy.** No CORS work, no `https?` regex change.
- **Respect the A.2 contract.** Upload the full frame as multipart field `file`;
  read failures via `detail.error` / `detail.message`. Use the shoot-scoped
  endpoints (Decisions 2 & 6).

---

## Section 1 — Backend: roster reference-status endpoint  ✅ DONE (`fafe3fd`)

Added `GET /api/players/roster/{job_id}/reference-status` →
`{"player_ids_with_references": [...]}` in `players.py`'s roster group (before the
dynamic `/{player_id}` route), filtered on `captured_job_id == job_id`
(shoot-scoped per Decision 1). Imports `ReferenceFace`. 404 if the job is
missing. Counts a player regardless of current roster membership; the client only
badges players it displays.

**Tests (`test_players.py`):** shoot-scoping in **both directions** (a
cross-shoot-identity player whose ref is for shoot A shows ✓ only for shoot A;
NULL-provenance refs never count), the no-refs-for-this-shoot empty case, and
unknown-job → 404. (A `SessionLocal` is exposed on the `client` fixture to seed
`ReferenceFace` rows directly.)

**Check:** `pytest -v` green (`test_players.py` 25 passed; full suite 449 passed).
`curl …/reference-status` returns a player captured for this shoot and omits one
captured for another.

Commit: `phase B.2 section 1: roster reference-status endpoint (+ test)`

---

## Section 2 — Shoot picker screen

Refactor `capture/src/App.jsx` into a small view switch (`view` +
`selectedJob`/`selectedPlayer` in `App`) and add a **ShootPicker** as the
cold-start view. On mount it fetches `GET /api/jobs`, renders a dropdown (or
tappable list) of `name`s, and on select stores `{id, name}` and advances to the
roster view. Reuse the dark tokens; a centered card, not the camera. States:
**loading / empty ("No shoots found — create one in the editor app and load a
roster") / error (HTTP/network + Retry) / loaded**. `res.ok`-checked fetch; no
error body in state.

**Manual check:** on the phone, cold-load shows the dropdown listing the seeded
shoot(s); picking one advances to the roster view; with the backend stopped, the
error state shows a clear message + Retry.

Commit: `phase B.2 section 2: shoot picker (GET /api/jobs dropdown)`

---

## Section 3 — Load roster + reference status into App state

On shoot-select, fetch **in parallel** `GET /api/players/roster/{job_id}` and
`GET /api/players/roster/{job_id}/reference-status`. Store in `App`: the
membership `items`, and a `referencedPlayerIds` **`Set`** from
`player_ids_with_references`. Lift to `App` (not the roster component) so it
survives the capture round-trip (Decision 5). Render a **bare** list (name,
team, "Coach" tag, ✓ when `referencedPlayerIds.has(player_id)`) — filters come in
Section 4. Handle **no-roster** (`memberships_loaded === 0`): "No roster loaded
for this shoot — upload one (see README)." Include **Back to shoots**.

**Manual check:** selecting the seeded shoot shows every player with the correct
team + "Coach" tags; the players you pre-captured **for this shoot** show ✓ and
the rest don't; counts match `GET /api/players/roster/{job_id}`. A shoot with no
roster shows the empty state.

Commit: `phase B.2 section 3: load roster + reference-status, ✓ badges`

---

## Section 4 — Roster filters: team + search + needs-photo (stacking)

Three controls above the list, all reading/writing the **lifted** filter state in
`App`, applied together (AND):
- **Team filter** — dropdown of the distinct `team` values + "All teams" default.
- **Name search** — case-insensitive substring on `name`.
- **"Needs photo only" toggle** — keep only players **not** in
  `referencedPlayerIds`.

Default = **all teams, empty search, toggle off** → the **full unfiltered
roster**. Show a result count + a "no matches" state. This screen **scrolls** —
relax the B.1 `html,body{overflow:hidden}` for the roster/picker views (a
screen-level class) so long rosters scroll while the camera stays full-bleed.

**Manual check:** each control alone narrows correctly; **stacked** (e.g. Tigers
+ "ma" + needs-photo) they compose; clearing all returns the full roster;
toggling "needs photo only" hides the pre-captured ✓ players; coaches appear and
obey the filters.

Commit: `phase B.2 section 4: team filter + name search + needs-photo toggle`

---

## Section 5 — Backend: shoot-scoped replace + delete endpoints

The two write endpoints, built together (they share the scoped wipe), **before**
the UI consumes them. **Additive** — A.2's existing functions/routes/tests stay
byte-for-byte.

In `services/references.py` add (alongside the existing helpers):
- `_wipe_player_references_for_job(db, player_id, job_id)` — a scoped variant of
  `_wipe_player_references` filtering `captured_job_id == job_id` (rows + files,
  best-effort unlink).
- `replace_shoot_reference(db, player_id, job_id, data, *, original_filename)` —
  `_require_player` + require the job exists; **validate the new photo
  (`evaluate_reference_quality(detect_faces(tmp))`) BEFORE wiping**; then
  `_wipe_player_references_for_job`; then `_store_reference(... captured_job_id=
  job_id)`. Mirrors `replace_player_references` exactly but shoot-scoped.
- `delete_shoot_references(db, player_id, job_id)` — `_require_player`; scoped
  delete (rows + files); return `{"deleted": n}` (0 if none — idempotent).

In `api/references.py` add two routes (3-segment paths — no collision with
`/{player_id}/references/{ref_id}`, whose `ref_id` is typed `int`):
```
PUT    /api/players/{player_id}/references/shoot/{job_id}   (multipart file) → 200 summary | 400 gate | 404
DELETE /api/players/{player_id}/references/shoot/{job_id}                     → 200 {"deleted": n} | 404 player
```
Reuse `_quality_400` for the 400 mapping.

**Tests (`test_references.py`):**
- scoped replace sets exactly one ref for `(player, job)` and **leaves another
  shoot's ref intact**; re-replacing keeps it at one for that shoot;
- a failed gate on replace **keeps the existing shoot ref** (validate-before-wipe);
- scoped delete removes only this shoot's ref(s), **leaves other shoots'**,
  returns the count, and is **idempotent** (`deleted: 0` when absent);
- 404s (unknown player; unknown job on replace).

**Check:** `pytest -v` green (full suite + new tests). `curl` a replace then a
delete against a seeded player and confirm other-shoot refs survive.

Commit: `phase B.2 section 5: scoped replace + delete reference endpoints (+ tests)`

---

## Section 6 — Capture for the selected player (scoped replace + provenance)

Tapping a roster row sets `selectedPlayer` and switches to the **capture**
screen — B.1's `useCamera` + capture/review/upload machine, mounted on demand.
Changes vs. B.1:
- Upload via
  **`PUT /api/players/{selectedPlayer.id}/references/shoot/{selectedJob.id}`**
  (Decisions 2 & 4), multipart field `file`, filename `capture.jpg`. Reuse the
  `res.ok` + `detail.error`/`detail.message` handling.
- **Remove `VITE_REF_PLAYER_ID`** and its 404/"no player set" branch (Decision
  3); the top bar shows the **selected player's name + team**, e.g.
  "Capturing → Ava-Nguyen · Lions".
- **"Replacing existing" affordance:** if the player is in `referencedPlayerIds`,
  show "Ava-Nguyen already has a photo for this shoot — this will replace it" on
  the live/review screen and label the confirm "Replace photo" instead of "Use
  photo." A failed gate keeps the old shoot ref (server validates before wiping);
  surface the gate message + Retake as in B.1.
- A **Back to roster** / Cancel control returns without uploading (camera tears
  down on unmount).
- **On success:** return to the roster with the prior **filter state intact**
  (it lives in `App`) and add the just-captured `player_id` to
  `referencedPlayerIds` in client state — **live ✓, no re-fetch**. On a hard 404,
  route back to the shoot picker.

**Manual check:** pick an un-captured player → frame → **Replace/Use** → 200
"Saved," and `GET /api/players/{id}/references` shows one ref with
`captured_job_id` = the selected shoot. Pick a **✓** player: the "will replace"
affordance shows; replacing keeps exactly one ref **for this shoot** and leaves
any other-shoot ref intact. Trip a rejection (wall/blurry): the message shows,
Retake works, the pre-existing shoot ref is unchanged. Back on the roster the
just-captured player shows ✓ live (and drops out if "needs photo only" is on).

Commit: `phase B.2 section 6: capture targets selected player (scoped replace + job_id)`

---

## Section 7 — Remove photo (shoot-scoped delete)

Add a **"Remove photo"** action on the capture screen for an already-✓ player
(Decision 6). Tapping it opens a **confirmation** ("Remove Ava-Nguyen's photo for
this shoot? This can't be undone."). On confirm:
`DELETE /api/players/{id}/references/shoot/{job}` → on 200, return to the roster,
**remove the player from `referencedPlayerIds`** (live un-✓, no re-fetch); they
reappear if "needs photo only" is on. Surface a delete failure with a clear
message + the option to retry/cancel; never strand the UI.

**Manual check:** select a ✓ player → Remove → confirm → back on the roster the ✓
is gone live; with "needs photo only" on, the player reappears in the list. A
ref for that player in **another** shoot (if any) is untouched
(`GET /api/players/{id}/references`). Cancelling the confirm leaves the photo.

Commit: `phase B.2 section 7: remove-photo (scoped delete + confirm + live un-✓)`

---

## Section 8 — README + multi-team seed recipe

Update **`capture/README.md`**: replace the B.1 single-player flow with the B.2
flow (pick shoot → filter roster → tap player → capture; retake replaces *this
shoot's* photo; Remove deletes it), fold in the **3-team seed recipe** (Context
section), and remove the `VITE_REF_PLAYER_ID` instructions (delete it from
`.env`/`.env.example`, per Decision 3 — done where the env var was removed in
Section 6; the README just stops referencing it).

**Manual check:** a teammate can reproduce the whole flow from the README on a
fresh phone — seed → pick shoot → filter → capture → ✓ → remove → un-✓.

Commit: `phase B.2 section 8: README + 3-team seed recipe`

---

## Conventions (match the existing code)

- **Backend (Sections 1 & 5):** additive functions/routes only; testable via the
  existing `client`/`db` fixtures; `{...}` plain-dict responses; `pytest -v`
  green after each. Follows A-series rules, **not** the capture app's no-test
  rule.
- **Capture app:** Vite + React 18, JSX (no TS), function components + hooks;
  reuse B.1's `useCamera` + capture machine; one `src/styles.css` with the
  existing dark tokens; mobile-first.
- `fetch` with mandatory `res.ok` checks; read error bodies via
  `body?.detail?.message || body?.detail?.error`. No error object lands in state.
- Target player + shoot come from **UI selection in `App` state**, never a
  hardcoded id/env var.
- Filter state + the captured-✓ `Set` live in `App` (lifted) so they survive
  navigation — no `react-router` (Decision 5).
- Single-line commit messages per section (`phase B.2 section N: …`).
- Same-origin `/api` proxy in dev; no CORS / `main.py` change.

## Acceptance

1. The cold-start screen lists **non-archived shoots** from `GET /api/jobs`;
   selecting one loads that shoot's roster.
2. The roster shows **every membership** (name + team + Coach tag), defaulting to
   the **full unfiltered roster**.
3. **Team filter, name search, and "needs photo only" each work and STACK** (AND);
   clearing all returns the full roster.
4. A **✓ badge** reflects **shoot-specific** reference existence
   (`captured_job_id == this shoot`), correct against seeded data.
5. Selecting a player opens the capture screen targeting **that player + shoot**;
   a good close-up uploads via the **scoped-replace** endpoint and shows "Saved";
   the player ends with **exactly one** reference **for this shoot**, other shoots
   untouched.
6. A **✓ player can be reselected**; the UI makes **"replacing existing" clear**;
   a **failed gate leaves the shoot reference intact**. Each rejection surfaces
   its message + Retake (no raw error objects).
7. **Remove photo** deletes only **this shoot's** reference (other shoots
   untouched), behind a **confirmation**, and clears ✓ **live**.
8. After capture/remove, the app **returns to the roster with prior filters
   preserved**, and ✓ updates **live without a reload**.
9. **`VITE_REF_PLAYER_ID` is removed**; the app needs no hardcoded player.
10. Backend: only the **three new additive routes** are added; A.1/A.2 are
    otherwise byte-for-byte unchanged; `pytest -v` is fully green incl. the new
    tests. `capture/README.md` documents the flow + seed recipe; `frontend/` is
    untouched.

## Out of scope (defer — do NOT build in B.2)

- **Auth / volunteer login.** (Later phase.)
- **Installable-PWA behavior** — manifest, service worker, offline/queue, "Add to
  Home Screen." (Later phase.)
- **Productionizing on the R640** — real cert/domain, the CORS `https?` change,
  hosting. B.2 stays dev-device testing via the tunnel + `/api` proxy.
- **Filtering the shoot dropdown to recent/active shoots.** Lists all
  non-archived jobs.
- **Multiple references per (player, shoot) from the app.** B.2 is
  one-reference-per-(player,shoot) via the scoped-replace endpoint; multi-angle
  capture is a later phase.
- **Scoping A.4 matching to a shoot's own reference.** Matching keeps using all of
  a roster player's references; a per-shoot match index is a separate A-series
  change.
- **Client-side face detection / quality pre-check, cropping to the oval,
  resizing.** Unchanged from B.1 — the server gate is the only quality authority;
  upload the full frame.
- **Creating jobs or uploading rosters from the capture app.** Seeding is a manual
  setup step (the recipe), not a feature.
- **Front/rear camera flip, torch/zoom**, and other capture polish (rear default
  stays from B.1).
- **Automated tests in the capture app / a JS test runner.** On-device manual
  checks remain the verification, consistent with B.1.
