import { useEffect, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import ExportModal from '../components/ExportModal.jsx';
import PipelineProgress from '../components/PipelineProgress.jsx';
import CardMenu from '../components/CardMenu.jsx';
import Toast from '../components/Toast.jsx';
import ConfirmModal from '../components/ConfirmModal.jsx';
import RosterModal from '../components/RosterModal.jsx';
import PlayerRosterModal from '../components/PlayerRosterModal.jsx';
import ConnectivityBanner from '../components/ConnectivityBanner.jsx';

export default function JobDetail() {
  const { id } = useParams();
  const nav = useNavigate();
  const [job, setJob] = useState(null);
  const [exporting, setExporting] = useState(false);
  const [showRoster, setShowRoster] = useState(false);
  const [showPlayerRoster, setShowPlayerRoster] = useState(false);
  // {entries_loaded, mismatch_count, suggestions_count}
  //   entries_loaded     — Phase 6 RosterEntry row count (vestigial — see
  //                        memory: match-team-alias-issue)
  //   mismatch_count     — Phase 6 cross-check (RosterEntry-driven)
  //   suggestions_count  — Option α: sessions needing folder→team mapping,
  //                        sourced from /roster-folder-suggestions which
  //                        now reads PlayerMembership when RosterEntry is
  //                        empty. The button label surfaces this count when
  //                        RosterEntry is empty so PlayerMembership-only
  //                        jobs (the modern default) get an actionable hint.
  const [rosterSummary, setRosterSummary] = useState(null);
  const [running, setRunning] = useState(false);
  const [showArchived, setShowArchived] = useState(false);
  const [toast, setToast] = useState(null);
  const [confirm, setConfirm] = useState(null); // {session}
  // Destructive-action gate for "Run pipeline on all teams" (Phase 9 safety).
  // Populated when the backend returns 409 with the impact dict; cleared
  // when the user cancels or confirms.
  const [runAllImpact, setRunAllImpact] = useState(null);
  const [runAllConfirmText, setRunAllConfirmText] = useState('');

  const load = () => {
    const q = showArchived ? '?include_archived=true' : '';
    return fetch(`/api/jobs/${id}${q}`).then(r => r.json()).then(setJob);
  };
  // Lightweight roster summary for the header button. Failures here mustn't
  // break the page — degrade silently.
  const loadRosterSummary = async () => {
    try {
      const [r1, r2, r3] = await Promise.all([
        fetch(`/api/jobs/${id}/roster`),
        fetch(`/api/jobs/${id}/roster-mismatches`),
        fetch(`/api/jobs/${id}/roster-folder-suggestions`),
      ]);
      if (!r1.ok || !r2.ok) return;
      const rj = await r1.json();
      const mj = await r2.json();
      // Option α: suggestions_count is best-effort. A failed fetch leaves
      // the field 0 so the button just shows the existing labels.
      const sj = r3.ok ? await r3.json() : { items: [] };
      setRosterSummary({
        entries_loaded: rj.entries_loaded || 0,
        mismatch_count: (mj.items || []).length,
        suggestions_count: (sj.items || []).length,
      });
    } catch { /* leave summary null — button just shows generic label */ }
  };
  useEffect(() => { load(); loadRosterSummary(); }, [id, showArchived]);

  useEffect(() => {
    if (!job) return;
    const anyRunning = job.sessions.some(s => s.status === 'running');
    if (!anyRunning) return;
    const t = setInterval(load, 1000);
    return () => clearInterval(t);
  }, [job]);

  if (!job) return <div className="page">Loading…</div>;

  const postRunAll = async (force) => {
    const res = await fetch(`/api/jobs/${id}/run-all`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ force: !!force }),
    });
    return res;
  };

  const runAll = async () => {
    setRunning(true);
    try {
      const res = await postRunAll(false);
      if (res.status === 409) {
        const body = await res.json().catch(() => ({}));
        const detail = body?.detail;
        if (detail?.error === 'destructive_run_all') {
          setRunAllImpact(detail);
          setRunAllConfirmText('');
          return;     // wait for the confirm dialog
        }
      }
      if (!res.ok) {
        alert(`Couldn't start (HTTP ${res.status}).`);
        return;
      }
      load();
    } finally {
      setRunning(false);
    }
  };

  const confirmRunAllForce = async () => {
    setRunning(true);
    try {
      const res = await postRunAll(true);
      if (!res.ok) {
        alert(`Couldn't start (HTTP ${res.status}).`);
        return;
      }
      setRunAllImpact(null);
      setRunAllConfirmText('');
      load();
    } finally {
      setRunning(false);
    }
  };

  const archiveJob = async () => {
    await fetch(`/api/jobs/${id}/archive`, { method: 'POST' });
    nav('/');  // archived jobs drop off the home list
  };
  const unarchiveJob = async () => {
    await fetch(`/api/jobs/${id}/unarchive`, { method: 'POST' });
    load();
  };

  const archiveSession = async (s) => {
    await fetch(`/api/sessions/${s.id}/archive`, { method: 'POST' });
    await load();
    setToast({
      message: `Team "${s.name}" archived.`,
      actionLabel: 'Undo',
      onAction: async () => {
        await fetch(`/api/sessions/${s.id}/unarchive`, { method: 'POST' });
        load();
      },
    });
  };
  const unarchiveSession = async (s) => {
    await fetch(`/api/sessions/${s.id}/unarchive`, { method: 'POST' });
    load();
  };
  const deleteSession = async (s) => {
    await fetch(`/api/sessions/${s.id}`, { method: 'DELETE' });
    setConfirm(null);
    await load();
    setToast({ message: `Team "${s.name}" deleted.` });
  };

  // Stats over non-archived sessions only (matches backend list behavior).
  const active = job.sessions.filter(s => !s.archived);
  const totalImages = active.reduce((a, s) => a + (s.image_count || 0), 0);
  const totalReview = active.reduce((a, s) => a + (s.needs_review_count || 0), 0);
  const reviewedCount = active.filter(s => s.reviewed).length;
  const allReviewed = active.length > 0 && reviewedCount === active.length;
  const runningSessions = active.filter(s => s.status === 'running');
  const pendingSessions = active.filter(s => s.status === 'pending');

  return (
    <div className="page">
      <ConnectivityBanner />
      <header className="row-between">
        <div>
          <Link to="/">← All jobs</Link>
          <h1>
            {job.name}
            {job.archived && <span className="archived-pill" title="This job is archived">Archived</span>}
          </h1>
          <p className="muted">
            <code>{job.root_path}</code> · {active.length} teams · {totalImages} images ·{' '}
            {reviewedCount}/{active.length} reviewed · {totalReview} need review
          </p>
        </div>
        <div className="actions">
          <button onClick={runAll} disabled={running}>
            {running ? 'Kicking off…' : 'Run pipeline on all teams'}
          </button>
          <button
            onClick={() => setShowRoster(true)}
            title="Cross-check roster: flags wrong-team photos and surfaces folder→team mapping suggestions"
          >
            {/* Label precedence:
                  (1) Phase 6 cross-check is loaded → show its mismatch count
                  (2) Else if Option α suggestions exist (modern jobs with
                      only PlayerMembership) → show the "N teams need mapping"
                      hint so the operator opens the modal and resolves them
                  (3) Else generic "Roster cross-check" label */}
            {rosterSummary?.entries_loaded > 0
              ? `Cross-check · ${rosterSummary.mismatch_count} mismatch${rosterSummary.mismatch_count === 1 ? '' : 'es'}`
              : rosterSummary?.suggestions_count > 0
                ? `${rosterSummary.suggestions_count} team${rosterSummary.suggestions_count === 1 ? '' : 's'} need mapping`
                : 'Roster cross-check'}
          </button>
          <button
            onClick={() => setShowPlayerRoster(true)}
            title="Upload the player roster (for reference-photo capture & matching)"
          >
            Player roster
          </button>
          <button onClick={() => setExporting(true)}>Export job</button>
          {job.archived
            ? <button className="ghost" onClick={unarchiveJob}>Unarchive job</button>
            : <button className="ghost" onClick={archiveJob}>Archive job</button>}
        </div>
      </header>

      <label className="show-archived-toggle">
        <input
          type="checkbox"
          checked={showArchived}
          onChange={(e) => setShowArchived(e.target.checked)}
        />
        Show archived teams
      </label>

      {runningSessions.length > 0 && (
        <div className="pipeline-banner">
          <div className="pipeline-banner-status">
            <span className="spinner" />
            <span>
              Pipeline running on <b>{runningSessions.length}</b> team{runningSessions.length === 1 ? '' : 's'}
              {pendingSessions.length > 0 && ` · ${pendingSessions.length} queued`}
            </span>
          </div>
        </div>
      )}

      {allReviewed && (
        <div className="all-reviewed-banner">
          <div>
            <b>All teams reviewed.</b> Ready to export.
          </div>
          <button className="primary" onClick={() => setExporting(true)}>Export job →</button>
        </div>
      )}

      {job.sessions.length === 0 && (
        <div className="all-reviewed-banner">
          <div>
            <b>No images yet.</b> Attach a Player roster now; import the photos
            once the shoot is done.
          </div>
          <button
            className="primary"
            onClick={() => nav(`/job/${id}/import-images`)}
          >
            Import images →
          </button>
        </div>
      )}

      <div className="team-grid">
        {job.sessions.map(s => {
          const menuItems = [
            { label: 'Open', onClick: () => nav(`/session/${s.id}`) },
            s.archived
              ? { label: 'Unarchive', onClick: () => unarchiveSession(s) }
              : { label: 'Archive', onClick: () => archiveSession(s) },
            { label: 'Delete', danger: true, onClick: () => setConfirm({ session: s }) },
          ];
          return (
            <Link
              key={s.id}
              to={`/session/${s.id}`}
              className={`team-card ${s.needs_review_count > 0 ? 'review' : ''} ${s.reviewed ? 'reviewed' : ''} ${s.status === 'running' ? 'running' : ''} ${s.archived ? 'archived' : ''}`}
            >
              {s.archived && <span className="archived-pill">Archived</span>}
              {s.reviewed && !s.archived && <span className="reviewed-tick" title="Marked reviewed">✓ Reviewed</span>}
              <CardMenu items={menuItems} />
              <h3>{s.name}</h3>
              <p className="muted">
                {s.image_count} images · {s.cluster_count} clusters · status: {s.status}
              </p>
              <PipelineProgress session={s} compact />
              {s.needs_review_count > 0 && (
                <span className="review-badge">⚠ {s.needs_review_count} review</span>
              )}
            </Link>
          );
        })}
      </div>

      {exporting && <ExportModal job={job} onClose={() => setExporting(false)} />}

      {showRoster && (
        <RosterModal
          job={job}
          onClose={() => setShowRoster(false)}
          onChanged={() => { loadRosterSummary(); load(); }}
        />
      )}

      {showPlayerRoster && (
        <PlayerRosterModal
          job={job}
          onClose={() => setShowPlayerRoster(false)}
        />
      )}

      {confirm && (
        <ConfirmModal
          title={`Delete team "${confirm.session.name}"?`}
          lines={[
            `${confirm.session.image_count || 0} images`,
            `${confirm.session.cluster_count || 0} clusters`,
            'All role assignments and manual overrides',
            'Cached thumbnails',
          ]}
          onConfirm={() => deleteSession(confirm.session)}
          onCancel={() => setConfirm(null)}
        />
      )}

      {toast && (
        <Toast
          message={toast.message}
          actionLabel={toast.actionLabel}
          onAction={toast.onAction || (() => {})}
          onClose={() => setToast(null)}
        />
      )}

      {runAllImpact && (
        <div className="modal-backdrop" onClick={() => setRunAllImpact(null)}>
          <div className="modal-panel" onClick={(e) => e.stopPropagation()}>
            <h2>Re-run pipeline on every team?</h2>
            <p className="muted">
              This deletes existing clusters, role decisions, and manual
              labels for every team in <b>{runAllImpact.job_name}</b>.
              {' '}<b>This cannot be undone.</b>
            </p>
            <ul className="confirm-impact">
              {runAllImpact.impact.reviewed_teams > 0 && (
                <li>
                  <b>{runAllImpact.impact.reviewed_teams}</b> team
                  {runAllImpact.impact.reviewed_teams === 1 ? '' : 's'} marked
                  reviewed
                </li>
              )}
              {runAllImpact.impact.manual_labels > 0 && (
                <li>
                  <b>{runAllImpact.impact.manual_labels}</b> manually-renamed
                  cluster{runAllImpact.impact.manual_labels === 1 ? '' : 's'}
                </li>
              )}
              {runAllImpact.impact.manual_coach_overrides > 0 && (
                <li>
                  <b>{runAllImpact.impact.manual_coach_overrides}</b> manual
                  coach/player override
                  {runAllImpact.impact.manual_coach_overrides === 1 ? '' : 's'}
                </li>
              )}
              {runAllImpact.impact.manual_role_decisions > 0 && (
                <li>
                  <b>{runAllImpact.impact.manual_role_decisions}</b> manual
                  TEAM/PANO/role pick
                  {runAllImpact.impact.manual_role_decisions === 1 ? '' : 's'}
                </li>
              )}
              {/* 2026-06-25 Phase B: copied clusters have no face data and
                  cannot be re-derived by a pipeline re-run. Surface the
                  count so the operator knows what they're about to lose
                  before the wipe. */}
              {runAllImpact.impact.copied_clusters > 0 && (
                <li>
                  <b>{runAllImpact.impact.copied_clusters}</b> copied
                  cluster{runAllImpact.impact.copied_clusters === 1 ? '' : 's'}
                  {' '}(no face data, cannot be re-derived — re-running deletes them)
                </li>
              )}
            </ul>
            <p className="muted">
              Type the job name <code>{runAllImpact.job_name}</code> to confirm:
            </p>
            <input
              autoFocus
              value={runAllConfirmText}
              onChange={(e) => setRunAllConfirmText(e.target.value)}
              placeholder={runAllImpact.job_name}
              style={{ width: '100%', marginBottom: 8 }}
            />
            <div className="actions" style={{ justifyContent: 'flex-end' }}>
              <button onClick={() => { setRunAllImpact(null); setRunAllConfirmText(''); }}>
                Cancel
              </button>
              <button
                className="danger-btn"
                disabled={runAllConfirmText !== runAllImpact.job_name || running}
                onClick={confirmRunAllForce}
              >
                {running ? 'Starting…' : 'Re-run all teams'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
