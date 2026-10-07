# Phase B.5 — Hand-off (Walk-up player: add a not-on-roster player in the capture app, offline-capable)

> **Status: decisions LOCKED 2026-05-26; DRAFT build plan for review. Do NOT build
> yet.** This is the **fifth** step of the B-series capture app and the first to add
> a **second kind of work** to the B.3 sync queue. It is the **riskiest** of the
> three follow-ups and is intentionally scheduled **last** (after C.3 + B.4) so the
> proven B.3 sync core isn't disturbed until we're focused on it. Read this in full
> — especially "How this stays inside the B.3 invariant."

## What this feature does

At a shoot, a kid shows up who isn't on the roster. The volunteer adds them on the
spot: **enter a name + a team**, where team is either **typed-new** or **picked from
the teams already on this shoot's roster**. The added player then behaves
**identically to a CSV-uploaded roster player** — a real `Player` + a
`PlayerMembership` on this job — so the captured reference photo attaches correctly
and they flow into matching like everyone else.

**Offline-capable (the design crux, locked).** A walk-up happens **at** the shoot,
which may be off-grid. So the player is created **locally** and reconciled to the
backend **at sync time**, integrated with the **existing B.3 queue/drain model** —
the same model captures use. We assessed online-only-v1 and chose offline because an
off-grid walk-up that can't be captured defeats the entire B.3 premise. The
integration is designed to **stay inside B.3's correctness invariant** (below).

---

## Why this is safe *because of* the identity model

The thing that makes offline reconciliation tractable is how identity already works
(`services/players.py`, `models/db_models.py`):

- **`Player.norm_name` is globally unique** and `upsert_player` (`players.py:36`) is
  **find-or-create by normalized name**. So "create Jane Smith" is naturally
  **idempotent** and **convergent**: it never makes a duplicate `Player`, no matter
  how many times it runs or how many tablets run it.
- The reference upload (`PUT …/references/shoot/{job_id}`) is **already idempotent
  by scoped-replace** (B.3 Context). One reference per (player, shoot).
- `PlayerMembership` has `uq_membership_job_player_team` — adding the same
  (player, job, team) twice collapses to one row.

So the **backend reconciliation is convergent by construction**. The genuinely new
work is all on the **client**: a player who has **no real `player_id` yet** at
capture time, and getting that resolved at sync without breaking the queue.

---

## ⚠️ Decisions — LOCKED 2026-05-26

### Decision 1 — Offline-capable, on the B.3 queue model *(LOCKED)*
Walk-up add + capture works fully off-grid and reconciles at sync, reusing the B.3
queue/drainer. One mechanism, not a parallel "online add" path.

### Decision 2 — ONE additive backend endpoint; B.3's "zero backend" ends here *(LOCKED)*
The only way to write a `PlayerMembership` today is `replace_shoot_memberships`
(`players.py:56`), which **wipes and reloads the whole roster** — unusable for
adding one kid. B.5 adds **one additive endpoint** to add a single player without
touching anyone else's memberships. A.1/A.2/C.1 cores and the positional endpoint
stay byte-for-byte; the destructive replace is not touched.

