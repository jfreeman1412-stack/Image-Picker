// Phase B.3 §6 — the lossless home for sync-time rejections. Offline, the server
// can't reject a capture in front of the volunteer, so failures surface here:
//  • failed_quality — the server's quality gate (blurry / multiple faces / no
//    face). Shows the server's own message.
//  • failed_gone — the target player or shoot no longer exists on the server.
// Nothing is auto-deleted. Re-shoot replaces the failed item with a fresh
// capture; Discard is the only non-200 deletion (confirmation-gated).
// Items are the CURRENT shoot's failed queue records (passed from App).
// See ../PHASE_B3_OFFLINE_CAPTURE.md.
//
// Phase B.6 §6 (2026-06-08) — operator-driven SALVAGE for multiple_faces.
// When a rejection is salvageable (failReason === 'multiple_faces' OR the
// `/detect` probe finds ≥2 faces on a pre-B.6 item without failReason), an
// inline "Select face" panel appears: the photo renders with each detected
// face as a tappable box; the operator taps the right one and confirms.
// On success the queue item is removed and the player is marked synced.
//
// The probe-and-self-classify path lets B.6 retroactively rescue items
// captured BEFORE Section 4 stamped `failReason`. detect is read-only on
// the server side so this is cheap.
import { useEffect, useRef, useState } from 'react';
import { getCaptureBytes } from './db.js';

