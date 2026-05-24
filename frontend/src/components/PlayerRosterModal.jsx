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
  // Column mapping (the serializable object sent to the backend).
  const [nameMode, setNameMode] = useState('full');   // 'full' | 'split'
  const [nameColumn, setNameColumn] = useState('');
  const [firstNameColumn, setFirstNameColumn] = useState('');
  const [lastNameColumn, setLastNameColumn] = useState('');
  const [teamColumn, setTeamColumn] = useState('');
  // Validation (dry-run).
  const [validating, setValidating] = useState(false);
  const [report, setReport] = useState(null);          // dry-run response body
  const [validateError, setValidateError] = useState(null);
  // Commit.
  const [committing, setCommitting] = useState(false);
  const [commitResult, setCommitResult] = useState(null);  // success summary
  const [commitError, setCommitError] = useState(null);
  const [replaceConfirm, setReplaceConfirm] = useState(null); // 409 {count,message}

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

  // Column names change when a new file is inspected or the header toggle
  // flips (header names ↔ "Column N"), so stale selections must clear.
  useEffect(() => {
    setNameColumn(''); setFirstNameColumn(''); setLastNameColumn('');
    setTeamColumn('');
  }, [columns, hasHeader]);

  // Any change to the mapping invalidates a prior validation result, so the
  // operator can't upload against a stale "valid" report.
  useEffect(() => {
    setReport(null); setValidateError(null);
    setCommitResult(null); setCommitError(null); setReplaceConfirm(null);
  }, [nameMode, nameColumn, firstNameColumn, lastNameColumn, teamColumn,
      hasHeader, columns]);

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

  // ── Mapping: assemble (name, team) the same way the backend does ──────────
  const cellOf = (row, ref) => {
    const i = displayColumns.indexOf(ref);
    return i >= 0 && i < row.length ? String(row[i] ?? '').trim() : '';
  };
  const assemble = (row) => {
    const name = nameMode === 'full'
      ? cellOf(row, nameColumn)
      : [cellOf(row, firstNameColumn), cellOf(row, lastNameColumn)]
          .filter(Boolean).join(' ');
    return { name, team: cellOf(row, teamColumn) };
  };
  const mappingComplete = Boolean(teamColumn) && (
    nameMode === 'full' ? Boolean(nameColumn)
                        : Boolean(firstNameColumn) || Boolean(lastNameColumn)
  );
  // The serializable mapping object (also drop-in for future saved templates).
  const mapping = {
    has_header: hasHeader,
    name_mode: nameMode,
    name_column: nameMode === 'full' ? (nameColumn || null) : null,
    first_name_column: nameMode === 'split' ? (firstNameColumn || null) : null,
    last_name_column: nameMode === 'split' ? (lastNameColumn || null) : null,
    team_column: teamColumn || null,
  };

  // ── Validate (dry-run) ────────────────────────────────────────────────────
  const buildForm = (extra = {}) => {
    const fd = new FormData();
    fd.append('file', file);
    fd.append('mapping', JSON.stringify(mapping));
    Object.entries(extra).forEach(([k, v]) => fd.append(k, v));
    return fd;
  };

  const validate = async () => {
    setValidating(true);
    setValidateError(null);
    setReport(null);
    try {
      const res = await fetch(`/api/players/roster/${job.id}/mapped`, {
        method: 'POST', body: buildForm({ dry_run: 'true' }),
      });
      // A dry-run returns 200 even when the roster is invalid (the report says
      // so). A non-200 is a real failure (job gone, malformed mapping JSON).
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body?.detail?.message
          || `Validation failed (HTTP ${res.status}).`);
      }
      setReport(await res.json());
    } catch (e) {
      setValidateError(e.message || String(e));
    } finally {
      setValidating(false);
    }
  };

  // ── Commit (replace this shoot's roster) ──────────────────────────────────
  const commit = async (confirmReplace = false) => {
    setCommitting(true);
    setCommitError(null);
    try {
      const res = await fetch(`/api/players/roster/${job.id}/mapped`, {
        method: 'POST',
        body: buildForm({ confirm_replace: confirmReplace ? 'true' : 'false' }),
      });
      if (res.status === 409) {
        const body = await res.json().catch(() => ({}));
        if (body?.detail?.error === 'references_exist') {
          setReplaceConfirm(body.detail);   // wait for the confirm step
          return;
        }
      }
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        const d = body?.detail;
        throw new Error(
          (d && d.message)
          || (d?.row_errors ? 'Roster no longer valid — re-validate.' : null)
          || `Upload failed (HTTP ${res.status}).`,
        );
      }
      setReplaceConfirm(null);
      setCommitResult(await res.json());
    } catch (e) {
      setCommitError(e.message || String(e));
    } finally {
      setCommitting(false);
    }
  };

  const ColumnSelect = ({ label, value, onChange }) => (
    <label style={{ display: 'flex', flexDirection: 'column', gap: 4, fontSize: 13 }}>
      <span className="muted">{label}</span>
      <select value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">— pick a column —</option>
        {displayColumns.map((c, i) => (
          <option key={i} value={c}>{c || `(blank ${i + 1})`}</option>
        ))}
      </select>
    </label>
  );

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
              Showing up to 5 of {dataRowCount} rows.
            </p>
          </section>
        )}

        {/* ── Map columns ─────────────────────────────────────────────── */}
        {inspected && (
          <section style={{ marginBottom: 16 }}>
            <h3 style={{ marginBottom: 8 }}>Map columns</h3>
            <p className="muted" style={{ marginTop: 0 }}>
              <b>Name</b> and <b>team</b> are required. Other columns (parent
              contact, etc.) are ignored.
            </p>

            <div className="wizard-choices" style={{ marginBottom: 10 }}>
              <button
                className={nameMode === 'full' ? 'primary' : ''}
                onClick={() => setNameMode('full')}
              >
                Full name in one column
              </button>
              <button
                className={nameMode === 'split' ? 'primary' : ''}
                onClick={() => setNameMode('split')}
              >
                Separate first / last columns
              </button>
            </div>

            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 14 }}>
              {nameMode === 'full'
                ? ColumnSelect({ label: 'Name column', value: nameColumn,
                                 onChange: setNameColumn })
                : (
                  <>
                    {ColumnSelect({ label: 'First-name column', value: firstNameColumn,
                                    onChange: setFirstNameColumn })}
                    {ColumnSelect({ label: 'Last-name column', value: lastNameColumn,
                                    onChange: setLastNameColumn })}
                  </>
                )}
              {ColumnSelect({ label: 'Team column', value: teamColumn,
                              onChange: setTeamColumn })}
            </div>
            {nameMode === 'split' && (
              <p className="muted" style={{ fontSize: 12, marginTop: 6 }}>
                Map at least one — a last-name-only roster is valid.
              </p>
            )}

            {/* Live assembled preview */}
            {mappingComplete && (
              <div style={{ marginTop: 12 }}>
                <p className="muted" style={{ margin: '0 0 4px' }}>Result preview:</p>
                <div style={{ overflowX: 'auto' }}>
                  <table className="roster-preview">
                    <thead>
                      <tr>
                        <th style={{ textAlign: 'left', padding: '4px 8px' }}>Name</th>
                        <th style={{ textAlign: 'left', padding: '4px 8px' }}>Team</th>
                      </tr>
                    </thead>
                    <tbody>
                      {previewRows.slice(0, 5).map((row, ri) => {
                        const { name, team } = assemble(row);
                        return (
                          <tr key={ri}>
                            <td style={{ padding: '3px 8px',
                              color: name ? 'inherit' : 'var(--warn-text, #d97706)' }}>
                              {name || '⚠ (missing)'}
                            </td>
                            <td style={{ padding: '3px 8px',
                              color: team ? 'inherit' : 'var(--warn-text, #d97706)' }}>
                              {team || '⚠ (missing)'}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
          </section>
        )}

        {/* ── Validate ────────────────────────────────────────────────── */}
        {inspected && (
          <section style={{ marginBottom: 4 }}>
            <div className="actions" style={{ gap: 8 }}>
              <button
                disabled={!mappingComplete || validating || committing}
                onClick={validate}
                title={mappingComplete ? '' : 'Map name + team first'}
              >
                {validating ? 'Validating…' : 'Validate'}
              </button>
              <button
                className="primary"
                disabled={!report?.ok || committing || !!commitResult}
                onClick={() => commit(false)}
                title={report?.ok ? '' : 'Validate a clean roster first'}
              >
                {committing ? 'Uploading…' : 'Upload roster'}
              </button>
            </div>

            {validateError && (
              <p className="error" style={{ marginTop: 8 }}>{validateError}</p>
            )}
            {commitError && (
              <p className="error" style={{ marginTop: 8 }}>{commitError}</p>
            )}

            {/* Replace guard: photos were already captured for this shoot. */}
            {replaceConfirm && !commitResult && (
              <div style={{ marginTop: 10 }}>
                <p className="warn" style={{ margin: 0 }}>
                  {replaceConfirm.message} Replacing the roster won't delete those
                  photos, but may change which player they line up with.
                </p>
                <div className="actions" style={{ gap: 8, marginTop: 6 }}>
                  <button onClick={() => setReplaceConfirm(null)}>Cancel</button>
                  <button
                    className="danger-btn"
                    disabled={committing}
                    onClick={() => commit(true)}
                  >
                    {committing ? 'Replacing…' : 'Replace anyway'}
                  </button>
                </div>
              </div>
            )}

            {commitResult && (
              <div style={{ marginTop: 10 }}>
                <p style={{ color: 'var(--success)', margin: 0 }}>
                  ✓ Roster uploaded — <b>{commitResult.memberships_loaded}</b>{' '}
                  player{commitResult.memberships_loaded === 1 ? '' : 's'} across{' '}
                  <b>{commitResult.distinct_teams}</b> team
                  {commitResult.distinct_teams === 1 ? '' : 's'}
                  {commitResult.coaches > 0 && ` · ${commitResult.coaches} coach${commitResult.coaches === 1 ? '' : 'es'}`}.
                </p>
                <div className="actions" style={{ gap: 8, marginTop: 6 }}>
                  <button className="primary" onClick={onClose}>Done</button>
                </div>
              </div>
            )}

            {report && report.ok && (
              <div style={{ marginTop: 8 }}>
                <p style={{ color: 'var(--success)', margin: 0 }}>
                  ✓ {report.summary.valid_rows} player
                  {report.summary.valid_rows === 1 ? '' : 's'} across{' '}
                  {report.summary.distinct_teams} team
                  {report.summary.distinct_teams === 1 ? '' : 's'}
                  {report.summary.coaches > 0 &&
                    ` · ${report.summary.coaches} coach${report.summary.coaches === 1 ? '' : 'es'}`}.
                </p>
                {report.references_warning?.count > 0 && (
                  <p className="warn" style={{ marginTop: 6 }}>
                    {report.references_warning.message}{' '}
                    Uploading will ask you to confirm the replace.
                  </p>
                )}
              </div>
            )}

            {report && !report.ok && (
              <div style={{ marginTop: 8 }}>
                <p className="error" style={{ margin: 0 }}>
                  Upload blocked — fix the CSV (or the mapping) and re-validate.
                </p>
                <ul className="confirm-impact" style={{ marginTop: 4 }}>
                  {report.mapping_errors.map((m) => <li key={m}>{m}</li>)}
                  {report.row_errors.map((e) => (
                    <li key={e.reason}>
                      <b>{e.rows.length}</b> {e.rows.length === 1 ? 'row' : 'rows'}{' '}
                      {e.label}: rows {e.rows.join(', ')}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </section>
        )}
      </div>
    </div>
  );
}
