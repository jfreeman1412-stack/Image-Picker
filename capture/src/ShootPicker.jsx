// Section 2 — shoot picker. The cold-start screen: fetch the list of shoots
// from GET /api/jobs (a bare array, archived excluded by default) and let the
// volunteer choose one. Picking a shoot hands {id, name} up to App, which
// advances to the roster view. See ../PHASE_B2_ROSTER_CAPTURE.md.
import { useEffect, useState } from 'react';

export default function ShootPicker({ onPick }) {
  const [jobs, setJobs] = useState([]);
  const [status, setStatus] = useState('loading'); // loading | ready | error
  const [error, setError] = useState(null);

  const load = async () => {
    setStatus('loading');
    setError(null);
    try {
      const res = await fetch('/api/jobs');
      if (!res.ok) throw new Error(`Couldn’t load shoots (HTTP ${res.status}).`);
      const data = await res.json();
      setJobs(Array.isArray(data) ? data : []);
      setStatus('ready');
    } catch {
      setError('Couldn’t reach the server. Check the connection and the backend, then retry.');
      setStatus('error');
    }
  };
  useEffect(() => { load(); }, []);

  return (
    <main className="screen picker-screen">
      <div className="picker-card">
        <h1 className="picker-title">Reference capture</h1>
        <p className="picker-sub">Choose the shoot you’re checking in.</p>

        {status === 'loading' && <p className="muted">Loading shoots…</p>}

        {status === 'error' && (
          <div className="picker-block">
            <p className="result-line warn">{error}</p>
            <button className="btn" onClick={load}>Retry</button>
          </div>
        )}

        {status === 'ready' && jobs.length === 0 && (
          <p className="muted">
            No shoots found. Create one in the editor app and load a roster, then reload.
          </p>
        )}

        {status === 'ready' && jobs.length > 0 && (
          <select
            className="picker-select"
            defaultValue=""
            onChange={(e) => {
              const job = jobs.find((j) => String(j.id) === e.target.value);
              if (job) onPick(job);
            }}
          >
            <option value="" disabled>Choose a shoot…</option>
            {jobs.map((j) => (
              <option key={j.id} value={j.id}>{j.name}</option>
            ))}
          </select>
        )}
      </div>
    </main>
  );
}
