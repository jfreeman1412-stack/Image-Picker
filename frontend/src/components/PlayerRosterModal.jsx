import { useEffect, useRef, useState } from 'react';

/**
 * Phase C.1 — Player roster upload (column mapping).
 *
 * Distinct from RosterModal: that uploads the Phase 6 *cross-check* roster
 * (`/api/jobs/{id}/roster`, flags wrong-team clusters). THIS uploads the A.1
 * *player* roster (`/api/players/roster/{id}`) — the identity spine the
 * reference-photo / capture system reads. Rosters arrive in many column
 * layouts, so the operator maps columns to the canonical fields (name + team),
 * the app validates strictly, and on success the roster is written for the job.
 *
 * Flow (built across C.1 Sections 4–7): pick CSV → inspect columns → map →
 * validate (dry-run, named-row errors) → upload (replace, guarded if photos
 * were already captured for this shoot).
 *
 * Every fetch checks `res.ok`; no error body ever lands in state (the Phase 5
 * lesson the rest of the app follows).
 */
export default function PlayerRosterModal({ job, onClose }) {
  const fileRef = useRef(null);
  const [file, setFile] = useState(null);
  const [inspecting, setInspecting] = useState(false);
  const [inspectError, setInspectError] = useState(null);
  // Inspect result.
  const [columns, setColumns] = useState([]);
  const [sampleRows, setSampleRows] = useState([]);
  const [totalRows, setTotalRows] = useState(0);
  const [hasHeader, setHasHeader] = useState(true);

  // ── ESC closes ──────────────────────────────────────────────────────────
  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  // ── Inspect the picked CSV ────────────────────────────────────────────────
  const inspect = async (f) => {
    setInspecting(true);
    setInspectError(null);
    setColumns([]); setSampleRows([]); setTotalRows(0);
    try {
      const fd = new FormData();
      fd.append('file', f);
      const res = await fetch(`/api/players/roster/${job.id}/inspect`, {
        method: 'POST', body: fd,
      });
      if (!res.ok) throw new Error(`Couldn't read that CSV (HTTP ${res.status}).`);
      const body = await res.json();
      if (!body.columns.length) throw new Error('That file has no readable rows.');
      setColumns(body.columns);
      setSampleRows(body.sample_rows);
      setTotalRows(body.total_rows);
    } catch (e) {
      setInspectError(e.message || String(e));
    } finally {
      setInspecting(false);
    }
  };

  const onPickFile = (e) => {
    const f = e.target.files?.[0] || null;
    setFile(f);
    if (f) inspect(f);
  };

  // ── Derived: column labels + preview reconcile with the header toggle ─────
  // With a header, row 1 is column names. Without, the first row is data and
  // columns are positional "Column N" (1-based) — the exact labels the backend
  // synthesizes, so these strings double as the mapping's column references.
  const displayColumns = hasHeader
    ? columns
    : columns.map((_, i) => `Column ${i + 1}`);
  const previewRows = hasHeader ? sampleRows : [columns, ...sampleRows];
  const dataRowCount = hasHeader ? Math.max(totalRows - 1, 0) : totalRows;
  const inspected = columns.length > 0;

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal-panel"
        onClick={(e) => e.stopPropagation()}
        style={{ maxWidth: 760, width: 'min(760px, 92vw)' }}
      >
        <header className="row-between" style={{ marginBottom: 12 }}>
          <h2 style={{ margin: 0 }}>Player roster</h2>
          <button className="ghost" onClick={onClose} aria-label="Close">×</button>
        </header>

        <p className="muted" style={{ marginTop: 0 }}>
          Upload this shoot's roster for reference-photo capture &amp; matching.
          Any column layout works — you'll map columns to <b>name</b> and{' '}
          <b>team</b> next. Re-uploading replaces the roster for this shoot.
        </p>

        {/* ── Pick a CSV ──────────────────────────────────────────────── */}
        <section style={{ marginBottom: 16 }}>
          <div className="actions" style={{ gap: 8 }}>
            <input
              ref={fileRef}
              type="file"
              accept=".csv,text/csv"
              onChange={onPickFile}
            />
            {inspecting && <span className="muted">Reading…</span>}
          </div>
          {file && !inspecting && inspected && (
            <p className="muted" style={{ marginTop: 6, fontSize: 13 }}>
              <b>{file.name}</b> · {columns.length} columns · {dataRowCount} data
              {dataRowCount === 1 ? ' row' : ' rows'}.
            </p>
          )}
          {inspectError && (
            <p className="error" style={{ marginTop: 6 }}>{inspectError}</p>
          )}
        </section>

        {/* ── Columns + header toggle + preview ───────────────────────── */}
        {inspected && (
          <section style={{ marginBottom: 16 }}>
            <label
              className="row-between"
              style={{ justifyContent: 'flex-start', gap: 8, marginBottom: 10 }}
            >
              <input
                type="checkbox"
                checked={hasHeader}
                onChange={(e) => setHasHeader(e.target.checked)}
              />
              First row is a header
            </label>

            <div style={{ overflowX: 'auto' }}>
              <table className="roster-preview">
                <thead>
                  <tr>
                    {displayColumns.map((c, i) => (
                      <th key={i} style={{ textAlign: 'left', padding: '4px 8px',
                        borderBottom: '1px solid var(--border, #333)', whiteSpace: 'nowrap' }}>
                        {c || <span className="muted">(blank)</span>}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {previewRows.slice(0, 5).map((row, ri) => (
                    <tr key={ri}>
                      {displayColumns.map((_, ci) => (
                        <td key={ci} style={{ padding: '3px 8px',
                          color: 'var(--text-muted)', whiteSpace: 'nowrap' }}>
                          {row[ci] ?? ''}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="muted" style={{ fontSize: 12, marginTop: 6 }}>
              Showing up to 5 of {dataRowCount} rows. Column mapping comes next.
            </p>
          </section>
        )}
      </div>
    </div>
  );
}