```
POST /api/players/roster/{job_id}/walkup
     body: { "name": str, "team": str }
     → 200 { player_id, name, team, is_coach, created, membership_created }
     404 if job missing · 400 if name/team blank
```
Behavior: `upsert_player(name)` (find-or-create by `norm_name`) → insert a
`PlayerMembership(player_id, job_id, team_name, norm_team=normalize_name(team),
is_coach=is_coach_name(name))` **unless** one already exists for
(job, player, norm_team) → commit. **Idempotent** and **race-safe** (catches the
`norm_name` UNIQUE violation under concurrent same-name adds — §1): re-POSTing the
same name+team returns the same `player_id` with `created/membership_created =
false`. Reuses `upsert_player` + `is_coach_name` + `normalize_name`; factor the
membership-insert out of `replace_shoot_memberships` only if clean, else inline
(don't modify the replace path). One small **additive** infra change rides along —
a WAL + `busy_timeout` pragma in `db.py` (§0) for concurrent-sync safety — but no
existing backend logic is modified.

### Decision 3 — Provisional client-uuid identity, reconciled at drain *(LOCKED)*
Offline, a walk-up gets a **client-generated `localId` (uuid string)** and lives in
a new IndexedDB store. The capture is queued against that `localId`. At drain, the
drainer **resolves `localId` → real integer `player_id`** by calling the walkup
endpoint, records the mapping, then uploads the reference to the real id. The
`norm_name` dedup guarantees the resolved `Player` is shared with any CSV roster
entry or other tablet that has the same name.

### Decision 4 — "Create-then-capture" is a SUB-STEP of the existing queue item, NOT a second queue op-type *(LOCKED)*
This is what keeps B.5 inside the B.3 invariant. We do **not** add a separate
"create player" queue entry with its own ordering. Instead, for a queue item whose
player is a still-unresolved `localId`, `uploadItem` does *create → remap → upload*
as one atomic-from-the-queue's-view step. The queue stays **one op-type (a reference
upload), FIFO, idempotent, delete-on-200** — exactly as B.3 proved correct. The
create is just a prerequisite the upload performs for itself, composed of two
already-idempotent calls.

---

## How this stays inside the B.3 invariant (read before coding the drainer)

B.3's correctness rests on: *one idempotent op-type · serial FIFO · no inter-item
dependencies · an item is deleted only after a confirmed 200 · interrupted
`uploading` resets to `pending` and re-sends safely.* B.5 must preserve every word.

- **One op-type:** still just "upload a reference." The walkup `POST` is an internal
  prerequisite of that item's own `uploadItem`, not a queue entry. ✓
- **Idempotent + at-least-once = exactly-once:** the walkup `POST` is idempotent
  (norm_name upsert + membership uq); the reference `PUT` is idempotent
  (scoped-replace). Re-running either is safe. ✓
- **Crash safety:** if the app dies *after* the `POST` commits but *before*
  recording the `localId→realId` map, the next drain re-`POST`s → upsert returns the
  **same** `player_id` → no duplicate; then the `PUT` proceeds. If it dies after the
  `PUT` 200 but before delete, the item re-sends → scoped-replace → identical
  result, then deletes. ✓
- **Delete-on-200 unchanged:** the item is removed only after the reference `PUT`
  returns 200, never after just the create. ✓
- **The new risk to watch — mixed id types.** Real members key on an **integer**
  `player_id`; walk-ups key on a **string** `localId` until resolved. Every place
  that handles ids — the roster list, the two-state badges, the `[jobId, playerId]`
  queue index, `markRosterSynced`, the synced-`✓` set — must tolerate **both**.
  This is the primary correctness surface; the checklist (Section 6) hammers it.

---

## Multi-tablet: three concurrent tablets (the real deployment)

Real shoots run **three tablets at once** — three volunteers on three lines, each
capturing a **different portion** of the same shoot's roster (same `job_id`) on its
own tablet. B.5 must handle this gracefully. It does, by construction:

- **Walk-ups are per-device and never shared.** Each tablet's `localPlayers` +
  `localId` (uuid) live only in that tablet's IndexedDB and are **never sent to the
  server** — the server only ever receives `name + team`. So **two tablets' local
  state can never collide** (uuids are unique; the queue index `[jobId, playerId]`
  is per-device). A walk-up added on tablet A simply doesn't exist on B/C until A
  syncs and B/C refetch online — which is fine, because each volunteer owns their
  line and doesn't need to see the others' adds mid-shoot.
- **Disjoint captures (the normal case) are fully independent.** Three tablets
  upload references for **different** `player_id`s to the same shoot — no shared
  mutable state, no conflict; just three independent idempotent PUTs.
- **Same kid added on two/three tablets → converges to ONE identity.** Each tablet
  makes its own `localId`, but at sync each `POST …/walkup {name, team}` resolves
  through `upsert_player` by `norm_name` to the **same** `Player`; memberships
  dedup via the uq key; the reference scoped-replaces to **one** (last sync wins,
  exactly like a retake). **No duplicate Player/membership, no error** — convergent
  for N tablets, not just two.
