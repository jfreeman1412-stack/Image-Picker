import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import FolderBrowser from './FolderBrowser.jsx';

/**
 * Phase C.2 — create an image-less "shoot" job.
 *
 * The image-based sort wizard ("New job") requires a folder of photos. Pre-shoot
 * there are none — but you still want the job to exist so a Player roster can be
 * attached early (the capture app shoots reference photos against it). This makes
 * just the Job row (POST /api/jobs/shoot); images are imported later from the
 * job's page. res.ok-checked; no error body lands in state.
 */
export default function NewShootModal({ onClose }) {
  const nav = useNavigate();
  const [name, setName] = useState('');
  const [rootPath, setRootPath] = useState('');
  const [showFolderPicker, setShowFolderPicker] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const nameRef = useRef(null);

  useEffect(() => {
    nameRef.current?.focus();
    const onKey = (e) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const create = async () => {
    if (!name.trim()) { setError('Give the shoot a name.'); return; }
    setBusy(true);
    setError(null);
    try {
      const res = await fetch('/api/jobs/shoot', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: name.trim(), root_path: rootPath.trim() || null }),
      });
      if (!res.ok) throw new Error(`Couldn't create the shoot (HTTP ${res.status}).`);
      const { job_id } = await res.json();
      nav(`/job/${job_id}`);
    } catch (e) {
      setError(e.message || String(e));
      setBusy(false);
    }
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal-panel"
        onClick={(e) => e.stopPropagation()}
        style={{ maxWidth: 520, width: 'min(520px, 92vw)' }}
      >
        <header className="row-between" style={{ marginBottom: 12 }}>
          <h2 style={{ margin: 0 }}>New shoot</h2>
          <button className="ghost" onClick={onClose} aria-label="Close">×</button>
        </header>

        <p className="muted" style={{ marginTop: 0 }}>
          Creates a job with no images yet — attach a <b>Player roster</b> now and
          import the photos later, once the shoot is done.
        </p>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <label style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            <span className="muted">Shoot name</span>
            <input
              ref={nameRef}
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. Princeton Spring 2026"
              onKeyDown={(e) => { if (e.key === 'Enter' && name.trim() && !busy) create(); }}
            />
          </label>
          <label style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            <span className="muted">Image folder (optional — you can set it at import time)</span>
            <div className="actions" style={{ gap: 8 }}>
              <input
                value={rootPath}
                onChange={(e) => setRootPath(e.target.value)}
                placeholder="Leave blank for now"
                style={{ flex: 1 }}
              />
              <button onClick={() => setShowFolderPicker(true)}>Browse…</button>
            </div>
          </label>
        </div>

        {error && <p className="error" style={{ marginTop: 8 }}>{error}</p>}

        <div className="actions" style={{ justifyContent: 'flex-end', marginTop: 14 }}>
          <button onClick={onClose}>Cancel</button>
          <button className="primary" disabled={!name.trim() || busy} onClick={create}>
            {busy ? 'Creating…' : 'Create shoot'}
          </button>
        </div>

        {showFolderPicker && (
          <FolderBrowser
            title="Pick the (future) image folder"
            initialPath={rootPath || ''}
            onPick={(p) => setRootPath(p)}
            onClose={() => setShowFolderPicker(false)}
          />
        )}
      </div>
    </div>
  );
}
