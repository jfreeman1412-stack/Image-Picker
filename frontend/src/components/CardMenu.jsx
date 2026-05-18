import { useState, useRef, useEffect } from 'react';

/* A ⋯ overflow menu. `items` = [{label, onClick, danger?}].
 * Stops click propagation so it works on top of clickable cards. */
export default function CardMenu({ items }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e) => {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [open]);

  return (
    <div className="card-menu" ref={ref}>
      <button
        className="card-menu-btn"
        title="Actions"
        onClick={(e) => { e.preventDefault(); e.stopPropagation(); setOpen(o => !o); }}
      >
        ⋯
      </button>
      {open && (
        <div className="card-menu-pop" onClick={(e) => e.preventDefault()}>
          {items.map((it) => (
            <button
              key={it.label}
              className={`card-menu-item ${it.danger ? 'danger' : ''}`}
              onClick={(e) => {
                e.preventDefault();
                e.stopPropagation();
                setOpen(false);
                it.onClick();
              }}
            >
              {it.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
