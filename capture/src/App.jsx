// Phase B.3 §3 — offline roster cache. App caches the roster + shoot-scoped ✓
// status on shoot-select and, when the fetch fails (offline), serves the cached
// copy. The capture model is still B.2's (a successful upload marks ✓ live); the
// local queue + drainer arrive in §4–5. See ../PHASE_B3_OFFLINE_CAPTURE.md.
import { useEffect, useState } from 'react';
import ShootPicker from './ShootPicker.jsx';
import RosterScreen from './RosterScreen.jsx';
import CaptureScreen from './CaptureScreen.jsx';
import { putRoster, getRoster } from './db.js';

export default function App() {
  const [view, setView] = useState('shoots');             // shoots | roster | capture
  const [selectedJob, setSelectedJob] = useState(null);     // { id, name }
  const [selectedPlayer, setSelectedPlayer] = useState(null);

  // Roster state, lifted here so it persists across roster → capture → roster.
  const [roster, setRoster] = useState([]);                 // membership items
  const [referencedPlayerIds, setReferencedPlayerIds] = useState(new Set());
  const [rosterStatus, setRosterStatus] = useState('loading'); // loading|ready|error
  const [rosterError, setRosterError] = useState(null);
  const [reloadKey, setReloadKey] = useState(0);            // bump to refetch
  const [fromCache, setFromCache] = useState(false);        // roster served offline
  const [cachedAt, setCachedAt] = useState(null);           // when it was cached

  // Filter state, lifted here too (Decision 5) so it's preserved when the user
  // captures a player and returns. Default = all teams / empty search / off.
  const [teamFilter, setTeamFilter] = useState('');
  const [search, setSearch] = useState('');
  const [needsPhotoOnly, setNeedsPhotoOnly] = useState(false);

  // Fetch the roster + shoot-scoped ✓ status when the chosen shoot changes (or a
  // reload is requested). Online: render + CACHE for offline. Offline (fetch
  // fails): fall back to the cached copy.
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
      }
    })();
    return () => { cancelled = true; };
  }, [selectedJob, reloadKey]);

  const backToShoots = () => {
    setSelectedJob(null);
    setSelectedPlayer(null);
    setRoster([]);
    setReferencedPlayerIds(new Set());
    setFromCache(false);
    setCachedAt(null);
    setTeamFilter('');           // fresh filters for the next shoot
    setSearch('');
    setNeedsPhotoOnly(false);
    setView('shoots');
  };

  // Tap a roster row → capture for that player.
  const pickPlayer = (member) => {
    setSelectedPlayer(member);
    setView('capture');
  };

  // Capture succeeded → mark ✓ live (no refetch) and return to the roster with
  // filter state intact (it lives here).
  const onCaptured = (playerId) => {
    setReferencedPlayerIds((prev) => new Set(prev).add(playerId));
    setSelectedPlayer(null);
    setView('roster');
  };

  const cancelCapture = () => {
    setSelectedPlayer(null);
    setView('roster');
  };

  // Photo removed for this shoot → clear ✓ live and return to the roster.
  const onRemoved = (playerId) => {
    setReferencedPlayerIds((prev) => {
      const next = new Set(prev);
      next.delete(playerId);
      return next;
    });
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
        fromCache={fromCache}
        cachedAt={cachedAt}
        status={rosterStatus}
        error={rosterError}
        teamFilter={teamFilter}
        search={search}
        needsPhotoOnly={needsPhotoOnly}
        onTeamFilter={setTeamFilter}
        onSearch={setSearch}
        onNeedsPhotoOnly={setNeedsPhotoOnly}
        onPickPlayer={pickPlayer}
        onReload={() => setReloadKey((k) => k + 1)}
        onBack={backToShoots}
      />
    );
  }

  // view === 'capture'
  return (
    <CaptureScreen
      job={selectedJob}
      player={selectedPlayer}
      alreadyCaptured={
        selectedPlayer ? referencedPlayerIds.has(selectedPlayer.player_id) : false
      }
      onSaved={onCaptured}
      onCancel={cancelCapture}
      onGone={backToShoots}
      onRemoved={onRemoved}
    />
  );
}
