// 2026-08 — player-card (cluster) ordering for the session review page.
//
// Two modes back the Name / Photographed toggle:
//   'name'         — A–Z by the card's display label, case-insensitive AND
//                    numeric-aware ("Player 2" before "Player 10"). Blank
//                    labels sort last.
//   'photographed' — the order the players were shot, i.e. by the cluster's
//                    EARLIEST own-block frame: (capture_time, filename).
//                    Buddy/rejected frames are excluded because a buddy shot
//                    belongs to another kid's block and would skew the order;
//                    if a cluster somehow has only buddy frames, it falls back
//                    to its earliest image. capture_time is the primary key
//                    (untimed → earliest, matching the API's datetime.min
//                    sentinel); filename breaks ties (that's the shutter order).
//
// Confirmed cross-team GUEST clusters always sink to the end regardless of
// mode — folding in the guests-last rule SessionDetail applied before this.
//
// Pure: no React, no fetch. The API returns each cluster's `images` already
// sorted earliest-first with per-image `role`, so both keys are read straight
// from the payload — no backend change. Tested by clusterSort.test.mjs (node).

export const DEFAULT_CLUSTER_SORT = 'name';

export function isClusterSortMode(mode) {
  return mode === 'name' || mode === 'photographed';
}

// Case-insensitive, numeric-aware label compare. Blank labels sort last.
function compareByLabel(a, b) {
  const an = (a && a.label ? String(a.label) : '').trim();
  const bn = (b && b.label ? String(b.label) : '').trim();
  if (!an && !bn) return 0;
  if (!an) return 1;
  if (!bn) return -1;
  return an.localeCompare(bn, undefined, { numeric: true, sensitivity: 'base' });
}

// The cluster's earliest own-block frame — the API delivers images earliest-
// first, so the first non-buddy/non-rejected image is it.
function shootFrame(cluster) {
  const imgs = Array.isArray(cluster && cluster.images) ? cluster.images : [];
  const own = imgs.filter((im) => im.role !== 'buddy' && im.role !== 'rejected');
  const pool = own.length ? own : imgs;
  return pool.length ? pool[0] : null;
}

function comparePhotographed(a, b) {
  const ka = shootFrame(a);
  const kb = shootFrame(b);
  if (!ka && !kb) return 0;
  if (!ka) return 1;      // imageless cluster → last
  if (!kb) return -1;
  const ta = ka.capture_time;
  const tb = kb.capture_time;
  if (ta && tb) {
    if (ta !== tb) return ta < tb ? -1 : 1;   // ISO strings compare chronologically
  } else if (ta && !tb) {
    return 1;             // b untimed → sorts earlier (API datetime.min sentinel)
  } else if (!ta && tb) {
    return -1;
  }
  return String(ka.filename || '').localeCompare(
    String(kb.filename || ''), undefined, { numeric: true, sensitivity: 'base' },
  );
}

// Returns a NEW array. Guests last; within each group, by the chosen key.
export function sortClusters(clusters, mode = DEFAULT_CLUSTER_SORT) {
  const list = Array.isArray(clusters) ? clusters.slice() : [];
  const keyCmp = mode === 'photographed' ? comparePhotographed : compareByLabel;
  return list.sort((a, b) => {
    const guest = (a.guest_of ? 1 : 0) - (b.guest_of ? 1 : 0);
    if (guest) return guest;
    return keyCmp(a, b);
  });
}
