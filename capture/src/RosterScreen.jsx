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
import { useMemo, useState } from 'react';

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
  teamFilter, search, needsPhotoOnly, sortMode,
  onTeamFilter, onSearch, onNeedsPhotoOnly, onSortMode, onPickPlayer, onAddWalkup,
  onSyncNow, onOpenAttention, onReload, onBack,
  showSignups, signupState, onGetSignups,
}) {
  const teams = useMemo(
    // Booking-calendar sign-ups have no team (blank) — not a filter option.
    () => Array.from(new Set(items.map((m) => m.team).filter(Boolean))).sort((a, b) => a.localeCompare(b)),
    [items],
  );

  // 2026-10-09: booking-calendar shoots sort by booking slot by default;
  // any shoot can switch to A–Z.
  const hasBookings = useMemo(() => items.some((m) => m.booked_at), [items]);
  const sortBy = sortMode || (hasBookings ? 'booking' : 'roster');

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    const out = items.filter((m) => {
      if (teamFilter && m.team !== teamFilter) return false;
      if (q && !m.name.toLowerCase().includes(q)) return false;
      // "Needs photo" = no synced AND no pending capture (failed still needs one).
      if (needsPhotoOnly
        && (referencedPlayerIds.has(m.player_id) || pendingPlayerIds.has(m.player_id))) {
        return false;
      }
      return true;
    });
    const byName = (a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: 'base' });
    if (sortBy === 'name') return out.sort(byName);
    if (sortBy === 'booking') {
      // No booking time (walk-ups, roster players) goes last.
      return out.sort((a, b) => (a.booked_at || '\uffff').localeCompare(b.booked_at || '\uffff')
        || byName(a, b));
    }
    return out;
  }, [items, teamFilter, search, needsPhotoOnly, referencedPlayerIds, pendingPlayerIds, sortBy]);

  const ready = status === 'ready';
  const hasRoster = ready && items.length > 0;

  // B.5 — walk-up add form (local UI state; the new player is created in App).
  const [adding, setAdding] = useState(false);
  const [waName, setWaName] = useState('');
  const [waTeam, setWaTeam] = useState('');
  const [waNewTeam, setWaNewTeam] = useState('');
  const [waErr, setWaErr] = useState(null);

  const closeAdd = () => {
    setAdding(false); setWaName(''); setWaTeam(''); setWaNewTeam(''); setWaErr(null);
  };
  const submitWalkup = () => {
    const name = waName.trim();
    const team = (waTeam === '__new__' ? waNewTeam : waTeam).trim();
    if (!name) { setWaErr('Enter a player name.'); return; }
    if (!team) { setWaErr('Choose or type a team.'); return; }
    closeAdd();
    onAddWalkup({ name, team });   // App creates the walk-up + opens its capture
  };

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

        {showSignups && (
          <div className="sync-bar">
            <span className={signupState?.error ? 'sync-line error' : 'sync-line'}>
              {signupState?.busy ? 'Checking Sytist…'
                : signupState?.error || signupState?.msg || 'Booking calendar sign-ups'}
            </span>
            <button className="link-sync" disabled={!!signupState?.busy} onClick={onGetSignups}>
              Get new sign-ups
            </button>
          </div>
        )}

        {attentionCount > 0 && (
          <button className="attention-line" onClick={onOpenAttention}>
            ⚠ {attentionCount} photo{attentionCount === 1 ? '' : 's'}
            {attentionCount === 1 ? ' needs' : ' need'} attention →
          </button>
        )}

        {ready && (hasRoster || !adding) && (
          <div className="roster-filters">
            {hasRoster && (
              <>
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
                <select
                  className="filter-select"
                  value={sortBy}
                  onChange={(e) => onSortMode(e.target.value)}
                  aria-label="Sort players"
                >
                  {hasBookings
                    ? <option value="booking">Booking time</option>
                    : <option value="roster">Roster order</option>}
                  <option value="name">A–Z</option>
                </select>
                <button
                  type="button"
                  className={needsPhotoOnly ? 'pill active' : 'pill'}
                  aria-pressed={needsPhotoOnly}
                  onClick={() => onNeedsPhotoOnly(!needsPhotoOnly)}
                >
                  Needs photo
                </button>
              </>
            )}
            {/* Primary ACTION (filled blue), in the same row as the filters but
                deliberately NOT styled like the .pill toggles. */}
            {!adding && (
              <button type="button" className="btn-add" onClick={() => setAdding(true)}>
                + Add player
              </button>
            )}
          </div>
        )}

        {ready && adding && (
          <div className="walkup-form">
            <input
              className="filter-search"
              value={waName}
              onChange={(e) => setWaName(e.target.value)}
              placeholder="Player name"
              aria-label="Walk-up player name"
            />
            <select
              className="filter-select"
              value={waTeam}
              onChange={(e) => setWaTeam(e.target.value)}
              aria-label="Walk-up team"
            >
              <option value="">Choose team…</option>
              {teams.map((t) => (
                <option key={t} value={t}>{t}</option>
              ))}
              <option value="__new__">+ New team…</option>
            </select>
            {waTeam === '__new__' && (
              <input
                className="filter-search"
                value={waNewTeam}
                onChange={(e) => setWaNewTeam(e.target.value)}
                placeholder="New team name"
                aria-label="New team name"
              />
            )}
            <div className="controls-inline">
              <button type="button" className="btn ghost" onClick={closeAdd}>Cancel</button>
              <button type="button" className="btn" onClick={submitWalkup}>Add &amp; capture</button>
            </div>
            {waErr && <p className="result-line warn small">{waErr}</p>}
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
            {showSignups
              ? 'No sign-ups on the roster yet. Tap “Get new sign-ups” to pull them from the booking calendar.'
              : 'No roster loaded for this shoot. Upload one for this job (see README), then retry.'}
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
                      {sortBy === 'booking' && m.booked_at && (
                        <span className="roster-team">{formatSlot(m.booked_at)}</span>
                      )}
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


// "2026-10-10 13:30" -> "Sat 1:30 PM"; a date alone -> "Sat Oct 10".
function formatSlot(slot) {
  const [day, clock] = slot.split(' ');
  const d = new Date(`${day}T${clock || '00:00'}`);
  if (Number.isNaN(d.getTime())) return slot;
  const weekday = d.toLocaleDateString(undefined, { weekday: 'short' });
  if (!clock) return `${weekday} ${d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}`;
  return `${weekday} ${d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })}`;
}
