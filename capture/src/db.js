// Phase B.3 — the offline data layer (Decision 1). One IndexedDB database holds
// the durable capture QUEUE, the cached ROSTERS (+ shoot-scoped ✓ status), and a
// cached SHOOTS snapshot for the offline picker.
//
// Two scale-minded choices:
//  • Photo bytes live in a SEPARATE `blobs` store from the lightweight `queue`
//    metadata, so listing/counting the queue on every roster render never loads
//    ~500 photos into memory. The drainer fetches bytes by id only at upload.
//  • Photos are stored as bytes (ArrayBuffer), never as a Blob — several iOS
//    Safari versions mishandle Blobs round-tripped through IndexedDB. We rebuild
//    a Blob at upload time (Section 5).
//
// See ../PHASE_B3_OFFLINE_CAPTURE.md.
import { openDB } from 'idb';

const DB_NAME = 'player-sort-capture';
const DB_VERSION = 2; // v2 (B.5): adds the `localPlayers` store for walk-ups

// Queue item status lifecycle:
//   pending        — captured + durably queued, not yet uploaded
//   uploading      — a drain attempt is in flight (reset to pending on resume)
//   failed_quality — server rejected the photo's quality (kept + surfaced)
//   failed_gone    — target player/shoot gone server-side (kept + surfaced)
// A SYNCED item is deleted, not kept: its absence from the queue + presence in
// the cached server status set is what "synced ✓" means.
export const QUEUE_STATUS = {
  PENDING: 'pending',
  UPLOADING: 'uploading',
  FAILED_QUALITY: 'failed_quality',
  FAILED_GONE: 'failed_gone',
};

