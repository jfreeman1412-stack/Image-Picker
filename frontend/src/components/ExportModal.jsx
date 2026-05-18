import { useState, useEffect, useRef } from 'react';

function fmtDuration(secs) {
  if (secs == null) return '—';
  if (secs < 60) return `${secs}s`;
  const m = Math.floor(secs / 60);
  const s = secs % 60;
  return s ? `${m}m ${s}s` : `${m}m`;
}

export default function ExportModal({ job, onClose }) {
  const [mode, setMode] = useState('copy');
  const [overwrite, setOverwrite] = useState(true);
  const [phase, setPhase] = useState('form'); // form | running | done | error
  const [status, setStatus] = useState(null); // /export-status payload
  const [error, setError] = useState(null);
  const pollRef = useRef(null);

  const expectedOutput = job.root_path.replace(/[\\/]+$/, '') + '_sorted';

  useEffect(() => () => clearInterval(pollRef.current), []);

  const poll = () => {
    pollRef.current = setInterval(async () => {
      const s = await fetch(`/api/jobs/${job.id}/export-status`).then(r => r.json());
      setStatus(s);
      if (s.status === 'done') {
        clearInterval(pollRef.current);
        setPhase('done');
      } else if (s.status === 'error') {
        clearInterval(pollRef.current);
        setError(s.error || 'Export failed.');
        setPhase('error');
      }
    }, 1000);
  };

  const submit = async () => {
    setError(null);
    const res = await fetch(`/api/jobs/${job.id}/export`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode, overwrite }),
    });
    if (!res.ok) {
      // 409 (output exists / already running), 400, etc. — synchronous.
      let msg;
      try { msg = (await res.json()).detail; } catch { msg = await res.text(); }
      setError(typeof msg === 'string' ? msg : 'Could not start export.');
      return;
    }
    const data = await res.json();
    setStatus({ status: 'exporting', progress: 0, total: data.export_total || 0 });
    setPhase('running');
    poll();
  };

  const pct = status && status.total
    ? Math.round((status.progress / status.total) * 100) : 0;
  const busy = phase === 'running';

  return (
    <div className="modal-backdrop" onClick={busy ? undefined : onClose}>
      <div className="modal-panel" onClick={e => e.stopPropagation()}>
        <h2>Export job</h2>
        <p className="muted">Output: <code>{expectedOutput}</code></p>

        {phase === 'form' && (
          <>
            <div className="form-row">
              <label>
                <input type="radio" name="mode" value="copy"
                       checked={mode === 'copy'} onChange={() => setMode('copy')} />
                Copy (originals stay)
              </label>
              <label>
                <input type="radio" name="mode" value="move"
                       checked={mode === 'move'} onChange={() => setMode('move')} />
                Move (originals are moved out)
              </label>
            </div>
            <div className="form-row">
              <label>
                <input type="checkbox" checked={overwrite}
                       onChange={e => setOverwrite(e.target.checked)} />
                Overwrite if {expectedOutput} already exists
              </label>
            </div>
            {error && <p className="error">{error}</p>}
            <div className="actions">
              <button className="ghost" onClick={onClose}>Cancel</button>
              <button onClick={submit}>Export</button>
            </div>
          </>
        )}

        {phase === 'running' && (
          <div className="ingest-progress">
            <p><b>Exporting…</b> {status?.current_team && <>copying <b>{status.current_team}</b></>}</p>
            <div className="progress-bar">
              <div className="progress-fill" style={{ width: `${pct}%` }} />
            </div>
            <p className="muted">
              {status?.progress || 0} of {status?.total || 0} files · {pct}%
              {' · '}elapsed {fmtDuration(status?.elapsed_seconds)}
              {status?.eta_seconds != null
                ? ` · ~${fmtDuration(status.eta_seconds)} left`
                : ' · estimating…'}
            </p>
            <p className="muted">Leave this open — copying over the network can take a while.</p>
          </div>
        )}

        {phase === 'done' && status?.result && (
          <>
            <p>
              <b>Done.</b> Exported {status.result.files_copied} files across{' '}
              {status.result.team_count} teams.{' '}
              {status.result.files_skipped_rejected} rejected images skipped.
            </p>
            {status.result.sessions_skipped?.length > 0 && (
              <p className="warn">
                Skipped: {status.result.sessions_skipped
                  .map(s => `${s.name} (${s.reason})`).join(', ')}
              </p>
            )}
            <div className="actions"><button onClick={onClose}>Close</button></div>
          </>
        )}

        {phase === 'error' && (
          <>
            <p className="error">Export failed: {error}</p>
            <div className="actions">
              <button className="ghost" onClick={() => { setPhase('form'); setError(null); }}>
                Back
              </button>
              <button onClick={onClose}>Close</button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
