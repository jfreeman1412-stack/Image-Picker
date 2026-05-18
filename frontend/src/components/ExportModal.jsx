import { useState } from 'react';

export default function ExportModal({ job, onClose }) {
  const [mode, setMode] = useState('copy');
  const [overwrite, setOverwrite] = useState(true);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);

  const expectedOutput = job.root_path.replace(/[\\/]+$/, '') + '_sorted';

  const submit = async () => {
    setBusy(true); setError(null);
    try {
      const res = await fetch(`/api/jobs/${job.id}/export`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode, overwrite }),
      });
      if (!res.ok) throw new Error(await res.text());
      setResult(await res.json());
    } catch (e) { setError(String(e.message || e)); }
    finally { setBusy(false); }
  };

  return (
    <div className="modal-backdrop" onClick={busy ? undefined : onClose}>
      <div className="modal-panel" onClick={e => e.stopPropagation()}>
        <h2>Export job</h2>
        <p className="muted">Output: <code>{expectedOutput}</code></p>

        {result ? (
          <>
            <p>
              <b>Done.</b> Exported {result.files_copied} files across{' '}
              {result.team_count} teams. {result.files_skipped_rejected} rejected
              images skipped.
            </p>
            {result.sessions_skipped?.length > 0 && (
              <p className="warn">
                Sessions skipped (pipeline not complete):{' '}
                {result.sessions_skipped.map(s => s.name).join(', ')}
              </p>
            )}
            <div className="actions"><button onClick={onClose}>Close</button></div>
          </>
        ) : (
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
              <button className="ghost" onClick={onClose} disabled={busy}>Cancel</button>
              <button onClick={submit} disabled={busy}>
                {busy ? 'Exporting…' : 'Export'}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
