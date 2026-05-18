import { useEffect } from 'react';

/* Bottom toast with an optional action (e.g. Undo). Auto-dismisses after
 * `duration` ms. Render at most one at a time per page. */
export default function Toast({ message, actionLabel, onAction, onClose, duration = 6000 }) {
  useEffect(() => {
    const t = setTimeout(onClose, duration);
    return () => clearTimeout(t);
  }, [onClose, duration]);

  return (
    <div className="toast">
      <span>{message}</span>
      {actionLabel && (
        <button
          className="toast-action"
          onClick={() => { onAction(); onClose(); }}
        >
          {actionLabel}
        </button>
      )}
      <button className="toast-close" onClick={onClose} aria-label="Dismiss">×</button>
    </div>
  );
}
