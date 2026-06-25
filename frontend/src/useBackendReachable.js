// 2026-06-25 — set-role-defensive (b). Connectivity poll for the backend.
//
// The trigger: job 58 Abra Kadabra session had operator edits silently fail
// to save during a run.bat restart while the cluster cards were open. The
// operator had zero signal — the popover closed instantly on click and the
// re-rendered card showed identical roles because the auto-rule output
// happened to match. This hook surfaces a global "backend reachable?" boolean
// that ConnectivityBanner.jsx renders as a sticky warning when uvicorn is
// unreachable.
//
// Design (locked in scope-verification):
//   - Polls GET /api/health every 5 seconds (REACHABILITY_POLL_MS).
//     /health is a no-DB no-I/O endpoint defined in main.py — purely
//     "is uvicorn accepting requests on :8020?". Restart windows are 10-15s,
//     so 5s cadence catches every one within a single cycle.
//   - 2-failure hysteresis (CONSECUTIVE_FAILURES_TO_FLIP): a single failed
//     poll doesn't flip the state; two consecutive failures do. Dampens the
//     one-off network blip / browser-tab-throttling false positive.
//     Recovery is single-success: as soon as one /health succeeds, flip
//     reachable=true. Asymmetric on purpose — slow to alarm, fast to
//     reassure.
//   - Returns reachable: boolean | null. Null is the "haven't checked yet"
//     state on first mount so the banner doesn't flash false-on briefly
//     while the initial fetch is in flight.
//   - Single AbortController per cycle so a slow /health can't pile up
//     overlapping requests if the backend's molasses.
import { useEffect, useState } from 'react';

const REACHABILITY_POLL_MS = 5000;
const CONSECUTIVE_FAILURES_TO_FLIP = 2;
const REACHABILITY_TIMEOUT_MS = 4000;   // shorter than poll cadence

export default function useBackendReachable() {
  // null on mount → "no verdict yet" → banner suppressed. After the first
  // poll resolves, the state is always true|false.
  const [reachable, setReachable] = useState(null);

  useEffect(() => {
    let cancelled = false;
    let failures = 0;
    let timer;

    const check = async () => {
      const controller = new AbortController();
      const t = setTimeout(() => controller.abort(), REACHABILITY_TIMEOUT_MS);
      try {
        const res = await fetch('/api/health', { signal: controller.signal });
        if (cancelled) return;
        if (res.ok) {
          // Any success → flip back to reachable (fast recovery).
          failures = 0;
          setReachable(true);
        } else {
          // Non-2xx (5xx during partial-startup, 502 from a confused proxy,
          // etc.) counts as a failure for hysteresis purposes.
          failures += 1;
          if (failures >= CONSECUTIVE_FAILURES_TO_FLIP) setReachable(false);
        }
      } catch (e) {
        // Network error / abort / fetch reject.
        if (cancelled) return;
        failures += 1;
        if (failures >= CONSECUTIVE_FAILURES_TO_FLIP) setReachable(false);
      } finally {
        clearTimeout(t);
      }
    };

    // Fire immediately on mount so the operator gets a fast first verdict.
    check();
    timer = setInterval(check, REACHABILITY_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);

  return reachable;
}
