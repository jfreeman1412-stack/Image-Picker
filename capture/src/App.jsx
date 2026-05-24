// Phase B.2 — the capture app's top-level view switch (routerless; Decision 5).
// App owns the cross-screen state — the selected shoot/player, the roster, and
// the captured-✓ Set (and, from Section 4, the filter state) — so it all
// survives the round-trip to the capture screen and updates live afterward.
//
//   shoots → (pick a shoot)  → roster → (pick a player) → capture → roster …
//
// Section 3 wires: fetch the roster + shoot-scoped reference-status on
// shoot-select and render the list with ✓ badges. Sections 4–7 add filters and
// capture/remove. See ../PHASE_B2_ROSTER_CAPTURE.md.
import { useEffect, useState } from 'react';
import ShootPicker from './ShootPicker.jsx';
import RosterScreen from './RosterScreen.jsx';
import CaptureScreen from './CaptureScreen.jsx';

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

  // Filter state, lifted here too (Decision 5) so it's preserved when the user
  // captures a player and returns. Default = all teams / empty search / off.
  const [teamFilter, setTeamFilter] = useState('');
  const [search, setSearch] = useState('');
  const [needsPhotoOnly, setNeedsPhotoOnly] = useState(false);

  // Fetch the roster + shoot-scoped ✓ status whenever the chosen shoot changes
  // (or a manual reload is requested). Kept in App so a later capture can update
  // the ✓ Set in place without a refetch, and filters/state survive navigation.
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
        setRoster(rosterBody.items || []);
        setReferencedPlayerIds(new Set(statusBody.player_ids_with_references || []));
        setRosterStatus('ready');
      } catch (e) {
        if (cancelled) return;
        setRosterError(e.message || 'Couldn’t reach the server. Check the connection.');
        setRosterStatus('error');
      }
    })();
    return () => { cancelled = true; };
  }, [selectedJob, reloadKey]);

  const backToShoots = () => {
    setSelectedJob(null);
    setRoster([]);
    setReferencedPlayerIds(new Set());
    setTeamFilter('');           // fresh filters for the next shoot
    setSearch('');
    setNeedsPhotoOnly(false);
    setView('shoots');
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
        status={rosterStatus}
        error={rosterError}
        teamFilter={teamFilter}
        search={search}
        needsPhotoOnly={needsPhotoOnly}
        onTeamFilter={setTeamFilter}
        onSearch={setSearch}
        onNeedsPhotoOnly={setNeedsPhotoOnly}
        onReload={() => setReloadKey((k) => k + 1)}
        onBack={backToShoots}
      />
    );
  }

  // view === 'capture' — unreachable until Section 6 wires player selection.
  return (
    <CaptureScreen
      job={selectedJob}
      player={selectedPlayer}
      onDone={() => setView('roster')}
    />
  );
}
