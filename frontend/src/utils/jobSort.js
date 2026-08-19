// 2026-08-12 — job-list ordering for the editor + capture apps.
//
// Two modes back the A–Z / Newest sort toggle on the job list:
//   'name'   — alphabetical, case-insensitive AND numeric-aware, so
//              "Team 2" sorts before "Team 10" (not after). Blank/missing
//              names are pushed to the END rather than throwing.
//   'newest' — the order the backend already returns (jobs.py list_jobs
//              orders by created_at DESC), i.e. leave the list UNTOUCHED.
//              Capture's job objects carry no created_at (ShootPicker maps
//              to {id, name} only), so "newest" MUST mean "as received" —
//              never a client-side re-sort by a field that isn't there.
//
// Array.prototype.sort is stable (guaranteed ES2019+), so jobs with equal
// names keep their incoming (newest-first) relative order.
//
// Pure function: no React, no fetch, no side effects. This file is mirrored
// VERBATIM in the capture app (capture/src/jobSort.js) — the two apps share
// no build root, so keep the copies in lockstep so a job list looks
// identical in the editor and in capture. Tested by jobSort.test.mjs (node).

export const DEFAULT_SORT_MODE = 'name';

// Valid modes for the toggle. Anything else falls back to preserving the
// incoming order (treated like 'newest').
export function isSortMode(mode) {
  return mode === 'name' || mode === 'newest';
}

// Case-insensitive, numeric-aware comparator. Blank/missing names sort last.
export function compareJobsByName(a, b) {
  const an = (a && a.name != null ? String(a.name) : '').trim();
  const bn = (b && b.name != null ? String(b.name) : '').trim();
  if (!an && !bn) return 0;
  if (!an) return 1;    // a is blank → after b
  if (!bn) return -1;   // b is blank → after a
  return an.localeCompare(bn, undefined, { numeric: true, sensitivity: 'base' });
}

// Returns a NEW array (never mutates the input). Non-arrays → [].
export function sortJobs(jobs, mode = DEFAULT_SORT_MODE) {
  const list = Array.isArray(jobs) ? jobs.slice() : [];
  if (mode === 'name') list.sort(compareJobsByName);
  return list;  // 'newest' / unknown → backend order (as received)
}
