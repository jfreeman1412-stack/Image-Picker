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
  // Move-card Phase 1 (2026-06-03): inline controls when this cluster has
  // match_team_mismatch firing. Parent (SessionDetail) supplies the team
  // options + handlers; ClusterCard renders the suggestion + dropdown +
  // dismiss controls and handles the force-confirm inline if the backend
  // returns 409 with safety-guard impact.
  currentSessionName,  // name of the session this cluster currently lives in
  // Move-card Phase 2 (2026-06-08): teamOptions shape expanded to include
  // roster teams without sessions yet (Case 2) — each entry is
  // {name, norm_name, session_id|null, archived}. A null session_id signals
  // the smart-button and dropdown to invoke onMoveToNewSession instead of
  // onMoveWithGuards. The "+ Add new team…" sentinel at the bottom of the
  // dropdown opens the modal that drives Case 3 (onMoveToAddTeam).
  teamOptions,
  onMoveWithGuards,     // async (cluster_id, target_session_id, force) → {ok, impact?}
  onMoveToNewSession,   // async (cluster_id, team_name, force) → {ok, impact?}      (Case 2)
  onMoveToAddTeam,      // async (cluster_id, team_name, force) → {ok, impact?}      (Case 3)
  onDismissCrossTeam,   // async (cluster_id) → void
  onUndismissCrossTeam, // async (cluster_id) → void
}) {
  const [editing, setEditing] = useState(false);
  const [label, setLabel] = useState(cluster.label);
  const [openPopover, setOpenPopover] = useState(null); // image_id
  const [dropHover, setDropHover] = useState(false);
  // Move-card inline state. Phase 2 (2026-06-08) replaced the bare
  // session-id selection with a richer pickedTarget that distinguishes
  // (a) existing-session moves, (b) roster-team-without-session moves
  // (Case 2 — auto-creates the session), and (c) the "+ Add new team…"
  // modal entry point (Case 3). blockedImpact's `retry` carries the
  // original handler so the force-confirm panel re-invokes the right
  // case after the operator confirms.
  const [pickedTarget, setPickedTarget] = useState('');     // dropdown selected value
  const [blockedImpact, setBlockedImpact] = useState(null); // {impact, retry: () => Promise}
  const [moveBusy, setMoveBusy] = useState(false);
  const [addTeamModal, setAddTeamModal] = useState(null);   // {typedName, error} or null

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

  // Move-card Phase 1: derive the visibility + smart-suggestion target.
  // Show the inline controls when match_team_mismatch is in the visible
  // reasons OR the operator already dismissed (so they can undo). Guest
  // clusters never show these — they have their own banner.
  const teamMismatchVisible = visibleReasons.includes('match_team_mismatch');
  const showMoveCardActions =
    !guest && onMoveWithGuards && (teamMismatchVisible || cluster.accepted_cross_team);
  // Move-card Phase 2 (2026-06-08): smart suggestion now finds a target
  // whose name matches the matched player's roster team in EITHER form —
  // an existing session (Case 1) or a roster-only team (Case 2). The
  // button text + the handler branch on whether session_id is null.
  const rosterTeam = (cluster.match?.roster_team || '').toLowerCase();
  const smartTarget = teamOptions?.find(
    (o) => !o.archived && o.name.toLowerCase() === rosterTeam,
  );
  const smartIsCase2 = smartTarget && smartTarget.session_id == null;

  // The dropdown only ever shows non-archived targets, and excludes the
  // smart-suggestion target (it already has its own button). The
  // ADD_NEW_TEAM_SENTINEL is the last option — picking it opens the modal.
  const ADD_NEW_TEAM = '__add_new_team__';
  const dropdownOptions = (teamOptions || []).filter(
    (o) => !o.archived && (!smartTarget || o.norm_name !== smartTarget.norm_name),
  );

  // Centralised force-confirm + busy bookkeeping. `runner` is an async
  // closure that performs the move (already bound to the chosen
  // case-2/3/1 endpoint + force flag). On 409 we capture `runner` so the
  // "Force move" button re-invokes the SAME case path with force=true.
  const runMove = async (runner, retryWithForce) => {
    if (moveBusy) return;
    setMoveBusy(true);
    try {
      const res = await runner();
      if (!res || res.ok) {
        setBlockedImpact(null);
        setPickedTarget('');
        setAddTeamModal(null);
        return;
      }
      // 409 with impact: surface the force-confirm prompt.
      setBlockedImpact({ impact: res.impact, retry: retryWithForce });
    } finally {
      setMoveBusy(false);
    }
  };

  // Three case dispatchers. Each closes over the team key + cluster id.
  const moveToExistingSession = (sessionId, force) =>
    runMove(
      () => onMoveWithGuards(cluster.cluster_id, Number(sessionId), force),
      () => moveToExistingSession(sessionId, true),
    );

  const moveToRosterTeam = (teamName, force) =>
    runMove(
      () => onMoveToNewSession(cluster.cluster_id, teamName, force),
      () => moveToRosterTeam(teamName, true),
    );

  const moveToTypedTeam = (teamName, force) =>
    runMove(
      () => onMoveToAddTeam(cluster.cluster_id, teamName, force),
      () => moveToTypedTeam(teamName, true),
    );

  // Pick from the dropdown. Routes Case 1 vs Case 2 based on session_id.
  const handleDropdownPick = (force) => {
    if (!pickedTarget || pickedTarget === ADD_NEW_TEAM) return;
    const opt = dropdownOptions.find((o) => o.norm_name === pickedTarget);
    if (!opt) return;
    if (opt.session_id != null) {
      moveToExistingSession(opt.session_id, force);
    } else {
      moveToRosterTeam(opt.name, force);
    }
  };

  // The smart-suggestion button. Routes Case 1 vs Case 2 based on the
  // target's session_id.
  const handleSmartMove = (force) => {
    if (!smartTarget) return;
    if (smartTarget.session_id != null) {
      moveToExistingSession(smartTarget.session_id, force);
    } else {
      moveToRosterTeam(smartTarget.name, force);
    }
  };

  // Case 3 modal commit. Validates the typed name isn't blank locally
  // (server-side validation is the source of truth for the rest).
  const handleAddTeamCommit = () => {
    const typed = (addTeamModal?.typedName || '').trim();
    if (!typed) {
      setAddTeamModal({ ...(addTeamModal || {}), error: 'Team name required.' });
      return;
    }
    moveToTypedTeam(typed, false);
  };

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

      {/* Move-card Phase 1 (2026-06-03): inline smart-suggestion + dropdown
          + dismiss. Visible only when match_team_mismatch is firing or
          when the operator already dismissed (so they can undo). */}
      {showMoveCardActions && (
        <div className="move-card-actions">
          {cluster.accepted_cross_team ? (
            <div className="dismissed-banner">
              <span>
                ✓ Accepted as intentional cross-team appearance
                {cluster.match?.player_name && (
                  <> — <b>{cluster.match.player_name}</b> rostered with{' '}
                  <b>{cluster.match.roster_team}</b></>
                )}.
              </span>
              <button
                className="ghost"
                onClick={() => onUndismissCrossTeam(cluster.cluster_id)}
              >
                Undo dismiss
              </button>
            </div>
          ) : blockedImpact ? (
            <div className="move-blocked-banner">
              <p style={{ margin: 0 }}>
                <b>Move blocked.</b>{' '}
                {blockedImpact.impact.manual_role_overrides > 0 && (
                  <>{blockedImpact.impact.manual_role_overrides} manual
                    role lock{blockedImpact.impact.manual_role_overrides === 1 ? '' : 's'}
                    {' '}would be carried over.</>
                )}
                {blockedImpact.impact.source_session_reviewed && (
                  <> Source session is marked reviewed.</>
                )}
                {' '}Force the move?
              </p>
              <div className="actions" style={{ gap: 8, marginTop: 6 }}>
                <button
                  className="danger"
                  disabled={moveBusy}
                  onClick={() => blockedImpact.retry && blockedImpact.retry()}
                >
                  Force move
                </button>
                <button
                  className="ghost"
                  onClick={() => setBlockedImpact(null)}
                >
                  Cancel
                </button>
              </div>
            </div>
          ) : (
            <>
              <p className="move-suggestion-text" style={{ margin: 0 }}>
                <b>Likely on wrong team:</b>{' '}
                {cluster.match?.player_name && (
                  <><b>{cluster.match.player_name}</b> is rostered with{' '}
                  <b>{cluster.match.roster_team}</b> but this card is in{' '}
                  <b>{currentSessionName}</b>.</>
                )}
              </p>
              <div className="actions" style={{ gap: 8, marginTop: 6, flexWrap: 'wrap' }}>
                {smartTarget && (
                  <button
                    className="primary"
                    disabled={moveBusy}
                    onClick={() => handleSmartMove(false)}
                    title={smartIsCase2
                      ? `Create a new session named "${smartTarget.name}" and move the card there`
                      : `Move into the existing "${smartTarget.name}" session`}
                  >
                    Move card to {smartTarget.name}
                    {smartIsCase2 && ' (new session)'}
                  </button>
                )}
                <select
                  value={pickedTarget}
                  onChange={(e) => {
                    const v = e.target.value;
                    if (v === ADD_NEW_TEAM) {
                      setPickedTarget('');
                      setAddTeamModal({ typedName: '', error: null });
                      return;
                    }
                    setPickedTarget(v);
                  }}
                  disabled={moveBusy}
                >
                  <option value="">
                    {smartTarget ? 'Pick different team…' : 'Pick team…'}
                  </option>
                  {dropdownOptions.map((opt) => (
                    <option key={opt.norm_name} value={opt.norm_name}>
                      {opt.name}
                      {opt.session_id == null ? ' (no session yet)' : ''}
                    </option>
                  ))}
                  <option disabled>──────────</option>
                  <option value={ADD_NEW_TEAM}>+ Add new team…</option>
                </select>
                {pickedTarget && pickedTarget !== ADD_NEW_TEAM && (
                  <button
                    disabled={moveBusy}
                    onClick={() => handleDropdownPick(false)}
                  >
                    Move
                  </button>
                )}
                <button
                  className="ghost"
                  disabled={moveBusy}
                  onClick={() => onDismissCrossTeam(cluster.cluster_id)}
                >
                  Dismiss as cross-team
                </button>
              </div>

              {/* Move-card Phase 2: "+ Add new team…" modal (Case 3 entry).
                  Inline panel — kept lightweight; the backend handles the
                  three sub-cases (typed name matches existing session →
                  Case 1, matches roster team → Case 2, brand new → Case 3
                  with optional add_walkup_player). */}
              {addTeamModal && (
                <div
                  className="add-team-modal"
                  style={{
                    marginTop: 8, padding: 8,
                    border: '1px solid #aaa', borderRadius: 4,
                    background: '#fafafa',
                  }}
                >
                  <p style={{ margin: '0 0 6px 0' }}>
                    <b>Add new team for this card:</b>
                  </p>
                  <div className="actions" style={{ gap: 8, flexWrap: 'wrap' }}>
                    <input
                      type="text"
                      autoFocus
                      placeholder="Team name (e.g. 11U Wildcats)"
                      value={addTeamModal.typedName}
                      onChange={(e) => setAddTeamModal({
                        ...addTeamModal,
                        typedName: e.target.value,
                        error: null,
                      })}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') handleAddTeamCommit();
                        if (e.key === 'Escape') setAddTeamModal(null);
                      }}
                      disabled={moveBusy}
                      style={{ flex: 1, minWidth: 180 }}
                    />
                    <button
                      className="primary"
                      disabled={moveBusy}
                      onClick={handleAddTeamCommit}
                    >
                      Create & move
                    </button>
                    <button
                      className="ghost"
                      disabled={moveBusy}
                      onClick={() => setAddTeamModal(null)}
                    >
                      Cancel
                    </button>
                  </div>
                  {addTeamModal.error && (
                    <p style={{ margin: '6px 0 0 0', color: '#c00' }}>
                      {addTeamModal.error}
                    </p>
                  )}
                  <p style={{ margin: '6px 0 0 0', fontSize: '0.85em', color: '#666' }}>
                    If this name matches a team you've already added (any case), we'll move
                    the card into that team instead of creating a duplicate.
                  </p>
                </div>
              )}
            </>
          )}
        </div>
      )}

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
