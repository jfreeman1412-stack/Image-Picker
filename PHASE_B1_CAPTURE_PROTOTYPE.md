# Phase B.1 — Hand-off (Capture-and-upload prototype, mobile PWA)

Read this in full before starting. This is the **first step** of the **B-series**:
a browser-based **mobile capture app** for volunteers at photo shoots. The
end state (later phases, NOT this one) is — a volunteer loads a shoot roster on
a phone/tablet, taps a player's name, snaps a close-up face photo, and it
uploads as that player's reference. The A-series already built everything the
photo lands in: `Player`/`PlayerMembership` (A.1), the reference-upload endpoint
with its quality gate (A.2), the matching service (A.3), and pipeline
integration (A.4). **B.1 builds the client that feeds A.2's upload endpoint —
nothing on the backend changes.**

**Phase B.1 builds only the scariest, most-novel slice:** a bare
capture-and-upload screen. Open the device camera (`getUserMedia`), show a
**fixed framing oval** the volunteer fills with the player's face, snap a still,
and `POST` it to the existing
`POST /api/players/{player_id}/references` endpoint for a **hardcoded test
player**. There is **no** roster, **no** tap-a-name, **no** auth, **no** offline
/ installable-PWA behavior in this phase. Do not build any of that here — see
*Out of scope* at the bottom and respect it.

The goal is to **prove the hard parts work on a real device**: browser camera
access, the framing overlay, capturing a still to a JPEG, a successful
multipart upload, and clean handling of the quality-gate rejections A.2 can
return. Everything else in the B-series is comparatively ordinary React; this
is the part that can only be validated by holding a phone.

**There are no automated tests in this phase.** The existing `frontend/` ships
no test runner (no vitest/jest in `package.json`), and the things B.1 must
prove — camera permission, live preview, a real upload round-trip — are
inherently on-device. Each section below therefore ends with a concrete
**manual check** you perform on a phone/tablet. (Adding a unit-test runner is a
deliberate non-goal — see *Out of scope*.)

---

## ⚠️ Decisions

These need your sign-off before implementation. Each lists my **proposed**
default and the alternatives. I'll lock them once you confirm.

1. **Capture app location: a new top-level `capture/` folder (sibling to
   `frontend/` and `backend/`) — its own Vite + React app.** *(proposed)*
   - Why a separate app, not a route inside `frontend/`: different audience
     (volunteers vs. the editor reviewing clusters), different device class
     (phone/tablet vs. desktop), different lifecycle (it will become an
     installable PWA), and it must be served over HTTPS for the camera while
     the editor app stays plain `http`. Keeping them separate avoids polluting
     the editor's bundle and routing.
   - It mirrors `frontend/`'s stack exactly (React 18, Vite 5, the same
     `fetch`-with-`res.ok` and dark-theme conventions) so it reads like the
     rest of the repo.
   - Alternative (rejected for B.1): a `/capture` route inside `frontend/`.
     Cheaper to scaffold but entangles the HTTPS/camera concerns with the
     editor and complicates the eventual PWA manifest.

2. **Target a player without a roster: a hardcoded test `player_id`, supplied
   via a Vite env var `VITE_REF_PLAYER_ID` (default `1`).** *(proposed)*
   - A.2's `add_reference` does `_require_player(...)` and **404s if the player
     row doesn't exist** (`services/references.py:96`). So B.1 needs **one real
     `Player` row in the backend DB**. Recipe to seed one (no new code):
     create a job in the editor app, then
     `POST /api/players/roster/{job_id}` with a one-line CSV
     (`Test-Player,Test-Team`), then `GET /api/players` to read the assigned
     `id`. Put that id in `capture/.env` as `VITE_REF_PLAYER_ID`.
   - `?job_id=` provenance is **optional** in A.2 and B.1 **omits it** (a bare
     prototype has no shoot context yet).
   - Alternative: hardcode the literal `1` in source. Rejected — an env var
     keeps the magic id out of committed code and makes it trivial to retarget
     per machine.

