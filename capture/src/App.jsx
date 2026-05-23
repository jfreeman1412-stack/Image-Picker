// Phase B.1 — capture screen.
//   Section 2: open the rear camera (useCamera).
//   Section 3: fixed framing oval (guidance only).
//   Section 4: capture a still → review → Retake / Use photo.
// The captured JPEG is the FULL frame (the oval doesn't crop). The camera
// stream stays live across capture/retake so retake is instant — we never
// re-acquire it. "Use photo" lands on a placeholder "ready" state; Section 5
// replaces it with the multipart upload to the reference endpoint.
// See ../PHASE_B1_CAPTURE_PROTOTYPE.md.
import { useEffect, useState } from 'react';
import useCamera from './useCamera.js';

export default function App() {
  const { videoRef, status, error } = useCamera();
  const [shot, setShot] = useState(null);        // { blob, url } | null
  const [confirmed, setConfirmed] = useState(false);

  // Free the object URL when the shot changes or the screen unmounts.
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

  const retake = () => {
    setShot(null);          // effect cleanup revokes the URL
    setConfirmed(false);
  };
  const usePhoto = () => setConfirmed(true);

  const live = status === 'live';
  const capturing = live && !shot;             // live preview, ready to shoot
  const reviewing = live && shot && !confirmed; // froze a shot, deciding
  const ready = live && shot && confirmed;      // accepted (Section 5 will upload)

  return (
    <main className="camera-screen">
      <video ref={videoRef} className="camera-video" autoPlay playsInline muted />

      {/* The frozen still sits above the live video during review/ready. */}
      {shot && <img className="shot-image" src={shot.url} alt="Captured photo" />}

      {/* Framing oval only while composing a live shot. */}
      {capturing && (
        <div className="frame-overlay" aria-hidden="true">
          <div className="frame-oval" />
          <p className="frame-hint">Fill the oval with the player’s face</p>
        </div>
      )}

      {ready && (
        <div className="banner">Photo ready — uploading is wired in Section 5.</div>
      )}

      {/* Controls (interactive — outside the pointer-events:none overlay). */}
      {capturing && (
        <div className="controls">
          <button className="shutter" onClick={capture} aria-label="Capture photo" />
        </div>
      )}
      {reviewing && (
        <div className="controls">
          <button className="btn ghost" onClick={retake}>Retake</button>
          <button className="btn" onClick={usePhoto}>Use photo</button>
        </div>
      )}
      {ready && (
        <div className="controls">
          <button className="btn ghost" onClick={retake}>Capture another</button>
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
