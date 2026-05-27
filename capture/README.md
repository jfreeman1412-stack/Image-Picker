# Capture (Phase B.3)

Mobile reference-photo **check-in tool** for the Player Sort system, now an
**installable, offline-capable PWA**. A volunteer installs it to the tablet's
home screen at the office (online), loads/caches the shoots they'll work, takes
the device on-site **with no connection**, captures reference photos all day, and
**syncs back at the office**. Having service during the shoot is a bonus, not a
requirement. See `../PHASE_B3_OFFLINE_CAPTURE.md`.

Built on B.1 (camera → framing oval → capture-to-JPEG) and B.2 (shoot + player
selection, filterable roster, shoot-scoped replace/delete). **B.3 inserts a
durable local queue between "Use photo" and the network:** every capture is
compressed and banked to IndexedDB, and a single drainer uploads it to the same
A.2/B.2 endpoint whenever a connection is available.

## The offline model (what's new in B.3)

- **One mechanism, no modes.** Capturing always enqueues locally and returns
  instantly; a background drainer uploads when connected. Good service drains in
  ~1s (feels like B.2); no service drains at the office.
- **Two-state ✓.** A player shows **synced (green ✓)** once their photo is on the
  server, **pending (amber ↑)** while a capture is queued, and **failed (red !)**
  if the server rejected it on sync. A synced player with a queued retake shows
  both (✓ ↑). The header shows **"N photos waiting to sync."** State is rebuilt
  from IndexedDB, so it survives close/reopen and device restart.
- **Sync is idempotent + lossless.** Uploads go to the shoot-scoped *replace*
  endpoint (last-write-wins), an item is deleted only after a confirmed 200, and
  an interrupted drain resumes without double-uploads or dropped photos.
- **Failures are surfaced, never dropped.** A quality rejection or a
  removed-on-the-server target lands in **Needs attention** with the server's
  message, where it can be **Re-shot** or **Discarded**.
- **Roster works offline.** A shoot's roster + ✓ status are cached when opened
  online (or pre-staged with **Download for offline**); offline the picker shows
  **"Ready offline ✓"** and the roster reads from cache.
- **Walk-up players (B.5).** A kid not on the roster can be added on the spot with
  **+ Add player** (name + a team picked from the roster or typed new), fully
  offline; they capture like anyone else and are created on the server + attached
  at sync.

## Walk-up players (B.5 — not-on-roster adds)

At a shoot a kid sometimes isn't on the roster. Tap **+ Add player** on the roster
screen, enter a name and a team (an existing team or **+ New team…**), and capture
as usual — **no connection required.**

- The walk-up is stored locally with a client id and merged into the roster; the
  capture queues against it like any other.
- At sync the drainer first **creates the real player** on the server (the additive
  `POST /api/players/roster/<job>/walkup`, idempotent by normalized name), then
  uploads the reference to that player — so a walk-up ends up **indistinguishable
  from a CSV-roster player** and flows into matching.
- **Multi-tablet safe.** Each tablet's walk-up has its own local id and the server
  dedups by name, so the same kid added on two tablets converges to **one** player
  (last capture wins), never a duplicate. A walk-up added on one tablet isn't
  visible on the others until everyone syncs.
- A failed walk-up (e.g. the shoot was deleted server-side) lands in **Needs
  attention**; **Discard** there also removes the local walk-up.

## Run it (development, on the laptop)

1. **Backend** (separate terminal, from `../backend`):
   ```
   uvicorn app.main:app --reload --port 8020
   ```

