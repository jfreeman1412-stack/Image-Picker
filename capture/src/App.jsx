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
  useEffect(() => {
    if (!selectedJob) return undefined;
    let cancelled = false;
    setRosterStatus('loading');
    setRosterError(null);
    (async () => {
      try {
        const [r1, r2] = await Promise.all([
          fetch(`/api/players/roster/${selectedJob.id}`),
          fetch(`/api/players/roster/${selectedJob.id}/reference-status`),
        ]);
        if (!r1.ok) throw new Error(`Couldn’t load the roster (HTTP ${r1.status}).`);
        if (!r2.ok) throw new Error(`Couldn’t load capture status (HTTP ${r2.status}).`);
        const rosterBody = await r1.json();
        const statusBody = await r2.json();
        if (cancelled) return;
        const items = rosterBody.items || [];
        const statusIds = statusBody.player_ids_with_references || [];
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