function uuid() {
  // Secure context (https/localhost) always in our setup; guard anyway.
  if (typeof crypto !== 'undefined' && crypto.randomUUID) return crypto.randomUUID();
  return `id-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

let _dbPromise = null;
function db() {
  if (!_dbPromise) {
    _dbPromise = openDB(DB_NAME, DB_VERSION, {
      // Version-aware so an existing v1 install (queued photos + cached rosters)
      // upgrades to v2 WITHOUT dropping data — we only ADD the new store.
      upgrade(d, oldVersion) {
        if (oldVersion < 1) {
          const queue = d.createObjectStore('queue', { keyPath: 'id' });
          queue.createIndex('by-capturedAt', 'capturedAt'); // FIFO drain order
          queue.createIndex('by-job', 'jobId');             // this shoot's items
          queue.createIndex('by-job-player', ['jobId', 'playerId']); // retake lookup
          queue.createIndex('by-status', 'status');         // cheap pending count
          d.createObjectStore('blobs', { keyPath: 'id' });  // id → { id, bytes, mime }
          d.createObjectStore('rosters', { keyPath: 'jobId' });
          d.createObjectStore('shoots', { keyPath: 'id' });
        }
        if (oldVersion < 2) {
          // B.5: walk-up players added at the shoot. Keyed by a client uuid
          // (localId); `realPlayerId` is filled once the server creates the
          // Player at sync. Queue items for a walk-up carry playerId = localId.
          const lp = d.createObjectStore('localPlayers', { keyPath: 'localId' });
          lp.createIndex('by-job', 'jobId');
        }
      },
    });
  }
  return _dbPromise;
}

// ── capture queue ──────────────────────────────────────────────────────────

/**
 * Bank a capture durably. Retake semantics: any not-yet-synced item for the same
 * (job, player) is dropped first, so the queue holds at most one latest pending
 * capture per player. Writes metadata + bytes atomically in one transaction.
 * Returns the stored metadata (no bytes).
 */
export async function enqueueCapture({
  playerId, jobId, playerName, team, bytes, mime, capturedAt,
}) {
  const d = await db();
  const id = uuid();
  const tx = d.transaction(['queue', 'blobs'], 'readwrite');
  const q = tx.objectStore('queue');
  const b = tx.objectStore('blobs');
  let cur = await q.index('by-job-player').openCursor(IDBKeyRange.only([jobId, playerId]));
  while (cur) {
    await b.delete(cur.primaryKey);
    await cur.delete();
    cur = await cur.continue();
  }
  const meta = {
    id, playerId, jobId, playerName, team,
    mime: mime || 'image/jpeg',
    capturedAt: capturedAt ?? Date.now(),
    status: QUEUE_STATUS.PENDING,
    attempts: 0,
    lastError: null,
  };
  await q.put(meta);
  await b.put({ id, bytes, mime: meta.mime });
  await tx.done;
  return meta;
}

/** All queue metadata (no bytes), FIFO by capture time — the drain order. */
export async function listQueueMeta() {
  const d = await db();
  return d.getAllFromIndex('queue', 'by-capturedAt');
}

/** This shoot's queue metadata (no bytes) — for per-player pending/failed badges. */
export async function listQueueMetaByJob(jobId) {
  const d = await db();
  return d.getAllFromIndex('queue', 'by-job', IDBKeyRange.only(jobId));
}

/** Bytes for one item, fetched only at upload time. → { id, bytes, mime } | undefined */
export async function getCaptureBytes(id) {
  const d = await db();
  return d.get('blobs', id);
}

/** Patch a queue item's metadata (status / attempts / lastError). */
export async function updateQueueItem(id, patch) {
  const d = await db();
  const tx = d.transaction('queue', 'readwrite');
  const cur = await tx.store.get(id);
  if (cur) await tx.store.put({ ...cur, ...patch });
  await tx.done;
}

/** Remove an item (metadata + bytes) — on confirmed sync or an explicit remove. */
export async function removeQueueItem(id) {
  const d = await db();
  const tx = d.transaction(['queue', 'blobs'], 'readwrite');
  await tx.objectStore('queue').delete(id);
  await tx.objectStore('blobs').delete(id);
  await tx.done;
}

/** Count of items still waiting to sync (pending + uploading), cheap (no bytes). */
export async function countPending() {
  const d = await db();
  const tx = d.transaction('queue');
  const idx = tx.store.index('by-status');
  const p = await idx.count(IDBKeyRange.only(QUEUE_STATUS.PENDING));
  const u = await idx.count(IDBKeyRange.only(QUEUE_STATUS.UPLOADING));
  await tx.done;
  return p + u;
}

// ── cached roster + status (per shoot) ───────────────────────────────────────

export async function putRoster({ jobId, name, items, statusIds }) {
  const d = await db();
  await d.put('rosters', {
    jobId, name, items: items || [], statusIds: statusIds || [], cachedAt: Date.now(),
  });
}

export async function getRoster(jobId) {
  const d = await db();
  return d.get('rosters', jobId);
}

/**
 * Mark a player as synced in the CACHED roster too, so the offline view stays
 * correct between online refreshes: after a drain removes the queue item, the
 * cached status set is what tells an offline reopen the player has a photo.
 */
export async function markRosterSynced(jobId, playerId) {
  const d = await db();
  const tx = d.transaction('rosters', 'readwrite');
  const r = await tx.store.get(jobId);
  if (r) {
    const set = new Set(r.statusIds || []);
    set.add(playerId);
    await tx.store.put({ ...r, statusIds: [...set] });
  }
  await tx.done;
}

/** The set of jobIds with a cached roster — drives the picker's "Ready offline" badge. */
export async function getCachedRosterJobIds() {
  const d = await db();
  return new Set(await d.getAllKeys('rosters'));
}

// ── cached shoots snapshot (the offline picker list) ─────────────────────────

/** Replace the cached capture-ready shoots with the latest online snapshot. */
export async function putShoots(shoots) {
  const d = await db();
  const tx = d.transaction('shoots', 'readwrite');
  await tx.store.clear();
  const cachedAt = Date.now();
  for (const s of shoots) await tx.store.put({ id: s.id, name: s.name, cachedAt });
  await tx.done;
}

export async function getShoots() {
  const d = await db();
  return d.getAll('shoots');
}

// ── local (walk-up) players — added at the shoot, reconciled at sync (B.5) ────
// A walk-up is a not-on-roster kid added on the spot. It lives here with a client
// uuid (localId) until the drainer creates the real server Player and records its
// id (realPlayerId). The capture is queued against the localId; the drainer
// resolves localId → realPlayerId before uploading the reference. See
// ../PHASE_B5_WALKUP_PLAYER.md.

export async function addLocalPlayer({ jobId, name, team, isCoach = false }) {
  const d = await db();
  const localId = uuid();
  const rec = {
    localId, jobId, name, team,
    isCoach: !!isCoach,
    createdAt: Date.now(),
    realPlayerId: null, // set once the server creates the Player (sync time)
  };
  await d.put('localPlayers', rec);
  return rec;
}

/** This shoot's local walk-up players (merged into the roster view in App). */
export async function listLocalPlayersByJob(jobId) {
  const d = await db();
  return d.getAllFromIndex('localPlayers', 'by-job', IDBKeyRange.only(jobId));
}

export async function getLocalPlayer(localId) {
  const d = await db();
  return d.get('localPlayers', localId);
}

/** Record the real server player_id once the walk-up has been created server-side. */
export async function setLocalPlayerReal(localId, realPlayerId) {
  const d = await db();
  const tx = d.transaction('localPlayers', 'readwrite');
  const rec = await tx.store.get(localId);
  if (rec) await tx.store.put({ ...rec, realPlayerId });
  await tx.done;
}

export async function removeLocalPlayer(localId) {
  const d = await db();
  await d.delete('localPlayers', localId);
}

// ── storage durability (Decision 1 / Section 6) ──────────────────────────────

/** Ask the browser not to evict our storage. Installed PWAs are usually granted. */
export async function requestPersistentStorage() {
  if (navigator.storage?.persist) {
    try { return await navigator.storage.persist(); } catch { return false; }
  }
  return false;
}

/** { usage, quota, ratio } in bytes, or null if the API is unavailable. */
export async function storageEstimate() {
  if (navigator.storage?.estimate) {
    try {
      const { usage = 0, quota = 0 } = await navigator.storage.estimate();
      return { usage, quota, ratio: quota ? usage / quota : 0 };
    } catch { return null; }
  }
  return null;
}
