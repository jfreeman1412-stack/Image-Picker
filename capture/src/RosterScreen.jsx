// Section 3 — the roster list for the selected shoot, with shoot-specific ✓
// badges. Presentational: the membership items and the captured-✓ Set are
// fetched and held in App (lifted, Decision 5) and passed in as props, so they
// survive the round-trip to the capture screen and update live afterward.
// Filters (team / search / needs-photo) arrive in Section 4; tapping a player
// to capture arrives in Section 6. See ../PHASE_B2_ROSTER_CAPTURE.md.
export default function RosterScreen({
  job, items, referencedPlayerIds, status, error, onReload, onBack,
}) {
  return (
    <main className="screen roster-screen">
      <header className="roster-header">
        <button className="link-back" onClick={onBack}>← Shoots</button>
        <div className="roster-titles">
          <h1 className="roster-title">{job?.name}</h1>
          {status === 'ready' && (
            <span className="roster-count">
              {items.length} player{items.length === 1 ? '' : 's'}
            </span>
          )}
        </div>
      </header>

      <div className="roster-body">
        {status === 'loading' && <p className="muted roster-pad">Loading roster…</p>}

        {status === 'error' && (
          <div className="picker-block roster-pad">
            <p className="result-line warn">{error}</p>
            <button className="btn" onClick={onReload}>Retry</button>
          </div>
        )}

        {status === 'ready' && items.length === 0 && (
          <p className="muted roster-pad">
            No roster loaded for this shoot. Upload one for this job (see README),
            then retry.
          </p>
        )}

        {status === 'ready' && items.length > 0 && (
          <ul className="roster-list">
            {items.map((m) => {
              const captured = referencedPlayerIds.has(m.player_id);
              return (
                <li key={`${m.player_id}-${m.team}`} className="roster-row">
                  <div className="roster-row-main">
                    <span className="roster-name">{m.name}</span>
                    {m.is_coach ? <span className="tag">Coach</span> : null}
                  </div>
                  <div className="roster-row-meta">
                    <span className="roster-team">{m.team}</span>
                    {captured && (
                      <span className="badge-check" title="Has a photo for this shoot">
                        ✓
                      </span>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </main>
  );
}
