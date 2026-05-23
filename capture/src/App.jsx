// Phase B.1 — Section 2: live camera preview. Opens the rear camera via
// getUserMedia and fills the screen with the feed; the framing oval (Section 3),
// capture (Section 4), and upload (Section 5) build on top of this. Permission,
// insecure-context, and no-camera failures each show a clear message.
// See ../PHASE_B1_CAPTURE_PROTOTYPE.md.
import useCamera from './useCamera.js';

export default function App() {
  const { videoRef, status, error } = useCamera();

  return (
    <main className="camera-screen">
      <video ref={videoRef} className="camera-video" autoPlay playsInline muted />

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