3. **HTTPS / camera permission for device testing — LOCKED: a quick tunnel
   (`cloudflared` / ngrok).** `getUserMedia` only works in a **secure context**
   — `https://` or `localhost`. A phone hitting `http://<lan-ip>:<port>` gets a
   blocked camera and a confusing failure. **You chose the tunnel because iOS
   (iPhone/iPad) is in scope.**
   - The Vite dev server stays plain `http` on `localhost:8022`;
     `cloudflared tunnel --url http://localhost:8022` (or `ngrok http 8022`)
     publishes it at a **real, trusted HTTPS URL** (e.g.
     `https://<random>.trycloudflare.com`). Camera works on **every** device
     including iOS, with no cert warnings. Cost: dev traffic briefly transits
     the tunnel provider — acceptable for in-house prototype testing.
   - **Vite implication:** Vite 5 rejects requests whose `Host` header isn't a
     known host, so `vite.config.js` must set `server.allowedHosts` to include
     the tunnel host (use `allowedHosts: true` in dev to accept any, or pin the
     specific `*.trycloudflare.com` host). HMR over the tunnel isn't required
     for a prototype; a full reload is fine.
   - **No HTTPS dependency in the app** — the tunnel terminates TLS, so we do
     **not** add `@vitejs/plugin-basic-ssl` or configure `server.https`.
   - Considered and rejected: self-signed Vite cert (Android-only — iOS Safari
     often refuses `getUserMedia` on an untrusted origin) and the Chrome
     `treat-insecure-origin-as-secure` flag (per-device, Chrome/Android only).

4. **How it's served in development: its own Vite dev server on port `8022`,
   with a same-origin `/api` proxy to the backend on `8020`** *(proposed)* —
   exactly the pattern `frontend/vite.config.js` already uses (`proxy: { '/api':
   'http://localhost:8020' }`). The browser only ever talks to the capture
   origin, so **the upload is same-origin and never triggers CORS** — which
   matters because the backend's CORS allow-list only permits `http://` LAN
   origins (`main.py:45`), and our capture origin will be `https://`. The proxy
   sidesteps that entirely and keeps B.1 **backend-change-free.** (Forward note,
   not a B.1 task: if the capture app is ever served cross-origin to the API
   over HTTPS, the `_LAN_ORIGIN_REGEX` would need `https?` and the `https` LAN
   host — out of scope here.)

Minor calls made for you (flagged, easy to flip):
- **Rear camera by default:** request `facingMode: { ideal: 'environment' }`.
  The volunteer points the device *at* the player, not at themselves. A B.1
  "flip camera" control is optional; default to rear and move on.
- **Capture format JPEG:** `canvas.toBlob(..., 'image/jpeg', 0.92)` and upload
  with filename `capture.jpg`, so A.2's `_safe_ext` stores it as `.jpg`. (A.2
  accepts `.jpg/.jpeg/.png` and keeps the original bytes.)
- **Overlay shape: a vertical oval** (faces are taller than wide) centered in
  the frame, drawn as a CSS/SVG overlay — **not** a hard crop. We upload the
  **full frame**, not the oval interior (A.2 wants the whole photo; cropping is
  a future nicety). The oval is guidance only.
- **No `?job_id=` on the upload** (provenance deferred, per decision 2).

---

## Context you need

### The endpoint B.1 uploads to (A.2 — already shipped, do not change)

`POST /api/players/{player_id}/references` — multipart, the **field name is
`file`** (`api/references.py:38`, `file: UploadFile = File(...)`). Optional
`?job_id=` query param (omit it). On success returns **200** with
`{"id", "player_id", "captured_job_id", "det_score", "face_area_ratio",
"image_path"}`.

Failure responses B.1 must surface clearly:
- **400** with `detail = {"error": <code>, "message": <human text>}` when a
  quality gate fails. The `code` is one of: `no_face`, `multiple_faces`,
  `low_confidence`, `face_too_small` (`services/references.py:52`). The
  `message` is already volunteer-friendly — **show it verbatim** and offer
  "Retake".
- **404** with `detail = "Player not found"` if `VITE_REF_PLAYER_ID` points at
  a row that doesn't exist (i.e. you didn't seed the player — see decision 2).

The reference HTTP wrapper turns the quality error into that 400 shape
(`api/references.py:32`, `_quality_400`). Mirror `frontend/`'s error-reading
idiom — `RosterModal.jsx:109` does exactly this:
```js
if (!res.ok) {
  const body = await res.json().catch(() => ({}));
  throw new Error(body?.detail?.message || body?.detail?.error || `HTTP ${res.status}`);
}
```

### The quality gate, and what the overlay does (and doesn't) guarantee

A.2's gate (`services/references.py`, constants at lines 32–33):
- `REF_MIN_AREA_RATIO = 0.02` — the face bbox must be **≥2% of the frame area.**
- `REF_MIN_DET_SCORE = 0.65` — single-face detection confidence floor.
- `multiple_faces` — **any** second face at the 0.5 detector floor rejects the
  photo (the user wants bystanders over-caught).

