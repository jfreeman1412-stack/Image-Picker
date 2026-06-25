// 2026-06-25 — set-role-defensive (b). Sticky banner that warns when the
// backend is unreachable. Renders nothing in the reachable + initial-load
// cases. Driven by useBackendReachable.js.
//
// Behavior locked in scope-verification:
//   - reachable === null  → render nothing (first-poll-in-flight; don't
//                            flash a false alarm).
//   - reachable === true  → render nothing (the healthy case is silent).
//   - reachable === false → render the warning banner sticky at the top.
//
// Per the build's design decisions: warn prominently, do NOT hard-disable
// mutation buttons. The companion setRole patch in SessionDetail.jsx
// already alerts on failure, so a click during an outage is loud either
// way. Disabling buttons would frustrate operators who clicked just before
// the poll detected the outage; the banner gives them context to retry.
import useBackendReachable from '../useBackendReachable.js';

export default function ConnectivityBanner() {
  const reachable = useBackendReachable();
  if (reachable !== false) return null;

  // Inline styles keep the banner self-contained — no extra styles.css
  // edits to land cleanly on tablet + desktop. Bright enough to grab
  // attention without looking like an error toast.
  return (
    <div
      role="alert"
      style={{
        position: 'sticky',
        top: 0,
        zIndex: 1000,
        background: '#ffe4a8',
        color: '#5b3a00',
        borderBottom: '2px solid #d4881d',
        padding: '8px 16px',
        fontWeight: 600,
        textAlign: 'center',
      }}
    >
      ⚠ Backend unreachable — your edits won't save. Reconnecting…
    </div>
  );
}
