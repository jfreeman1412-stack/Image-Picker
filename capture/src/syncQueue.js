// Phase B.3 §5 — the drainer. Uploads queued captures to B.2's existing
// shoot-scoped replace endpoint whenever a connection is available. There is one
// drain path; "good service vs office-return" is just how often it succeeds.
//
// Correctness model (the heart of B.3):
//  • Serial FIFO by capture time — a retake (queued later) uploads last and wins,
//    matching B.2's "latest replaces" semantics.
//  • Idempotent by construction — PUT .../references/shoot/{job} REPLACES this
//    shoot's reference, so re-sending a maybe-already-landed item is safe.
//    At-least-once delivery + idempotent endpoint = exactly-once effect.
//  • An item is DELETED only after a confirmed 200, so a crash mid-upload never
//    loses a photo; at worst it re-sends.
//  • On start, any item stranded in `uploading` by a previous crash is reset to
//    `pending` (we can't know if it landed; re-sending is safe).
// See ../PHASE_B3_OFFLINE_CAPTURE.md.
import {
  QUEUE_STATUS, listQueueMeta, getCaptureBytes, updateQueueItem, removeQueueItem,
  getLocalPlayer, setLocalPlayerReal,
} from './db.js';

// Per-item outcome — drives the live ✓ and the §6 "Needs attention" surfacing.
export const SYNC_RESULT = {
  SYNCED: 'synced',
  FAILED_QUALITY: 'failed_quality',  // server quality gate (400) — keep + surface
  FAILED_GONE: 'failed_gone',        // player/shoot gone (404)  — keep + surface
  RETRY: 'retry',                    // network/5xx — stays pending, try later
};

const isPending = (it) => it.status === QUEUE_STATUS.PENDING;

let _draining = false; // module guard: never two drains at once

/**
 * Drain the queue, serially, FIFO, until empty or the connection drops. Calls:
 *   onProgress({ done, total })    — before each upload + at the end
 *   onItemResult(item, resultKind) — after each item resolves
 * Re-queries after each batch so captures made DURING a drain are also sent
 * (continuous draining on good service). Returns { synced, failed, retry } or
 * null if a drain was already running.
 */
export async function drainOnce({ onProgress, onItemResult } = {}) {
  if (_draining) return null;
  _draining = true;
  try {
    // Recover anything left mid-flight by a previous crash/kill.
    for (const it of await listQueueMeta()) {
      if (it.status === QUEUE_STATUS.UPLOADING) {
        await updateQueueItem(it.id, { status: QUEUE_STATUS.PENDING });
      }
    }

    const summary = { synced: 0, failed: 0, retry: 0 };
    let done = 0;
    let total = (await listQueueMeta()).filter(isPending).length;
    let networkLost = false;

    while (!networkLost) {
      const pending = (await listQueueMeta()).filter(isPending);
      if (pending.length === 0) break;
      total = Math.max(total, done + pending.length); // monotonic for the UI
      for (const item of pending) {
        onProgress?.({ done, total });
        const result = await uploadItem(item);
        if (result === SYNC_RESULT.SYNCED) summary.synced += 1;
        else if (result === SYNC_RESULT.RETRY) { summary.retry += 1; networkLost = true; }
        else summary.failed += 1;
        onItemResult?.(item, result);
        done += 1;
        if (networkLost) break; // connection gone — stop; a later trigger resumes
      }
    }
    onProgress?.({ done, total });
    return summary;
  } finally {
    _draining = false;
  }
}

