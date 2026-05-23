// Phase B.1 — capture screen. Section 2 opened the rear camera (useCamera);
// Section 3 adds the fixed framing oval the volunteer fills with the player's
// face. It's purely visual guidance — no crop, no gate (pointer-events: none) —
// sized large enough that a filled face clears A.2's 2% area gate with no
// client-side detection. See ../PHASE_B1_CAPTURE_PROTOTYPE.md.
import useCamera from './useCamera.js';

export default function App() {
  const { videoRef, status, error } = useCamera();

  return (
    <main className="camera-screen">
      <video ref={videoRef} className="camera-video" autoPlay playsInline muted />

      {status === 'live' && (
        <div className="frame-overlay" aria-hidden="true">
          <div className="frame-oval" />
          <p className="frame-hint">Fill the oval with the player’s face</p>
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
