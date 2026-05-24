// Section 6 — capture for the selected player. Reuses B.1's useCamera + the
// capture/review/upload state machine, now targeting the chosen player + shoot
// via the SHOOT-SCOPED replace endpoint (Decisions 2 & 4): retake replaces only
// this shoot's photo, other shoots untouched, and a failed gate keeps the
// existing one. No VITE_REF_PLAYER_ID (Decision 3) — the target comes from props.
//
// Props: job {id,name}, player {player_id,name,team,is_coach},
//   alreadyCaptured (bool — show the "will replace" affordance),
//   onSaved(playerId) — App marks ✓ live + returns to the roster,
//   onCancel() — back to the roster without uploading,
//   onGone() — hard 404 (player/shoot gone), back to the shoot picker.
// See ../PHASE_B2_ROSTER_CAPTURE.md.
import { useEffect, useState } from 'react';
import useCamera from './useCamera.js';

export default function CaptureScreen({
  job, player, alreadyCaptured, onSaved, onCancel, onGone, onRemoved,
}) {
  const { videoRef, status, error } = useCamera();
  const [shot, setShot] = useState(null);               // { blob, url } | null
  const [upload, setUpload] = useState('idle');         // idle | uploading | error
  const [uploadError, setUploadError] = useState(null); // { kind, message } | null
  const [removePhase, setRemovePhase] = useState('none'); // none|confirm|removing|error
  const [removeError, setRemoveError] = useState(null);

  // Free the captured object URL when the shot changes or the screen unmounts.
  useEffect(() => {
    if (!shot) return undefined;
    return () => URL.revokeObjectURL(shot.url);
  }, [shot]);

  const capture = () => {
    const video = videoRef.current;
    if (!video) return;
    const w = video.videoWidth;
    const h = video.videoHeight;
    if (!w || !h) return; // metadata not ready yet
    const canvas = document.createElement('canvas');
    canvas.width = w;
    canvas.height = h;
    canvas.getContext('2d').drawImage(video, 0, 0, w, h);
    canvas.toBlob(
      (blob) => {
        if (blob) setShot({ blob, url: URL.createObjectURL(blob) });
      },
      'image/jpeg',
      0.92,
    );
  };

  // Back to a live preview, ready to shoot again (Retake).
  const reset = () => {
    setShot(null);          // effect cleanup revokes the URL
    setUpload('idle');
    setUploadError(null);
  };

  const sendUpload = async () => {
    if (!shot) return;
    setUpload('uploading');
    setUploadError(null);
    try {
      const fd = new FormData();
      fd.append('file', shot.blob, 'capture.jpg');
      // Shoot-scoped replace: sets this player's single reference FOR this shoot.
      const res = await fetch(
        `/api/players/${player.player_id}/references/shoot/${job.id}`,
        { method: 'PUT', body: fd },
      );
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        const code = body?.detail?.error;
        if (res.status === 404) {
          // Player or shoot no longer exists — selection is stale.
          setUploadError({
            kind: 'gone',
            message: 'This player or shoot no longer exists on the server. Go back and reselect.',
          });
        } else if (code) {
          // Quality gate (no_face / multiple_faces / low_confidence / face_too_small).
          setUploadError({ kind: 'quality', code, message: body.detail.message });
        } else {
          setUploadError({
            kind: 'other',
            message:
              (typeof body?.detail === 'string' && body.detail) ||
              `Upload failed (HTTP ${res.status}).`,
          });
        }
        setUpload('error');
        return;
      }
      // Success → mark ✓ live and auto-return to the roster (no tap, per spec).
      // This unmounts CaptureScreen, so don't set any further state here.
      onSaved(player.player_id);
    } catch {
      setUploadError({
        kind: 'network',
        message: 'Upload failed — check the connection and try again.',
      });
      setUpload('error');
    }
  };

  // Remove this player's photo FOR THIS shoot (Decision 6). Confirmation-gated;
  // on success App clears the ✓ live and returns to the roster.
  const doRemove = async () => {
    setRemovePhase('removing');
    setRemoveError(null);
    try {
      const res = await fetch(
        `/api/players/${player.player_id}/references/shoot/${job.id}`,
        { method: 'DELETE' },
      );
      if (!res.ok) {
        setRemoveError(`Couldn’t remove the photo (HTTP ${res.status}).`);
        setRemovePhase('error');
        return;
      }
      // Unmounts CaptureScreen — don't set further state here.
      onRemoved(player.player_id);
    } catch {
      setRemoveError('Remove failed — check the connection and try again.');
      setRemovePhase('error');
    }
  };

  const live = status === 'live';
  const capturing = live && !shot;
  const reviewing = live && shot && upload === 'idle';
  const uploading = live && shot && upload === 'uploading';
  const failed = live && shot && upload === 'error';
  const isGone = failed && uploadError?.kind === 'gone';
  // Network/unknown failures can retry the same blob; a quality rejection needs
  // a fresh shot (Retake); a stale selection (gone) needs to go back.
  const retryable = failed && (uploadError?.kind === 'network' || uploadError?.kind === 'other');

  return (
    <main className="camera-screen">
      <video ref={videoRef} className="camera-video" autoPlay playsInline muted />

      <header className="topbar">
        <button className="topbar-back" onClick={onCancel}>← Roster</button>
        <span className="topbar-target">
          {player ? `${player.name} · ${player.team}` : ''}
        </span>
      </header>

      {shot && <img className="shot-image" src={shot.url} alt="Captured photo" />}

      {(capturing || reviewing) && alreadyCaptured && (
        <div className="replace-banner">
          <span>{player?.name} already has a photo for this shoot — this will replace it.</span>
          {capturing && (
            <button className="banner-remove" onClick={() => setRemovePhase('confirm')}>
              Remove photo instead
            </button>
          )}
        </div>
      )}

      {capturing && (
        <div className="frame-overlay" aria-hidden="true">
          <div className="frame-oval" />
          <p className="frame-hint">Fill the oval with the player’s face</p>
        </div>
      )}

      {capturing && (
        <div className="controls">
          <button className="shutter" onClick={capture} aria-label="Capture photo" />
        </div>
      )}

      {reviewing && (
        <div className="controls">
          <button className="btn ghost" onClick={reset}>Retake</button>
          <button className="btn" onClick={sendUpload}>
            {alreadyCaptured ? 'Replace photo' : 'Use photo'}
          </button>
        </div>
      )}

      {uploading && <div className="status-pill">Uploading…</div>}
      {removePhase === 'removing' && <div className="status-pill">Removing…</div>}

      {removePhase === 'confirm' && (
        <div className="confirm-overlay">
          <div className="confirm-card">
            <p className="confirm-text">
              Remove {player?.name}’s photo for this shoot? This can’t be undone.
            </p>
            <div className="controls-inline">
              <button className="btn ghost" onClick={() => setRemovePhase('none')}>
                Cancel
              </button>
              <button className="btn danger" onClick={doRemove}>Remove photo</button>
            </div>
          </div>
        </div>
      )}

      {removePhase === 'error' && (
        <div className="confirm-overlay">
          <div className="confirm-card">
            <p className="result-line warn">{removeError}</p>
            <div className="controls-inline">
              <button className="btn ghost" onClick={() => setRemovePhase('none')}>
                Cancel
              </button>
              <button className="btn danger" onClick={doRemove}>Try again</button>
            </div>
          </div>
        </div>
      )}

      {failed && uploadError && (
        <div className="result-panel">
          <p className="result-line warn">{uploadError.message}</p>
          <div className="controls-inline">
            {isGone ? (
              <button className="btn" onClick={onGone}>Back to shoots</button>
            ) : (
              <>
                <button className="btn ghost" onClick={reset}>Retake</button>
                {retryable && (
                  <button className="btn" onClick={sendUpload}>Try again</button>
                )}
              </>
            )}
          </div>
        </div>
      )}

      {status === 'starting' && (
        <div className="camera-overlay">
          <p className="muted">Starting camera…</p>
        </div>
      )}
      {status === 'error' && error && (
        <div className="camera-overlay">
          <div className="camera-message">
            <p>{error.message}</p>
            <div className="controls-inline">
              <button className="btn ghost" onClick={onCancel}>Back</button>
              <button className="btn" onClick={() => window.location.reload()}>Reload</button>
            </div>
          </div>
        </div>
      )}
    </main>
  );
}