**The framing oval exists to clear the 2% gate with no client-side detection
and no GPU call at capture time.** If the volunteer fills a centered oval that
is a large fraction of the frame, the resulting face bbox is *far* above 2% —
e.g. on a 1080×1920 frame, 2% is a ~204×204 px face, while a face filling an
oval ~60% of the frame width is ~600+ px wide. The margin is enormous, so the
oval only needs to be a *substantial* fraction of the frame (≥~50% width), not
precisely tuned.

**What the oval does NOT guarantee:** `det_score ≥ 0.65` (driven by focus,
lighting, and pose, not framing) and single-person framing. So uploads can
still come back `low_confidence` or `multiple_faces`. That's expected and is
**part of what B.1 proves** — that the rejection path round-trips and shows the
volunteer a clear "retake" message. Do **not** try to pre-empt these on the
client in B.1 (no client-side detection — that's the whole architectural
point).

### Camera frames and EXIF orientation

Frames drawn from a `<video>` element to a `<canvas>` are already pixel-oriented
as displayed — there is **no EXIF orientation tag to honor** (unlike a
file-picker upload). So the captured JPEG needs no rotation handling. Good.

### Frontend stack & conventions to match (`frontend/`)

- **Vite 5 + React 18 + react-router-dom 6**, ESM, function components with
  hooks (`package.json`, `main.jsx`). No TypeScript (JSX only).
- `fetch` directly (no axios), **always check `res.ok`** before reading the body
  and never let an error body land in state (`RosterModal.jsx:19` calls this out
  as the "Phase 5 lesson").
- Dark theme; styles live in a single `src/styles.css` with CSS variables
  (`--danger`, `--warn`, `--success`, `--text-dim`, …). The capture app gets its
  own `styles.css` in the same spirit (mobile-first, full-viewport).
- `index.html` already sets `viewport` + `theme-color`; reuse that head shape
  and add `viewport-fit=cover` for full-bleed camera on notched phones.
- Vite dev server binds `host: true` and uses `strictPort` (`vite.config.js`) —
  carry both over so the LAN URL is stable.

---

## Don't break

- **No backend changes.** B.1 adds a new folder only. Do **not** edit
  `main.py`, the CORS regex, `api/references.py`, or `services/references.py`.
  The endpoint contract above is fixed.
- **Don't touch `frontend/`.** The editor app is independent; the capture app
  is a separate Vite project. No shared `package.json`, no shared build.
- **Respect the A.2 contract.** Upload the full frame as multipart field
  `file`; read failures via `detail.error` / `detail.message`. Don't invent a
  new request shape.

---

## Section 1 — Scaffold the capture app

Create `capture/` as a standalone Vite + React app mirroring `frontend/`:

- `capture/package.json` — same deps as `frontend/` (`react`, `react-dom`;
  `react-router-dom` only if you add routing — a single screen may not need it).
  No HTTPS dev dep — the tunnel terminates TLS (decision 3). Scripts: `dev`
  (Vite on **8022**), `build`, `preview`.
- `capture/vite.config.js` — `port: 8022`, `strictPort: true`,
  `proxy: { '/api': 'http://localhost:8020' }`, and `server.allowedHosts` set to
  accept the tunnel host (decision 3 — `true` in dev, or pin
  `*.trycloudflare.com`). Plain `http` on `localhost`; the tunnel provides
  `https`.
- `capture/index.html` — `viewport` with `viewport-fit=cover`, dark
  `theme-color`, `<div id="root">`, mount `src/main.jsx`.
