# Phase B.3 — Hand-off (Offline capture: install, queue, sync)

> **Status: decisions LOCKED + design APPROVED 2026-05-24; building
> section-by-section.** Read this in full before starting. This is the **third
> step** of the **B-series** mobile capture app and the first that touches the
> *shape* of how a capture reaches the backend.

**B.1** (`6b75794`) proved the on-device camera + a real multipart upload.
**B.2** (`main`) turned it into a check-in tool: pick a shoot → filter the
roster → tap a player → capture → upload via the **shoot-scoped replace**
endpoint, with a live shoot-specific ✓. Both assume a working connection at
capture time.

**B.3 makes the app work with no internet for an entire shoot.** Volunteers
load rosters at the office (online), take the device on-site where there is **no
connection at all**, capture reference photos **all day completely offline**,
and sync back at the office afterward. Having service during the shoot is a
*bonus*, not an assumption.

**The design rule that drives everything:** ONE mechanism — *queue every capture
locally, drain the queue whenever a connection is available.* Good service
drains it continuously (a capture looks near-instant); no service drains it when
the device is back at the office. **We do not build an "online mode" and an
"offline mode."** The capture screen stops uploading directly; it always
enqueues, and a single drainer owns every upload.

**Synced references must land exactly as a normal online upload would** — same
`PUT /api/players/{id}/references/shoot/{job_id}`, same `captured_job_id`, so the
desktop sorting/matching sees them the instant they sync. There is **no separate
office server**; the sync target is the existing `:8020` backend.

**Reuse, don't rewrite.** B.1's `useCamera` + the capture/review state machine
and B.2's view-switch, lifted roster state, and scoped endpoints all stay.
B.3 inserts a durable queue between "Use photo" and the network, adds an
offline-readable roster cache, and wraps the app in a service worker so it opens
with no connection.

**Backend footprint: ZERO.** The sync target is B.2's existing
`PUT/DELETE .../references/shoot/{job_id}` and the existing read endpoints
(`/api/jobs?stage=capture`, `/roster/{job_id}`, `/roster/{job_id}/reference-status`).
The scoped-replace `PUT` is *already* idempotent-by-replacement (see Context),
which is the whole reason no backend change is needed. **A.1/A.2 and every other
backend file stay byte-for-byte unchanged; `pytest -v` is untouched and stays
green.** The capture app stays test-free (B.1/B.2 precedent) — B.3's verification
is an **adversarial on-device checklist** (Section 7).

---

## ⚠️ Decisions — LOCKED 2026-05-24

### Decision 1 — Storage: IndexedDB, photos as bytes, conservative compression *(LOCKED)*

**Storage mechanism — IndexedDB.** `localStorage` is out (≈5 MB, strings,
synchronous). The Cache API is for the *shell* (the service worker, Decision 4),
not structured queue records. IndexedDB holds large binary payloads
asynchronously with a generous quota — the right home for ~500 queued photos +
the cached roster.

> **iOS gotcha — store bytes, not `Blob`.** Several iOS Safari versions mishandle
> `Blob` round-tripping through IndexedDB. Store the photo as an `ArrayBuffer` /
> `Uint8Array` and reconstruct `new Blob([bytes], { type: 'image/jpeg' })` at
> upload time. Costs nothing and dodges a platform-specific data-loss trap.

**Compression — conservative downscale + re-encode (chosen).** B.2 captures the
full video frame (≈1920×1080) at JPEG `q0.92` (`CaptureScreen.jsx:42`). B.3's
unified path runs **every** capture (online and offline) through one compression
step before it's queued: **downscale the long edge to ≤ 1600 px, re-encode at
`q0.85`.** Roughly halves storage (~250 KB/photo → ~125 MB for 500) and speeds
the eventual sync, while keeping the face far larger than the gate needs.

- **Why this is safe for the gate:** the framing oval makes the face ≈60% of the
  frame width, so even at a 1600 px long edge the face is ≈700+ px — far above
  InsightFace's internal 112 px alignment and the 2% area floor. `det_score` is
  driven by focus/pose/lighting, not by a mild downscale.
