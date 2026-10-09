/**
 * 2026-10-09 — shared Sytist pieces: the read-only login form, the picker
 * for where a job's families come from (booking-calendar events / days and
 * gallery pre-registrations), and `startSytistRoster`, which turns a job
 * into a booking-calendar roster job (passcodes on, sign-ups added to the
 * roster on every sync). Used by the new-job wizard, New shoot and the
 * Sytist families screen.
 */
import { useEffect, useState } from 'react';

export async function api(url, opts) {
  const res = await fetch(url, opts);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const d = body.detail;
    throw new Error((d && (d.message || d)) || `Request failed (${res.status})`);
  }
  return body;
}

export const jsonOpts = (method, body) => ({
  method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
});

export function ConnectionForm({ onSaved }) {
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


export const emptySources = () => ({ booking_event_ids: [], booking_dates: [], gallery_ids: [] });

export const hasAnySource = (s) =>
  !!(s && (s.booking_event_ids.length || s.booking_dates.length || s.gallery_ids.length));

/** Passcodes on, link the sources with auto-add, then pull the sign-ups. */
export async function startSytistRoster(jobId, sources) {
  await api(`/api/jobs/${jobId}/sytist-passcodes`, jsonOpts('POST', { enabled: true }));
  await api(`/api/sytist/jobs/${jobId}/sources`, jsonOpts('PUT', { ...sources, auto_add: true }));
  return api(`/api/sytist/jobs/${jobId}/sync`, { method: 'POST' });
}

// "2026-10-18" → "Sun, Oct 18" (dates only, no time-zone shift).
const fmtDay = (iso) => {
  const [y, m, d] = String(iso || '').split('-').map(Number);
  if (!y || !m || !d) return iso || '';
  return new Date(y, m - 1, d).toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' });
};
const fmtRange = (a, b) => (a && b && a !== b ? `${fmtDay(a)} – ${fmtDay(b)}` : fmtDay(a));

/**
 * Pick booking-calendar events / days and galleries. `value` is
 * { booking_event_ids, booking_dates, gallery_ids }; `onChange` gets the
 * next value. Shows the login form first when Sytist isn't set up yet.
 */
export function SytistSourcePicker({ value, onChange, showGalleries = true }) {
  const [configured, setConfigured] = useState(null);
  const [events, setEvents] = useState([]);
  const [galleryQuery, setGalleryQuery] = useState('');
  const [galleries, setGalleries] = useState([]);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(false);
  const [showLogin, setShowLogin] = useState(false);

  const loadLists = () => {
    setLoading(true);
    setError(null);
    api('/api/sytist/booking-events').then(r => setEvents(r.items))
      .catch(e => setError(e.message)).finally(() => setLoading(false));
    if (showGalleries) {
      api(`/api/sytist/galleries?q=${encodeURIComponent(galleryQuery)}`)
        .then(r => setGalleries(r.items)).catch(() => {});
    }
  };

  useEffect(() => {
    api('/api/sytist/settings').then(c => {
      setConfigured(c.configured);
      if (c.configured) loadLists();
    }).catch(e => setError(e.message));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const toggle = (key, id) => {
    const cur = new Set(value[key]);
    cur.has(id) ? cur.delete(id) : cur.add(id);
    onChange({ ...value, [key]: [...cur] });
  };

  if (configured === null) return <p className="muted">Connecting to Sytist…</p>;
  if (!configured || showLogin) {
    return <ConnectionForm onSaved={() => { setConfigured(true); setShowLogin(false); loadLists(); }} />;
  }

  // A job's already-linked sessions stay visible even after they age out of
  // the recent list (the server only lists the last few days onward).
  const eventRows = [
    ...value.booking_event_ids.filter(id => !events.some(e => e.kind === 'event' && e.id === id))
      .map(id => ({ kind: 'event', id, title: `Booking session ${id}`, first_date: '', last_date: '', bookings: null })),
    ...value.booking_dates.filter(d => !events.some(e => e.kind === 'day' && e.id === d))
      .map(d => ({ kind: 'day', id: d, title: 'Booking calendar', first_date: d, last_date: d, bookings: null })),
    ...events,
  ];

  const galleryRows = [
    ...value.gallery_ids.filter(id => !galleries.some(g => g.id === id))
      .map(id => ({ id, title: `Gallery ${id}`, registrations: null })),
    ...galleries,
  ];

  return (
    <div style={{ display: 'grid', gridTemplateColumns: showGalleries ? '1fr 1fr' : '1fr', gap: 16 }}>
      <div>
        <h3 style={{ margin: '0 0 6px' }}>Booking calendar</h3>
        <p className="muted" style={{ margin: '0 0 6px', fontSize: 12 }}>
          Upcoming and the last 3 days, soonest first.
        </p>
        <div style={{ maxHeight: 280, overflowY: 'auto' }}>
          {eventRows.map(ev => {
            const key = ev.kind === 'event' ? 'booking_event_ids' : 'booking_dates';
            const when = fmtRange(ev.first_date, ev.last_date);
            return (
              <label key={`${ev.kind}-${ev.id}`}
                     style={{ display: 'flex', gap: 8, alignItems: 'flex-start', fontSize: 13,
                              padding: '6px 2px', borderBottom: '1px solid rgba(127,127,127,0.25)' }}>
                <input type="checkbox" checked={value[key].includes(ev.id)}
                       onChange={() => toggle(key, ev.id)} style={{ marginTop: 3 }} />
                <span style={{ display: 'flex', flexDirection: 'column', minWidth: 0 }}>
                  <span>{ev.title}</span>
                  <span className="muted" style={{ fontSize: 12 }}>
                    {[when, ev.bookings != null ? `${ev.bookings} booked` : null].filter(Boolean).join(' · ')}
                  </span>
                </span>
              </label>
            );
          })}
          {!loading && eventRows.length === 0 && !error && <p className="muted">No upcoming or recent bookings.</p>}
          {loading && <p className="muted">Loading…</p>}
        </div>
      </div>
      {showGalleries && (
        <div>
          <h3 style={{ margin: '0 0 6px' }}>Gallery pre-registration</h3>
          <div className="actions" style={{ gap: 6, marginBottom: 6 }}>
            <input value={galleryQuery} placeholder="Search galleries"
                   onChange={e => setGalleryQuery(e.target.value)}
                   onKeyDown={e => { if (e.key === 'Enter') loadLists(); }} />
            <button className="ghost" onClick={loadLists}>Search</button>
          </div>
          <div style={{ maxHeight: 210, overflowY: 'auto' }}>
            {galleryRows.map(g => (
              <label key={g.id} style={{ display: 'block', fontSize: 13 }}>
                <input type="checkbox" checked={value.gallery_ids.includes(g.id)}
                       onChange={() => toggle('gallery_ids', g.id)} />
                {' '}{g.title}
                {g.registrations != null && <span className="muted"> · {g.registrations} registered</span>}
              </label>
            ))}
          </div>
        </div>
      )}
      {error && (
        <p className="error" style={{ gridColumn: '1 / -1' }}>
          {error}{' '}
          <button className="ghost" onClick={() => setShowLogin(true)}>Sytist login…</button>
        </p>
      )}
    </div>
  );
}