- `capture/src/main.jsx` + `src/App.jsx` — render a placeholder screen ("Capture
  — coming up") so the scaffold is verifiable before the camera exists.
- `capture/src/styles.css` — minimal mobile-first reset, full-viewport, dark.
- `capture/.env.example` — document `VITE_REF_PLAYER_ID=1`; add a real
  `capture/.env` locally (git-ignored).
- `capture/README.md` — exact run steps: seed a player (decision 2 recipe),
  start the backend (`8020`), `npm install` + `npm run dev` (`8022`), then
  `cloudflared tunnel --url http://localhost:8022` and open the printed
  `https://…` URL on the device.
- Add `capture/node_modules`, `capture/dist`, `capture/.env` to `.gitignore`.

**Manual check:** `npm run dev` in `capture/` serves on `localhost:8022`;
`cloudflared tunnel --url http://localhost:8022` prints an `https://…` URL that,
opened **on a phone**, shows the placeholder with **no cert warning**.

Commit: `phase B.1 section 1: scaffold capture app (vite + react, https dev server)`

---

## Section 2 — Camera preview

A `useCamera` hook (or inline in the capture screen) that calls
`navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal:
'environment' }, width: { ideal: 1920 }, height: { ideal: 1080 } }, audio:
false })`, attaches the stream to a full-viewport `<video autoplay playsinline
muted>` (`playsinline` is **required** so iOS doesn't force fullscreen), and
**stops all tracks on unmount** (`stream.getTracks().forEach(t => t.stop())`).

Handle the failure modes explicitly and show a human message:
- `NotAllowedError` → permission denied: "Camera access was blocked. Allow it in
  your browser settings and reload."
- `NotFoundError` / `NotReadableError` → no/again-busy camera.
- `getUserMedia` undefined → **not a secure context** (the http-on-LAN trap):
  "Camera needs a secure (https) connection — see the run steps." This message
  is the early-warning that decision 3 wasn't satisfied.

**Manual check:** on the device, the live rear-camera feed fills the screen;
denying permission shows the block message; revisiting over plain `http`
(if you force it) shows the secure-context message.

Commit: `phase B.1 section 2: live camera preview (getUserMedia)`

---

## Section 3 — Framing overlay

A fixed, centered **vertical oval** drawn over the live video (SVG or a CSS
border-radius element with a dimmed surround, e.g. a full-screen overlay with an
oval "hole" via SVG mask or `box-shadow: 0 0 0 9999px rgba(0,0,0,.5)`). Sizing:
oval width ≈ **60–70% of the shorter viewport dimension**, height ≈ 1.3× its
width, vertically centered. Add short helper text ("Fill the oval with the
player's face"). The overlay is **non-interactive** (`pointer-events: none`) and
**purely visual guidance** — it does not crop or gate anything.

**Manual check:** the oval renders crisply centered over the live feed on both
portrait phone and landscape tablet; rotating the device keeps it centered and
proportional; it never blocks taps on the capture button.

Commit: `phase B.1 section 3: fixed framing oval overlay`

---

## Section 4 — Capture a still

A "Capture" button that draws the current video frame to an offscreen
`<canvas>` sized to the video's `videoWidth`/`videoHeight`, then
`canvas.toBlob(blob => ..., 'image/jpeg', 0.92)`. Show the captured frame as a
**review state** with **"Retake"** (discard, return to live preview) and
**"Use photo"** (proceed to upload, Section 5). Keep the `Blob` in state for the
upload; revoke any object URLs you create for the preview on retake/unmount.

**Manual check:** tapping Capture freezes the exact framed shot; Retake returns
to a live feed; the review image matches what was on screen (no mirroring
surprises — rear camera shouldn't mirror; if you add a front-camera flip later,
that's when mirroring matters).

Commit: `phase B.1 section 4: capture still to JPEG with retake/confirm`

---

## Section 5 — Upload to the reference endpoint

On "Use photo", `POST` the blob to
`/api/players/${import.meta.env.VITE_REF_PLAYER_ID}/references` as multipart:
```js
const fd = new FormData();
fd.append('file', blob, 'capture.jpg');
const res = await fetch(`/api/players/${PLAYER_ID}/references`, { method: 'POST', body: fd });
```
States to render: **uploading** (spinner/disabled), **success** (show the
returned `det_score` / `face_area_ratio` as a "Saved ✓" confirmation, then offer
"Capture another"), and **failure**. For failure, read the body the
`frontend/` way (see Context) and map by `detail.error`:
- `no_face` / `low_confidence` / `face_too_small` / `multiple_faces` → show
  `detail.message` verbatim with a **Retake** button (back to live preview).
- `404` → a setup-error banner: "Test player not configured — set
  `VITE_REF_PLAYER_ID` to a real player id (see README)." This is a
  developer-facing message, distinct from the volunteer-facing quality
  messages.
- Network error → "Upload failed — check the connection and try again."

**Manual check (the headline acceptance):** capture a good close-up of a face →
**200**, "Saved ✓" with a `det_score`; confirm a new file landed under
`backend/.../data/references/{player_id}/` (and/or `GET
/api/players/{player_id}/references` lists it). Then deliberately trip each
rejection: a too-far/blurry shot → `low_confidence`; two people in frame →
`multiple_faces`; a photo of a wall → `no_face` — each shows its message and a
working Retake.

Commit: `phase B.1 section 5: multipart upload to reference endpoint + result/error UI`

---

## Section 6 — Assemble the end-to-end screen

Wire Sections 2–5 into one screen with a clear state machine:
`live → (capture) → review → (use photo) → uploading → success|error → live`.
Make sure the camera stream is acquired once and reused across capture/retake
(don't re-request `getUserMedia` per shot), and is torn down on unmount. Add a
minimal top bar showing the target player id (so a tester knows what they're
uploading as) and the connection target. No routing is required if it's a
single screen.

**Manual check:** a volunteer can, on a real phone, go from cold-load →
allow camera → frame in the oval → capture → use → "Saved ✓" → "Capture
another" without a reload, and the rejection paths loop back cleanly to live
preview.

Commit: `phase B.1 section 6: end-to-end capture→upload flow`

---

## Conventions (match the existing code)

- New `capture/` folder; **no edits to `backend/` or `frontend/`.**
- Vite + React 18, JSX (no TS), function components + hooks — mirror
  `frontend/`.
- `fetch` with mandatory `res.ok` checks; read error bodies via
  `body?.detail?.message || body?.detail?.error` (the `RosterModal.jsx` idiom).
- Single `src/styles.css`, dark, mobile-first, full-viewport; CSS variables for
  colors as in `frontend/`.
- The target player id comes from `import.meta.env.VITE_REF_PLAYER_ID` — no
  magic literal in source.
- Single-line commit messages per section (`phase B.1 section N: ...`).
- Same-origin `/api` proxy in dev so no CORS and no backend change.

## Acceptance

1. `capture/` is a standalone Vite + React app that builds and runs on **8022**,
   served over a **secure context** (per decision 3), reachable on a
   phone/tablet on the LAN.
2. On a real device, the app opens the **rear camera** to a full-screen live
   preview and renders the **fixed framing oval**; camera-permission and
   secure-context failures show clear messages.
3. Capturing produces a JPEG of the framed shot with a working **Retake**.
4. "Use photo" uploads to `POST /api/players/{VITE_REF_PLAYER_ID}/references`
   (multipart field `file`); a good close-up returns **200** and the app shows
   "Saved ✓" with the returned `det_score`, and the file is present under the
   backend's `data/references/{player_id}/`.
5. Each A.2 quality rejection (`no_face`, `low_confidence`, `face_too_small`,
   `multiple_faces`) and the `404` "player not found" case are surfaced with
   their message and loop back to a usable state — **no raw error objects in the
   UI.**
6. The backend is **byte-for-byte unchanged**; `frontend/` is untouched; all
   existing backend tests still pass (B.1 adds none and removes none).

## Out of scope (defer — do NOT build in B.1)

- **Roster loading and tap-a-player-name.** B.1 uploads as one hardcoded
  `VITE_REF_PLAYER_ID`. (Wiring `GET /api/players/roster/{job_id}` and a name
  picker is a later B-phase.)
- **Auth / shared volunteer login.** No login screen, no tokens. (Later phase.)
- **Installable-PWA behavior:** no `manifest.webmanifest`, no service worker, no
  offline/queue-and-retry, no "Add to Home Screen" polish. B.1 is a *page*, not
  yet an installed app. (The folder is named for the eventual PWA, but the PWA
  shell comes later.)
- **Client-side face detection or any quality pre-check.** The architecture is
  deliberately "no GPU/detector at capture" — the oval ensures area, and the
  server's A.2 gate is the only quality authority. Don't add a client detector.
- **Cropping to the oval, image resizing, or re-encoding beyond the single JPEG
  toBlob.** Upload the full frame.
- **Choosing/seeding the player from the app, or creating Players.** Seeding one
  test Player is a manual setup step (decision 2), not a feature.
- **`?job_id=` provenance**, multi-photo batching, gallery/upload from the photo
  library, torch/zoom/exposure controls, and a front/rear flip control (rear is
  the default; a flip toggle is optional polish, not required).
- **Production HTTPS/hosting on the R640** (real cert, domain, the CORS-regex
  `https?` change). B.1 is dev-device testing only; productionizing the host is
  a later phase.
- **Backend changes of any kind**, including any deferred-detection / queueing
  rework. (Forward note: A.2's upload runs InsightFace **synchronously** to
  compute the embedding and quality gate. Whether the internet-reachable R640
  should run that detection inline or hand it to the GPU workstation is a real
  architecture question — but it is **not** a B.1 concern; B.1 just points at a
  running backend.)
- **Automated tests / adding a JS test runner.** B.1's verification is the
  on-device manual checks above, consistent with `frontend/` having no test
  suite.
