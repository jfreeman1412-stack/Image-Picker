import { useEffect, useState } from 'react';

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
async function api(url, opts) {
  const res = await fetch(url, opts);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const d = body.detail;
    throw new Error((d && (d.message || d)) || `Request failed (${res.status})`);
  }
  return body;
}

const jsonOpts = (method, body) => ({
  method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
});

function ConnectionForm({ onSaved }) {
  const [cfg, setCfg] = useState({ host: '', port: 3306, user: '', password: '', database: 'sportsline' });
  const [passwordSet, setPasswordSet] = useState(false);
  const [msg, setMsg] = useState(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api('/api/sytist/settings').then(c => {
      setCfg({ host: c.host, port: c.port, user: c.user, password: '', database: c.database });
      setPasswordSet(c.password_set);
    }).catch(e => setMsg(e.message));
  }, []);

  const save = async () => {
    setBusy(true); setMsg(null);
    try {
      await api('/api/sytist/settings', jsonOpts('PUT', { ...cfg, port: Number(cfg.port) || 3306 }));
      await api('/api/sytist/test', { method: 'POST' });
      setMsg('Connected.');
      onSaved();
    } catch (e) {
      setMsg(e.message);
    } finally {
      setBusy(false);
    }
  };

  const field = (key, label, type = 'text') => (
    <label style={{ display: 'flex', flexDirection: 'column', fontSize: 13 }}>
      {label}
      <input type={type} value={cfg[key]} onChange={e => setCfg({ ...cfg, [key]: e.target.value })}
             placeholder={key === 'password' && passwordSet ? '(saved)' : ''} />
    </label>
  );

  return (
    <section style={{ marginBottom: 16 }}>
      <p className="muted" style={{ marginTop: 0 }}>
        Sytist database login (read-only). Same details as the production dashboard.
      </p>
      <div style={{ display: 'grid', gridTemplateColumns: '2fr 1fr', gap: 8 }}>
        {field('host', 'Host')}
        {field('port', 'Port')}
        {field('user', 'User')}
        {field('password', 'Password', 'password')}
        {field('database', 'Database')}
      </div>
      <div className="actions" style={{ marginTop: 8 }}>
        <button onClick={save} disabled={busy || !cfg.host || !cfg.user}>
          {busy ? 'Connecting…' : 'Save and test'}
        </button>
        {msg && <span className={msg === 'Connected.' ? 'muted' : 'error'}>{msg}</span>}
      </div>
    </section>
  );
}

export default function SytistFamiliesModal({ job, onClose }) {
  const [configured, setConfigured] = useState(null);
  const [showConnection, setShowConnection] = useState(false);
  const [events, setEvents] = useState([]);
  const [galleryQuery, setGalleryQuery] = useState('');
  const [galleries, setGalleries] = useState([]);
  const [sources, setSources] = useState({ booking_event_ids: [], gallery_ids: [] });
  const [state, setState] = useState(null);       // GET /jobs/{id}
  const [syncResult, setSyncResult] = useState(null);
  const [teams, setTeams] = useState([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [addTeam, setAddTeam] = useState({});     // family id -> team
  const [addName, setAddName] = useState({});     // family id -> typed name

  const loadState = () => api(`/api/sytist/jobs/${job.id}`).then(s => {
    setState(s);
    setSources(s.sources);
    return s;
  });

  const loadSytistLists = () => {
    api('/api/sytist/booking-events').then(r => setEvents(r.items)).catch(e => setError(e.message));
    api(`/api/sytist/galleries?q=${encodeURIComponent(galleryQuery)}`)
      .then(r => setGalleries(r.items)).catch(() => {});
  };

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
    api('/api/sytist/settings').then(c => {
      setConfigured(c.configured);
      setShowConnection(!c.configured);
      if (c.configured) loadSytistLists();
    }).catch(e => setError(e.message));
    // Opening this pulls new sign-ups when the job is already linked.
    loadState().then(s => {
      if (s.sources.booking_event_ids.length || s.sources.gallery_ids.length) sync();
    }).catch(e => setError(e.message));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job.id]);

  const toggle = (key, id) => {
    const cur = new Set(sources[key]);
    cur.has(id) ? cur.delete(id) : cur.add(id);
    setSources({ ...sources, [key]: [...cur] });
  };

  const saveSourcesAndSync = async () => {
    setError(null);
    try {
      await api(`/api/sytist/jobs/${job.id}/sources`, jsonOpts('PUT', sources));
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

  // Linked galleries stay visible even when the search doesn't list them.
  const galleryRows = [
    ...sources.gallery_ids.filter(id => !galleries.some(g => g.id === id))
      .map(id => ({ id, title: `Gallery ${id}`, registrations: null })),
    ...galleries,
  ];
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

        {configured !== null && (
          <p style={{ marginTop: 0 }}>
            <button className="ghost" onClick={() => setShowConnection(v => !v)}>
              {showConnection ? 'Hide Sytist login' : 'Sytist login…'}
            </button>
          </p>
        )}
        {showConnection && (
          <ConnectionForm onSaved={() => { setConfigured(true); setShowConnection(false); loadSytistLists(); }} />
        )}

        {configured && (
          <section style={{ marginBottom: 16 }}>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
              <div>
                <h3 style={{ margin: '0 0 6px' }}>Booking calendar</h3>
                <div style={{ maxHeight: 220, overflowY: 'auto' }}>
                  {events.map(ev => (
                    <label key={ev.id} style={{ display: 'block', fontSize: 13 }}>
                      <input type="checkbox" checked={sources.booking_event_ids.includes(ev.id)}
                             onChange={() => toggle('booking_event_ids', ev.id)} />
                      {' '}{ev.title} <span className="muted">{ev.date} · {ev.bookings} booked</span>
                    </label>
                  ))}
                  {events.length === 0 && <p className="muted">No booking events found.</p>}
                </div>
              </div>
              <div>
                <h3 style={{ margin: '0 0 6px' }}>Gallery pre-registration</h3>
                <div className="actions" style={{ gap: 6, marginBottom: 6 }}>
                  <input value={galleryQuery} placeholder="Search galleries"
                         onChange={e => setGalleryQuery(e.target.value)}
                         onKeyDown={e => { if (e.key === 'Enter') loadSytistLists(); }} />
                  <button className="ghost" onClick={loadSytistLists}>Search</button>
                </div>
                <div style={{ maxHeight: 190, overflowY: 'auto' }}>
                  {galleryRows.map(g => (
                    <label key={g.id} style={{ display: 'block', fontSize: 13 }}>
                      <input type="checkbox" checked={sources.gallery_ids.includes(g.id)}
                             onChange={() => toggle('gallery_ids', g.id)} />
                      {' '}{g.title}
                      {g.registrations != null && <span className="muted"> · {g.registrations} registered</span>}
                    </label>
                  ))}
                </div>
              </div>
            </div>
            <div className="actions" style={{ marginTop: 10 }}>
              <button onClick={saveSourcesAndSync} disabled={busy}>
                {busy ? 'Syncing…' : 'Save and sync now'}
              </button>
            </div>
          </section>
        )}

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
                  {' '}{syncResult.removed} removed.
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
