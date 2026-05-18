import { useEffect, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import ExportModal from '../components/ExportModal.jsx';
import PipelineProgress from '../components/PipelineProgress.jsx';

export default function JobDetail() {
  const { id } = useParams();
  const nav = useNavigate();
  const [job, setJob] = useState(null);
  const [exporting, setExporting] = useState(false);
  const [running, setRunning] = useState(false);
  const [archiving, setArchiving] = useState(false);

  const load = () => fetch(`/api/jobs/${id}`).then(r => r.json()).then(setJob);
  useEffect(() => { load(); }, [id]);

  useEffect(() => {
    if (!job) return;
    const anyRunning = job.sessions.some(s => s.status === 'running');
    if (!anyRunning) return;
    const t = setInterval(load, 1000);    // pretty fast while pipeline is moving
    return () => clearInterval(t);
  }, [job]);

  if (!job) return <div className="page">Loading…</div>;

  const runAll = async () => {
    setRunning(true);
    await fetch(`/api/jobs/${id}/run-all`, { method: 'POST' });
    setRunning(false);
    load();
  };

  const toggleArchive = async () => {
    const next = !job.archived;
    if (next) {
      const ok = window.confirm(
        `Archive "${job.name}"? It'll drop off the home page but stay accessible from the Archived tab.`
      );
      if (!ok) return;
    }
    setArchiving(true);
    await fetch(`/api/jobs/${id}/archived`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ archived: next }),
    });
    setArchiving(false);
    if (next) nav('/');     // bounce back to home after archiving
    else load();
  };

  const totalImages = job.sessions.reduce((a, s) => a + (s.image_count || 0), 0);
  const totalReview = job.sessions.reduce((a, s) => a + (s.needs_review_count || 0), 0);
  const reviewedCount = job.sessions.filter(s => s.reviewed).length;
  const allReviewed = job.sessions.length > 0 && reviewedCount === job.sessions.length;
  const runningSessions = job.sessions.filter(s => s.status === 'running');
  const pendingSessions = job.sessions.filter(s => s.status === 'pending');

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
            <code>{job.root_path}</code> · {job.sessions.length} teams · {totalImages} images ·{' '}
            {reviewedCount}/{job.sessions.length} reviewed · {totalReview} need review
          </p>
        </div>
        <div className="actions">
          <button onClick={runAll} disabled={running}>
            {running ? 'Kicking off…' : 'Run pipeline on all teams'}
          </button>
          <button onClick={() => setExporting(true)}>Export job</button>
          <button className="ghost" onClick={toggleArchive} disabled={archiving}>
            {job.archived ? 'Unarchive' : 'Archive'}
          </button>
        </div>
      </header>

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
        {job.sessions.map(s => (
          <Link
            key={s.id}
            to={`/session/${s.id}`}
            className={`team-card ${s.needs_review_count > 0 ? 'review' : ''} ${s.reviewed ? 'reviewed' : ''} ${s.status === 'running' ? 'running' : ''}`}
          >
            {s.reviewed && <span className="reviewed-tick" title="Marked reviewed">✓ Reviewed</span>}
            <h3>{s.name}</h3>
            <p className="muted">
              {s.image_count} images · {s.cluster_count} clusters · status: {s.status}
            </p>
            <PipelineProgress session={s} compact />
            {s.needs_review_count > 0 && (
              <span className="review-badge">⚠ {s.needs_review_count} review</span>
            )}
          </Link>
        ))}
      </div>

      {exporting && <ExportModal job={job} onClose={() => setExporting(false)} />}
    </div>
  );
}