- **Edge — same kid, *different team* typed on two tablets** (a volunteer mishears
  the team): one `Player`, but **two memberships** (distinct `norm_team`), so the
  kid shows **twice** in the roster (once per team) with **one** reference (keyed by
  player+shoot, not team). Not a bug — it mirrors a CSV that lists someone on two
  teams; the desktop operator reconciles it. Flagged so it isn't mistaken for a
  sync fault.

The throughline: a tablet never depends on another tablet's local state, so there's
nothing to be "out of sync" about — **the backend is the single convergence point**,
and its idempotent endpoints make convergence automatic.

## Concurrent sync from three tablets (the backend question you asked)

You've only ever tested one device syncing. Here's the honest assessment of three
at once.

**Correctness is not at risk** — and that's a property of the B.3 model, not of the
backend's concurrency handling. Every sync op is idempotent (norm_name upsert,
membership uq, scoped-replace), and the drainer **retries any network/5xx** result
(`syncQueue.js`: 5xx → stays `pending` → retried). At-least-once delivery + an
idempotent endpoint = exactly-once effect, **even under contention** — a request
that loses a race or hits a transient lock just retries and converges.

**What actually happens at the backend today (verified):** the engine is plain
SQLite with **no WAL and no explicit `busy_timeout`** (`db.py:13-16`; pysqlite's
implicit ~5 s timeout applies), a **single uvicorn worker** (README run line), and
the reference endpoints are `async def` that call **blocking** `detect_faces`
*inline* (`api/references.py:107`). A blocking call in an `async def` **holds the
event loop**, so three concurrent uploads are effectively **serialized** at the
server — processed one at a time. Correctness: fine. Cost: a three-way end-of-day
sync is **slower** (serialized detection), not broken.

**Three things to do about it:**

1. **Budget for slower office-return sync, not failure.** Serialized detection ×
   three backlogs takes longer than one device; the progress UI already shows it.
   No code needed — just know it.
2. **Harden SQLite for concurrent writes (small, recommended).** Add a one-time
   pragma in `db.py` — **WAL mode + an explicit `busy_timeout`** (e.g. 10 s) — so
   concurrent writers don't raise "database is locked" even if the implicit
   serialization ever stops holding (a future multi-worker deploy, or moving
   detection to a threadpool to *get* parallelism for 3-tablet throughput). Low
   risk, benefits plain B.3 reference sync too. **This is latent in B.3 already —
   never stressed because only one device was tested.**
3. **Harden the walkup upsert against a same-name insert race.** `upsert_player`
   (`players.py:36`) is check-then-insert on the unique `norm_name`; two concurrent
   `/walkup`s for the same name *could* both read "absent" then both insert → the
   second hits the UNIQUE constraint. Today's event-loop serialization likely masks
   it, but don't rely on that. The **new endpoint** must catch the `IntegrityError`,
   roll back, re-query by `norm_name`, and reuse the existing `Player` — keeping
   A.1's `upsert_player` frozen. Covered by a test (Section 1).

**Bottom line:** ship-safe as-is thanks to idempotency + retry; do #2 and #3 as
cheap insurance, and **actually run a three-device concurrent-sync soak** (Section 6
checklist) since it's genuinely untested.

---

## Context you need

- **Capture flow today:** `App.pickPlayer(member)` → `CaptureScreen` enqueues
  `{ playerId: member.player_id, jobId, playerName, team, bytes, … }`
  (`CaptureScreen.jsx:83`). The drainer `uploadItem` PUTs to
  `/api/players/{playerId}/references/shoot/{jobId}` (`syncQueue.js:98`). The queue
  item **already carries `playerName` + `team`** — exactly what the walkup `POST`
  needs.
- **Roster state** is lifted in `App` (`roster` items, `referencedPlayerIds`,
  `pendingPlayerIds`, …) and the `RosterScreen` renders from it. A walk-up must
  **merge into `roster`** so it appears as a row.
- **The teams for the combobox** come from the current roster items (`RosterScreen`
  already derives `teams` at line 33) — available offline from the cached roster.
- **Capture-ready list (`?stage=capture`)** is *rostered + no sessions*
  (`jobs.py:391`). Adding a walk-up membership keeps the job rostered and adds no
  session, so the shoot stays capture-ready — no interaction to manage.
