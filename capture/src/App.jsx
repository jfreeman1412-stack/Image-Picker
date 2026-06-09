// The capture app's top-level view switch (routerless; B.2 Decision 5). App owns
// the cross-screen state — selected shoot/player, the roster, the synced-✓ set,
// the filter state — so it survives navigation and updates live.
//
//   shoots → (pick a shoot) → roster → (pick a player) → capture → roster …
//                                   ↘ (needs attention) ↗
//
// Phase B.3 offline spine:
//  • the roster + ✓ status are CACHED on select and read back offline (§3);
//  • a capture ENQUEUES locally instead of uploading (§4);
//  • a single DRAINER uploads the queue whenever connected (§5), flipping ✓
//    amber→green live; sync-time rejections land in "Needs attention" (§6).
// App derives a two-state ✓ (synced / pending / failed) from (cached server
// status) ∪ (the local queue). See ../PHASE_B3_OFFLINE_CAPTURE.md.
import { useCallback, useEffect, useMemo, useState } from 'react';
import ShootPicker from './ShootPicker.jsx';
import RosterScreen from './RosterScreen.jsx';
import CaptureScreen from './CaptureScreen.jsx';
import NeedsAttention from './NeedsAttention.jsx';
import useSyncQueue from './useSyncQueue.js';
import { SYNC_RESULT } from './syncQueue.js';
import {
  QUEUE_STATUS, putRoster, getRoster, markRosterSynced,
  listQueueMetaByJob, countPending, removeQueueItem, storageEstimate,
  addLocalPlayer, listLocalPlayersByJob, removeLocalPlayer, getLocalPlayer,
} from './db.js';

const STORAGE_WARN = 0.8; // surface a warning past 80% of quota (§6)

// Live multi-tablet poll cadence (2026-06-08). Twenty seconds is the chosen
// trade-off between "B sees A's activity promptly" and not hammering the
// shared backend during a multi-tablet shoot.
const ROSTER_POLL_MS = 20000;


/**
 * GET /api/players/roster/{jobId}/reference-status → array of player_ids.
 * Single source of truth for "which players are captured on the server"; used
 * by BOTH the initial roster fetch (REPLACE semantics — establishes baseline)
 * AND the live multi-tablet poll (ADDITIVE union semantics — never regresses
 * a locally-known-synced player back to grey). Throws on non-2xx so callers
 * choose how to react: initial fetch falls back to cache; poll silently
 * skips. See PHASE_B3_OFFLINE_CAPTURE.md for the surrounding state model.
 */
async function fetchReferenceStatus(jobId) {
  const res = await fetch(`/api/players/roster/${jobId}/reference-status`);
  if (!res.ok) throw new Error(`reference-status HTTP ${res.status}`);
  const body = await res.json();
  return body.player_ids_with_references || [];
}


/**
 * GET /api/players/roster/{jobId} → array of membership items (player_id,
 * name, team, is_coach). Used by both the initial fetch and the live poll.
 *
 * Unlike fetchReferenceStatus, this returns a LIST not a set: the poll
 * applies REPLACE semantics on the roster state, which is safe because
 * the merge with locally-added walkups happens at render time in
 * `mergedItems` (server-wins-on-collision filter by realPlayerId), and
 * the `localPlayers` IndexedDB state is structurally separate from
 * `roster` — so a poll-driven REPLACE can't regress an in-flight local
 * walkup. Throws on non-2xx; same caller contract as fetchReferenceStatus.
 */
async function fetchRoster(jobId) {
  const res = await fetch(`/api/players/roster/${jobId}`);
  if (!res.ok) throw new Error(`roster HTTP ${res.status}`);
  const body = await res.json();
  return body.items || [];
}

