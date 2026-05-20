import { useEffect, useRef, useState } from 'react';

/**
 * Roster upload + mismatch review modal.
 *
 * Single panel covers Phase 6's UI:
 *  - Upload a roster CSV (positional name,team — no header).
 *  - Show current roster summary + warnings (unmatched teams,
 *    sessions-missing-from-roster, duplicate names).
 *  - List every cluster across the job that's flagged `roster_mismatch`,
 *    with per-row actions: Merge into existing / Create new cluster /
 *    Reject as stray. Defaults to merge when a target cluster exists,
 *    otherwise to create. The user can always flip.
 *  - "Delete roster" removes it.
 *
 * Every fetch checks `res.ok` and never lets an error body land in state
 * (Phase 5 lesson — see SessionDetail.load).
 */
export default function RosterModal({ job, onClose, onChanged }) {
  const [roster, setRoster] = useState(null);          // GET /roster body | null
  const [mismatches, setMismatches] = useState([]);    // items array
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [uploadResult, setUploadResult] = useState(null); // last POST response
  const [uploadError, setUploadError] = useState(null);
  const [uploading, setUploading] = useState(false);
  const [busyClusterId, setBusyClusterId] = useState(null);
  const fileRef = useRef(null);

  // ── Data fetching ──────────────────────────────────────────────────────
  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      const r1 = await fetch(`/api/jobs/${job.id}/roster`);
      if (!r1.ok) throw new Error(`Couldn't load roster (HTTP ${r1.status}).`);
      setRoster(await r1.json());

      const r2 = await fetch(`/api/jobs/${job.id}/roster-mismatches`);
      if (!r2.ok) throw new Error(`Couldn't load mismatches (HTTP ${r2.status}).`);
      setMismatches((await r2.json()).items || []);
    } catch (e) {
      setError(e.message || String(e));
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => { load(); }, [job.id]);

  // ── ESC closes ─────────────────────────────────────────────────────────
  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  // ── Upload ────────────────────────────────────────────────────────────
  const submitUpload = async () => {
    const file = fileRef.current?.files?.[0];
    if (!file) return;
    setUploading(true);
    setUploadResult(null);
    setUploadError(null);
    try {
      const fd = new FormData();
      fd.append('file', file);
      const res = await fetch(`/api/jobs/${job.id}/roster`, { method: 'POST', body: fd });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(
          body?.detail?.message || body?.detail?.error || `Upload failed (HTTP ${res.status}).`
        );
      }
      setUploadResult(await res.json());
      await load();
      onChanged && onChanged();
    } catch (e) {
      setUploadError(e.message || String(e));
    } finally {
      setUploading(false);
      if (fileRef.current) fileRef.current.value = '';
    }
  };

  // ── Delete roster ─────────────────────────────────────────────────────
  const deleteRoster = async () => {
    if (!confirm('Delete the roster for this job? Mismatch flags will disappear.')) return;
    const res = await fetch(`/api/jobs/${job.id}/roster`, { method: 'DELETE' });
    if (!res.ok) {
      alert(`Couldn't delete roster (HTTP ${res.status}).`);
      return;
    }
    setUploadResult(null);
    await load();
    onChanged && onChanged();
  };

  // ── Per-row actions ───────────────────────────────────────────────────
  const moveCluster = async (row, mode) => {
    if (!row.target_session_id) return;
    setBusyClusterId(row.source_cluster_id);
    try {
      const res = await fetch(`/api/clusters/${row.source_cluster_id}/move`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          target_session_id: row.target_session_id,
          mode,
        }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        alert(body?.detail?.message || `Move failed (HTTP ${res.status}).`);
        return;
      }
      await load();
      onChanged && onChanged();
    } finally {
      setBusyClusterId(null);
    }
  };

  const rejectAsStray = async (row) => {
    if (!row.source_image_ids?.length) return;
    if (!confirm(
      `Mark all ${row.source_image_ids.length} images in "${row.source_cluster_label}" as rejected? ` +
      `They stay in ${row.source_session_name} but will be skipped at export.`
    )) return;
    setBusyClusterId(row.source_cluster_id);
    try {
      for (const image_id of row.source_image_ids) {
        const res = await fetch(`/api/sessions/${row.source_session_id}/set-role`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            image_id,
            cluster_id: row.source_cluster_id,
            role: 'rejected',
          }),
        });
        if (!res.ok) {
          alert(`Reject failed on image ${image_id} (HTTP ${res.status}).`);
          break;
        }
      }
      await load();
      onChanged && onChanged();
    } finally {
      setBusyClusterId(null);
    }
  };

  // ── Render ────────────────────────────────────────────────────────────
  const hasRoster = roster && roster.entries_loaded > 0;
  const warnings = roster?.warnings || {};
  const anyWarning =
    (warnings.unmatched_csv_teams?.length || 0) +
    (warnings.sessions_missing_from_roster?.length || 0) +
    (warnings.duplicate_names?.length || 0) > 0;

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal-panel roster-modal"
        onClick={(e) => e.stopPropagation()}
        style={{ maxWidth: 760, width: 'min(760px, 92vw)' }}
      >
        <header className="row-between" style={{ marginBottom: 12 }}>
          <h2 style={{ margin: 0 }}>Roster</h2>
          <button className="ghost" onClick={onClose} aria-label="Close">×</button>
        </header>

        {/* ── Upload section ─────────────────────────────────────────── */}
        <section style={{ marginBottom: 16 }}>
          <p className="muted" style={{ marginTop: 0 }}>
            CSV with two positional columns, no header: <code>player-name,team</code>.
            Re-uploading replaces the previous roster.
          </p>
          <div className="actions" style={{ gap: 8 }}>
            <input ref={fileRef} type="file" accept=".csv,text/csv" />
            <button onClick={submitUpload} disabled={uploading}>
              {uploading ? 'Uploading…' : (hasRoster ? 'Replace roster' : 'Upload roster')}
            </button>
            {hasRoster && (
              <button className="ghost" onClick={deleteRoster}>Delete roster</button>
            )}
          </div>
          {uploadError && (
            <p className="muted" style={{ color: 'var(--danger)' }}>{uploadError}</p>
          )}
          {uploadResult && (
            <p className="muted">
              Loaded <b>{uploadResult.entries_loaded}</b> entries across{' '}
              <b>{uploadResult.distinct_teams}</b> teams
              {uploadResult.entries_skipped > 0 &&
                ` · ${uploadResult.entries_skipped} skipped`}.
            </p>
          )}
        </section>

        {/* ── Roster summary + warnings ──────────────────────────────── */}
        {hasRoster && (
          <section style={{ marginBottom: 16 }}>
            <p className="muted">
              <b>{roster.entries_loaded}</b> entries · <b>{roster.distinct_teams}</b> teams
            </p>
            {anyWarning && (
              <ul className="confirm-impact" style={{ marginTop: 4 }}>
                {warnings.unmatched_csv_teams?.length > 0 && (
                  <li>
                    <b>{warnings.unmatched_csv_teams.length}</b> team
                    {warnings.unmatched_csv_teams.length === 1 ? '' : 's'} in CSV not
                    matched to any team folder in this job:{' '}
                    <code>{warnings.unmatched_csv_teams.join(', ')}</code>
                  </li>
                )}
                {warnings.sessions_missing_from_roster?.length > 0 && (
                  <li>
                    <b>{warnings.sessions_missing_from_roster.length}</b> team
                    {warnings.sessions_missing_from_roster.length === 1 ? '' : 's'} in this
                    job not covered by the roster:{' '}
                    <code>{warnings.sessions_missing_from_roster.join(', ')}</code>
                  </li>
                )}
                {warnings.duplicate_names?.length > 0 && (
                  <li>
                    <b>{warnings.duplicate_names.length}</b> name
                    {warnings.duplicate_names.length === 1 ? '' : 's'} on multiple teams
                    (lookup will abstain on these):{' '}
                    <code>{warnings.duplicate_names.join(', ')}</code>
                  </li>
                )}
              </ul>
            )}
          </section>
        )}

        {/* ── Mismatches list ────────────────────────────────────────── */}
        <section>
          <h3 style={{ marginBottom: 8 }}>
            Mismatches {mismatches.length > 0 && `(${mismatches.length})`}
          </h3>
          {loading && <p className="muted">Loading…</p>}
          {error && (
            <p className="muted" style={{ color: 'var(--danger)' }}>{error}</p>
          )}
          {!loading && !error && !hasRoster && (
            <p className="muted">Upload a roster to see mismatches.</p>
          )}
          {!loading && !error && hasRoster && mismatches.length === 0 && (
            <p className="muted">No mismatches — every cluster is on the right team.</p>
          )}
          {mismatches.map((row) => {
            const busy = busyClusterId === row.source_cluster_id;
            const canMove = row.target_session_id != null;
            const canMerge = row.target_cluster_id != null;
            return (
              <div key={row.source_cluster_id} className="roster-row">
                <div className="row-between" style={{ alignItems: 'baseline' }}>
                  <div>
                    <b>{row.source_session_name}</b>
                    {' → '}
                    <b>{row.expected_team_name}</b>
                    {' · '}
                    <span>{row.source_cluster_label}</span>
                    {' · '}
                    <span className="muted">{row.source_image_count} images</span>
                  </div>
                </div>
                {!canMove && (
                  <p className="muted" style={{ marginTop: 4 }}>
                    Expected team <b>{row.expected_team_name}</b> is not a team folder in
                    this job — verify the roster.
                  </p>
                )}
                {row.target_session_reviewed && canMove && (
                  <p className="muted" style={{ marginTop: 4 }}>
                    ⚠ <b>{row.expected_team_name}</b> is marked reviewed — moving will
                    un-review it.
                  </p>
                )}
                {canMove && (
                  <div className="actions" style={{ gap: 8, marginTop: 6 }}>
                    {canMerge ? (
                      <>
                        <button
                          className="primary"
                          disabled={busy}
                          onClick={() => moveCluster(row, 'merge')}
                          title={`Merge into the existing cluster of this player in ${row.expected_team_name}`}
                        >
                          Merge into existing cluster
                        </button>
                        <button
                          className="ghost"
                          disabled={busy}
                          onClick={() => moveCluster(row, 'create')}
                        >
                          Create new cluster
                        </button>
                      </>
                    ) : (
                      <button
                        className="primary"
                        disabled={busy}
                        onClick={() => moveCluster(row, 'create')}
                        title="No matching cluster in the target team — create a new one"
                      >
                        Create new cluster in {row.expected_team_name}
                      </button>
                    )}
                    <button
                      className="ghost"
                      disabled={busy}
                      onClick={() => rejectAsStray(row)}
                      title="Mark every image in this cluster as rejected (stays in source team but excluded from export)"
                    >
                      Reject as stray
                    </button>
                    {busy && <span className="muted">Working…</span>}
                  </div>
                )}
              </div>
            );
          })}
        </section>
      </div>
    </div>
  );
}
