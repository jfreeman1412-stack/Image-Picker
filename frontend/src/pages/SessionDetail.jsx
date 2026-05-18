import { useEffect, useState, useCallback } from 'react';
import { useParams, Link, useNavigate } from 'react-router-dom';
import ClusterCard from '../components/ClusterCard.jsx';
import ImageModal from '../components/ImageModal.jsx';
import ReviewActionBar from '../components/ReviewActionBar.jsx';
import PipelineProgress from '../components/PipelineProgress.jsx';

export default function SessionDetail() {
  const { id } = useParams();
  const nav = useNavigate();
  const [session, setSession] = useState(null);
  const [clusters, setClusters] = useState([]);
  const [filter, setFilter] = useState('all'); // all | review
  const [previewImage, setPreviewImage] = useState(null);
  const [job, setJob] = useState(null);        // for header progress
  const [siblingIds, setSiblingIds] = useState({ previous_session_id: null, next_session_id: null });
  const [navBusy, setNavBusy] = useState(false);

  const load = async () => {
    const s = await fetch(`/api/sessions/${id}`).then(r => r.json());
    setSession(s);
    const c = await fetch(`/api/sessions/${id}/clusters`).then(r => r.json());
    setClusters(c);
    const sib = await fetch(`/api/sessions/${id}/siblings`).then(r => r.json());
    setSiblingIds(sib);
    if (s.job_id) {
      const j = await fetch(`/api/jobs/${s.job_id}`).then(r => r.json());
      setJob(j);
    } else {
      setJob(null);
    }
  };
  useEffect(() => {
    window.scrollTo(0, 0);
    load();
  }, [id]);

  // Auto-poll while the pipeline is running, even if it was kicked off
  // elsewhere (run-all on the job page, another tab, server restart, etc.).
  useEffect(() => {
    if (session?.status !== 'running') return;
    const t = setInterval(async () => {
      const s = await fetch(`/api/sessions/${id}`).then(r => r.json());
      setSession(s);
      if (s.status === 'done' || s.status === 'error') {
        clearInterval(t);
        load();
      }
    }, 1000);
    return () => clearInterval(t);
  }, [session?.status, id]);

  const run = async () => {
    await fetch(`/api/sessions/${id}/run`, { method: 'POST' });
    const poll = setInterval(async () => {
      const s = await fetch(`/api/sessions/${id}`).then(r => r.json());
      setSession(s);
      if (s.status === 'done' || s.status === 'error') {
        clearInterval(poll);
        load();
      }
    }, 800);  // pretty fast while running so the progress bar feels live
  };

  const reassign = async (image_id, from_cluster_id, to_cluster_id) => {
    await fetch(`/api/sessions/${id}/reassign`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ image_id, from_cluster_id, to_cluster_id }),
    });
    load();
  };

  const rename = async (cluster_id, label) => {
    await fetch(`/api/sessions/${id}/clusters/${cluster_id}/rename`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ label }),
    });
    load();
  };

  const newCluster = async () => {
    await fetch(`/api/sessions/${id}/new-cluster`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ label: 'New player' }),
    });
    load();
  };

  const setRole = async (image_id, cluster_id, role) => {
    await fetch(`/api/sessions/${id}/set-role`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ image_id, cluster_id, role }),
    });
    load();
  };

  const clearOverride = async (image_id, cluster_id) => {
    await fetch(`/api/sessions/${id}/clear-role-override`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ image_id, cluster_id }),
    });
    load();
  };

  const setCoachOverride = async (cluster_id, override) => {
    await fetch(`/api/sessions/${id}/set-coach-override`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ cluster_id, override }),
    });
    load();
  };

  const markAndNext = useCallback(async () => {
    if (!session) return;
    setNavBusy(true);
    try {
      const nextReviewed = !session.reviewed;
      await fetch(`/api/sessions/${id}/reviewed`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reviewed: nextReviewed }),
      });
      // Only chase the next unreviewed when we just MARKED reviewed.
      // When un-marking, just refresh.
      if (nextReviewed) {
        const r = await fetch(`/api/sessions/${id}/next-unreviewed`).then(r => r.json());
        if (r.session_id) nav(`/session/${r.session_id}`);
        else if (session.job_id) nav(`/job/${session.job_id}`);
        else load();
      } else {
        load();
      }
    } finally {
      setNavBusy(false);
    }
  }, [session, id, nav]);

  const skipNext = () => {
    if (siblingIds.next_session_id) nav(`/session/${siblingIds.next_session_id}`);
  };
  const goPrev = () => {
    if (siblingIds.previous_session_id) nav(`/session/${siblingIds.previous_session_id}`);
  };

  // Keyboard: R triggers mark-and-next. Skip when typing in an input.
  useEffect(() => {
    const onKey = (e) => {
      if (e.key !== 'r' && e.key !== 'R') return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const tag = (e.target?.tagName || '').toLowerCase();
      if (tag === 'input' || tag === 'textarea' || e.target?.isContentEditable) return;
      e.preventDefault();
      markAndNext();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [markAndNext]);

  if (!session) return <div className="page">Loading…</div>;

  const visible = filter === 'review'
    ? clusters.filter(c => c.needs_review)
    : clusters;

  // Progress indicator: team N of M · X reviewed
  let progress = null;
  if (job?.sessions?.length) {
    const sortedIds = job.sessions.map(s => s.id).sort((a, b) => a - b);
    const ordinal = sortedIds.indexOf(session.id) + 1;
    const reviewedCount = job.sessions.filter(s => s.reviewed).length;
    progress = (
      <Link to={`/job/${session.job_id}`} className="progress-pill">
        Team {ordinal} of {sortedIds.length} · {reviewedCount} reviewed
      </Link>
    );
  }

  const actionBarProps = {
    reviewed: session.reviewed,
    onMarkAndNext: markAndNext,
    onSkipNext: skipNext,
    onPrev: goPrev,
    prevDisabled: !siblingIds.previous_session_id,
    nextDisabled: !siblingIds.next_session_id,
    busy: navBusy,
  };

  return (
    <div className="page session-page">
      <header className="row-between">
        <div>
          {session.job_id ? (
            <Link to={`/job/${session.job_id}`}>← {session.job_name || 'Job'}</Link>
          ) : (
            <Link to="/">← All jobs</Link>
          )}
          <h1>
            {session.name}
            {session.reviewed && <span className="reviewed-pill">Reviewed ✓</span>}
          </h1>
          <p className="muted">
            {progress && <>{progress} · </>}
            {session.image_count} images · {session.cluster_count} clusters ·
            {' '}status: {session.status} ·
            {' '}needs review: {session.needs_review_count}
          </p>
        </div>
        <div className="actions">
          <button onClick={run} disabled={session.status === 'running'}>
            {session.status === 'running' ? 'Running…' : 'Run pipeline'}
          </button>
          <button onClick={newCluster}>+ New cluster</button>
        </div>
      </header>

      {session.job_id && <ReviewActionBar variant="inline" {...actionBarProps} />}

      <PipelineProgress session={session} />

      <div className="filter-bar">
        <label>
          <input type="radio" name="filter" checked={filter === 'all'}
                 onChange={() => setFilter('all')} />
          All clusters
        </label>
        <label>
          <input type="radio" name="filter" checked={filter === 'review'}
                 onChange={() => setFilter('review')} />
          Needs review only
        </label>
      </div>

      <div className="cluster-grid">
        {visible.map(c => (
          <ClusterCard
            key={c.cluster_id}
            cluster={c}
            allClusters={clusters}
            onReassign={reassign}
            onRename={rename}
            onPreview={setPreviewImage}
            onSetRole={setRole}
            onClearOverride={clearOverride}
            onSetCoachOverride={setCoachOverride}
          />
        ))}
      </div>

      {clusters.length === 0 && session.status !== 'running' && (
        <p className="muted">
          No clusters yet. Click "Run pipeline" to process the session.
        </p>
      )}

      <ImageModal
        src={previewImage?.full_url || previewImage?.thumb_url}
        alt={previewImage?.filename}
        onClose={() => setPreviewImage(null)}
      />

      {session.job_id && (
        <ReviewActionBar variant="sticky" hint {...actionBarProps} />
      )}
    </div>
  );
}