// Phase B.5 — map a queue item to its real server player_id. A normal roster
// capture already carries a real (int) id, so this is a no-op for it. A walk-up
// carries a client localId: if its server Player was already created, reuse the
// stored realPlayerId; otherwise create it now via the additive /walkup endpoint
// (idempotent by norm_name) and remember the id. Returns { playerId } on
// success, { retry } for network/5xx (try later), or { failed, error } for a
// kept rejection. See ../PHASE_B5_WALKUP_PLAYER.md.
async function resolveServerPlayerId(item) {
  const local = await getLocalPlayer(item.playerId).catch(() => null);
  if (!local) return { playerId: item.playerId };          // roster player: real id
  if (local.realPlayerId != null) return { playerId: local.realPlayerId };
  try {
    const res = await fetch(`/api/players/roster/${item.jobId}/walkup`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: item.playerName, team: item.team }),
    });
    if (res.ok) {
      const body = await res.json();
      await setLocalPlayerReal(item.playerId, body.player_id);
      return { playerId: body.player_id };
    }
    if (res.status === 404) {
      return { failed: SYNC_RESULT.FAILED_GONE,
        error: 'This shoot no longer exists on the server.' };
    }
    if (res.status === 400) {
      const body = await res.json().catch(() => ({}));
      return { failed: SYNC_RESULT.FAILED_QUALITY,
        error: body?.detail?.message || 'Could not add this player on the server.' };
    }
    return { retry: true, error: `Add-player failed (HTTP ${res.status}).` };
  } catch {
    return { retry: true, error: 'No connection.' };
  }
}


async function uploadItem(item) {
  await updateQueueItem(item.id, {
    status: QUEUE_STATUS.UPLOADING,
    attempts: (item.attempts || 0) + 1,
  });

  // B.5 — resolve the target player first. A walk-up is queued against a client
  // localId; create (or look up) its real server Player before the reference can
  // attach. A sub-step of THIS item — no second queue op-type — and both the
  // create and the reference PUT are idempotent, so a crash/retry between them
  // is safe (re-create returns the same Player; re-PUT replaces).
  const resolved = await resolveServerPlayerId(item);
  if (resolved.retry) {
    await updateQueueItem(item.id, {
      status: QUEUE_STATUS.PENDING, lastError: resolved.error || 'No connection.',
    });
    return SYNC_RESULT.RETRY;
  }
  if (resolved.failed) {
    await updateQueueItem(item.id, { status: resolved.failed, lastError: resolved.error });
    return resolved.failed;
  }
  const playerId = resolved.playerId;

  const rec = await getCaptureBytes(item.id).catch(() => null);
  if (!rec || !rec.bytes) {
    // Bytes vanished (shouldn't happen) — surface rather than loop forever.
    await updateQueueItem(item.id, {
      status: QUEUE_STATUS.FAILED_GONE,
      lastError: 'Photo data is missing on the device.',
    });
    return SYNC_RESULT.FAILED_GONE;
  }

  try {
    const blob = new Blob([rec.bytes], { type: rec.mime || 'image/jpeg' });
    const fd = new FormData();
    fd.append('file', blob, 'capture.jpg');
    const res = await fetch(
      `/api/players/${playerId}/references/shoot/${item.jobId}`,
      { method: 'PUT', body: fd },
    );
    if (res.ok) {
      await removeQueueItem(item.id); // confirmed — only now is it dropped
      return SYNC_RESULT.SYNCED;
    }
    if (res.status === 404) {
      await updateQueueItem(item.id, {
        status: QUEUE_STATUS.FAILED_GONE,
        lastError: 'This player or shoot no longer exists on the server.',
      });
      return SYNC_RESULT.FAILED_GONE;
    }
    if (res.status === 400) {
      const body = await res.json().catch(() => ({}));
      await updateQueueItem(item.id, {
        status: QUEUE_STATUS.FAILED_QUALITY,
        lastError: body?.detail?.message || body?.detail?.error || 'Quality check failed.',
      });
      return SYNC_RESULT.FAILED_QUALITY;
    }
    // 5xx / unexpected — transient; keep pending and try later.
    await updateQueueItem(item.id, {
      status: QUEUE_STATUS.PENDING,
      lastError: `Upload failed (HTTP ${res.status}).`,
    });
    return SYNC_RESULT.RETRY;
  } catch {
    // Network error → offline; keep pending, retry on the next trigger.
    await updateQueueItem(item.id, {
      status: QUEUE_STATUS.PENDING,
      lastError: 'No connection.',
    });
    return SYNC_RESULT.RETRY;
  }
}
