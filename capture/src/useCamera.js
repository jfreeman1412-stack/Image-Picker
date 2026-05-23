import { useEffect, useRef, useState } from 'react';

// Map a getUserMedia rejection to a stable code + a volunteer-facing message.
// Names per the MediaDevices spec.
function describeError(err) {
  switch (err && err.name) {
    case 'NotAllowedError':
    case 'SecurityError':
      return {
        code: 'permission_denied',
        message:
          'Camera access was blocked. Allow the camera for this site in your ' +
          'browser settings, then reload.',
      };
    case 'NotFoundError':
    case 'OverconstrainedError':
      return { code: 'no_camera', message: 'No camera was found on this device.' };
    case 'NotReadableError':
      return {
        code: 'camera_busy',
        message: 'The camera is being used by another app. Close it and reload.',
      };
    default:
      return {
        code: 'camera_error',
        message: `Couldn't start the camera (${(err && err.name) || 'unknown error'}).`,
      };
  }
}

/**
 * Acquire the rear camera once and stream it into a <video>. Returns a ref to
 * attach to the element plus a status/error pair to drive the UI.
 *
 * The stream is acquired on mount and torn down on unmount (no per-shot
 * re-acquire — that comes when capture/retake is wired in later sections).
 * main.jsx deliberately omits StrictMode so this effect runs once and we don't
 * open the camera twice in dev.
 */
export default function useCamera() {
  const videoRef = useRef(null);
  const [status, setStatus] = useState('starting'); // starting | live | error
  const [error, setError] = useState(null);          // { code, message } | null

  useEffect(() => {
    let stream = null;
    let cancelled = false;

    async function start() {
      // getUserMedia only exists in a secure context (https or localhost). A
      // phone hitting http://<lan-ip> gets `undefined` here — the classic trap.
      // Surface it distinctly so it's obvious the https tunnel wasn't used.
      if (!navigator.mediaDevices?.getUserMedia) {
        if (cancelled) return;
        setError({
          code: 'insecure_context',
          message:
            'The camera needs a secure (https) connection. Open the app via the ' +
            'https tunnel URL on your device — see the run steps.',
        });
        setStatus('error');
        return;
      }
      try {
        stream = await navigator.mediaDevices.getUserMedia({
          video: {
            facingMode: { ideal: 'environment' }, // rear camera — point it at the player
            width: { ideal: 1920 },
            height: { ideal: 1080 },
          },
          audio: false,
        });
        if (cancelled) {
          // Unmounted while awaiting — don't leak the stream.
          stream.getTracks().forEach((t) => t.stop());
          return;
        }
        const video = videoRef.current;
        if (video) {
          video.srcObject = stream;
          video.muted = true;            // belt-and-suspenders so autoplay isn't blocked
          video.play().catch(() => {});  // play() can reject on a benign interrupt race
        }
        setStatus('live');
      } catch (err) {
        if (cancelled) return;
        setError(describeError(err));
        setStatus('error');
      }
    }

    start();

    return () => {
      cancelled = true;
      if (stream) stream.getTracks().forEach((t) => t.stop());
    };
  }, []);

  return { videoRef, status, error };
}
