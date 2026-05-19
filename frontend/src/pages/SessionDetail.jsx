import { useEffect, useState, useCallback, useRef } from 'react';
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
  // Modal preview is tracked as { cluster_id, index }, NOT a snapshot image
  // list: the images shown are always re-derived from the freshly-loaded
  // `clusters` by cluster_id below, so a role change → load() refetch can't
  // desync the modal. index is clamped on read in case the cluster shrank.
  const [preview, setPreview] = useState(null);
  const [job, setJob] = useState(null);        // for header progress
  const [siblingIds, setSiblingIds] = useState({ previous_session_id: null, next_session_id: null });
  const [navBusy, setNavBusy] = useState(false);
  const [readiness, setReadiness] = useState({ ready: true, incomplete_clusters: [] });
  const [readinessLoaded, setReadinessLoaded] = useState(false);
  const [showGate, setShowGate] = useState(false); // validation-gate modal
  const [dragFrom, setDragFrom] = useState(null);  // source cluster_id of active thumbnail drag
  const [loadError, setLoadError] = useState(null); // HTTP status if the session failed to load
  // cluster_id → missing roles (e.g. ["pano"]); absent ⇒ complete. Same source
  // as the validation gate, so the card's complete/incomplete badge can never
  // disagree with "would this block Mark reviewed?".
  const incompleteById = new Map(
    (readiness.incomplete_clusters || []).map(c => [c.cluster_id, c.missing])
  );
  const incompleteClusterIds = new Set(incompleteById.keys());
  // Read in the window keydown handler so R is ignored while the modal is
  // open (modal owns the keyboard then) without re-subscribing every arrow.
  const previewOpenRef = useRef(false);
  previewOpenRef.current = preview != null;

  // A failed fetch must NOT poison state (e.g. a 404 body becoming `clusters`,
  // then `clusters.map` throwing and blanking the whole app). The session GET
  // is the gate: if it fails we surface a recoverable error screen and bail;
  // every other response is shape-checked before it lands in state.
  const load = async () => {
    const sRes = await fetch(`/api/sessions/${id}`);
    if (!sRes.ok) { setLoadError(sRes.status); return; }
    const s = await sRes.json();
    setLoadError(null);
    setSession(s);

    const cRes = await fetch(`/api/sessions/${id}/clusters`);
    const c = cRes.ok ? await cRes.json() : [];
    setClusters(Array.isArray(c) ? c : []);

    const sibRes = await fetch(`/api/sessions/${id}/siblings`);
    setSiblingIds(sibRes.ok
      ? await sibRes.json()
      : { previous_session_id: null, next_session_id: null });

    const rrRes = await fetch(`/api/sessions/${id}/review-readiness`);
    if (rrRes.ok) {
      setReadiness(await rrRes.json());
      setReadinessLoaded(true);
    }

    if (s.job_id) {
      const jRes = await fetch(`/api/jobs/${s.job_id}`);
      setJob(jRes.ok ? await jRes.json() : null);
    } else {
      setJob(null);
    }
  };
  useEffect(() => {
    window.scrollTo(0, 0);
    setLoadError(null);   // don't flash the previous session's error screen
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

  const markAndNext = useCallback(async (force = false) => {
    if (!session) return;
    const nextReviewed = !session.reviewed;
    // Gate only applies when MARKING reviewed (not when un-marking).
    if (nextReviewed && !readiness.ready && !force) {
      setShowGate(true);
      return;
    }
    setNavBusy(true);
    try {
      const res = await fetch(`/api/sessions/${id}/reviewed`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reviewed: nextReviewed, force }),
      });
      if (!res.ok) {
        // Backend guard tripped (e.g. raced past the pre-check) — surface it.
        setShowGate(true);
        return;
      }
      setShowGate(false);
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
  }, [session, id, nav, readiness]);

  const skipNext = () => {
    if (siblingIds.next_session_id) nav(`/session/${siblingIds.next_session_id}`);
  };
  const goPrev = () => {
    if (siblingIds.previous_session_id) nav(`/session/${siblingIds.previous_session_id}`);
  };

  // ── Drag-to-reassign: track the active drag so cards can highlight, and
  // auto-scroll the page when the pointer nears the top/bottom edge (native
  // HTML5 DnD doesn't scroll on its own). ────────────────────────────────────
  const onThumbDragStart = useCallback((from_cluster_id) => {
    setDragFrom(from_cluster_id);
  }, []);
  const onThumbDragEnd = useCallback(() => setDragFrom(null), []);

  useEffect(() => {
    if (dragFrom == null) return;
    const EDGE = 90;        // px from a viewport edge that triggers scrolling
    const MAX_SPEED = 22;   // px per tick at the very edge
    let pointerY = null;
    let raf = null;

    const onDragOver = (e) => { pointerY = e.clientY; };
    const tick = () => {
      if (pointerY != null) {
        const h = window.innerHeight;
        if (pointerY < EDGE) {
          const f = (EDGE - pointerY) / EDGE;
          window.scrollBy(0, -Math.ceil(MAX_SPEED * f));
        } else if (pointerY > h - EDGE) {
          const f = (pointerY - (h - EDGE)) / EDGE;
          window.scrollBy(0, Math.ceil(MAX_SPEED * f));
        }
      }
      raf = requestAnimationFrame(tick);
    };
    window.addEventListener('dragover', onDragOver);
    raf = requestAnimationFrame(tick);
    return () => {
      window.removeEventListener('dragover', onDragOver);
      if (raf) cancelAnimationFrame(raf);
    };
  }, [dragFrom]);

  // Keyboard: R triggers mark-and-next. Skip when typing in an input.
  useEffect(() => {
    const onKey = (e) => {
      if (e.key !== 'r' && e.key !== 'R') return;
      if (previewOpenRef.current) return; // modal keys take precedence while open
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const tag = (e.target?.tagName || '').toLowerCase();
      if (tag === 'input' || tag === 'textarea' || e.target?.isContentEditable) return;
      e.preventDefault();
      markAndNext();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [markAndNext]);

  // Close the modal if its cluster vanished (deleted/merged) or emptied out.
  useEffect(() => {
    if (!preview) return;
    const c = clusters.find(c => c.cluster_id === preview.cluster_id);
    if (!c || !c.images?.length) setPreview(null);
  }, [preview, clusters]);

  if (loadError) return (
    <div className="page">
      <p><Link to="/">← All jobs</Link></p>
      <h1>Couldn't load this team</h1>
      <p className="muted">
        The server returned{' '}
        {loadError === 404
          ? 'a 404 — this session id was not found.'
          : `an error (HTTP ${loadError}).`}{' '}
        This usually means the backend is running against a different database
        than the one that has this team (e.g. a second copy of the project, or
        a backend that needs restarting). Pick a team from the list, or retry.
      </p>
      <div className="actions">
        <button onClick={() => { setLoadError(null); load(); }}>Retry</button>
        {session?.job_id && (
          <Link to={`/job/${session.job_id}`}>
            <button className="ghost">Back to job</button>
          </Link>
        )}
      </div>
    </div>
  );

  if (!session) return <div className="page">Loading…</div>;

  const previewCluster = preview
    ? clusters.find(c => c.cluster_id === preview.cluster_id)
    : null;
  const previewImages = previewCluster?.images ?? [];
  const previewIndex = previewImages.length
    ? Math.min(preview.index, previewImages.length - 1)
    : 0;

  // A cluster "needs attention" if it has any visible (non-hidden) review
  // flags OR it's incomplete per review-readiness (missing team/pano —
  // populated by the Section 6 readiness fetch). This matches "would this
  // block Mark reviewed & next?" rather than the raw needs_review flag,
  // which is true for almost everything and ignores flag-visibility.
  const clusterNeedsAttention = (c) => {
    const reasons = c.visible_review_reasons
      ?? (c.review_reason || '').split(',').map(s => s.trim()).filter(Boolean);
    if (reasons.length > 0) return true;
    return incompleteClusterIds.has(c.cluster_id);
  };

  const visible = filter === 'review'
    ? clusters.filter(clusterNeedsAttention)
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
    onMarkAndNext: () => markAndNext(false),
    onSkipNext: skipNext,
    onPrev: goPrev,
    prevDisabled: !siblingIds.previous_session_id,
    nextDisabled: !siblingIds.next_session_id,
    busy: navBusy,
    markGated: !readiness.ready,
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
            incomplete={readinessLoaded ? incompleteClusterIds.has(c.cluster_id) : undefined}
            missing={incompleteById.get(c.cluster_id)}
            onReassign={reassign}
            onRename={rename}
            onPreview={(cluster_id, index) => setPreview({ cluster_id, index })}
            onSetRole={setRole}
            onClearOverride={clearOverride}
            onSetCoachOverride={setCoachOverride}
            dragFrom={dragFrom}
            onDragStart={onThumbDragStart}
            onDragEnd={onThumbDragEnd}
          />
        ))}
      </div>

      {clusters.length === 0 && session.status !== 'running' && (
        <p className="muted">
          No clusters yet. Click "Run pipeline" to process the session.
        </p>
      )}

      <ImageModal
        images={previewImages}
        index={previewIndex}
        clusterId={preview?.cluster_id}
        onIndexChange={(i) => setPreview(p => (p ? { ...p, index: i } : p))}
        onClose={() => setPreview(null)}
        onSetRole={setRole}
        onClearOverride={clearOverride}
      />

      {session.job_id && (
        <ReviewActionBar variant="sticky" hint {...actionBarProps} />
      )}

      {showGate && (
        <div className="modal-backdrop" onClick={() => setShowGate(false)}>
          <div className="modal-panel" onClick={(e) => e.stopPropagation()}>
            <h2>Can't mark reviewed yet</h2>
            <p className="muted">
              {readiness.incomplete_clusters.length} cluster
              {readiness.incomplete_clusters.length === 1 ? '' : 's'} still
              need a team/pano pick:
            </p>
            <ul className="confirm-impact">
              {readiness.incomplete_clusters.map((c) => (
                <li key={c.cluster_id}>
                  <b>{c.label}</b> — missing {c.missing.join(' and ')}
                </li>
              ))}
            </ul>
            <div className="actions" style={{ justifyContent: 'space-between' }}>
              <button
                className="ghost"
                onClick={() => markAndNext(true)}
                disabled={navBusy}
              >
                Skip validation and mark reviewed anyway
              </button>
              <button onClick={() => setShowGate(false)}>Keep reviewing</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