2. **Capture app — build + preview** (from this folder). The service worker and
   offline behavior must be tested against the **production build**, not the dev
   server (a dev-mode SW over Vite's module graph is unreliable):
   ```
   npm install
   npm run build
   npm run preview        # serves the built app on http://localhost:8022
   ```
   `npm run dev` is fine for quick UI work, but run the **offline / install /
   sync checks against `preview`**. Both `server` and `preview` proxy `/api` to
   `:8020`, so uploads stay same-origin (no CORS, no backend change).

3. **HTTPS** — the camera *and* the service worker both require a secure context,
   so the tablet must load the app over `https`. In development a tunnel
   terminates TLS (see below).

## Stable hostname (B.3 — required for install)

A home-screen install, and its IndexedDB queue, are bound to the **origin**
(scheme + host + port). A `cloudflared` *quick* tunnel hands out a **new random
host every launch**, which would orphan the install and its queued photos. So
B.3 uses a **named tunnel at a fixed hostname**.

For the dress rehearsal the backend + this app run on the **office desktop**, and
the named tunnel publishes **`https://capture.sportslinephotography.com`** → the
desktop's `:8022`. On the desktop:

```
# one-time, interactive (opens a browser; pick the sportslinephotography.com zone)
cloudflared tunnel login
cloudflared tunnel create capture
cloudflared tunnel route dns capture capture.sportslinephotography.com
```
`~/.cloudflared/config.yml` (Windows: `C:\Users\<user>\.cloudflared\config.yml`):
```yaml
tunnel: capture
credentials-file: C:\Users\<user>\.cloudflared\<UUID>.json
ingress:
  - hostname: capture.sportslinephotography.com
    service: http://localhost:8022
  - service: http_status:404
```
```
cloudflared tunnel run capture
```
The hostname is pinned in `vite.config.js` `allowedHosts`. (On the dev laptop the
tunnel can point at the laptop's `:8022`; re-point it to the desktop at rehearsal.)

## Install + use it on the tablet

1. Open `https://capture.sportslinephotography.com` and **Add to Home Screen**.
   Launch from the icon (standalone).
2. **At the office (online):** pick a shoot, or tap **Download for offline** on
   each shoot you'll work so it shows **Ready offline ✓** before you leave.
3. **On-site (offline):** open a cached shoot, filter to the player, tap → frame
   in the oval → **Use photo**. The player turns **amber ↑** and the header
   counts up. Retake replaces the queued capture; **Remove photo** drops a pending
   capture locally.
4. **Back at the office (online):** the queue **drains automatically** (or tap
   **Sync now**) — progress shows "Syncing X of N…", badges flip **amber → green**,
   and it confirms "Synced N ✓." Anything the server rejected appears under
   **Needs attention**.

## Seed a test shoot (multi-team)

The roster needs **no images** — any rostered, not-yet-imported job appears in the
picker. With the backend running:

1. **Create a shoot (Job)** in the editor app's **+ New shoot**, or:
   ```
   curl.exe -s -X POST http://localhost:8020/api/jobs -H "Content-Type: application/json" -d "{\"name\":\"Test Shoot\",\"root_path\":\"C:/path/to/any/folder\",\"has_lines\":false,\"auto_run\":false}"
   ```
2. **Upload a 3-team roster** — save as `test-roster.csv`:
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
   ```
   curl.exe -F "file=@test-roster.csv;type=text/csv" http://localhost:8020/api/players/roster/<JOB_ID>
   ```

**Reset a player's capture for a re-test:** in-app **Remove photo**, or
`DELETE /api/players/<PLAYER_ID>/references/shoot/<JOB_ID>`. To clear the local
queue/cache on a device: the browser's site-data clear (or DevTools → Application
→ Clear storage).

## Offline test checklist (the real acceptance)

Offline is adversarial — run these on the device, not just the desktop. **Items 7
and 10 MUST be run on the target older Android tablets**, where
compression-vs-`det_score` and storage quota/eviction actually get stressed; a
modern phone won't surface either.

1. **Cold offline open** — install online → airplane mode → relaunch from the
   home-screen icon → the shell opens and a cached shoot's roster reads fully.
2. **Airplane-mode mid-capture** — capture several offline → each queues as
   pending instantly, the count is right.
3. **Close/reopen with queue pending** — kill the app → relaunch → re-select the
   shoot → pending badges + count intact (rebuilt from IndexedDB).
4. **Device restart with queue pending** — reboot the tablet → relaunch → queue
   survives.
5. **Sync on reconnect** — disable airplane mode → auto-drain with progress →
   green ✓ + "Synced N ✓".
6. **Interrupted sync resumes, no dupes** — toggle connectivity / kill-relaunch
   mid-drain → completes; exactly one ref per player
   (`GET /api/players/<id>/references`).
7. **Compressed capture clears the gate** *(target tablet)* — an offline capture
   on an older Android tablet syncs `200` with a healthy `det_score`.
8. **Gate reject is kept** — an offline bad shot (a wall → `no_face`) → **Needs
   attention** with the server message, not dropped; Re-shoot / Discard work.
9. **Player removed server-side** — deleting a queued player's roster *membership*
   still lets the ref sync (membership isn't required); deleting the **Player/Job**
   → the item goes `failed_gone`, kept.
10. **~500-photo soak** *(target tablet)* — queue ~500 compressed captures offline
    → storage stays under quota, the pressure warning fires near the ceiling,
    then a full drain completes with **no loss** (server count == captured count).
11. **Walk-up add offline** — **+ Add player** (one with an existing team, one with
    a typed-new team) → both appear in the roster and capture → pending; survive
    close/reopen + device restart.
12. **Walk-up syncs on reconnect** — reconnect → each walk-up is created on the
    server then its reference uploads → green ✓; `GET /api/players/roster/<job>`
    now lists them and `GET /api/players/<id>/references` shows exactly one ref.
13. **Three-tablet concurrent sync** *(target tablets)* — all three tablets capture
    disjoint roster players + a couple of walk-ups offline, then return and let them
    drain **at the same time** → every reference lands, server counts == captured
    counts, **no duplicate players** and **no "database is locked"**.
14. **Same walk-up on two tablets** — add the same kid (same name) on two tablets;
    after both sync the desktop shows **one** player with one reference (a typed
    *different* team on each yields one player with two team memberships — not a
    bug). Mid-create kill/relaunch → completes with no duplicate.

There is **no JS test runner** (per B.1/B.2) — this checklist is the verification.
