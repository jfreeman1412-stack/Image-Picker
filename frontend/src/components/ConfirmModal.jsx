import { useEffect, useRef } from 'react';

/* Destructive-action confirm dialog. Cancel is default-focused; the confirm
 * button is danger-styled. `lines` is an array of impact bullet strings. */
export default function ConfirmModal({
  title, lines = [], confirmLabel = 'Delete permanently',
  onConfirm, onCancel,
}) {
  const cancelRef = useRef(null);

  useEffect(() => {
    cancelRef.current?.focus();
    const onKey = (e) => { if (e.key === 'Escape') onCancel(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onCancel]);

  return (
    <div className="modal-backdrop" onClick={onCancel}>
      <div className="modal-panel" onClick={(e) => e.stopPropagation()}>
        <h2>{title}</h2>
        {lines.length > 0 && (
          <ul className="confirm-impact">
            {lines.map((l) => <li key={l}>{l}</li>)}
          </ul>
        )}
        <p className="muted">This cannot be undone.</p>
        <div className="actions" style={{ justifyContent: 'flex-end' }}>
          <button ref={cancelRef} onClick={onCancel}>Cancel</button>
          <button className="danger-btn" onClick={onConfirm}>{confirmLabel}</button>
        </div>
      </div>
    </div>
  );
}