- **The risk we accept (and must verify):** `low_confidence` / `multiple_faces`
  are still server-decided (see Context), and offline that rejection surfaces
  only at sync time. So Section 7's checklist includes a **"compressed offline
  capture still clears the gate on sync"** comparison. If on-device testing shows
  borderline shots regressing, the fallback is "no compression" (full `q0.92`,
  ~300 MB/500) — a one-constant change.
- *Rejected — no compression:* zero gate risk but ~300 MB for 500 photos is tight
  on older/full iPhones and slow to drain. *Rejected — aggressive (≤1280 px,
  `q0.8`):* max headroom but the highest chance of sync-time `low_confidence`.

**Durability — request persistent storage.** Call `navigator.storage.persist()`
on first run (an installed PWA is far more likely to be granted it) and monitor
`navigator.storage.estimate()` for headroom (Decision 6 / Section 6).

### Decision 2 — One unified path: every capture enqueues; one drainer uploads *(LOCKED)*

The capture screen **never calls the network directly.** "Use photo" →
compress → write a durable queue record → return to the roster immediately. A
single **drainer** (owned by `App`) uploads from the queue whenever a connection
is available. This is the "one mechanism" rule made concrete:

- **Good service:** the drainer fires right after enqueue and empties the queue
  in ~1 s — the capture *looks* like B.2's instant upload.
- **No service:** the upload fails, the record stays `pending`, the queue grows;
  the drainer empties it later when connectivity returns.

A capture is therefore confirmed **the moment it's durably queued**, not when it
uploads. This is the change that makes "offline all day" the default instead of
a special case.

### Decision 3 — Two-state ✓: "pending sync" vs "synced" *(LOCKED)*

B.2's single ✓ ("has a photo for this shoot") splits into two visible states so
nothing feels lost:

- **Pending (amber)** — captured and durably queued, not yet on the server.
- **Synced (green)** — confirmed on the server (a `200` came back).

The roster derives a player's state on every render from **(cached
reference-status) ∪ (queue contents)** — both live in IndexedDB, so the ✓/queue
state **survives the app being closed, reopened, or the device restarted**
mid-shoot. A small header count ("**N photos waiting to sync**") is always
visible while the queue is non-empty.

### Decision 4 — Service worker via `vite-plugin-pwa` (Workbox); `/api` is network-only *(LOCKED)*

`vite-plugin-pwa` generates the precache manifest from the hashed build, wires
the manifest link, and handles SW versioning/update — far less error-prone than
hand-maintaining a hashed-asset list. **Shell = cache-first** (so the app opens
with no connection); **`/api/* = NetworkOnly`** (never serve stale API data; a
failed API call is exactly what tells the app it's offline and must queue).
`navigateFallback` serves the cached `index.html` for SPA navigations, with
`/api` on the denylist.

- **Test the BUILT app.** A dev-server SW over Vite's module graph is finicky;
  the realistic target is `npm run build && npm run preview` behind the tunnel.
  Section 1 configures `preview.proxy` + `allowedHosts` to mirror `server`, and
  the offline checklist (Section 7) runs against `preview`. `devOptions.enabled`
  is set for convenience but the acceptance run is the build.

### Decision 5 — A stable HTTPS hostname is part of B.3 *(LOCKED)*

A home-screen PWA install — and its IndexedDB queue and Cache storage — are
**bound to the origin (scheme+host+port).** A `cloudflared` *quick* tunnel hands
out a **new random hostname every launch**, so Monday's install (and any photos
queued under it) is orphaned the moment the tunnel restarts. The real workflow
("install at the office, go off-grid for the day, come back") **requires a fixed
origin.**

**LOCKED:** B.3 establishes a **stable HTTPS hostname** — a **named `cloudflared`
tunnel** at **`capture.sportslinephotography.com`**, routed to the **office
desktop** that runs the backend (`http://localhost:8022` → Vite, which proxies
`/api → :8020`). Pinned in `vite.config.js` `allowedHosts` (replacing the
dev-only `true`). The browser still hits the tunnel host and Vite still proxies
`/api` same-origin, so **there is still no CORS work and no backend change.** The
*requirement* is a hostname that doesn't change between runs — which the named
tunnel guarantees, unlike a quick tunnel's random host.

