// Phase B.3 §4 — two-state ✓ per player: synced (green ✓, from the server status)
// vs pending (amber ↑, a queued local capture), rendered independently (a synced
// player with a queued retake shows both), plus a "N waiting to sync" line.
// "Needs photo only" hides both (a queued capture is no longer "needs photo").
// The sync UI + needs-attention arrive in §5–6. See ../PHASE_B3_OFFLINE_CAPTURE.md.
import { useMemo } from 'react';

function formatAgo(ts) {
  if (!ts) return '';
  const s = Math.max(0, Math.round((Date.now() - ts) / 1000));
  if (s < 60) return 'just now';
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h} h ago`;
  return `${Math.round(h / 24)} d ago`;
}

export default function RosterScreen({
  job, items, referencedPlayerIds, pendingPlayerIds, pendingCount,
  fromCache, cachedAt, status, error,
  teamFilter, search, needsPhotoOnly,
  onTeamFilter, onSearch, onNeedsPhotoOnly, onPickPlayer,
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
      // "Needs photo" = no synced AND no pending capture.
      if (needsPhotoOnly
        && (referencedPlayerIds.has(m.player_id) || pendingPlayerIds.has(m.player_id))) {
        return false;
      }
      return true;
    });
  }, [items, teamFilter, search, needsPhotoOnly, referencedPlayerIds, pendingPlayerIds]);

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

        {fromCache && (
          <p className="offline-line" title={cachedAt ? new Date(cachedAt).toLocaleString() : ''}>
            ⚠︎ Offline — roster cached {formatAgo(cachedAt)}
          </p>
        )}

        {pendingCount > 0 && (
          <p className="sync-line">
            {pendingCount} photo{pendingCount === 1 ? '' : 's'} waiting to sync
          </p>
        )}

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
              const synced = referencedPlayerIds.has(m.player_id);
              const pending = pendingPlayerIds.has(m.player_id);
              return (
                <li key={`${m.player_id}-${m.team}`}>
                  <button className="roster-row" onClick={() => onPickPlayer(m)}>
                    <div className="roster-row-main">
                      <span className="roster-name">{m.name}</span>
                      {m.is_coach ? <span className="tag">Coach</span> : null}
                    </div>
                    <div className="roster-row-meta">
                      <span className="roster-team">{m.team}</span>
                      {/* Independent badges: a synced player with a queued retake
                          shows ✓ AND ↑. */}
                      {synced && (
                        <span className="badge-check" title="Synced">✓</span>
                      )}
                      {pending && (
                        <span className="badge-pending" title="Captured — waiting to sync">↑</span>
                      )}
                    </div>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </main>
  );
}
