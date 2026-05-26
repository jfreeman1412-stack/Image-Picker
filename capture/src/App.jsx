// Phase B.3 §5 — the drainer. A single useSyncQueue hook uploads the queue
// whenever connected (mount / online / after enqueue / "Sync now" / periodic),
// flipping ✓ amber→green live and keeping the offline cache correct. Sync-time
// failures are kept in the queue; §6 surfaces them. See ../PHASE_B3_OFFLINE_CAPTURE.md.
import { useCallback, useEffect, useMemo, useState } from 'react';
import ShootPicker from './ShootPicker.jsx';
import RosterScreen from './RosterScreen.jsx';
import CaptureScreen from './CaptureScreen.jsx';
import useSyncQueue from './useSyncQueue.js';
import { SYNC_RESULT } from './syncQueue.js';
import {
  QUEUE_STATUS, putRoster, getRoster, markRosterSynced,
  listQueueMetaByJob, countPending,
} from './db.js';

export default function App() {
  const [view, setView] = useState('shoots');             // shoots | roster | capture
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

  // Local queue view for the CURRENT shoot (metadata only) + a global pending count.
  const [queueMeta, setQueueMeta] = useState([]);
  const [pendingCount, setPendingCount] = useState(0);

  // Filter state, lifted (Decision 5) so it survives the capture round-trip.
  const [teamFilter, setTeamFilter] = useState('');
  const [search, setSearch] = useState('');
  const [needsPhotoOnly, setNeedsPhotoOnly] = useState(false);

  const refreshQueue = useCallback(async () => {
    const jobId = selectedJob?.id;
    const [meta, pending] = await Promise.all([
      jobId != null ? listQueueMetaByJob(jobId) : Promise.resolve([]),
      countPending(),
    ]);
    setQueueMeta(meta);
    setPendingCount(pending);
  }, [selectedJob]);

  // Each item the drainer resolves: a SYNC flips ✓ green live (current shoot) and
  // updates the cached roster so an offline reopen still shows it captured.
  const handleItemResult = useCallback((item, result) => {
    if (result !== SYNC_RESULT.SYNCED) return;
    markRosterSynced(item.jobId, item.playerId);
    if (item.jobId === selectedJob?.id) {
      setReferencedPlayerIds((prev) => new Set(prev).add(item.playerId));
    }
  }, [selectedJob]);

  const { syncing, progress, lastSummary, syncNow } = useSyncQueue({
    active: pendingCount > 0,
    onItemResult: handleItemResult,
  });

  // Reconcile the local queue view whenever a drain finishes (and on mount).
  useEffect(() => { if (!syncing) refreshQueue(); }, [syncing, refreshQueue]);

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

  // Per-player queue state for the CURRENT shoot, from the lightweight metadata.
  const pendingPlayerIds = useMemo(
    () => new Set(
      queueMeta
        .filter((i) => i.status === QUEUE_STATUS.PENDING || i.status === QUEUE_STATUS.UPLOADING)
        .map((i) => i.playerId),
    ),
    [queueMeta],
  );
  const queueItemByPlayer = useMemo(() => {
    const m = new Map();
    for (const i of queueMeta) m.set(i.playerId, i);
    return m;
  }, [queueMeta]);

  const backToShoots = () => {
    setSelectedJob(null);
    setSelectedPlayer(null);
    setRoster([]);
    setReferencedPlayerIds(new Set());
    setQueueMeta([]);
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

  if (view === 'roster') {
    return (
      <RosterScreen
        job={selectedJob}
        items={roster}
        referencedPlayerIds={referencedPlayerIds}
        pendingPlayerIds={pendingPlayerIds}
        pendingCount={pendingCount}
        fromCache={fromCache}
        cachedAt={cachedAt}
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
        onSyncNow={syncNow}
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
