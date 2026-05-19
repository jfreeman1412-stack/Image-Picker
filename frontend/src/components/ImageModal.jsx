import { useEffect, useState } from 'react';

// Single-key → role. `u` is handled separately (clear override, not set-role).
const ROLE_KEYS = {
  t: 'team',
  p: 'panoramic',
  i: 'individual',
  b: 'buddy',
  x: 'rejected',
};

const ROLE_LABEL = {
  team: 'TEAM',
  panoramic: 'PANO',
  individual: 'IND',
  buddy: 'BUDDY',
  rejected: 'REJ',
};
const ROLE_TAG_CLASS = {
  team: 'badge-team',
  panoramic: 'badge-pano',
  individual: 'badge-individual',
  buddy: 'badge-buddy',
  rejected: 'badge-rejected',
};

/**
 * Full-size preview with keyboard-driven review.
 *
 * Props: `images` (the cluster's image array, same order as the card),
 * `index` (which one is shown), `clusterId`, `onIndexChange(i)`, `onClose`,
 * `onSetRole(image_id, cluster_id, role)`, `onClearOverride(image_id, cluster_id)`.
 *
 * The parent (SessionDetail) re-derives `images` from the freshly-loaded
 * clusters by cluster_id after every role change and preserves `index`, so
 * this component stays a thin view of server truth — no local role copy to
 * drift out of sync.
 */
export default function ImageModal({
  images,
  index,
  clusterId,
  onIndexChange,
  onClose,
  onSetRole,
  onClearOverride,
}) {
  const [showHelp, setShowHelp] = useState(false);

  const open = Array.isArray(images) && images.length > 0;
  const img = open ? images[index] : null;

  useEffect(() => {
    if (!open) return;
    const last = images.length - 1;
    const advance = () => onIndexChange(Math.min(last, index + 1)); // clamp, no wrap

    const onKey = (e) => {
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const tag = (e.target?.tagName || '').toLowerCase();
      if (tag === 'input' || tag === 'textarea' || e.target?.isContentEditable) return;

      if (e.key === 'Escape') { onClose(); return; }
      if (e.key === 'ArrowLeft') {
        e.preventDefault();
        onIndexChange(Math.max(0, index - 1));      // stop at first
        return;
      }
      if (e.key === 'ArrowRight') {
        e.preventDefault();
        onIndexChange(Math.min(last, index + 1));   // stop at last
        return;
      }
      if (e.key === '?') { e.preventDefault(); setShowHelp((s) => !s); return; }

      const k = e.key.toLowerCase();
      if (k === 'u') {
        e.preventDefault();
        onClearOverride(img.image_id, clusterId);
        if (!e.shiftKey) advance();
        return;
      }
      if (ROLE_KEYS[k]) {
        e.preventDefault();
        onSetRole(img.image_id, clusterId, ROLE_KEYS[k]);
        if (!e.shiftKey) advance();   // Shift+key: set without advancing
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, images, index, clusterId, img, onIndexChange, onClose, onSetRole, onClearOverride]);

  if (!open) return null;

  const prev = index > 0 ? images[index - 1] : null;
  const next = index < images.length - 1 ? images[index + 1] : null;

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <button className="modal-close" onClick={onClose} aria-label="Close">×</button>
      <div className="modal-content" onClick={(e) => e.stopPropagation()}>
        <div className="modal-bar">
          <span className="modal-pos">
            Image {index + 1} of {images.length} · {img.filename}
            {img.role && (
              <span className={`modal-roletag ${ROLE_TAG_CLASS[img.role] || ''}`}>
                {ROLE_LABEL[img.role] || img.role}
                {img.manual_override && <span className="lock"> 🔒</span>}
              </span>
            )}
          </span>
          <button
            className="modal-keys"
            onClick={() => setShowHelp((s) => !s)}
            title="Keyboard shortcuts"
          >
            ? keys
          </button>
        </div>

        <img src={img.full_url || img.thumb_url} alt={img.filename} />

        {showHelp && (
          <div className="modal-help">
            <div><b>← / →</b> previous / next image (stops at the ends)</div>
            <div>
              <b>T</b> team · <b>P</b> pano · <b>I</b> individual ·
              {' '}<b>B</b> buddy · <b>X</b> rejected
            </div>
            <div><b>U</b> reset to auto · <b>Shift</b> + any role key sets without advancing</div>
            <div><b>Esc</b> close · <b>?</b> toggle this help</div>
          </div>
        )}
      </div>

      {/* Preload the neighbours so ←/→ feels instant. */}
      {prev?.full_url && <img src={prev.full_url} alt="" style={{ display: 'none' }} />}
      {next?.full_url && <img src={next.full_url} alt="" style={{ display: 'none' }} />}
    </div>
  );
}