> **Deployment shape (dress rehearsal):** the backend runs on the **office
> desktop**; the capture devices are **older Android tablets**. Tablets cache
> rosters at the office (online via the named tunnel), go off-grid on-site, and
> sync back at the office. The named tunnel is stood up **collaboratively** when
> Section 1 is reached (Cloudflare DNS + the `cloudflared` config on the desktop).

> **This is NOT R640 productionizing.** It's the minimum fixed origin an install
> needs. Real cert/domain ON the R640, hosting, and the CORS `https?` change
> remain out of scope (see Out of scope).

### Decision 6 — Sync failures are kept and surfaced, never dropped *(LOCKED)*

At drain time the existing endpoint can still reject a queued capture **after the
volunteer has left that player.** B.3 is **lossless**: a failed item keeps its
photo and is surfaced, never silently discarded.

- **`400` quality (`low_confidence` / `multiple_faces` / `no_face`)** → mark
  `failed_quality`, **keep the bytes**, list it under a "**Needs attention**" view
  with the server's message. The player stays *un-synced* (needs photo).
- **`404` (player or shoot gone server-side)** → mark `failed_gone`, keep, list.
- **Network / 5xx** → stays `pending`, retried on the next drain.

**Storage-pressure** (the eviction edge with ~500 queued) is handled here too:
`estimate()` drives a usage indicator and a visible warning ("Storage 80% full —
sync soon") as the queue nears the ceiling, so the volunteer acts before the
browser evicts. Persistent storage (Decision 1) makes eviction unlikely; the
warning is the backstop.

Minor calls made for you (flagged, easy to flip):

- **`idb` (~1 KB promise wrapper) for IndexedDB**, not raw `IDBRequest`
  boilerplate. One tiny dep alongside `vite-plugin-pwa`; flip to hand-rolled if
  you'd rather zero deps.
- **Serial FIFO drain**, ordered by `capturedAt`. Serial keeps idempotency and
  progress simple and is gentle on flaky gym wifi; FIFO means a **retake** (a
  later capture of the same player) uploads *last* and wins — matching B.2's
  "latest replaces" semantics. Progress reads "syncing 47 of 312."
- **Remove offline = drop the pending queue item locally** (no server call). The
  common case (ditch a bad shot you just took) works fully offline. Removing a
  *already-synced* photo while offline is **deferred** — it's rare (nothing syncs
  until you have a connection, and you can remove it once back online) and a
  queued-delete op would add a second op-type to the queue for little gain. Noted
  in Out of scope.
- **Connectivity oracle = the real request, not `navigator.onLine`.** `onLine`
  only reports a network interface, not backend reachability. The `online` event
  is a fine *wakeup* to trigger a drain; success/failure of the actual `PUT` is
  the source of truth.
- **Roster membership change does NOT block sync.** The scoped-replace `PUT`
  requires the **Player row + Job** to exist, not current membership
  (`references.py:302-303`). A player dropped from the *roster* still syncs fine;
  only a deleted Player/Job yields `404` → `failed_gone`. The desktop matcher
  loads references by `player_id` regardless, so a synced ref is immediately
  usable.

---

## Context you need

### The sync target — already idempotent (why no backend change)

`PUT /api/players/{player_id}/references/shoot/{job_id}` →
`replace_shoot_reference` (`services/references.py:291`): **validate the new
photo → wipe ONLY this shoot's refs (`captured_job_id == job_id`) → store one.**
End state is always "exactly one reference for (player, shoot)."

That makes the drain model trivially correct:

> **At-least-once delivery + an idempotent endpoint = exactly-once effect.**
> Uploading the same queued capture twice (e.g. the app died after the server
> committed but before we recorded success) just replaces the ref with identical
> bytes. **No double-data, no dedup key needed.** The only cost of a re-send is a
> wasted upload, never a wrong result.

Success returns `200` with `{id, player_id, captured_job_id, det_score,
face_area_ratio, image_path}`. We only delete the queue item **after** reading a
`200` — so a crash mid-upload loses nothing; at worst it re-sends (which is safe).

