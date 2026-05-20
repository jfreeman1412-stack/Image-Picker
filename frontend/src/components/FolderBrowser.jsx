import { useEffect, useState } from 'react';

/**
 * Reusable folder picker. Wraps GET /api/browse with breadcrumb + clickable
 * subdirectory list + editable path input. Used by the new job wizard
 * (Section 3) and the export modal (Section 2). Resilient-fetch pattern
 * matches Phase 5: every fetch checks res.ok and never lets an error body
 * land in component state.
 */

function joinPath(parent, child) {
  if (!parent) return child;
  const sep = parent.includes('\\') || parent.startsWith('\\\\') ? '\\' : '/';
  if (parent.endsWith('\\') || parent.endsWith('/')) return parent + child;
  return parent + sep + child;
}

/** Break an absolute Windows / UNC path into clickable breadcrumb segments. */
function pathSegments(path) {
  if (!path) return [];
  const out = [];
  let remaining = path;
  // UNC root \\server\share kept as one unit.
  if (remaining.startsWith('\\\\')) {
    const m = remaining.match(/^\\\\([^\\]+)\\([^\\]+)/);
    if (m) {
      const root = '\\\\' + m[1] + '\\' + m[2];
      out.push({ label: root, full: root });
      remaining = remaining.slice(m[0].length);
    }
  } else if (remaining.match(/^[A-Za-z]:[\\/]/)) {
    const root = remaining.slice(0, 3);                  // "C:\"
    out.push({ label: root, full: root });
    remaining = remaining.slice(3);
  }
  const parts = remaining.split(/[\\/]+/).filter(Boolean);
  let acc = out[0]?.full ?? '';
  for (const p of parts) {
    acc = acc.endsWith('\\') ? acc + p : acc + '\\' + p;
    out.push({ label: p, full: acc });
  }
  return out;
}

export default function FolderBrowser({
  initialPath = '',
  title = 'Choose folder',
  onPick,
  onClose,
}) {
  const [currentPath, setCurrentPath] = useState(initialPath);
  const [entries, setEntries] = useState([]);
  const [parent, setParent] = useState(null);
  const [exists, setExists] = useState(true);
  const [editing, setEditing] = useState(initialPath);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const load = async (path) => {
    setLoading(true);
    setError(null);
    try {
      const url = `/api/browse?path=${encodeURIComponent(path || '')}`;
      const res = await fetch(url);
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body?.detail || `HTTP ${res.status}`);
      }
      const body = await res.json();
      setCurrentPath(body.path);
      setEditing(body.path);
      setEntries(Array.isArray(body.entries) ? body.entries : []);
      setParent(body.parent);
      setExists(!!body.exists);
    } catch (e) {
      setError(e.message || String(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(initialPath); }, [initialPath]);

  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const drillInto = (name) => load(joinPath(currentPath, name));
  const goUp = () => { if (parent != null) load(parent); };
  const usePath = () => {
    if (!currentPath) return;     // synthetic-root view (drive letters) — not pickable
    onPick(currentPath);
    onClose();
  };

  const segments = pathSegments(currentPath);

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal-panel"
        onClick={(e) => e.stopPropagation()}
        style={{ width: 'min(720px, 92vw)', maxHeight: '88vh', display: 'flex', flexDirection: 'column' }}
      >
        <header className="row-between" style={{ marginBottom: 12 }}>
          <h2 style={{ margin: 0 }}>{title}</h2>
          <button className="ghost" onClick={onClose} aria-label="Close">×</button>
        </header>

        {/* Breadcrumb: clickable parents (only when we have a real path) */}
        {currentPath && (
          <div className="folder-crumbs">
            {segments.map((s, i) => (
              <span key={i}>
                <button
                  className="folder-crumb"
                  onClick={() => load(s.full)}
                  title={`Go to ${s.full}`}
                >
                  {s.label}
                </button>
                {i < segments.length - 1 && <span className="folder-sep">{'\\'}</span>}
              </span>
            ))}
          </div>
        )}

        {/* Editable path input + Up */}
        <div className="actions" style={{ gap: 8, marginBottom: 8 }}>
          <input
            type="text"
            value={editing}
            onChange={(e) => setEditing(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') load(editing); }}
            placeholder="Type or paste a path"
            style={{ flex: 1 }}
          />
          <button onClick={() => load(editing)} disabled={loading}>Go</button>
          <button onClick={goUp} disabled={parent == null || loading}>↑ Up</button>
        </div>

        {/* Listing */}
        <div
          className="folder-list"
          style={{
            flex: 1, minHeight: 200, overflowY: 'auto',
            border: '1px solid var(--border)', borderRadius: 'var(--radius)',
            background: 'var(--bg-elev-2)', padding: 6,
          }}
        >
          {loading && <div className="muted" style={{ padding: 8 }}>Loading…</div>}
          {!loading && error && (
            <div className="muted" style={{ padding: 8, color: 'var(--danger)' }}>
              {error}
            </div>
          )}
          {!loading && !error && !exists && (
            <div className="muted" style={{ padding: 8 }}>
              Path doesn't exist. Use ↑ Up or type a different path.
            </div>
          )}
          {!loading && !error && exists && entries.length === 0 && (
            <div className="muted" style={{ padding: 8 }}>
              (no subfolders)
            </div>
          )}
          {!loading && !error && entries.map((e) => (
            <button
              key={e.name}
              className="folder-entry"
              onDoubleClick={() => drillInto(e.name)}
              onClick={() => drillInto(e.name)}
              title="Open"
            >
              📁 {e.name}
            </button>
          ))}
        </div>

        <footer className="actions" style={{ justifyContent: 'space-between', marginTop: 12 }}>
          <span className="muted" style={{ fontSize: 12 }}>
            {currentPath || 'Pick a drive above, then drill into folders.'}
          </span>
          <div className="actions" style={{ gap: 8 }}>
            <button onClick={onClose}>Cancel</button>
            <button
              className="primary"
              onClick={usePath}
              disabled={!currentPath || !exists}
              title="Use this folder"
            >
              Use this folder
            </button>
          </div>
        </footer>
      </div>
    </div>
  );
}