export default function NeedsAttention({
  job, items, onReshoot, onDiscard, onResolved, onBack,
}) {
  const [confirmId, setConfirmId] = useState(null);
  // selectId: id of the item currently in the Select-face flow (or null).
  const [selectId, setSelectId] = useState(null);

  return (
    <main className="screen roster-screen">
      <header className="roster-header">
        <button className="link-back" onClick={onBack}>← Roster</button>
        <div className="roster-titles">
          <h1 className="roster-title">Needs attention</h1>
          <span className="roster-count">{job?.name}</span>
        </div>
      </header>

      <div className="roster-body">
        {items.length === 0 ? (
          <p className="muted roster-pad">Nothing needs attention for this shoot.</p>
        ) : (
          <ul className="roster-list">
            {items.map((item) => {
              const gone = item.status === 'failed_gone';
              // Phase B.6: multiple_faces is salvageable. failReason was
              // added Section 4 so older items don't carry it — we offer
              // the salvage action and self-classify via /detect on open.
              const declaredMulti = item.failReason === 'multiple_faces';
              const maybeSalvageable = !gone && (
                declaredMulti || item.failReason == null
              );
              return (
                <li key={item.id} className="attention-item">
                  <div className="attention-main">
                    <span className="roster-name">{item.playerName}</span>
                    <span className="roster-team">{item.team}</span>
                  </div>
                  <p className="attention-reason">
                    {gone ? 'Target removed on the server: ' : 'Rejected on sync: '}
                    {item.lastError}
                  </p>
                  {confirmId === item.id ? (
                    <div className="controls-inline">
                      <span className="muted small">Discard this photo?</span>
                      <button className="btn ghost" onClick={() => setConfirmId(null)}>Cancel</button>
                      <button className="btn danger" onClick={() => onDiscard(item)}>Discard</button>
                    </div>
                  ) : selectId === item.id ? (
                    <FaceSelectPanel
                      item={item}
                      onResolved={onResolved}
                      onCancel={() => setSelectId(null)}
                    />
                  ) : (
                    <div className="controls-inline">
                      {maybeSalvageable && (
                        <button className="btn primary" onClick={() => setSelectId(item.id)}>
                          Select face
                        </button>
                      )}
                      {!gone && (
                        <button className="btn" onClick={() => onReshoot(item)}>Re-shoot</button>
                      )}
                      <button className="btn ghost" onClick={() => setConfirmId(item.id)}>Discard</button>
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </main>
  );
}


// ── Phase B.6 §6 — Select-face panel ─────────────────────────────────────
//
// Inline lifecycle:
//   loading → choose → resolving → done | error | nofaces
// On done, App's onResolved removes the queue item + flips ✓ green.
//
// detect runs over a fresh fetch of the stored bytes. The retained-bytes
// invariant (B.3 losslessness) means an offline-stored capture still has
// its bytes here — no extra machinery needed.
function FaceSelectPanel({ item, onResolved, onCancel }) {
  const [phase, setPhase] = useState('loading');      // loading | choose | resolving | error | nofaces
  const [error, setError] = useState(null);
  const [imageUrl, setImageUrl] = useState(null);
  const [imageMime, setImageMime] = useState('image/jpeg');
  const [bytes, setBytes] = useState(null);
  const [detect, setDetect] = useState(null);         // { width, height, faces: [...] }
  const [selectedIndex, setSelectedIndex] = useState(null);
  const imgRef = useRef(null);
  const [imgRect, setImgRect] = useState(null);       // { dispW, dispH } rendered px

  // Fetch bytes + run /detect on mount.
  useEffect(() => {
    let cancelled = false;
    let url = null;
    (async () => {
      try {
        const rec = await getCaptureBytes(item.id);
        if (cancelled) return;
        if (!rec?.bytes) {
          setError('Photo data is missing on the device.');
          setPhase('error');
          return;
        }
        const blob = new Blob([rec.bytes], { type: rec.mime || 'image/jpeg' });
        url = URL.createObjectURL(blob);
        setImageUrl(url);
        setImageMime(rec.mime || 'image/jpeg');
        setBytes(rec.bytes);

        const fd = new FormData();
        fd.append('file', blob, 'capture.jpg');
        const res = await fetch(
          `/api/players/${item.playerId}/references/shoot/${item.jobId}/detect`,
          { method: 'POST', body: fd },
        );
        if (cancelled) return;
        if (!res.ok) {
          setError(`Detect failed (HTTP ${res.status}). Connect and try again.`);
          setPhase('error');
          return;
        }
        const body = await res.json();
        setDetect(body);
        // Self-classify: if fewer than 2 faces, the salvage doesn't apply —
        // fall through to the existing Re-shoot / Discard path.
        if ((body?.faces || []).length < 2) {
          setPhase('nofaces');
          return;
        }
        setPhase('choose');
      } catch (e) {
        if (cancelled) return;
        setError('Connect to resolve — detect failed.');
        setPhase('error');
      }
    })();
    return () => {
      cancelled = true;
      if (url) URL.revokeObjectURL(url);
    };
  }, [item.id, item.playerId, item.jobId]);

  // Measure rendered image size for scaling the boxes from source coords.
  const onImgLoad = () => {
    const el = imgRef.current;
    if (!el) return;
    setImgRect({ dispW: el.clientWidth, dispH: el.clientHeight });
  };
  useEffect(() => {
    const onResize = () => onImgLoad();
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);

  const submit = async () => {
    if (selectedIndex == null || !detect || !bytes) return;
    setPhase('resolving');
    try {
      const face = detect.faces[selectedIndex];
      const blob = new Blob([bytes], { type: imageMime });
      const fd = new FormData();
      fd.append('file', blob, 'capture.jpg');
      fd.append('selected_bbox', JSON.stringify(face.bbox));
      const res = await fetch(
        `/api/players/${item.playerId}/references/shoot/${item.jobId}/resolve`,
        { method: 'POST', body: fd },
      );
      if (res.ok) {
        await onResolved(item);
        return;
      }
      const body = await res.json().catch(() => ({}));
      setError(body?.detail?.message || `Resolve failed (HTTP ${res.status}).`);
      setPhase('error');
    } catch {
      setError('Connect to resolve — upload failed.');
      setPhase('error');
    }
  };

  // Scale a [x,y,w,h] source bbox to displayed-pixel rect.
  const scaledRect = (bbox) => {
    if (!imgRect || !detect) return null;
    const sx = imgRect.dispW / detect.width;
    const sy = imgRect.dispH / detect.height;
    return {
      left: bbox[0] * sx, top: bbox[1] * sy,
      width: bbox[2] * sx, height: bbox[3] * sy,
    };
  };

  return (
    <div className="face-select-panel" style={{ marginTop: 8 }}>
      {phase === 'loading' && (
        <p className="muted small">Loading photo & detecting faces…</p>
      )}
      {phase === 'error' && (
        <>
          <p className="warn small">{error}</p>
          <div className="controls-inline">
            <button className="btn ghost" onClick={onCancel}>Close</button>
          </div>
        </>
      )}
      {phase === 'nofaces' && (
        <>
          <p className="muted small">
            Only {detect?.faces?.length ?? 0} face detected. Select-face
            applies when there are 2+ faces in the photo. Use Re-shoot or
            Discard from this item's actions instead.
          </p>
          <div className="controls-inline">
            <button className="btn ghost" onClick={onCancel}>Close</button>
          </div>
        </>
      )}
      {(phase === 'choose' || phase === 'resolving') && (
        <>
          <p className="muted small" style={{ marginBottom: 6 }}>
            Tap <b>{item.playerName}'s</b> face below, then Save.
          </p>
          <div
            className="face-select-canvas"
            style={{ position: 'relative', maxWidth: 480, margin: '0 auto' }}
          >
            <img
              ref={imgRef}
              src={imageUrl}
              alt="Capture awaiting face selection"
              onLoad={onImgLoad}
              style={{ width: '100%', height: 'auto', display: 'block' }}
            />
            {detect && detect.faces.map((f) => {
              const r = scaledRect(f.bbox);
              if (!r) return null;
              const chosen = selectedIndex === f.index;
              return (
                <button
                  type="button"
                  key={f.index}
                  onClick={() => setSelectedIndex(f.index)}
                  style={{
                    position: 'absolute',
                    left: `${r.left}px`,
                    top: `${r.top}px`,
                    width: `${r.width}px`,
                    height: `${r.height}px`,
                    background: 'transparent',
                    border: chosen ? '3px solid #2ea043' : '3px solid #f8a23c',
                    boxShadow: chosen
                      ? '0 0 0 2px rgba(46,160,67,0.35)'
                      : '0 0 0 1px rgba(0,0,0,0.4)',
                    cursor: 'pointer',
                    padding: 0,
                  }}
                  title={`Face ${f.index + 1} (conf ${(f.det_score * 100).toFixed(0)}%)`}
                >
                  <span style={{
                    position: 'absolute', top: 2, left: 2,
                    background: chosen ? '#2ea043' : '#f8a23c',
                    color: 'white', padding: '0 4px', fontSize: 11,
                    borderRadius: 2,
                  }}>
                    {f.index + 1}
                  </span>
                </button>
              );
            })}
          </div>
          <div className="controls-inline" style={{ marginTop: 8 }}>
            <button className="btn ghost" onClick={onCancel} disabled={phase === 'resolving'}>
              Cancel
            </button>
            <button
              className="btn primary"
              onClick={submit}
              disabled={selectedIndex == null || phase === 'resolving'}
            >
              {phase === 'resolving' ? 'Saving…' : 'Save with selected face'}
            </button>
          </div>
        </>
      )}
    </div>
  );
}