### The split quality gate — the offline hazard to design around

The client oval guarantees only **face area** (clears `face_too_small`).
`no_face`, `multiple_faces`, and `low_confidence` are decided **server-side**
(`services/references.py:52-91`) and B.1/B.2 deliberately ship **no client
detector**. Online, the volunteer sees a `400` and retakes on the spot. **Offline,
that feedback loop is gone** — the rejection only appears at sync time, after the
volunteer has moved on. Hence Decision 6 (keep + surface, never drop) and the
"Needs attention" review list. This is the single biggest behavioral difference
from B.2 and the thing the checklist must hammer.

### What the app fetches today (all become cacheable reads)

- `GET /api/jobs?stage=capture` (`jobs.py:374`) — capture-ready shoots
  (rostered, no images imported). The picker's source (`ShootPicker.jsx:18`).
- `GET /api/players/roster/{job_id}` (`players.py:58`) — the roster `items`.
- `GET /api/players/roster/{job_id}/reference-status` (`players.py:85`) —
  shoot-scoped ✓ set. App merges roster + status into lifted state
  (`App.jsx:44-54`).

All three are GETs with no side effects → safe to snapshot into IndexedDB while
online and read back offline.

### B.1/B.2 building blocks to reuse (don't rewrite)

- **`useCamera.js`** — unchanged. Camera still mounts only on the capture screen.
- **`CaptureScreen.jsx`** capture/review state machine + the canvas capture. B.3
  changes only what happens on **"Use photo"**: compress → enqueue (instead of
  the direct `PUT`), and the "Remove" path for a *pending* item becomes a local
  queue delete.
