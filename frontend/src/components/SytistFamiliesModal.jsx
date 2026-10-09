import { useEffect, useState } from 'react';
import {
  ConnectionForm, SytistSourcePicker, api, emptySources, jsonOpts,
} from './SytistSources.jsx';

/**
 * 2026-10-08 — Sytist families for a passcode job.
 *
 * Link the job to Sytist booking-calendar photo days and/or galleries (their
 * pre-registrations), then Sync pulls every family in (read-only). Families
 * are matched to roster players by name and fill in the parent's name, email
 * and phone. Sign-ups don't carry a team, so unmatched families are listed
 * here to be put on a team. Export also syncs first, so new sign-ups are
 * picked up without opening this.
 */
export default function SytistFamiliesModal({ job, onClose }) {
  const [showConnection, setShowConnection] = useState(false);
  const [sources, setSources] = useState(emptySources());
  const [autoAdd, setAutoAdd] = useState(false);
  const [state, setState] = useState(null);       // GET /jobs/{id}
  const [syncResult, setSyncResult] = useState(null);
  const [teams, setTeams] = useState([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [addTeam, setAddTeam] = useState({});     // family id -> team
  const [addName, setAddName] = useState({});     // family id -> typed name

  const loadState = () => api(`/api/sytist/jobs/${job.id}`).then(s => {
    setState(s);
    const { auto_add: aa, ...src } = s.sources;
    setSources(src);
    setAutoAdd(aa);
    return s;
  });

  const sync = async () => {
    setBusy(true); setError(null);
    try {
      setSyncResult(await api(`/api/sytist/jobs/${job.id}/sync`, { method: 'POST' }));
      await loadState();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  useEffect(() => {
    api(`/api/players/roster/${job.id}`).then(r => {
      setTeams([...new Set(r.items.map(i => i.team))].sort());
    }).catch(() => {});
    // Opening this pulls new sign-ups when the job is already linked.
    loadState().then(s => {
      const src = s.sources;
      if (src.booking_event_ids.length || src.booking_dates.length || src.gallery_ids.length) sync();
    }).catch(e => setError(e.message));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job.id]);

  const saveSourcesAndSync = async () => {
    setError(null);
    try {
      await api(`/api/sytist/jobs/${job.id}/sources`,
                jsonOpts('PUT', { ...sources, auto_add: autoAdd }));
      await sync();
    } catch (e) {
      setError(e.message);
    }
  };

  const addFamily = async (fam) => {
    setError(null);
    try {
      const body = { team: addTeam[fam.id] || '' };
      if (addName[fam.id]) body.name = addName[fam.id];
      await api(`/api/sytist/jobs/${job.id}/families/${fam.id}/add`, jsonOpts('POST', body));
      await loadState();
    } catch (e) {
      setError(e.message);
    }
  };

  const unmatched = state?.unmatched || [];

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-panel" onClick={(e) => e.stopPropagation()}
           style={{ maxWidth: 820, width: 'min(820px, 94vw)' }}>
        <header className="row-between" style={{ marginBottom: 12 }}>
          <h2 style={{ margin: 0 }}>Sytist families</h2>
          <button className="ghost" onClick={onClose} aria-label="Close">×</button>
        </header>

        <p className="muted" style={{ marginTop: 0 }}>
          Pull parents' names, emails and phones from Sytist bookings and gallery
          pre-registrations. Families are matched to roster players by name.
          Export syncs again first, so later sign-ups are picked up.
        </p>

        <p style={{ marginTop: 0 }}>
          <button className="ghost" onClick={() => setShowConnection(v => !v)}>
            {showConnection ? 'Hide Sytist login' : 'Sytist login…'}
          </button>
        </p>
        {showConnection && <ConnectionForm onSaved={() => setShowConnection(false)} />}

        <section style={{ marginBottom: 16 }}>
          <SytistSourcePicker value={sources} onChange={setSources} />
          <label style={{ display: 'block', marginTop: 10, fontSize: 13 }}>
            <input type="checkbox" checked={autoAdd} onChange={e => setAutoAdd(e.target.checked)} />
            {' '}Add sign-ups that aren't on the roster automatically (no team; the
            team comes from the folder their photos are sorted into)
          </label>
          <div className="actions" style={{ marginTop: 10 }}>
            <button onClick={saveSourcesAndSync} disabled={busy}>
              {busy ? 'Syncing…' : 'Save and sync now'}
            </button>
          </div>
        </section>

        {error && <p className="error">{error}</p>}

        {state && (
          <section>
            <p>
              <b>{state.families}</b> sign-ups synced
              {state.last_synced_at && <> (last {new Date(state.last_synced_at + 'Z').toLocaleString()})</>}.
              {' '}<b>{state.matched_players}</b> matched to roster players.
              {syncResult && (
                <span className="muted">
                  {' '}This sync: {syncResult.added} new, {syncResult.updated} changed,
                  {' '}{syncResult.removed} removed
                  {syncResult.roster_added > 0 && `, ${syncResult.roster_added} added to the roster`}.
                </span>
              )}
            </p>
            {unmatched.length > 0 && (
              <>
                <h3 style={{ margin: '12px 0 6px' }}>Not on the roster ({unmatched.length})</h3>
                <p className="muted" style={{ marginTop: 0, fontSize: 13 }}>
                  Pick a team to add them, or leave them if they're on the roster
                  under a different spelling (fix the name and re-sync).
                </p>
                <table className="roster-preview" style={{ width: '100%' }}>
                  <thead>
                    <tr><th align="left">Player</th><th align="left">Parent</th><th align="left">Email</th><th /></tr>
                  </thead>
                  <tbody>
                    {unmatched.map(f => (
                      <tr key={f.id}>
                        <td>
                          {f.player || (
                            <input placeholder="Player name" value={addName[f.id] || ''}
                                   onChange={e => setAddName({ ...addName, [f.id]: e.target.value })} />
                          )}
                          {f.other_signups > 0 && <span className="muted"> (+{f.other_signups})</span>}
                        </td>
                        <td>{f.parent}</td>
                        <td className="muted">{f.parent_email}</td>
                        <td style={{ whiteSpace: 'nowrap' }}>
                          <input list={`teams-${job.id}`} placeholder="Team" style={{ width: 140 }}
                                 value={addTeam[f.id] || ''}
                                 onChange={e => setAddTeam({ ...addTeam, [f.id]: e.target.value })} />
                          {' '}
                          <button className="ghost" disabled={!addTeam[f.id]} onClick={() => addFamily(f)}>
                            Add
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <datalist id={`teams-${job.id}`}>
                  {teams.map(t => <option key={t} value={t} />)}
                </datalist>
              </>
            )}
          </section>
        )}

        <div className="actions" style={{ marginTop: 16 }}>
          <button onClick={onClose}>Close</button>
        </div>
      </div>
    </div>
  );
}
