import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';

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
  const nav = useNavigate();
  // Navigate to a team's review page; close the modal first so the user
  // lands cleanly on the cluster grid.
  const goToSession = (sessionId) => {
    if (!sessionId) return;
    onClose();
    nav(`/session/${sessionId}`);
  };
  const [roster, setRoster] = useState(null);          // GET /roster body | null
  const [namingErrors, setNamingErrors] = useState([]); // cross-team collisions
  const [mismatches, setMismatches] = useState([]);    // items array
  const [suggestions, setSuggestions] = useState({ items: [], available_teams: [] });
  const [coverage, setCoverage] = useState([]);  // [{session_id, session_name, report}, ...]
  const [expanded, setExpanded] = useState(new Set());  // session_ids whose detail rows are open
  const [savingSessionId, setSavingSessionId] = useState(null);
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

      const r3 = await fetch(`/api/jobs/${job.id}/roster-folder-suggestions`);
      if (!r3.ok) throw new Error(`Couldn't load folder suggestions (HTTP ${r3.status}).`);
      setSuggestions(await r3.json());

      const r4 = await fetch(`/api/jobs/${job.id}/naming-errors`);
      if (!r4.ok) throw new Error(`Couldn't load naming errors (HTTP ${r4.status}).`);
      setNamingErrors((await r4.json()).items || []);

      // Coverage report per session. One round-trip per non-archived
      // session — could be batched into a job-level aggregator later, but
      // the count is small (~30) and the call is cheap.
      const sessionList = (job.sessions || []).filter(s => !s.archived);
      const coverageResults = [];
      for (const s of sessionList) {
        try {
          const r = await fetch(`/api/sessions/${s.id}/roster-coverage`);
          if (!r.ok) continue;
          const body = await r.json();
          coverageResults.push({
            session_id: s.id,
            session_name: s.name,
            report: body,
          });
        } catch { /* skip — informational only */ }
      }
      setCoverage(coverageResults);
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

  // ── Folder ↔ CSV-team mapping ────────────────────────────────────────
  const saveMapping = async (sessionId, alias) => {
    setSavingSessionId(sessionId);
    try {
      const res = await fetch(`/api/sessions/${sessionId}/roster-mapping`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ roster_team_alias: alias }),
      });
      if (!res.ok) {
        alert(`Couldn't save mapping (HTTP ${res.status}).`);
        return;
      }
      await load();
      onChanged && onChanged();
    } finally {
      setSavingSessionId(null);
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

        {/* ── Map team folders ───────────────────────────────────────── */}
        {hasRoster && suggestions.items.length > 0 && (
          <section style={{ marginBottom: 16 }}>
            <h3 style={{ marginBottom: 8 }}>
              Map team folders ({suggestions.items.length})
            </h3>
            <p className="muted" style={{ marginTop: 0 }}>
              Folder names that don't match the CSV's team column. Map each
              one to its CSV team and the mismatch flags for that folder
              clear. Mappings survive roster re-uploads.
            </p>
            {suggestions.items.map((it) => {
              const sug = it.suggestion;
              const busy = savingSessionId === it.session_id;
              return (
                <div key={it.session_id} className="roster-row">
                  <div className="row-between" style={{ alignItems: 'baseline' }}>
                    <div>
                      <b>{it.session_name}</b>
                      {it.current_alias && (
                        <>
                          {' '}→{' '}
                          <span className="muted">currently mapped to</span>{' '}
                          <b>{it.current_alias}</b>
                        </>
                      )}
                    </div>
                  </div>
                  {sug && !it.current_alias && (
                    <p className="muted" style={{ marginTop: 4 }}>
                      Suggested: <b>{sug.suggested_team_name}</b>{' '}
                      ({sug.winning_clusters}/{sug.mapped_clusters} player
                      {sug.mapped_clusters === 1 ? '' : 's'} in this folder
                      belong to that team){' '}
                      <button
                        className="primary"
                        disabled={busy}
                        onClick={() => saveMapping(it.session_id, sug.suggested_team_name)}
                      >
                        Accept
                      </button>
                    </p>
                  )}
                  <div className="actions" style={{ gap: 8, marginTop: 6 }}>
                    <select
                      defaultValue={it.current_alias || ''}
                      disabled={busy}
                      onChange={(e) => {
                        const v = e.target.value;
                        if (v === '') return;
                        saveMapping(it.session_id, v);
                      }}
                    >
                      <option value="">
                        {it.current_alias ? 'Change to…' : 'Pick a CSV team…'}
                      </option>
                      {suggestions.available_teams.map((t) => (
                        <option key={t} value={t}>{t}</option>
                      ))}
                    </select>
                    {it.current_alias && (
                      <button
                        className="ghost"
                        disabled={busy}
                        onClick={() => saveMapping(it.session_id, null)}
                      >
                        Clear mapping
                      </button>
                    )}
                    {busy && <span className="muted">Saving…</span>}
                  </div>
                </div>
              );
            })}
          </section>
        )}

        {/* ── Coverage by team (Phase 9 Section 5) ─────────────────── */}
        {hasRoster && coverage.length > 0 && (
          <section style={{ marginBottom: 16 }}>
            <h3 style={{ marginBottom: 8 }}>Coverage by team</h3>
            <p className="muted" style={{ marginTop: 0 }}>
              For each team that has a matching roster, how many expected
              players the pipeline actually identified. Click a row to expand
              names. Updates as the pipeline finishes each team.
            </p>
            {coverage
              .filter(c => c.report.expected_players.length > 0)
              .sort((a, b) => {
                // Sort: teams with missing players first, then by name.
                const am = a.report.missing_players.length;
                const bm = b.report.missing_players.length;
                if (am !== bm) return bm - am;
                return a.session_name.localeCompare(b.session_name);
              })
              .map(({ session_id, session_name, report }) => {
                const expected = report.expected_players.length;
                const present = report.present_players.length;
                const missing = report.missing_players.length;
                const unident = report.unidentified_clusters.length;
                const isOpen = expanded.has(session_id);
                const toggle = () => {
                  const next = new Set(expanded);
                  if (next.has(session_id)) next.delete(session_id);
                  else next.add(session_id);
                  setExpanded(next);
                };
                return (
                  <div key={session_id} className="roster-row">
                    <button
                      onClick={toggle}
                      className="ghost"
                      style={{ width: '100%', textAlign: 'left', padding: 0, background: 'transparent', border: 'none' }}
                    >
                      <div className="row-between" style={{ alignItems: 'baseline' }}>
                        <div>
                          <b>{session_name}</b>{' '}
                          <span className="muted">
                            {present} of {expected} found
                            {missing > 0 && <> · <span style={{ color: 'var(--warn)' }}>{missing} missing</span></>}
                            {unident > 0 && <> · {unident} unidentified cluster{unident === 1 ? '' : 's'}</>}
                          </span>
                        </div>
                        <span className="muted">{isOpen ? '▾' : '▸'}</span>
                      </div>
                    </button>
                    {isOpen && (
                      <div style={{ marginTop: 8 }}>
                        {missing > 0 && (
                          <p className="muted" style={{ margin: '4px 0' }}>
                            <b>Missing:</b>{' '}
                            {report.missing_players.map(m => m.raw_name).join(', ')}
                          </p>
                        )}
                        {report.extra_clusters.length > 0 && (
                          <p className="muted" style={{ margin: '4px 0' }}>
                            <b>Extras:</b>{' '}
                            {report.extra_clusters.map(c => (
                              c.roster_team_raw
                                ? `${c.label} (roster says ${c.roster_team_raw})`
                                : `${c.label} (not on roster)`
                            )).join('; ')}
                          </p>
                        )}
                        {unident > 0 && (
                          <p className="muted" style={{ margin: '4px 0' }}>
                            <b>{unident}</b> cluster{unident === 1 ? '' : 's'} with no
                            copyright tag — couldn't be matched to the roster.
                          </p>
                        )}
                        {missing === 0 && report.extra_clusters.length === 0 && unident === 0 && (
                          <p className="muted" style={{ margin: '4px 0' }}>
                            ✓ Every roster player accounted for, no extras.
                          </p>
                        )}
                      </div>
                    )}
                  </div>
                );
              })}
          </section>
        )}

        {/* ── Possible naming errors (Phase 10) ─────────────────────── */}
        {namingErrors.length > 0 && (
          <section style={{ marginBottom: 16 }}>
            <h3 style={{ marginBottom: 8 }}>
              Possible naming errors ({namingErrors.length})
            </h3>
            <p className="muted" style={{ marginTop: 0 }}>
              A player name that appears in two different teams. Face matching
              tells you which case it is.
            </p>
            {namingErrors.map((it) => {
              const isNamingError = it.verdict === 'different_face';
              return (
                <div key={it.norm_name} className="roster-row">
                  <div>
                    <b>{it.raw_name}</b>{' · '}
                    {isNamingError ? (
                      <span style={{ color: 'var(--warn)' }}>
                        ⚠ different faces (likely a naming error)
                      </span>
                    ) : it.verdict === 'same_face' ? (
                      <span className="muted">✓ same face in two folders</span>
                    ) : (
                      <span className="muted">? couldn't compare faces</span>
                    )}
                  </div>
                  <div style={{ marginTop: 4 }}>
                    {it.clusters.map((c) => (
                      <div key={c.cluster_id} className="muted" style={{ fontSize: 13 }}>
                        <button
                          className="folder-crumb"
                          onClick={() => goToSession(c.session_id)}
                          title={`Open ${c.session_name}`}
                          style={{ padding: '1px 4px' }}
                        >
                          {c.session_name} ↗
                        </button>
                        {' '}{c.image_count} imgs
                        {c.in_correct_team && (
                          <span style={{ color: 'var(--success)' }}> · ✓ correct team</span>
                        )}
                        {c.session_reviewed && (
                          <span> · <span style={{ color: 'var(--text-dim)' }}>reviewed</span></span>
                        )}
                      </div>
                    ))}
                  </div>
                  <p className="muted" style={{ marginTop: 6 }}>
                    {isNamingError
                      ? 'The same name is on two different kids — the photographer probably didn’t update the copyright field when switching teams. Open both teams and rename the wrong one.'
                      : 'Same player photographed in two folders — use the Mismatches actions below to move one into the other.'}
                  </p>
                </div>
              );
            })}
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
                    <button
                      className="folder-crumb"
                      onClick={() => goToSession(row.source_session_id)}
                      title={`Open ${row.source_session_name}`}
                      style={{ padding: '1px 4px' }}
                    >
                      {row.source_session_name} ↗
                    </button>
                    {' → '}
                    {canMove ? (
                      <button
                        className="folder-crumb"
                        onClick={() => goToSession(row.target_session_id)}
                        title={`Open ${row.expected_team_name}`}
                        style={{ padding: '1px 4px' }}
                      >
                        {row.expected_team_name} ↗
                      </button>
                    ) : (
                      <b>{row.expected_team_name}</b>
                    )}
                    {' · '}
                    <span>{row.source_cluster_label}</span>
                    {' · '}
                    <span className="muted">{row.source_image_count} images</span>
                  </div>
                </div>
                <p className="muted" style={{ marginTop: 4 }}>
                  <b>{row.source_cluster_label}</b> should be on{' '}
                  <b>{row.expected_team_name}</b> (per roster); currently in{' '}
                  <b>{row.source_session_name}</b>.
                  {canMove ? ' Recommended: move.' : ''}
                </p>
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
