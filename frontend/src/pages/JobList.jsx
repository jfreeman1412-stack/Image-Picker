import { useEffect, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import CardMenu from '../components/CardMenu.jsx';
import Toast from '../components/Toast.jsx';
import ConfirmModal from '../components/ConfirmModal.jsx';
import NewShootModal from '../components/NewShootModal.jsx';

function formatDate(iso) {
  if (!iso) return '';
  try {
    return new Date(iso).toLocaleDateString(undefined, {
      year: 'numeric', month: 'short', day: 'numeric',
    });
  } catch {
    return '';
  }
}

export default function JobList() {
  const nav = useNavigate();
  const [jobs, setJobs] = useState(null);
  const [showArchived, setShowArchived] = useState(false);
  const [toast, setToast] = useState(null);     // {message, actionLabel, onAction}
  const [confirm, setConfirm] = useState(null); // {job}
  const [showNewShoot, setShowNewShoot] = useState(false);

  const load = async () => {
    const q = showArchived ? '?include_archived=true' : '';
    const j = await fetch(`/api/jobs${q}`).then(r => r.json());
    setJobs(j);
  };
  useEffect(() => { setJobs(null); load(); }, [showArchived]);

  const archiveJob = async (job) => {
    await fetch(`/api/jobs/${job.id}/archive`, { method: 'POST' });
    await load();
    setToast({
      message: `Job "${job.name}" archived.`,
      actionLabel: 'Undo',
      onAction: async () => {
        await fetch(`/api/jobs/${job.id}/unarchive`, { method: 'POST' });
        load();
      },
    });
  };

  const unarchiveJob = async (job) => {
    await fetch(`/api/jobs/${job.id}/unarchive`, { method: 'POST' });
    load();
  };

  const deleteJob = async (job) => {
    await fetch(`/api/jobs/${job.id}`, { method: 'DELETE' });
    setConfirm(null);
    await load();
    setToast({ message: `Job "${job.name}" deleted.` });
  };

  return (
    <div className="page">
      <header className="row-between">
        <div>
          <h1>Player Sort</h1>
          <p className="muted">All shoot jobs.</p>
        </div>
        <div className="actions">
          <Link to="/settings"><button className="ghost">Settings</button></Link>
          <button
            onClick={() => setShowNewShoot(true)}
            title="Create a job with no images yet so you can attach a roster pre-shoot"
          >
            + New shoot
          </button>
          <Link to="/job/new"><button className="primary-cta">+ New job</button></Link>
        </div>
      </header>

      <label className="show-archived-toggle">
        <input
          type="checkbox"
          checked={showArchived}
          onChange={(e) => setShowArchived(e.target.checked)}
        />
        Show archived
      </label>

      {jobs === null && <p className="muted">Loading…</p>}

      {jobs && jobs.length === 0 && (
        <section className="empty-state">
          <h2>{showArchived ? 'No jobs' : 'No jobs yet'}</h2>
          <p className="muted">
            {showArchived
              ? 'Nothing here. Archived jobs would show faded with an Unarchive action.'
              : <>Click <b>New job</b> to walk through importing a shoot folder.</>}
          </p>
        </section>
      )}

      {jobs && jobs.length > 0 && (
        <div className="job-grid">
          {jobs.map(j => {
            const teams = j.session_count || 0;
            const reviewedCount = j.reviewed_count || 0;
            const pct = teams > 0 ? Math.round((reviewedCount / teams) * 100) : 0;
            const allReviewed = teams > 0 && reviewedCount === teams;
            const isFresh = teams === 0;
            const menuItems = [
              { label: 'Open', onClick: () => nav(`/job/${j.id}`) },
              j.archived
                ? { label: 'Unarchive', onClick: () => unarchiveJob(j) }
                : { label: 'Archive', onClick: () => archiveJob(j) },
              { label: 'Delete', danger: true, onClick: () => setConfirm({ job: j }) },
            ];
            return (
              <Link
                key={j.id}
                to={`/job/${j.id}`}
                className={`job-card ${j.archived ? 'archived' : ''}`}
              >
                <div className="job-card-top">
                  <h3>{j.name}</h3>
                  {j.archived && <span className="archived-pill">Archived</span>}
                  {allReviewed && !j.archived && <span className="reviewed-tick">✓ All reviewed</span>}
                  {j.any_unprocessed && !isFresh && !j.archived && (
                    <span className="pending-pill">pipeline pending</span>
                  )}
                  <CardMenu items={menuItems} />
                </div>
                <p className="job-path muted" title={j.root_path}>{j.root_path}</p>

                <div className="job-stats">
                  <div className="stat">
                    <span className="stat-value">{teams}</span>
                    <span className="stat-label">teams</span>
                  </div>
                  <div className="stat">
                    <span className="stat-value">{j.image_count || 0}</span>
                    <span className="stat-label">images</span>
                  </div>
                  {j.needs_review_count > 0 && (
                    <div className="stat warn">
                      <span className="stat-value">{j.needs_review_count}</span>
                      <span className="stat-label">need review</span>
                    </div>
                  )}
                </div>

                {teams > 0 && (
                  <div className="job-progress">
                    <div className="progress-bar">
                      <div
                        className={`progress-fill ${allReviewed ? 'complete' : ''}`}
                        style={{ width: `${pct}%` }}
                      />
                    </div>
                    <span className="muted">{reviewedCount} of {teams} reviewed</span>
                  </div>
                )}

                <div className="job-card-footer">
                  <span className="muted">
                    {j.archived
                      ? `Archived ${formatDate(j.archived_at)}`
                      : formatDate(j.created_at)}
                  </span>
                </div>
              </Link>
            );
          })}
        </div>
      )}

      {confirm && (
        <ConfirmModal
          title={`Delete "${confirm.job.name}"?`}
          lines={[
            `${confirm.job.session_count || 0} teams`,
            `${confirm.job.image_count || 0} images`,
            'All cluster assignments and manual overrides',
            'Cached thumbnails',
          ]}
          onConfirm={() => deleteJob(confirm.job)}
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

      {showNewShoot && <NewShootModal onClose={() => setShowNewShoot(false)} />}
    </div>
  );
}