- **Roster re-upload wipes memberships** (`replace_shoot_memberships`). A walk-up
  added *before* a later roster re-upload would lose its **membership** (its
  `Player` and its captured **reference survive** — references cascade from `Player`
  and reference-status is keyed by `captured_job_id`, `players.py:96`). Rosters load
  pre-shoot and walk-ups happen at-shoot, so this is rare; it's noted in edge cases.

---

## Don't break

- **The B.3 sync invariant** (the section above) — no second queue op-type, no
  inter-item dependency, delete only on a reference 200.
- **A.1/A.2/C.1 stay frozen** — `replace_shoot_memberships`, `upsert_player`,
  `is_coach_name`, the positional/`mapped` roster endpoints, and `services/
  references.py` are untouched. B.5 adds **one** endpoint + (if needed) one small
  extracted helper.
- **Real-member capture is unchanged.** A walk-up is an *additional* path; capturing
  a normal roster member must behave exactly as in B.3 (integer id, no create step).
- **Losslessness** — a walk-up capture is a queue item like any other: never dropped
  except on a confirmed reference 200 or an explicit user remove/discard. A failed
  *create* (e.g. job gone) routes to Needs-attention, never silent loss.
- **`/api` stays NetworkOnly; no CORS change.** The walkup `POST` is same-origin via
  the existing proxy.
- **Backend tests stay green** — record the live `pytest -v` baseline and keep it
  green plus B.5's endpoint tests. The capture app stays test-free (B.1/B.2/B.3
  precedent); B.5's verification is the **on-device checklist** (Section 6).

---

## Build plan (dependency order; each ends green + committed)

### Section 0 — Backend concurrency hardening (small, recommended; do first)
Before the new endpoint, add the cheap insurance for three-tablet concurrent sync
(see "Concurrent sync from three tablets"): a `connect`/`PRAGMA` step in `db.py`
setting **WAL mode** + an explicit **`busy_timeout`** (e.g. 10 s) on each SQLite
connection (a `connect` event listener or `connect_args`). This is additive and
benefits plain B.3 reference sync too.

**Tests:** a small test asserting `PRAGMA journal_mode` is `wal` and `busy_timeout`
is set on a fresh connection; the full suite stays green (no behavior change).

**Check:** `pytest -v` green.
Commit: `phase B.5 section 0: SQLite WAL + busy_timeout for concurrent sync`

### Section 1 — Backend: the additive walkup-add endpoint (+ tests)
Add `POST /api/players/roster/{job_id}/walkup` in `api/players.py` (in the
`/roster/...` group, **before** the dynamic `/{player_id}` route, per the file's
match-order rule). Validate job (404) + non-blank name/team (400). `upsert_player`
→ membership insert guarded by the uq key → commit → return the summary.
**Concurrency-safe:** wrap the create in a `try/except IntegrityError` → on a
`norm_name` UNIQUE violation (a racing tablet created the same player first),
`db.rollback()`, re-query by `norm_name`, and reuse that `Player` — so two
concurrent same-name `/walkup`s converge to one Player without a 500. Keep A.1's
`upsert_player` **frozen**; the retry lives in the new endpoint.

**Tests (`test_players.py`):** new name → creates Player + membership, `is_coach`
derived from a `Coach-` prefix; **re-POST same name+team → same `player_id`, no
duplication** (`created/membership_created=false`); a name that **already exists**
globally → reuses that Player (no new Player), adds the membership; same player,
**different team** → second membership; blank name/team → 400; unknown job → 404;
**simulate the race** — pre-insert a `Player` with the same `norm_name` (or force an
`IntegrityError`) and assert the endpoint reuses it cleanly, no 500; the positional
+ `mapped` endpoints still pass (regression).

**Check:** `pytest -v` green; `curl` add-walkup returns a `player_id` and
`GET /api/players/roster/{job_id}` then includes them.

Commit: `phase B.5 section 1: add-walkup endpoint (single additive membership, race-safe) (+ tests)`

