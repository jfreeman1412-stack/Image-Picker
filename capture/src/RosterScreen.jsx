// Section 3 — the roster list for the selected shoot, with shoot-specific ✓
// badges. Section 4 — team filter + name search + "needs photo only" toggle,
// all stacking (AND), defaulting to the full unfiltered roster.
//
// Presentational: the membership items, the captured-✓ Set, AND the filter
// state are all held in App (lifted, Decision 5) and passed in as props, so
// they survive the round-trip to the capture screen and update live afterward.
// The filtered view + the team list are derived here (pure functions of props).
// Tapping a player to capture arrives in Section 6. See
// ../PHASE_B2_ROSTER_CAPTURE.md.
import { useMemo } from 'react';

export default function RosterScreen({
  job, items, referencedPlayerIds, status, error,
  teamFilter, search, needsPhotoOnly,
  onTeamFilter, onSearch, onNeedsPhotoOnly,
  onReload, onBack,
}) {
  const teams = useMemo(
    () => Array.from(new Set(items.map((m) => m.team))).sort((a, b) => a.localeCompare(b)),
    [items],
  );

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return items.filter((m) => {
      if (teamFilter && m.team !== teamFilter) return false;
      if (q && !m.name.toLowerCase().includes(q)) return false;
      if (needsPhotoOnly && referencedPlayerIds.has(m.player_id)) return false;
      return true;
    });
  }, [items, teamFilter, search, needsPhotoOnly, referencedPlayerIds]);

  const ready = status === 'ready';
  const hasRoster = ready && items.length > 0;

  return (
    <main className="screen roster-screen">
      <header className="roster-header">
        <button className="link-back" onClick={onBack}>← Shoots</button>
        <div className="roster-titles">
          <h1 className="roster-title">{job?.name}</h1>
          {hasRoster && (
            <span className="roster-count">
              {filtered.length === items.length
                ? `${items.length} player${items.length === 1 ? '' : 's'}`
                : `${filtered.length} of ${items.length}`}
            </span>
          )}
        </div>

        {hasRoster && (
          <div className="roster-filters">
            <select
              className="filter-select"
              value={teamFilter}
              onChange={(e) => onTeamFilter(e.target.value)}
              aria-label="Filter by team"
            >
              <option value="">All teams</option>
              {teams.map((t) => (
                <option key={t} value={t}>{t}</option>
              ))}
            </select>
            <input
              className="filter-search"
              type="search"
              value={search}
              onChange={(e) => onSearch(e.target.value)}
              placeholder="Search name…"
              aria-label="Search by name"
            />
            <button
              type="button"
              className={needsPhotoOnly ? 'pill active' : 'pill'}
              aria-pressed={needsPhotoOnly}
              onClick={() => onNeedsPhotoOnly(!needsPhotoOnly)}
            >
              Needs photo
            </button>
          </div>
        )}
      </header>

      <div className="roster-body">
        {status === 'loading' && <p className="muted roster-pad">Loading roster…</p>}

        {status === 'error' && (
          <div className="picker-block roster-pad">
            <p className="result-line warn">{error}</p>
            <button className="btn" onClick={onReload}>Retry</button>
          </div>
        )}

        {ready && items.length === 0 && (
          <p className="muted roster-pad">
            No roster loaded for this shoot. Upload one for this job (see README),
            then retry.
          </p>
        )}

        {hasRoster && filtered.length === 0 && (
          <p className="muted roster-pad">No players match these filters.</p>
        )}

        {hasRoster && filtered.length > 0 && (
          <ul className="roster-list">
            {filtered.map((m) => {
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
