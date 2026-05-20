import { useEffect, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import ExportModal from '../components/ExportModal.jsx';
import PipelineProgress from '../components/PipelineProgress.jsx';
import CardMenu from '../components/CardMenu.jsx';
import Toast from '../components/Toast.jsx';
import ConfirmModal from '../components/ConfirmModal.jsx';
import RosterModal from '../components/RosterModal.jsx';

export default function JobDetail() {
  const { id } = useParams();
  const nav = useNavigate();
  const [job, setJob] = useState(null);
  const [exporting, setExporting] = useState(false);
  const [showRoster, setShowRoster] = useState(false);
  const [rosterSummary, setRosterSummary] = useState(null); // {entries_loaded, mismatch_count}
  const [running, setRunning] = useState(false);
  const [showArchived, setShowArchived] = useState(false);
  const [toast, setToast] = useState(null);
  const [confirm, setConfirm] = useState(null); // {session}

  const load = () => {
    const q = showArchived ? '?include_archived=true' : '';
    return fetch(`/api/jobs/${id}${q}`).then(r => r.json()).then(setJob);
  };
  // Lightweight roster summary for the header button. Failures here mustn't
  // break the page — degrade silently.
  const loadRosterSummary = async () => {
    try {
      const r1 = await fetch(`/api/jobs/${id}/roster`);
      const r2 = await fetch(`/api/jobs/${id}/roster-mismatches`);
      if (!r1.ok || !r2.ok) return;
      const rj = await r1.json();
      const mj = await r2.json();
      setRosterSummary({
        entries_loaded: rj.entries_loaded || 0,
        mismatch_count: (mj.items || []).length,
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

  const runAll = async () => {
    setRunning(true);
    await fetch(`/api/jobs/${id}/run-all`, { method: 'POST' });
    setRunning(false);
    load();
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
            title="Upload roster CSV and review cross-team mismatches"
          >
            {rosterSummary?.entries_loaded > 0
              ? `Roster · ${rosterSummary.mismatch_count} mismatch${rosterSummary.mismatch_count === 1 ? '' : 'es'}`
              : 'Upload roster'}
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
    </div>
  );
}
