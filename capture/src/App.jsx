// Phase B.2 — the capture app's top-level view switch (routerless; Decision 5).
// App owns the cross-screen state — the selected shoot/player and (from later
// sections) the roster, filter state, and the captured-✓ Set — so it survives
// the round-trip to the capture screen and back.
//
//   shoots → (pick a shoot)  → roster → (pick a player) → capture → roster …
//
// Section 2 wires: shoots → roster(placeholder). Sections 3–7 fill in the
// roster list, filters, and capture/remove. See ../PHASE_B2_ROSTER_CAPTURE.md.
import { useState } from 'react';
import ShootPicker from './ShootPicker.jsx';
import CaptureScreen from './CaptureScreen.jsx';

export default function App() {
  const [view, setView] = useState('shoots');           // shoots | roster | capture
  const [selectedJob, setSelectedJob] = useState(null);   // { id, name }
  const [selectedPlayer, setSelectedPlayer] = useState(null);

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
    // Placeholder until Section 3 builds the real roster + ✓ status screen.
    return (
      <main className="screen picker-screen">
        <div className="picker-card">
          <h1 className="picker-title">{selectedJob?.name}</h1>
          <p className="muted">Roster + capture status — coming next (Section 3).</p>
          <button
            className="btn ghost"
            onClick={() => {
              setSelectedJob(null);
              setView('shoots');
            }}
          >
            ← Back to shoots
          </button>
        </div>
      </main>
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