- **`App.jsx`** view-switch + lifted roster/filter state (Decision 5 of B.2). B.3
  adds the queue + drainer state here (it's already the cross-screen owner).
- **`RosterScreen.jsx`** — the ✓ badge gains a second (amber/pending) state.
- **`vite.config.js`** — same-origin `/api → :8020` proxy stays; B.3 adds the
  `vite-plugin-pwa` plugin, the pinned `allowedHosts` hostname, and a matching
  `preview` block.

---

## Don't break

- **No backend changes.** B.3 is client + infra only. Do **not** edit any
  `backend/` file or the CORS regex; `pytest -v` stays green and untouched.
- **Keep the unified path honest.** After Section 4 there is **no** direct-upload
  code path left in `CaptureScreen`. Every capture goes through the queue. Don't
  reintroduce an "if online, upload directly" branch — that's the two-modes
  anti-goal.
- **`/api` is never cached.** The SW must treat `/api/*` as NetworkOnly. Caching
  an API response would show stale rosters/✓ and corrupt the offline model.
- **Reuse the camera + capture machine.** No re-implementing `getUserMedia`, the
  oval, or capture-to-JPEG.
- **Don't touch `frontend/`.** The editor app is independent.
- **Losslessness is a hard invariant.** No queue item is ever deleted except (a)
  after a confirmed `200`, or (b) by an explicit user "remove." A failed gate or
  a full disk must never drop a photo.

---

## Section 1 — Stable origin + installable offline shell

Stand up the fixed HTTPS hostname (Decision 5) and make the app an installable
PWA whose shell opens with no connection (Decision 4).

- **Stable hostname:** configure a **named `cloudflared` tunnel** (or reserved
  ngrok domain) routing the fixed hostname → `http://localhost:8022`. Pin that
  host in `vite.config.js` `allowedHosts` (replace the dev-only `true`), and add
  a `preview` block mirroring `server` (`port: 8022`, `strictPort`, the
  `/api → :8020` proxy, the pinned `allowedHosts`).
- **Manifest:** `capture/public/manifest.webmanifest` — `name`/`short_name`
  ("Reference Capture"), `start_url: "/"`, `scope: "/"`, `display: "standalone"`,
  `theme_color: "#0b0d12"`, `background_color: "#0b0d12"`, and **192 + 512 px
  icons (incl. a `maskable` one)** under `public/`. Link it in `index.html`.
- **Service worker** via `vite-plugin-pwa`: `registerType: 'autoUpdate'`, Workbox
  precaches the built shell, `navigateFallback: '/index.html'` with
  `navigateFallbackDenylist: [/^\/api\//]`, and a `runtimeCaching` rule making
  `/api/*` **NetworkOnly**. Register the SW in `main.jsx`. Enable `devOptions`
  for convenience but treat the build as the real target.

**Manual check:** `npm run build && npm run preview`, open the **stable** URL on
the phone, **Add to Home Screen**. Put the phone in **airplane mode** and launch
from the home-screen icon → the app shell loads (the shoot picker renders, even
if it can't reach the API yet). Restart the tunnel / reopen the next day → the
**same** origin, the install still works, no re-install.

Commit: `phase B.3 section 1: installable PWA shell + stable hostname (manifest + service worker)`

---

## Section 2 — IndexedDB foundation (queue + roster cache + storage helpers)

A small `capture/src/db.js` (using `idb`) opening one database with object
stores:

- **`queue`** — keyed by a client `id` (uuid). Record: `{ id, playerId, jobId,
  playerName, team, bytes (ArrayBuffer), mime, capturedAt, status
  ('pending'|'uploading'|'synced'|'failed_quality'|'failed_gone'), attempts,
  lastError }`. (`synced` rows are deleted, not kept; the status enum documents
  the lifecycle.)
- **`rosters`** — keyed by `jobId`. Record: `{ jobId, name, items, statusIds
  (synced ✓ from the server), cachedAt }`.
- **`shoots`** — the cached `?stage=capture` list for the offline picker:
  `{ id, name }[]` + `cachedAt`.

Plus helpers: `requestPersistentStorage()` (`navigator.storage.persist()`, called
once on first run) and `storageEstimate()` (wraps `navigator.storage.estimate()`
→ `{ usage, quota, ratio }`). No UI yet — pure data layer with small typed
accessors (`enqueue`, `listQueue`, `setStatus`, `removeItem`, `putRoster`,
`getRoster`, …).

**Manual check:** in DevTools → Application → IndexedDB the three stores exist;
`navigator.storage.persist()` resolves `true` (granted) on the installed app; a
record written via a temporary console call **survives a reload** and a full app
close/reopen.

Commit: `phase B.3 section 2: IndexedDB layer (queue + roster cache) + storage-persistence helpers`

---

## Section 3 — Roster offline caching + picker readiness

Make a shoot fully readable offline and let the volunteer *see* it's ready before
leaving (Decision: auto-cache + readiness badge).

- **Auto-cache on select (online):** when `App` fetches roster + reference-status
  (`App.jsx:44`), also `putRoster(jobId, …)` into IndexedDB with `cachedAt`. The
  `?stage=capture` list is cached on each successful picker load (`putShoots`).
- **Picker readiness:** `ShootPicker` reads the `rosters` store and shows a
  per-shoot **"Ready offline ✓"** badge for cached shoots, plus a **"Download for
  offline"** action to pre-stage a shoot (or several) without entering it. Offline,
  the picker lists the **cached** shoots (from the `shoots` store) so the volunteer
  can still choose one.
- **Offline read path:** when a roster fetch fails (no connection), `App` falls
  back to `getRoster(jobId)` and renders from cache. The roster screen shows a
  subtle "**Offline — cached {Xh} ago**" line.
- **Staleness:** online, a re-open re-fetches and overwrites the cache (server
  wins); a "Refresh roster" affordance forces it. Targeting is by **stable
  `player_id` + `job_id`**, so a slightly stale roster never mis-targets a queued
  capture.

**Manual check:** online, open a shoot (auto-caches) and/or tap "Download for
offline" → it shows **Ready offline ✓**. Airplane-mode, fully close the app,
relaunch from the home screen → the picker lists the cached shoot, opening it
shows the **full roster from cache** with the "cached Xh ago" line; an
*un-cached* shoot is clearly not available offline.

Commit: `phase B.3 section 3: offline roster cache + "ready offline" picker badge`

---

## Section 4 — Unified capture: compress → enqueue → two-state ✓

Replace `CaptureScreen`'s direct `PUT` with the queue, and split the ✓ badge.

- **On "Use / Replace photo":** draw to canvas (as today) → **downscale long edge
  to ≤ 1600 px + `toBlob('image/jpeg', 0.85)`** → read to an `ArrayBuffer` →
  `enqueue({ playerId, jobId, playerName, team, bytes, capturedAt: Date.now(),
  status: 'pending', attempts: 0 })` → return to the roster **immediately**. No
  network here. Removing the direct-upload code is the point — verify nothing
  else calls the endpoint.
- **Two-state ✓ (Decision 3):** `App` derives, per player, `synced` (in the cached
  status set) vs `pending` (a queue item exists for `(playerId, jobId)`).
  `RosterScreen` renders amber "pending" vs green "synced" badges; "needs photo
  only" hides **both** (a pending capture is no longer "needs photo"). Header
  shows "**N photos waiting to sync**" when the queue is non-empty.
- **Remove (pending, offline-safe):** for a player whose only state is a *pending*
  queue item, "Remove photo" deletes the **queue item locally** (no server) and
  clears the badge. (Removing an already-*synced* photo keeps B.2's
  connection-required `DELETE` — deferred offline, see Out of scope.)

**Manual check:** airplane-mode, capture 5 players → each returns to the roster
**instantly** with an **amber pending** badge; the header reads "5 waiting to
sync." Fully close and reopen the app → all 5 still pending (state rebuilt from
IndexedDB). Remove one pending capture → its badge clears and the count drops to
4, with no network.

Commit: `phase B.3 section 4: queue captures locally (compress + enqueue) + pending/synced badges`

---

## Section 5 — The drainer: sync whenever connected

A `capture/src/syncQueue.js` + a thin `useSyncQueue` hook owned by `App`. The
heart of B.3.

- **Drain loop (serial FIFO by `capturedAt`):** for each `pending` item — set
  `uploading` → reconstruct the `Blob` from bytes → `PUT
  /api/players/{playerId}/references/shoot/{jobId}` (multipart `file`) → on `200`,
  **delete the item** and add `playerId` to the synced set (live green ✓); on
  failure, route per Decision 6. Process one at a time.
- **Interrupted-resume (idempotency in practice):** on drain start, reset any
  item left in `uploading` (from a previous crash/kill) back to `pending` — we
  can't know if it landed, and re-`PUT` is **safe** because the endpoint replaces
  (Context). At-least-once + idempotent = exactly-once.
- **Triggers:** app mount (after shell + queue load), the `window` `online` event,
  immediately after each enqueue, a manual **"Sync now"** button, and a periodic
  retry while the queue is non-empty (backing off on repeated network failure).
- **UI:** a sync banner with **progress** ("syncing 47 of 312…"), a **completion**
  confirmation ("All N photos synced ✓"), and the persistent waiting-count. ✓
  badges flip amber→green live as each item lands.

**Manual check:** queue ~10 captures in airplane mode; turn airplane mode **off**
→ the drainer auto-starts, progress counts up, badges flip amber→green, and a
completion confirmation shows. Mid-drain, toggle airplane mode **on** then off →
sync **resumes** with no duplicates: `GET /api/players/{id}/references` shows
**exactly one** ref per player for this shoot. Kill the app mid-drain and
relaunch → the in-flight item re-sends and the drain completes; still exactly one
ref each.

Commit: `phase B.3 section 5: drain queue when connected (serial FIFO, idempotent resume, progress)`

---

## Section 6 — Sync failures kept + surfaced; storage-pressure warning

Make the lossless guarantees visible (Decision 6).

- **"Needs attention" view:** items in `failed_quality` / `failed_gone` render in
  a small list (reachable from the roster header when non-zero), each showing the
  player, the **server's message** (e.g. "Face detection confidence is too low…"),
  and actions — **Re-shoot** (jump to that player's capture screen, replacing the
  failed item) and **Discard** (explicit delete; the only non-`200` deletion).
  `failed_gone` items offer Discard only. Nothing here is auto-removed.
- **Storage pressure:** wire `storageEstimate()` to a header indicator and raise a
  warning banner ("Storage {pct}% full — sync when you can") past a threshold
  (e.g. 80%), so the volunteer acts before the browser evicts. Persistent storage
  makes eviction unlikely; this is the backstop for the ~500-photo edge.

**Manual check:** offline, deliberately queue a **bad** shot (a wall → `no_face`,
or two people → `multiple_faces`); on reconnect it lands in **Needs attention**
with the gate message — **not** silently dropped — and that player stays
un-synced. Re-shoot replaces it; a good shot then syncs green. Force the storage
indicator (lower the threshold temporarily) to confirm the warning fires.

Commit: `phase B.3 section 6: surface sync failures (needs-attention list) + storage-pressure warning`

---

## Section 7 — README + adversarial on-device offline checklist

Update **`capture/README.md`**: the **named-tunnel / stable-hostname** setup
(`capture.sportslinephotography.com` → office desktop), the **Add to Home Screen**
install steps, the **`build` + `preview`** flow for SW/offline testing (and why
dev-mode SW isn't the target), and the **offline model** (queue → drain,
two-state ✓, Needs attention). Fold in an **offline test checklist** — offline is
adversarial, so this is the real acceptance.

> **Hardware-specific checks.** The capture devices are **older Android tablets**,
> and that's where compression-vs-`det_score` (item 7) and storage quota/eviction
> (item 10) actually get stressed — a modern phone won't surface either. The
> README must state that **items 7 and 10 must be run on the target tablets**, not
> a dev phone, before the phase is accepted.

1. **Cold offline open:** install online → airplane mode → relaunch from icon →
   shell opens, cached shoot's roster reads fully.
2. **Airplane-mode mid-capture:** capture several offline → all queue as pending,
   instantly, count correct.
3. **Close/reopen with queue pending:** kill the app → relaunch → pending state +
   count intact (rebuilt from IndexedDB).
4. **Device restart with queue pending:** reboot the phone → relaunch → queue
   survives.
5. **Sync on reconnect:** disable airplane mode → auto-drain with progress →
   green ✓ + completion.
6. **Interrupted sync resumes, no dupes:** toggle connectivity / kill-relaunch
   mid-drain → completes; exactly one ref per player (`GET …/references`).
7. **Compressed capture clears the gate** *(run on the target tablet):* an
   offline capture taken on an older Android tablet syncs `200` with a healthy
   `det_score` (validates Decision 1's compression on real hardware).
8. **Gate reject is kept:** an offline bad shot → Needs attention with the
   message, not dropped.
9. **Player removed server-side:** delete a queued player's roster row → its ref
   still syncs (membership isn't required); delete the **Player/Job** → the item
   goes `failed_gone`, kept.
10. **~500-photo soak** *(run on the target tablet):* queue ~500 compressed
    captures offline on an older Android tablet → storage stays under quota, the
    pressure warning fires near the ceiling, then a full drain completes with **no
    loss** (server count == captured count). This is where eviction risk is real.

**Manual check:** a teammate reproduces the full lifecycle from the README on a
fresh phone — install → cache a shoot → go offline → capture a batch →
close/reopen → reconnect → all synced, failures triaged.

Commit: `phase B.3 section 7: README + adversarial offline checklist`

---

## Hard edge cases (where offline breaks) — and how B.3 handles each

| Edge case | Handling |
| --- | --- |
| App closed/reopened with photos queued | Queue lives in IndexedDB; ✓/pending state is rebuilt from (cached status ∪ queue) on load (§2–4). |
| Device restarted mid-shoot | Same — IndexedDB is durable across reboots; persistent storage requested (§2). |
| Sync interrupted halfway | `uploading` reset to `pending` on next drain; serial FIFO resumes (§5). |
| Same photo uploaded twice | Endpoint is replace-this-shoot — idempotent; item deleted only after `200` (Context, §5). |
| Browser evicts storage under pressure (~500) | Persistent storage + conservative compression keep usage low; `estimate()` warning fires before the ceiling (§1, §6). |
| Queued capture for a player later removed from roster | Membership isn't required by the endpoint — it still syncs; only a deleted Player/Job → `failed_gone`, kept + surfaced (Decision 6). |
| Queued capture fails the server gate at sync | Kept as `failed_quality` in Needs attention with the message; Re-shoot or Discard (§6). |
| Install orphaned by a changed tunnel host | Stable hostname (Decision 5 / §1) keeps the origin fixed across runs. |

## Conventions (match the existing code)

- **No backend changes**; `pytest -v` untouched & green. A.1/A.2 frozen.
- Capture app: Vite + React 18, JSX (no TS), function components + hooks; reuse
  B.1's `useCamera` + capture machine; one `src/styles.css` with the existing
  dark tokens; mobile-first. New deps limited to `vite-plugin-pwa` + `idb`.
- `fetch` with mandatory `res.ok` checks; read error bodies via
  `body?.detail?.message || body?.detail?.error`. No error object lands in state.
- Same-origin `/api` proxy (now in both `server` and `preview`); no CORS / no
  `main.py` change.
- Single-line commit messages per section (`phase B.3 section N: …`).
- Verification is the **on-device checklist** (§7), per B.1/B.2 — no JS test runner.

## Acceptance

1. The app is **installable** (Add to Home Screen) from a **stable HTTPS origin**,
   and its **shell opens with no connection** from the home-screen icon.
2. A shoot's **roster + ✓ status cache while online** and are **fully readable
   offline**; the picker shows **"Ready offline ✓"** and lets the volunteer
   pre-stage shoots before going off-grid.
3. **Every** capture (online and offline) **enqueues durably** (compressed, as
   bytes in IndexedDB) and returns instantly — **one path, no online/offline
   branch.**
4. The roster shows **pending (amber)** vs **synced (green)** distinctly, with a
   live "**N waiting to sync**" count; this state **survives close/reopen and
   device restart.**
5. The drainer **auto-syncs whenever connected** (and on a manual "Sync now"),
   with **progress + completion**, draining continuously on good service and at
   the office otherwise.
6. Sync is **idempotent and lossless**: an interrupted/repeated drain yields
   **exactly one** reference per (player, shoot); no item is dropped.
7. **Sync failures are surfaced, not lost** — quality rejects and gone-target
   items land in **Needs attention** with the server message and Re-shoot/Discard.
8. **~500 queued photos** are held within quota with a **storage-pressure
   warning**, and drain with **no loss** (server count == captured count).
9. Synced references are **indistinguishable from a normal online upload** (same
   endpoint, `captured_job_id` set) and immediately usable by desktop matching.
10. **Backend byte-for-byte unchanged**; `frontend/` untouched; `pytest -v` green;
    `capture/README.md` documents the install + offline model + checklist.

## Out of scope (defer — do NOT build in B.3)

- **Auth / volunteer login.** (Later phase.)
- **Productionizing on the R640** — real cert/domain **on** the R640, hosting, the
  CORS `https?` change. B.3's stable hostname is a fixed *tunnel* origin only
  (Decision 5), not R640 deployment.
- **Removing an already-*synced* photo while offline** *(deferral accepted in
  review).* The common offline remove (drop a *pending* capture) is supported; a
  queued-delete op for synced photos is deferred (rare — sync implies
  connectivity — and not worth a second queue op-type).
- **Client-side face detection / quality pre-check.** Unchanged from B.1/B.2 — the
  server gate is the only quality authority; offline rejections are surfaced
  (Decision 6), not pre-empted on the device.
- **Multi-photo / multi-angle capture per (player, shoot).** Still one reference
  per (player, shoot) via scoped-replace; the queue holds at most one *latest*
  pending capture per player (a retake replaces the pending item).
- **Background Sync API / push-triggered sync.** B.3 drains on foreground triggers
  (mount, `online`, enqueue, "Sync now", periodic). Service-worker Background Sync
  is a future nicety, not required for the office-return workflow.
- **Caching `/api` responses in the service worker.** `/api` stays NetworkOnly by
  design; the IndexedDB roster cache is the only offline data store.
- **Automated tests / a JS test runner.** On-device checklist remains the
  verification, consistent with B.1/B.2.
```