export default function App() {
  const [view, setView] = useState('shoots');             // shoots|roster|capture|attention
  const [selectedJob, setSelectedJob] = useState(null);     // { id, name }
  const [selectedPlayer, setSelectedPlayer] = useState(null);

  // Roster state, lifted here so it persists across roster → capture → roster.
  const [roster, setRoster] = useState([]);                 // membership items
  const [referencedPlayerIds, setReferencedPlayerIds] = useState(new Set()); // SYNCED ✓
  const [rosterStatus, setRosterStatus] = useState('loading'); // loading|ready|error
  const [rosterError, setRosterError] = useState(null);
  const [reloadKey, setReloadKey] = useState(0);            // bump to refetch
  const [fromCache, setFromCache] = useState(false);        // roster served offline
  const [cachedAt, setCachedAt] = useState(null);           // when it was cached

  // Local queue view for the CURRENT shoot (metadata only — no photo bytes), a
  // global "waiting to sync" count, and storage headroom. Refreshed after
  // enqueue/remove/drain + on load.
  const [queueMeta, setQueueMeta] = useState([]);
  const [pendingCount, setPendingCount] = useState(0);
  const [storageInfo, setStorageInfo] = useState(null);

  // Local walk-up players for the CURRENT shoot (B.5): added on-device, merged
  // into the roster, reconciled to real server players at sync.
  const [localPlayers, setLocalPlayers] = useState([]);

  // Filter state, lifted (B.2 Decision 5) so it survives the capture round-trip.
  const [teamFilter, setTeamFilter] = useState('');
  const [search, setSearch] = useState('');
  const [needsPhotoOnly, setNeedsPhotoOnly] = useState(false);

  const refreshQueue = useCallback(async () => {
    const jobId = selectedJob?.id;
    const [meta, pending, est] = await Promise.all([
      jobId != null ? listQueueMetaByJob(jobId) : Promise.resolve([]),
      countPending(),
      storageEstimate(),
    ]);
    setQueueMeta(meta);
    setPendingCount(pending);
    setStorageInfo(est);
  }, [selectedJob]);

  const refreshLocalPlayers = useCallback(async () => {
    const jobId = selectedJob?.id;
    setLocalPlayers(jobId != null ? await listLocalPlayersByJob(jobId) : []);
  }, [selectedJob]);

  // Each item the drainer resolves: a SYNC flips ✓ green live (current shoot) and
  // updates the cached roster so an offline reopen still shows it captured.
  const handleItemResult = useCallback((item, result) => {
    if (result !== SYNC_RESULT.SYNCED) return;
    markRosterSynced(item.jobId, item.playerId); // keep the offline cache correct
    if (item.jobId === selectedJob?.id) {
      setReferencedPlayerIds((prev) => new Set(prev).add(item.playerId));
    }
  }, [selectedJob]);

  const { syncing, progress, lastSummary, syncNow } = useSyncQueue({
    active: pendingCount > 0,
    onItemResult: handleItemResult,
  });

  // Reconcile the local queue + walk-up records whenever a drain finishes (and
  // on mount) — post-sync this picks up realPlayerId for server-wins de-dup.
  useEffect(() => {
    if (!syncing) { refreshQueue(); refreshLocalPlayers(); }
  }, [syncing, refreshQueue, refreshLocalPlayers]);

  // Fetch the roster + shoot-scoped ✓ status when the chosen shoot changes (or a
  // reload is requested). Online: render + CACHE for offline. Offline (fetch
  // fails): fall back to the cached copy. Either way, refresh the local queue.
  // REPLACE semantics on referencedPlayerIds — initial-fetch baseline. The
  // live poll below is ADDITIVE so it can run alongside without regressing.
  useEffect(() => {
    if (!selectedJob) return undefined;
    let cancelled = false;
    setRosterStatus('loading');
    setRosterError(null);
    (async () => {
      try {
        const r1 = await fetch(`/api/players/roster/${selectedJob.id}`);
        if (!r1.ok) throw new Error(`Couldn’t load the roster (HTTP ${r1.status}).`);
        const statusIds = await fetchReferenceStatus(selectedJob.id);
        const rosterBody = await r1.json();
        if (cancelled) return;
        const items = rosterBody.items || [];
        setRoster(items);
        setReferencedPlayerIds(new Set(statusIds));
        setFromCache(false);
        setCachedAt(null);
        setRosterStatus('ready');
        putRoster({ jobId: selectedJob.id, name: selectedJob.name, items, statusIds });
      } catch (e) {
        const cached = await getRoster(selectedJob.id).catch(() => null);
        if (cancelled) return;
        if (cached) {
          setRoster(cached.items || []);
          setReferencedPlayerIds(new Set(cached.statusIds || []));
          setFromCache(true);
          setCachedAt(cached.cachedAt || null);
          setRosterStatus('ready');
        } else {
          setRosterError(e.message || 'Couldn’t reach the server. Check the connection.');
          setRosterStatus('error');
        }
      } finally {
        if (!cancelled) refreshQueue();
      }
    })();
    return () => { cancelled = true; };
  }, [selectedJob, reloadKey, refreshQueue]);

  // 2026-06-08 — live multi-tablet roster poll. Every ROSTER_POLL_MS while
  // the user is on the roster screen AND online, refresh BOTH the server-
  // sourced ✓ set AND the server-sourced roster list so any activity on the
  // OTHER tablet (a fresh capture OR a walkup added) shows up here without
  // a manual reload. Closes the 2-tablet co-shoot blind spot where B's
  // operator can't tell A already captured a kid (or added a new player)
  // and either re-shoots a kid A already did or misses the new player.
  //
  // 2026-06-09 extension: the poll now also re-fetches /roster, so a walkup
  // added on the OTHER tablet appears here within 20s without re-picking
  // the shoot. The two endpoints fire in parallel via Promise.all; partial
  // success is treated as full failure (silent skip both) so the dual
  // state never goes inconsistent — e.g., we never show a new player with
  // a stale-known ✓ state from an old snapshot.
  //
  // Four invariants:
  //   (1) ADDITIVE union on referencedPlayerIds — never REPLACE. The
  //       drainer's handleItemResult adds player_ids the moment a local
  //       upload lands a 200; a REPLACE-semantic poll could regress that
  //       to grey if its request was inflight when the 200 returned.
  //       References-only-grow during a shoot, so additive is provably safe.
  //   (2) REPLACE semantics on roster (the list of memberships) — safe
  //       because tablet-local walkups live in a SEPARATE `localPlayers`
  //       state that the poll never touches. `mergedItems` merges
  //       (roster ∪ localPlayers) at render time with a server-wins-on-
  //       collision filter (filtered by realPlayerId), so a freshly-synced
  //       walkup's local copy disappears the moment the poll picks up its
  //       server membership row — clean handoff, no flicker. An owned
  //       walkup that's not yet drained stays in localPlayers and is shown
  //       regardless of poll snapshots (realPlayerId still null → not
  //       filtered → present in mergedItems).
  //   (3) Silent skip on offline OR fetch failure (either half) — no state
  //       change, no error UI, no cache clobber. The next tick retries.
  //       All-or-nothing: if /roster succeeds but /reference-status fails
  //       (or vice versa), we DON'T apply the half-update — otherwise B
  //       could see a brand-new player from A with a stale ✓ state that
  //       doesn't reflect whether the server has a reference for them yet.
  //   (4) Roster-view-only — the interval clears the moment the user
  //       navigates away (deps include `view`), so capture / attention
  //       screens don't poll.
  //
  // Cache freshness intentionally NOT addressed by the poll: a poll-
  // discovered ✓ or new walkup stays in-memory only, not written to the
  // cached roster/statusIds. The offline-first cache behavior is preserved
  // exactly as B.3 designed it (cache reflects the last full fetch). A
  // follow-up could mirror polled additions into putRoster/markRosterSynced
  // if the cold-offline-reopen gap matters, but that's a CHANGE to offline
  // behavior — out of scope here per the spec's "offline behavior
  // preserved" requirement.
  useEffect(() => {
    if (view !== 'roster' || !selectedJob) return undefined;
    const jobId = selectedJob.id;
    const tick = async () => {
      if (typeof navigator !== 'undefined' && navigator.onLine === false) return;
      let items;
      let statusIds;
      try {
        [items, statusIds] = await Promise.all([
          fetchRoster(jobId),
          fetchReferenceStatus(jobId),
        ]);
      } catch {
        // Silent skip — next tick retries. Partial success is treated as
        // full failure: Promise.all rejects on any rejection, so we never
        // apply a half-update that would leave the dual state inconsistent.
        return;
      }
      // REPLACE the roster list (see invariant 2). Skip the state update
      // when the new items shallow-equal the previous (same length + same
      // ids in order) so React doesn't re-render every 20s during quiet
      // stretches of the shoot.
      setRoster((prev) => {
        if (prev.length === items.length
            && prev.every((p, i) => p.player_id === items[i].player_id)) {
          return prev;
        }
        return items;
      });
      // ADDITIVE union on the ✓ set (see invariant 1). Skip the state
      // update when nothing new arrived.
      setReferencedPlayerIds((prev) => {
        let added = false;
        const next = new Set(prev);
        for (const id of statusIds) {
          if (!next.has(id)) { next.add(id); added = true; }
        }
        return added ? next : prev;
      });
    };
    const id = setInterval(tick, ROSTER_POLL_MS);
    return () => clearInterval(id);
  }, [view, selectedJob]);

  // Load this shoot's local walk-up players (offline-readable; no network).
  useEffect(() => { refreshLocalPlayers(); }, [refreshLocalPlayers, reloadKey]);

  // Per-player queue state for the CURRENT shoot, from the lightweight metadata.
  const pendingPlayerIds = useMemo(
    () => new Set(
      queueMeta
        .filter((i) => i.status === QUEUE_STATUS.PENDING || i.status === QUEUE_STATUS.UPLOADING)
        .map((i) => i.playerId),
    ),
    [queueMeta],
  );
  const failedItems = useMemo(
    () => queueMeta.filter(
      (i) => i.status === QUEUE_STATUS.FAILED_QUALITY || i.status === QUEUE_STATUS.FAILED_GONE,
    ),
    [queueMeta],
  );
  const failedPlayerIds = useMemo(
    () => new Set(failedItems.map((i) => i.playerId)),
    [failedItems],
  );
  const queueItemByPlayer = useMemo(() => {
    const m = new Map();
    for (const i of queueMeta) m.set(i.playerId, i);
    return m;
  }, [queueMeta]);

  const storageWarning = useMemo(() => {
    if (!storageInfo || !storageInfo.ratio || storageInfo.ratio < STORAGE_WARN) return null;
    return `Storage ${Math.round(storageInfo.ratio * 100)}% full — sync soon to free space.`;
  }, [storageInfo]);

  // The roster the screen shows = server memberships + local walk-ups. Once a
  // walk-up has synced (its realPlayerId appears in the refetched server
  // roster), drop the local copy so it shows once — server wins (§5).
  const mergedItems = useMemo(() => {
    const serverIds = new Set(roster.map((m) => m.player_id));
    const locals = localPlayers
      .filter((lp) => !(lp.realPlayerId != null && serverIds.has(lp.realPlayerId)))
      .map((lp) => ({
        player_id: lp.localId,
        name: lp.name,
        team: lp.team,
        is_coach: lp.isCoach,
        is_walkup: true,
      }));
    return [...roster, ...locals];
  }, [roster, localPlayers]);

  const backToShoots = () => {
    setSelectedJob(null);
    setSelectedPlayer(null);
    setRoster([]);
    setReferencedPlayerIds(new Set());
    setQueueMeta([]);
    setLocalPlayers([]);
    setFromCache(false);
    setCachedAt(null);
    setTeamFilter('');           // fresh filters for the next shoot
    setSearch('');
    setNeedsPhotoOnly(false);
    setView('shoots');
  };

  const pickPlayer = (member) => {
    setSelectedPlayer(member);
    setView('capture');
  };

  // B.5 — add a walk-up locally (no network), then jump straight to capturing
  // their photo. A `Coach-` name prefix flags a coach, same as the CSV roster.
  // The capture enqueues against rec.localId; the drainer creates the real
  // server player and remaps at sync time (§4).
  const addWalkup = async ({ name, team }) => {
    const rec = await addLocalPlayer({
      jobId: selectedJob.id, name, team, isCoach: /^coach-/i.test(name.trim()),
    });
    setLocalPlayers((prev) => [...prev, rec]);
    setSelectedPlayer({
      player_id: rec.localId, name: rec.name, team: rec.team,
      is_coach: rec.isCoach, is_walkup: true,
    });
    setView('capture');
  };

  // Capture banked → player now PENDING. Refresh, kick a drain (uploads at once
  // on good service; no-op offline), return to the roster with filters intact.
  const onQueued = () => {
    refreshQueue();
    syncNow();
    setSelectedPlayer(null);
    setView('roster');
  };

  const onRemoved = (playerId) => {
    setReferencedPlayerIds((prev) => {
      const next = new Set(prev);
      next.delete(playerId);
      return next;
    });
    refreshQueue();
    setSelectedPlayer(null);
    setView('roster');
  };

  const cancelCapture = () => {
    setSelectedPlayer(null);
    setView('roster');
  };

  // Re-shoot a failed item: target that player and capture afresh (enqueue
  // replaces the failed queue item). Discard: drop it (the only non-200 delete).
  const reshootFailed = (item) => {
    setSelectedPlayer({ player_id: item.playerId, name: item.playerName, team: item.team });
    setView('capture');
  };
  const discardFailed = async (item) => {
    await removeQueueItem(item.id);
    // B.5 — a discarded walk-up also drops its local record (it never became a
    // confirmed capture). A normal roster player has no local record → no-op.
    const local = await getLocalPlayer(item.playerId).catch(() => null);
    if (local) await removeLocalPlayer(item.playerId);
    refreshQueue();
    refreshLocalPlayers();
  };

  // Phase B.6 (2026-06-08) — Select-face salvage completed successfully.
  // The /resolve call already returned 200 (the panel handles that),
  // so all we do here is the post-success bookkeeping that mirrors a
  // drainer SYNC: drop the queue item, flip the offline cache to synced,
  // and refresh the ✓ badge on the roster.
  const resolvedFailed = async (item) => {
    await removeQueueItem(item.id);
    markRosterSynced(item.jobId, item.playerId);
    if (item.jobId === selectedJob?.id) {
      setReferencedPlayerIds((prev) => new Set(prev).add(item.playerId));
    }
    refreshQueue();
  };

  if (view === 'shoots') {
    return (
      <ShootPicker
        onPick={(job) => {
          setSelectedJob(job);
          setView('roster');
        }}
      />
    );
  }

  if (view === 'attention') {
    return (
      <NeedsAttention
        job={selectedJob}
        items={failedItems}
        onReshoot={reshootFailed}
        onDiscard={discardFailed}
        onResolved={resolvedFailed}
        onBack={() => setView('roster')}
      />
    );
  }

  if (view === 'roster') {
    return (
      <RosterScreen
        job={selectedJob}
        items={mergedItems}
        referencedPlayerIds={referencedPlayerIds}
        pendingPlayerIds={pendingPlayerIds}
        failedPlayerIds={failedPlayerIds}
        pendingCount={pendingCount}
        attentionCount={failedItems.length}
        fromCache={fromCache}
        cachedAt={cachedAt}
        storageWarning={storageWarning}
        syncing={syncing}
        syncProgress={progress}
        lastSummary={lastSummary}
        status={rosterStatus}
        error={rosterError}
        teamFilter={teamFilter}
        search={search}
        needsPhotoOnly={needsPhotoOnly}
        onTeamFilter={setTeamFilter}
        onSearch={setSearch}
        onNeedsPhotoOnly={setNeedsPhotoOnly}
        onPickPlayer={pickPlayer}
        onAddWalkup={addWalkup}
        onSyncNow={syncNow}
        onOpenAttention={() => setView('attention')}
        onReload={() => setReloadKey((k) => k + 1)}
        onBack={backToShoots}
      />
    );
  }

  // view === 'capture'
  const isSynced = selectedPlayer ? referencedPlayerIds.has(selectedPlayer.player_id) : false;
  const queueItem = selectedPlayer ? queueItemByPlayer.get(selectedPlayer.player_id) : undefined;
  return (
    <CaptureScreen
      job={selectedJob}
      player={selectedPlayer}
      isSynced={isSynced}
      queueItem={queueItem || null}
      onQueued={onQueued}
      onCancel={cancelCapture}
      onRemoved={onRemoved}
    />
  );
}
