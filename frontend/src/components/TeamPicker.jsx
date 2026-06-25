// 2026-06-25 (Phase B) — extracted from ClusterCard.jsx's move picker so
// both MOVE and COPY can drive the same dropdown UI with their own action
// callbacks. Parameterized per the locked design: action callbacks are
// onPickExisting / onPickRoster / onPickTyped, mode flips the commit
// button label between "Move" and "Copy". Add-new-team modal stays in
// ClusterCard (it owns the addTeamModal state).
//
// Props (load-bearing — don't drift without a corresponding ClusterCard
// update):
//   teamOptions:      [{session_id|null, name, norm_name, archived}]
//   excludeNormName:  optional — filters out a team from the list (used by
//                     the smart-panel flow to suppress the smart-suggestion
//                     target since it has its own primary button).
//   pickedTarget:     controlled value (norm_name of the chosen team, or '').
//   onPickedTargetChange: (newValue) => void.
//   onAddTeamClicked: () => void — fires when the operator picks the
//                     "+ Add new team…" sentinel.
//   onCommit:         () => void — fires when the operator clicks the
//                     commit button (Move/Copy). ClusterCard dispatches to
//                     the right endpoint via its handleDropdownPick equivalent.
//   onCancel:         () => void — fires when the operator clicks Cancel.
//   busy:             boolean — disables interactive elements during an
//                     in-flight request.
//   mode:             'move' | 'copy' — controls the commit button label
//                     and tooltip copy.
//   placeholder:      string — placeholder option text (e.g. "Pick team…"
//                     or "Pick different team…").

const ADD_NEW_TEAM = '__add_new_team__';

export default function TeamPicker({
  teamOptions,
  excludeNormName = null,
  pickedTarget,
  onPickedTargetChange,
  onAddTeamClicked,
  onCommit,
  onCancel,
  busy,
  mode,
  placeholder = 'Pick team…',
}) {
  const dropdownOptions = (teamOptions || []).filter(
    (o) => !o.archived && (!excludeNormName || o.norm_name !== excludeNormName),
  );
  const commitLabel = mode === 'copy' ? 'Copy' : 'Move';
  return (
    <>
      <select
        value={pickedTarget}
        onChange={(e) => {
          const v = e.target.value;
          if (v === ADD_NEW_TEAM) {
            onAddTeamClicked();
            return;
          }
          onPickedTargetChange(v);
        }}
        disabled={busy}
      >
        <option value="">{placeholder}</option>
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
        <button disabled={busy} onClick={onCommit}>
          {commitLabel}
        </button>
      )}
      <button className="ghost" disabled={busy} onClick={onCancel}>
        Cancel
      </button>
    </>
  );
}

// Export the sentinel so ClusterCard's state can compare against it.
export { ADD_NEW_TEAM };