### Section 2 — Client data layer: local players + id map (IndexedDB v2)
Bump `DB_VERSION` 1→2 in `capture/src/db.js` with an additive `upgrade` (existing
stores untouched). Add:
- **`localPlayers`** — keyed by `localId` (uuid). Record: `{ localId, jobId, name,
  team, isCoach, createdAt, realPlayerId (null until resolved) }`. Index `by-job`.
- **id resolution** stored on the record (`realPlayerId`) — no separate store
  needed. Add accessors: `addLocalPlayer`, `listLocalPlayersByJob`,
  `setLocalPlayerReal(localId, realPlayerId)`, `removeLocalPlayer`.
- The **queue item** for a walk-up uses `playerId = localId` (string) and, since it
  already stores `playerName`/`team`, carries everything the create needs. (No queue
  schema change — `playerId` was always opaque to the store.)

**Manual check:** in DevTools, `localPlayers` exists after upgrade with the v1 queue
data intact; a record written via console **survives reload + full app close**; the
existing B.3 queue/roster stores are unaffected (open an old install, confirm no
data loss on the version bump).

Commit: `phase B.5 section 2: IndexedDB v2 — local walk-up players + id mapping`

### Section 3 — Capture UI: "+ Add player" → local create → flows into capture
On `RosterScreen`, add an **"+ Add player"** action opening a small form: **name**
(text) + **team** (a combobox = existing roster teams **or** type-new). Submit →
`addLocalPlayer({ localId: uuid, jobId, name, team, isCoach: Coach-prefix })` →
`App` merges local players into the displayed `roster` (a local row looks like a
membership: `{ player_id: localId, name, team, is_coach }`) → optionally navigate
straight into capture for that player. **No network at add time** — identical
offline and online (B.3's one-path ethos; the drain handles the server side).

`App` merges `roster` (server) ∪ `localPlayers` (this job), de-duping a local player
once its `realPlayerId` appears in the server roster (Section 5). Badges/filters in
`RosterScreen` must accept a **string** `player_id` (the `localId`) — audit
`referencedPlayerIds.has(...)`, `pendingPlayerIds`, the `key=` props, and the
`needsPhotoOnly` filter for integer assumptions.

**Manual check (airplane mode):** "+ Add player", type a name + a **new** team and
again with an **existing** team → both appear in the roster list; tap one → capture
→ "Use photo" → returns with an **amber pending** badge; the header count includes
it; close/reopen → the walk-up + its pending capture survive (rebuilt from
IndexedDB).

Commit: `phase B.5 section 3: add walk-up players locally + flow into capture (offline)`

### Section 4 — Drainer: create-then-capture sub-step + id remap (the crux)
Extend `syncQueue.js` `uploadItem` **only** for items whose `playerId` is a
`localId` (look it up in `localPlayers`):
1. If the local record already has `realPlayerId`, use it. Else `POST
   …/roster/{jobId}/walkup { name, team }` → on 200, `setLocalPlayerReal(localId,
   player_id)` and use it; on **404** (job gone) → `failed_gone` (keep, surface); on
   network/5xx → **RETRY** (item stays `pending`, exactly like B.3); on **400**
   (shouldn't happen — name/team validated client-side) → `failed_quality` with the
   message (kept, surfaced).
2. With the real id, run the **existing** reference `PUT` path unchanged (set
   `uploading` → upload bytes → 200 deletes the item + marks synced).
Keep it serial/FIFO; the create is part of *this* item's attempt. Do **not** create
a new queue entry.

**Manual check (the heart of B.5):** airplane mode → add 2 walk-ups (one new team,
one existing) + capture each → reconnect → the drainer **creates the players then
uploads the refs**; badges flip green; `GET /api/players/roster/{jobId}` now lists
both; `GET /api/players/{id}/references` shows **exactly one** ref each. Kill the app
**mid-create** and relaunch → completes with **no duplicate Player/membership/ref**
(re-`POST` upserts). On a **second tablet**, add a walk-up with the **same name** →
after both sync, the desktop shows **one** Player with that name (norm_name dedup).

Commit: `phase B.5 section 4: drain walk-ups (create-then-capture, idempotent remap)`

### Section 5 — Reconciliation polish: server-wins de-dup + synced state + failures
- **markRosterSynced with the real id:** when a walk-up's reference syncs, add the
  **`realPlayerId`** (not the `localId`) to the synced set + cached roster
  `statusIds`, so an offline reopen shows it captured.
- **Server-wins de-dup:** after an online roster refetch, if a `localPlayers` record
  has a `realPlayerId` that now appears in the server roster, **drop the local
  record** so the player shows once (as a normal member). Until then, show the local
  row.
- **Failed create → Needs attention:** a `failed_gone`/`failed_quality` walk-up
  surfaces in the existing Needs-attention list (`NeedsAttention.jsx`) with the
  server message; **Discard** removes both the queue item **and** the orphan
  `localPlayers` record.

**Manual check:** sync a walk-up online, then "Refresh roster" → the player shows
**once** (no local+server duplicate) with a green ✓; force a `failed_gone` (delete
the job server-side mid-trip) → the walk-up lands in Needs attention, Discard clears
both records; an offline reopen of a synced walk-up still shows it captured.

Commit: `phase B.5 section 5: reconcile walk-ups (server-wins de-dup, synced state, failures)`

### Section 6 — README + adversarial offline checklist
Extend `capture/README.md`'s offline model + checklist with walk-ups: add locally
(offline), the create-then-capture sync, the mixed-id behavior, and the
roster-re-upload caveat. Fold in the checklist items below; mark the **two-tablet
same-name** and **mid-create crash** items as the must-run adversarial cases.

**Checklist additions (run on the target tablets — three where noted):**
1. Offline add (new team + existing team) → both appear, capture → pending, survive
   close/reopen + restart.
2. Reconnect → create-then-upload → exactly one Player / membership / reference each.
3. Mid-create crash/kill → relaunch → completes, **no duplicates**.
4. **Three-device concurrent sync soak (the must-run case):** all three tablets,
   same shoot, capture **disjoint** roster players + a couple of walk-ups offline,
   then bring all three back and **drain at the same time** → every reference lands,
   server counts == captured counts, **no duplicate Players/memberships**, no
   "database is locked" errors. This is the genuinely untested path.
5. **Same kid on two/three tablets, same team** → after all sync, the desktop shows
   **one** Player, one membership, one reference (last sync wins). **Same kid,
   *different* team on two tablets** → one Player, **two** memberships (the kid
   appears once per team), one reference — documented, not a bug.
6. Walk-up reference fails the quality gate at sync → Needs attention, kept, player
   still un-synced; re-shoot replaces.
7. Job deleted server-side mid-trip → walk-up → `failed_gone`, kept, Discard clears
   local + queue.
8. Roster re-uploaded after a walk-up was captured → walk-up drops from the roster
   list but its captured reference still matches on the desktop (membership wiped,
   `Player`+reference survive) — documented behavior, not a bug.

**Manual check:** the crew reproduces the full lifecycle across **all three
tablets** from the README: install → cache a shoot → offline → add walk-ups +
capture → close/reopen → bring all three back → concurrent sync → all synced, with
**no duplicate identities** server-side and no lock errors.

Commit: `phase B.5 section 6: README + walk-up adversarial offline checklist`

---

## Hard edge cases — and how B.5 handles each

| Edge case | Handling |
| --- | --- |
| Walk-up captured fully offline | Local `Player` (uuid) + queued capture; both in IndexedDB; resolved at drain (§3–4). |
| App/device dies mid-create | Re-`POST` on next drain; `upsert_player` returns the same id; no duplicate (§4). |
| Two/three tablets add the same name | All upsert the same `norm_name` → one `Player`; scoped-replace → one ref (last sync wins); convergent for N tablets (Decision 3, §4). |
| Same kid, *different* team on two tablets | One `Player`, two memberships (distinct `norm_team`) → kid shows once per team, one reference; mirrors a CSV two-team listing, not a bug (Multi-tablet §). |
| Three tablets sync concurrently | Serialized at the backend today (single worker + blocking detection); idempotent + 5xx-retry make it correct; WAL + `busy_timeout` (§0) is the insurance (Concurrent-sync §). |
| Two tablets race to create the same player | Endpoint catches the `norm_name` UNIQUE `IntegrityError`, re-queries, reuses — no 500, no duplicate (§1). |
| Walk-up name already on the roster | Upsert reuses the existing `Player`; membership added for the chosen team (§1). (Distinct kids sharing a name collapse to one identity — inherent to the roster model; noted.) |
| Walk-up reference fails the gate at sync | `failed_quality`, kept + surfaced like any B.3 capture (§5). **B.6 interaction:** if it's a salvageable flag (`multiple_faces`/`low_confidence`), B.6's resolve path must first resolve the walk-up's `localId` → real `player_id` (reuse §4's create-then-X), then run B.6's detect/resolve against that id. Compose the create step ahead of the resolve. |
| Job/Player gone server-side at sync | `failed_gone`, kept + surfaced; Discard clears local + queue (§5). |
| Roster re-uploaded after a walk-up | Membership wiped by `replace_shoot_memberships`; `Player` + captured reference survive and still match (Context, §6). |
| Same player shown twice (local + server) after sync | Server-wins de-dup drops the local record once its `realPlayerId` is in the server roster (§5). |
| Mixed string/int player ids across the UI | Audited in §3; all id-keyed paths accept both until resolution (the primary risk surface). |

## Conventions (match existing code)

- **One additive backend endpoint + tests;** `pytest -v` green; A.1/A.2/C.1 frozen.
- Capture app: Vite + React 18, JSX, function components + hooks; reuse the B.3
  queue/drainer/`db.js` patterns; `res.ok`-checked `fetch`, error bodies via
  `body?.detail?.message || body?.detail?.error`, no error object in state.
- Same-origin `/api` proxy; no CORS / no `main.py` change.
- Single-line commit per section (`phase B.5 section N: …`).
- Verification = the on-device checklist (§6), per B.1/B.2/B.3 — no JS test runner.

## Acceptance

1. A volunteer can **add a not-on-roster player** (name + team, team typed-new or
   picked from the shoot's teams) in the capture app, **fully offline**.
2. The walk-up **flows into capture** exactly like a roster player and queues as a
   pending capture that **survives close/reopen and restart**.
3. On reconnect, the drainer **creates the real `Player` + `PlayerMembership`** (via
   the additive endpoint) and **uploads the reference**, so the player is
   **indistinguishable from a CSV-uploaded roster player** and flows into matching.
4. Reconciliation is **convergent and lossless**: idempotent create + scoped-replace
   yield **exactly one** Player / membership / reference per walk-up, even across
   crashes, retries, and **two or three tablets** adding the same name.
5. **Three tablets** capturing the same shoot (disjoint players + walk-ups) **sync
   concurrently** with no duplicate identities and **no "database is locked"**
   errors; correctness holds via idempotency + retry, with WAL/`busy_timeout` (§0)
   as insurance.
6. The **B.3 sync invariant is preserved** — one op-type, serial FIFO,
   delete-on-200; no regression to normal roster-member capture/sync.
7. Failed creates/uploads land in **Needs attention** (kept + surfaced); no walk-up
   capture is ever silently lost.
8. **Backend = one additive endpoint (+ a WAL/`busy_timeout` pragma);** `frontend/`
   untouched; `pytest -v` green; `capture/README.md` documents the walk-up model +
   the three-tablet checklist.

## Out of scope (defer — do NOT build in B.5)

- **Editing / deleting a synced walk-up** from the capture app (beyond Discard of a
  failed item) — same deferral class as B.3's "remove a synced photo offline."
- **A desktop UI for walk-ups** — they appear in the normal roster
  (`GET …/roster/{job_id}`) once synced; no separate desktop surface needed.
- **Jersey numbers / extra fields** — name + team only, matching C.1's canonical
  fields.
- **Merging/splitting walk-up identities** or resolving same-name-different-kid
  collisions — inherits the roster model's `norm_name` identity; a future "Split
  Player" (per A.1's split-friendly mandate) would address it.
- **A second queue op-type / generalized offline mutation queue** — explicitly
  avoided (Decision 4) to protect the B.3 invariant.
