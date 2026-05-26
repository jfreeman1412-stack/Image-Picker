// The roster list for the selected shoot. Phase B.3 surfaces the offline state:
//  • a TWO-STATE badge per player — synced (green ✓) vs pending (amber ↑, captured
//    + waiting to sync) vs failed (red !, needs attention §6) — derived in App
//    from (cached server status) ∪ (the local queue);
//  • a "N waiting to sync" line (the device backlog);
//  • an "Offline — cached …" line when the roster is served from cache (§3).
// "Needs photo only" hides both synced and pending (a queued capture is no longer
// "needs photo"); failed players stay visible — they still need a usable photo.
//
// Presentational: membership items, the badge sets, and the filter state all live
// in App (lifted) and arrive as props. See ../PHASE_B3_OFFLINE_CAPTURE.md.
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
  job, items, referencedPlayerIds, pendingPlayerIds, failedPlayerIds, pendingCount,
  attentionCount, fromCache, cachedAt, storageWarning,
  syncing, syncProgress, lastSummary, status, error,
  teamFilter, search, needsPhotoOnly,
  onTeamFilter, onSearch, onNeedsPhotoOnly, onPickPlayer,
  onSyncNow, onOpenAttention, onReload, onBack,
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
      // "Needs photo" = no synced AND no pending capture (failed still needs one).
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

        {storageWarning && <p className="offline-line">⚠︎ {storageWarning}</p>}

        {syncing && syncProgress.total > 0 ? (
          <p className="sync-line">
            Syncing {Math.min(syncProgress.done + 1, syncProgress.total)} of {syncProgress.total}…
          </p>
        ) : pendingCount > 0 ? (
          <div className="sync-bar">
            <span className="sync-line">
              {pendingCount} photo{pendingCount === 1 ? '' : 's'} waiting to sync
            </span>
            <button className="link-sync" onClick={onSyncNow}>Sync now</button>
          </div>
        ) : lastSummary && lastSummary.synced > 0 ? (
          <p className="sync-line success">
            Synced {lastSummary.synced} photo{lastSummary.synced === 1 ? '' : 's'} ✓
          </p>
        ) : null}

        {attentionCount > 0 && (
          <button className="attention-line" onClick={onOpenAttention}>
            ⚠ {attentionCount} photo{attentionCount === 1 ? '' : 's'}
            {attentionCount === 1 ? ' needs' : ' need'} attention →
          </button>
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
              const failed = failedPlayerIds.has(m.player_id);
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
                          shows ✓ AND ↑ (pending/failed are mutually exclusive per
                          item, so at most one of those two ever shows). */}
                      {synced && (
                        <span className="badge-check" title="Synced">✓</span>
                      )}
                      {pending && (
                        <span className="badge-pending" title="Captured — waiting to sync">↑</span>
                      )}
                      {failed && (
                        <span className="badge-failed" title="Sync failed — see “needs attention”">!</span>
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
