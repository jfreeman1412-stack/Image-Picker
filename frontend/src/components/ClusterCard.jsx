import { useState, useEffect, useRef } from 'react';

const ROLE_BADGES = {
  team:       { label: 'TEAM',  className: 'badge badge-team' },
  panoramic:  { label: 'PANO',  className: 'badge badge-pano' },
  individual: { label: 'IND',   className: 'badge badge-individual' },
  buddy:      { label: 'BUDDY', className: 'badge badge-buddy' },
  rejected:   { label: 'REJ',   className: 'badge badge-rejected' },
};

const ROLE_OPTIONS = [
  { value: 'team',       label: 'TEAM' },
  { value: 'panoramic',  label: 'PANO' },
  { value: 'individual', label: 'IND' },
  { value: 'buddy',      label: 'BUDDY' },
  { value: 'rejected',   label: 'REJECTED' },
];

export default function ClusterCard({
  cluster,
  allClusters,
  incomplete,      // true | false (needs team/pano) | undefined (readiness not loaded yet)
  missing,         // e.g. ["pano"] — only when incomplete
  onReassign,
  onRename,
  onPreview,
  onSetRole,
  onClearOverride,
  onSetCoachOverride,
  onOpenSession,   // (session_id) → navigate to another team (guest link)
  dragFrom,        // cluster_id an image is currently being dragged from (or null)
  onDragStart,     // (from_cluster_id) → parent tracks the active drag
  onDragEnd,       // () → parent clears the active drag
}) {
  const [editing, setEditing] = useState(false);
  const [label, setLabel] = useState(cluster.label);
  const [openPopover, setOpenPopover] = useState(null); // image_id
  const [dropHover, setDropHover] = useState(false);

  // This card is a valid drop target only while an image from a *different*
  // cluster is being dragged.
  const isDropCandidate = dragFrom != null && dragFrom !== cluster.cluster_id;

  useEffect(() => { setLabel(cluster.label); }, [cluster.label]);

  const submitRename = () => {
    if (label && label !== cluster.label) onRename(cluster.cluster_id, label);
    setEditing(false);
  };

  const handleReassign = (image_id, target_cluster_id) => {
    if (target_cluster_id && target_cluster_id !== cluster.cluster_id) {
      onReassign(image_id, cluster.cluster_id, Number(target_cluster_id));
    }
  };

  // ── Drag source (a thumbnail) ──────────────────────────────────────────
  const handleThumbDragStart = (e, image_id) => {
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('application/json', JSON.stringify({
      image_id, from_cluster_id: cluster.cluster_id,
    }));
    onDragStart && onDragStart(cluster.cluster_id);
  };
  const handleThumbDragEnd = () => { onDragEnd && onDragEnd(); };

  // ── Drop target (this card) ────────────────────────────────────────────
  const handleCardDragOver = (e) => {
    if (!isDropCandidate) return;
    e.preventDefault();                 // required to allow a drop
    e.dataTransfer.dropEffect = 'move';
    if (!dropHover) setDropHover(true);
  };
  const handleCardDragLeave = (e) => {
    // Ignore leaves into child elements — only clear when truly leaving.
    if (e.currentTarget.contains(e.relatedTarget)) return;
    setDropHover(false);
  };
  const handleCardDrop = (e) => {
    setDropHover(false);
    if (!isDropCandidate) return;
    e.preventDefault();
    let payload;
    try {
      payload = JSON.parse(e.dataTransfer.getData('application/json'));
    } catch {
      return;
    }
    if (!payload || payload.from_cluster_id === cluster.cluster_id) return;
    onReassign(payload.image_id, payload.from_cluster_id, cluster.cluster_id);
  };

  const popoverRef = useRef(null);
  useEffect(() => {
    if (openPopover == null) return;
    const onDown = (e) => {
      if (popoverRef.current && !popoverRef.current.contains(e.target)) {
        setOpenPopover(null);
      }
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [openPopover]);

  // Backend already filtered hidden flags out of visible_review_reasons.
  // Fall back to splitting review_reason for older payloads.
  const visibleReasons = cluster.visible_review_reasons
    ?? (cluster.review_reason || '').split(',').map(s => s.trim()).filter(Boolean);
  const showReview = visibleReasons.length > 0;
  const isCoach = cluster.is_coach_for_sort ?? cluster.is_likely_coach;
  const coachManual = (cluster.manual_coach_override ?? 0) !== 0;

  // Per-cluster completeness (distinct from the per-team `reviewed` pill):
  // player needs team+pano, coach needs team. `incomplete` is computed by the
  // parent from review-readiness; undefined until that first fetch lands so we
  // don't flash a green ✓ on everything before the data arrives.
  const completeClass =
    incomplete === undefined ? '' : incomplete ? 'incomplete' : 'complete';

  // Phase 11: a confirmed cross-team guest (phantom sibling). When set, the
  // card is someone else's player caught in a buddy shot here — suppress the
  // coach pill, the team/pano completeness chip, and the review flags (all
  // noise for a non-member) and show a "belongs to <team>" banner instead.
  const guest = cluster.guest_of || null;

  return (
    <div
      className={`cluster-card ${guest ? 'guest' : ''} ${guest ? '' : completeClass} ${(!guest && showReview) ? 'review' : ''} ${isDropCandidate ? 'drop-candidate' : ''} ${dropHover ? 'drop-hover' : ''}`}
      onDragOver={handleCardDragOver}
      onDragLeave={handleCardDragLeave}
      onDrop={handleCardDrop}
    >
      <div className="cluster-header">
        {editing ? (
          <input
            value={label}
            autoFocus
            onChange={e => setLabel(e.target.value)}
            onBlur={submitRename}
            onKeyDown={e => e.key === 'Enter' && submitRename()}
          />
        ) : (
          <h3 onClick={() => setEditing(true)} title="Click to rename">
            {cluster.label}
          </h3>
        )}
        {guest && (
          <span className="guest-pill" title={`Face matches ${guest.name} (distance ${guest.distance})`}>
            Guest ·{' '}
            <button
              className="guest-link"
              onClick={() => onOpenSession && onOpenSession(guest.session_id)}
              title={`Open ${guest.team}`}
            >
              {guest.name} → {guest.team} ↗
            </button>
          </span>
        )}
        {!guest && isCoach && (
          <span className="coach-pill" title="Treated as a coach for sorting">
            COACH{coachManual && <span className="lock" title="Manually set"> 🔒</span>}
          </span>
        )}
        {!guest && onSetCoachOverride && (
          <select
            className="coach-select"
            value={cluster.manual_coach_override ?? 0}
            onChange={e => onSetCoachOverride(cluster.cluster_id, Number(e.target.value))}
            title="Override coach/player detection"
          >
            <option value={0}>Auto{cluster.is_likely_coach ? ' (coach)' : ' (player)'}</option>
            <option value={1}>Coach</option>
            <option value={-1}>Player</option>
          </select>
        )}
        <span className="cluster-count">{cluster.image_count} images</span>
        {!guest && incomplete === false && (
          <span className="complete-chip" title="Team + pano assigned">✓ complete</span>
        )}
        {!guest && incomplete === true && (
          <span
            className="incomplete-chip"
            title={`Missing ${(missing || []).join(' and ')}`}
          >
            ▲ needs {(missing || []).join(' + ')}
          </span>
        )}
        {!guest && visibleReasons.map(reason => (
          <span key={reason} className="review-badge" title={reason}>
            ⚠ {reason}
          </span>
        ))}
      </div>

      {guest && (
        <p className="guest-note">
          These photos are <b>{guest.name}</b> from <b>{guest.team}</b> — caught
          in a buddy shot here, not a member of this team. Handle them on{' '}
          <button className="guest-link" onClick={() => onOpenSession && onOpenSession(guest.session_id)}>
            {guest.team} ↗
          </button>; nothing to pick here.
        </p>
      )}

      <div className="thumb-strip">
        {cluster.images.map((img, idx) => {
          const badge = ROLE_BADGES[img.role] || { label: '·', className: 'badge badge-empty' };
          const isRejected = img.role === 'rejected';
          return (
            <div key={img.image_id} className={`thumb ${isRejected ? 'rejected' : ''}`}>
              <img
                src={img.thumb_url}
                alt={img.filename}
                draggable
                onDragStart={(e) => handleThumbDragStart(e, img.image_id)}
                onDragEnd={handleThumbDragEnd}
                onClick={() => onPreview && onPreview(cluster.cluster_id, idx)}
                title="Drag to another player, or click to enlarge"
              />
              <span
                className={badge.className}
                title="Click to change role"
                onClick={() => setOpenPopover(
                  openPopover === img.image_id ? null : img.image_id
                )}
              >
                {badge.label}
                {img.manual_override && <span className="lock" title="Locked">🔒</span>}
              </span>
              {openPopover === img.image_id && (
                <div className="role-popover" ref={popoverRef}>
                  {ROLE_OPTIONS.map(opt => (
                    <button
                      key={opt.value}
                      onClick={() => {
                        onSetRole(img.image_id, cluster.cluster_id, opt.value);
                        setOpenPopover(null);
                      }}
                    >
                      {opt.label}
                    </button>
                  ))}
                  {img.manual_override && (
                    <button
                      className="reset"
                      onClick={() => {
                        onClearOverride(img.image_id, cluster.cluster_id);
                        setOpenPopover(null);
                      }}
                    >
                      Reset to auto
                    </button>
                  )}
                </div>
              )}
              <div className="filename" title={img.filename}>{img.filename}</div>
              <select
                className="reassign-select"
                defaultValue=""
                onChange={e => {
                  handleReassign(img.image_id, e.target.value);
                  e.target.value = '';
                }}
                title="Reassign to another player"
              >
                <option value="" disabled>→ move to…</option>
                {allClusters
                  .filter(c => c.cluster_id !== cluster.cluster_id)
                  .map(c => (
                    <option key={c.cluster_id} value={c.cluster_id}>
                      {c.label}
                    </option>
                  ))}
              </select>
            </div>
          );
        })}
      </div>
    </div>
  );
}
