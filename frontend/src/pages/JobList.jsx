import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';

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
  const [jobs, setJobs] = useState(null);
  const [view, setView] = useState('active'); // 'active' | 'archived'

  const load = async () => {
    const q = view === 'archived' ? '?archived=true' : '';
    const j = await fetch(`/api/jobs${q}`).then(r => r.json());
    setJobs(j);
  };
  useEffect(() => { setJobs(null); load(); }, [view]);

  return (
    <div className="page">
      <header className="row-between">
        <div>
          <h1>Player Sort</h1>
          <p className="muted">All shoot jobs.</p>
        </div>
        <div className="actions">
          <Link to="/settings"><button className="ghost">Settings</button></Link>
          <Link to="/job/new"><button className="primary-cta">+ New job</button></Link>
        </div>
      </header>

      <div className="view-tabs">
        <button
          className={`tab ${view === 'active' ? 'active' : ''}`}
          onClick={() => setView('active')}
        >Active</button>
        <button
          className={`tab ${view === 'archived' ? 'active' : ''}`}
          onClick={() => setView('archived')}
        >Archived</button>
      </div>

      {jobs === null && <p className="muted">Loading…</p>}

      {jobs && jobs.length === 0 && view === 'active' && (
        <section className="empty-state">
          <h2>No jobs yet</h2>
          <p className="muted">
            Click <b>New job</b> to walk through importing a shoot folder.
          </p>
        </section>
      )}

      {jobs && jobs.length === 0 && view === 'archived' && (
        <section className="empty-state">
          <h2>No archived jobs</h2>
          <p className="muted">
            Archived jobs land here. They keep working — just out of sight.
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
    </div>
  );
}
