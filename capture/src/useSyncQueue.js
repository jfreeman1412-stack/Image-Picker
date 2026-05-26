// Phase B.3 §5 — owns the drainer's triggers and exposes the sync UI state.
// Triggers: on mount, when the browser regains connectivity (`online`), after
// each enqueue (App calls syncNow), the manual "Sync now" button, and a periodic
// fallback while items remain. A run-guard prevents overlapping drains so the
// progress UI never flickers. See ../PHASE_B3_OFFLINE_CAPTURE.md.
import { useCallback, useEffect, useRef, useState } from 'react';
import { drainOnce } from './syncQueue.js';

const RETRY_INTERVAL_MS = 30000; // gentle fallback while pending items remain

export default function useSyncQueue({ active = false, onItemResult } = {}) {
  const [syncing, setSyncing] = useState(false);
  const [progress, setProgress] = useState({ done: 0, total: 0 });
  const [lastSummary, setLastSummary] = useState(null);
  const runningRef = useRef(false);
  const onItemResultRef = useRef(onItemResult);
  onItemResultRef.current = onItemResult;

  const syncNow = useCallback(async () => {
    if (runningRef.current) return;     // a drain is already in flight
    runningRef.current = true;
    setSyncing(true);
    setLastSummary(null);
    try {
      const summary = await drainOnce({
        onProgress: setProgress,
        onItemResult: (item, result) => onItemResultRef.current?.(item, result),
      });
      if (summary) setLastSummary(summary);
    } finally {
      runningRef.current = false;
      setSyncing(false);
    }
  }, []);

  // Drain on mount, and again whenever connectivity returns.
  useEffect(() => {
    syncNow();
    const onOnline = () => syncNow();
    window.addEventListener('online', onOnline);
    return () => window.removeEventListener('online', onOnline);
  }, [syncNow]);

  // Periodic fallback — only while there's work to do.
  useEffect(() => {
    if (!active) return undefined;
    const id = setInterval(() => { syncNow(); }, RETRY_INTERVAL_MS);
    return () => clearInterval(id);
  }, [active, syncNow]);

  return { syncing, progress, lastSummary, syncNow };
}
