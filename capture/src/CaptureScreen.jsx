// Capture screen — the B.1 camera + capture/upload machine, extracted from the
// old single-screen App so B.2's App can switch between shoot picker → roster →
// capture (Decision 5). DORMANT until Section 6: it still targets
// VITE_REF_PLAYER_ID and POSTs like B.1. Section 6 rewires it to target the
// selected player/shoot (props) via the scoped-replace endpoint and removes the
// env var (Decision 3). See ../PHASE_B2_ROSTER_CAPTURE.md.
import { useEffect, useState } from 'react';
import useCamera from './useCamera.js';

const PLAYER_ID = import.meta.env.VITE_REF_PLAYER_ID;

export default function CaptureScreen() {
  const { videoRef, status, error } = useCamera();
  const [shot, setShot] = useState(null);               // { blob, url } | null
  const [upload, setUpload] = useState('idle');         // idle | uploading | success | error
  const [result, setResult] = useState(null);           // success summary dict | null
  const [uploadError, setUploadError] = useState(null); // { kind, message } | null

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

  // Back to a live preview, ready to shoot again (Retake / Capture another).
  const reset = () => {
    setShot(null);          // effect cleanup revokes the URL
    setUpload('idle');
    setResult(null);
    setUploadError(null);
  };

  const sendUpload = async () => {
    if (!shot) return;
    if (!PLAYER_ID) {
      setUploadError({
        kind: 'not_found',
        message:
          'VITE_REF_PLAYER_ID is not set. Point capture/.env at a real player id ' +
          'and restart the dev server.',
      });
      setUpload('error');
      return;
    }
    setUpload('uploading');
    setUploadError(null);
    try {
      const fd = new FormData();
      fd.append('file', shot.blob, 'capture.jpg');
      const res = await fetch(`/api/players/${PLAYER_ID}/references`, {
        method: 'POST',
        body: fd,
      });
      if (!res.ok) {
        // 400 quality gate → detail is { error, message }; 404 → detail is a string.
        const body = await res.json().catch(() => ({}));
        const code = body?.detail?.error;
        if (res.status === 404) {
          setUploadError({
            kind: 'not_found',
            message:
              `Player #${PLAYER_ID} wasn’t found on the server. Set ` +
              `VITE_REF_PLAYER_ID to a real player id (see README) and restart.`,
          });
        } else if (code) {
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
      setResult(await res.json());
      setUpload('success');
    } catch {
      setUploadError({
        kind: 'network',
        message: 'Upload failed — check the connection and try again.',
      });
      setUpload('error');
    }
  };

  const live = status === 'live';
  const capturing = live && !shot;
  const reviewing = live && shot && upload === 'idle';
  const uploading = live && shot && upload === 'uploading';
  const succeeded = live && shot && upload === 'success';
  const failed = live && shot && upload === 'error';
  // Network/unknown failures can retry the same blob; a quality rejection or a
  // missing player needs a fresh shot (or a config fix), so only Retake there.
  const retryable = failed && (uploadError?.kind === 'network' || uploadError?.kind === 'other');

  return (
    <main className="camera-screen">
      <video ref={videoRef} className="camera-video" autoPlay playsInline muted />

      <header className="topbar">
        <span className="topbar-title">Reference capture</span>
        <span className="topbar-target" title={`POST /api/players/${PLAYER_ID}/references`}>
          {PLAYER_ID ? `→ player #${PLAYER_ID}` : 'no player set'}
        </span>
      </header>

      {shot && <img className="shot-image" src={shot.url} alt="Captured photo" />}

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
          <button className="btn" onClick={sendUpload}>Use photo</button>
        </div>
      )}

      {uploading && <div className="status-pill">Uploading…</div>}

      {succeeded && result && (
        <div className="result-panel">
          <p className="result-line success">✓ Saved as player #{result.player_id}</p>
          <p className="result-meta">
            detection {Number(result.det_score).toFixed(2)}
            {result.face_area_ratio != null &&
              ` · face ${(result.face_area_ratio * 100).toFixed(1)}% of frame`}
          </p>
          <button className="btn" onClick={reset}>Capture another</button>
        </div>
      )}

      {failed && uploadError && (
        <div className="result-panel">
          <p className="result-line warn">{uploadError.message}</p>
          <div className="controls-inline">
            <button className="btn ghost" onClick={reset}>Retake</button>
            {retryable && (
              <button className="btn" onClick={sendUpload}>Try again</button>
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
            <button className="btn" onClick={() => window.location.reload()}>Reload</button>
          </div>
        </div>
      )}
    </main>
  );
}
