// The cold-start screen: choose the shoot to check in. Online it fetches the
// capture-ready shoots (GET /api/jobs?stage=capture) and caches the snapshot;
// offline it lists the cached snapshot so a shoot can still be opened.
//
// Phase B.3 adds offline readiness (§3): each shoot shows "Ready offline ✓" once
// its roster is cached, and a "Download for offline" action lets the volunteer
// PRE-STAGE shoots at the office before going off-grid — the whole anchor
// workflow. A shoot's roster is also auto-cached when it's opened (in App).
// See ../PHASE_B3_OFFLINE_CAPTURE.md.
import { useEffect, useState } from 'react';
import {
  getShoots, putShoots, getCachedRosterJobIds, putRoster,
} from './db.js';

export default function ShootPicker({ onPick }) {
  const [jobs, setJobs] = useState([]);
  const [source, setSource] = useState('network');   // network | cache
  const [status, setStatus] = useState('loading');   // loading | ready | error
  const [error, setError] = useState(null);
  const [cachedIds, setCachedIds] = useState(new Set());
  const [downloading, setDownloading] = useState(new Set());
  const [errors, setErrors] = useState({});           // jobId → message

  const load = async () => {
    setStatus('loading');
    setError(null);
    let cached = [];
    try { cached = await getShoots(); } catch { /* no cache yet */ }
    try {
      const res = await fetch('/api/jobs?stage=capture');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const list = Array.isArray(data) ? data.map((j) => ({ id: j.id, name: j.name })) : [];
      await putShoots(list);
      setJobs(list);
      setSource('network');
    } catch {
      if (cached.length) {
        setJobs(cached.map((s) => ({ id: s.id, name: s.name })));
        setSource('cache');
      } else {
        setError('Couldn’t reach the server and no shoots are cached. Connect, then retry.');
        setStatus('error');
        return;
      }
    }
    try { setCachedIds(await getCachedRosterJobIds()); } catch { /* ignore */ }
    setStatus('ready');
  };
  useEffect(() => { load(); }, []);

  // Pre-stage a shoot for offline use without entering it (cache roster + status).
  const download = async (job) => {
    setDownloading((prev) => new Set(prev).add(job.id));
    setErrors((prev) => { const n = { ...prev }; delete n[job.id]; return n; });
    try {
      const [r1, r2] = await Promise.all([
        fetch(`/api/players/roster/${job.id}`),
        fetch(`/api/players/roster/${job.id}/reference-status`),
      ]);
      if (!r1.ok || !r2.ok) throw new Error();
      const rb = await r1.json();
      const sb = await r2.json();
      await putRoster({
        jobId: job.id,
        name: job.name,
        items: rb.items || [],
        statusIds: sb.player_ids_with_references || [],
      });
      setCachedIds((prev) => new Set(prev).add(job.id));
    } catch {
      setErrors((prev) => ({ ...prev, [job.id]: 'Download failed — connect and try again.' }));
    } finally {
      setDownloading((prev) => { const n = new Set(prev); n.delete(job.id); return n; });
    }
  };

  const online = source === 'network';

  return (
    <main className="screen picker-screen">
      <div className="picker-card">
        <h1 className="picker-title">Reference capture</h1>
        <p className="picker-sub">Choose the shoot you’re checking in.</p>

        {!online && status === 'ready' && (
          <p className="offline-line">⚠︎ Offline — showing cached shoots.</p>
        )}

        {status === 'loading' && <p className="muted">Loading shoots…</p>}

        {status === 'error' && (
          <div className="picker-block">
            <p className="result-line warn">{error}</p>
            <button className="btn" onClick={load}>Retry</button>
          </div>
        )}

        {status === 'ready' && jobs.length === 0 && (
          <p className="muted">
            No shoots ready for capture. In the editor app, create a shoot and
            load its roster — shoots appear here until their photos are imported.
          </p>
        )}

        {status === 'ready' && jobs.length > 0 && (
          <ul className="shoot-list">
            {jobs.map((job) => {
              const cached = cachedIds.has(job.id);
              const busy = downloading.has(job.id);
              return (
                <li key={job.id} className="shoot-row">
                  <div className="shoot-row-top">
                    <button className="shoot-pick" onClick={() => onPick(job)}>
                      <span className="shoot-name">{job.name}</span>
                    </button>
                    <div className="shoot-action">
                      {cached ? (
                        <span className="ready-badge" title="Roster cached — ready offline">
                          Ready offline ✓
                        </span>
                      ) : busy ? (
                        <span className="muted small">Downloading…</span>
                      ) : online ? (
                        <button className="link-download" onClick={() => download(job)}>
                          Download for offline
                        </button>
                      ) : (
                        <span className="muted small">Not downloaded</span>
                      )}
                    </div>
                  </div>
                  {errors[job.id] && <p className="result-line warn small">{errors[job.id]}</p>}
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </main>
  );
}
