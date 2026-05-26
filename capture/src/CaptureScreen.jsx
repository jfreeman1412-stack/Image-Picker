// Capture for the selected player. Reuses B.1's useCamera + the capture/review
// state machine, but Phase B.3 changes what "Use photo" does: it no longer
// uploads. It COMPRESSES the frame (≤1600px long edge, q0.85 — Decision 1) and
// banks it to the durable local queue (Decision 2). A single drainer (§5) uploads
// it whenever connected; good service drains it in ~1s, so online it still feels
// instant. There is deliberately NO direct-upload path here — that's the
// "one mechanism, not two modes" rule.
//
// Server-side quality (400) / gone (404) handling is GONE from this screen —
// there's no network call at capture time. Those rejections surface at drain time
// in the roster's "Needs attention" view (§6).
//
// Props: job {id,name}, player {player_id,name,team,is_coach},
//   isSynced (bool — a synced server reference exists for this shoot),
//   queueItem ({id,…}|null — a not-yet-synced local capture for this player),
//   onQueued() — App refreshes the queue + returns to the roster,
//   onCancel() — back to the roster without capturing,
//   onRemoved(playerId) — photo removed (pending dropped locally, or synced deleted).
// See ../PHASE_B3_OFFLINE_CAPTURE.md.
import { useEffect, useState } from 'react';
import useCamera from './useCamera.js';
import { enqueueCapture, removeQueueItem } from './db.js';

const MAX_EDGE = 1600;     // downscale long edge (Decision 1)
const JPEG_QUALITY = 0.85;

export default function CaptureScreen({
  job, player, isSynced, queueItem, onQueued, onCancel, onRemoved,
}) {
  const { videoRef, status, error } = useCamera();
  const [shot, setShot] = useState(null);               // { blob, url } | null
  const [save, setSave] = useState('idle');             // idle | saving | error
  const [saveError, setSaveError] = useState(null);     // string | null
  const [removePhase, setRemovePhase] = useState('none'); // none|confirm|removing|error
  const [removeError, setRemoveError] = useState(null);

  const alreadyCaptured = isSynced || !!queueItem;

  // Free the captured object URL when the shot changes or the screen unmounts.
  useEffect(() => {
    if (!shot) return undefined;
    return () => URL.revokeObjectURL(shot.url);
  }, [shot]);

  // Draw the current frame, downscaled, to a JPEG (Decision 1: ≤1600px @ q0.85).
  const capture = () => {
    const video = videoRef.current;
    if (!video) return;
    const vw = video.videoWidth;
    const vh = video.videoHeight;
    if (!vw || !vh) return; // metadata not ready yet
    const scale = Math.min(1, MAX_EDGE / Math.max(vw, vh));
    const w = Math.round(vw * scale);
    const h = Math.round(vh * scale);
    const canvas = document.createElement('canvas');
    canvas.width = w;
    canvas.height = h;
    canvas.getContext('2d').drawImage(video, 0, 0, w, h);
    canvas.toBlob(
      (blob) => {
        if (blob) setShot({ blob, url: URL.createObjectURL(blob) });
      },
      'image/jpeg',
      JPEG_QUALITY,
    );
  };

  // Back to a live preview, ready to shoot again (Retake).
  const reset = () => {
    setShot(null);          // effect cleanup revokes the URL
    setSave('idle');
    setSaveError(null);
  };

  // Bank the capture to the durable queue (no network). enqueueCapture replaces
  // any not-yet-synced item for this (player, shoot), so a retake supersedes it.
  const saveCapture = async () => {
    if (!shot) return;
    setSave('saving');
    setSaveError(null);
    try {
      const bytes = await shot.blob.arrayBuffer();
      await enqueueCapture({
        playerId: player.player_id,
        jobId: job.id,
        playerName: player.name,
        team: player.team,
        bytes,
        mime: 'image/jpeg',
        capturedAt: Date.now(),
      });
      // Unmounts this screen — don't set further state here.
      onQueued();
    } catch (e) {
      setSaveError(
        e && e.name === 'QuotaExceededError'
          ? 'Storage is full — sync queued photos to free space, then try again.'
          : 'Couldn’t save the photo on this device. Try again.',
      );
      setSave('error');
    }
  };

  // Remove this player's photo for this shoot. A not-yet-synced local capture is
  // dropped offline-safe (no network); a SYNCED reference needs the server (B.2
  // behavior — removing a synced photo offline is deferred).
  const doRemove = async () => {
    setRemovePhase('removing');
    setRemoveError(null);
    try {
      if (queueItem) {
        await removeQueueItem(queueItem.id);
        onRemoved(player.player_id); // unmounts — stop here
        return;
      }
      const res = await fetch(
        `/api/players/${player.player_id}/references/shoot/${job.id}`,
        { method: 'DELETE' },
      );
      if (!res.ok && res.status !== 404) {
        setRemoveError(`Couldn’t remove the photo (HTTP ${res.status}).`);
        setRemovePhase('error');
        return;
      }
      onRemoved(player.player_id); // 404 ⇒ already gone ⇒ treat as removed
    } catch {
      setRemoveError(
        'Couldn’t remove the photo — a synced photo needs a connection. Try again when online.',
      );
      setRemovePhase('error');
    }
  };

  const live = status === 'live';
  const capturing = live && !shot;
  const reviewing = live && shot && save === 'idle';
  const saving = live && shot && save === 'saving';
  const failed = live && shot && save === 'error';

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
          <button className="btn" onClick={saveCapture}>
            {alreadyCaptured ? 'Replace photo' : 'Use photo'}
          </button>
        </div>
      )}

      {saving && <div className="status-pill">Saving…</div>}
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

      {failed && saveError && (
        <div className="result-panel">
          <p className="result-line warn">{saveError}</p>
          <div className="controls-inline">
            <button className="btn ghost" onClick={reset}>Retake</button>
            <button className="btn" onClick={saveCapture}>Try again</button>
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
