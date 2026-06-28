// 2026-06-28 wizard-mixed-structure fix.
//
// Picks a "representative" team-folder peek from a list of per-folder peeks.
// Load-bearing for the wizard's subfolder structure detection: the chosen
// peek feeds the jobs 55/56 logic (rootDisabled, recommended-star,
// root-RAW warning) that decides which subfolder option the operator
// sees in step 6.
//
// Why this exists: the wizard used to peek only teamFolders[0] to decide
// structure. When the first folder (alphabetical) was a non-team
// (color-swatch with no Adjusted subfolder), the operator was led to a
// silent under-ingest: imageSubfolder=None saved → real teams' images
// (which live in Adjusted) ingested 0 files. See [[wizard-mixed-structure]]
// memory + the trace in the conversation that locked this fix.
//
// Selection rules, in priority order:
//   1. Folder with the most subfolders whose name matches a known
//      rendition pattern (e.g. "Adjusted", "JPG"). This is the structural
//      signal we care about — if any folder in the job has rendition
//      subfolders, we want to detect them.
//   2. Folder with the most subfolders total.
//   3. Folder with the highest image_count in its richest subfolder
//      (data weight tiebreaker).
//   4. peeks[0] — the alphabetical-first fallback. This is the regression
//      guarantee: when all peeks score identically (the common case of
//      a structurally-uniform job), the picker returns the same folder
//      the pre-fix code did, so 55/56 logic runs unchanged.
//
// Pure function: no React, no fetch, no side effects. Tested by
// pickRepresentativeTeam.test.mjs (run with `node`).
//
// Each peek must have shape: {name, subfolders: [{name, image_count, ...}],
// image_count, raw_count}. (Same shape as POST /api/jobs/peek response.)

// Case-insensitive substring match — keep this list TIGHT. Adding generic
// terms ("photos", "edits") would false-positive on real team naming.
const RENDITION_PATTERNS = [
  'adjusted',
  'jpg',
  'jpeg',
  'renditions',
  'edits',  // less common but seen in some shoot workflows
];

function _renditionMatchCount(peek) {
  const subs = peek.subfolders || [];
  return subs.filter(s => {
    const n = (s.name || '').toLowerCase();
    return RENDITION_PATTERNS.some(p => n.includes(p));
  }).length;
}

function _richestSubfolderImages(peek) {
  const subs = peek.subfolders || [];
  if (subs.length === 0) return 0;
  return Math.max(...subs.map(s => s.image_count || 0));
}

/**
 * Pick the representative peek for structure detection.
 *
 * Returns the SAME peek object passed in (not a copy) so the caller can
 * use it directly as firstTeamPeek replacement.
 *
 * @param {Array<{name, subfolders, image_count, raw_count}>} peeks
 *   Per-team peeks in alphabetic-team-folder order. peeks[0] is the
 *   fallback when all peeks score identically.
 * @returns the chosen peek, or null if peeks is empty.
 */
export function pickRepresentativeTeam(peeks) {
  if (!peeks || peeks.length === 0) return null;
  if (peeks.length === 1) return peeks[0];

  // Score each peek as a tuple [rendition_matches, total_subs, richest_imgs].
  // Higher tuples win (lexicographic). Ties resolve to peeks[0] via the
  // stable Array.prototype.reduce — we keep the FIRST tied winner, which
  // matches alphabetical order since peeks come in already sorted by
  // backend peek_folder() (jobs.py:65 sorted(folder.iterdir())).
  const scored = peeks.map(p => ({
    peek: p,
    score: [
      _renditionMatchCount(p),
      (p.subfolders || []).length,
      _richestSubfolderImages(p),
    ],
  }));

  return scored.reduce((best, curr) => {
    // Lexicographic tuple comparison: higher wins, equal keeps `best`
    // (which preserves the lower-index — alphabetical-first — peek
    // when all scores are equal, the regression-fallback guarantee).
    for (let i = 0; i < curr.score.length; i++) {
      if (curr.score[i] > best.score[i]) return curr;
      if (curr.score[i] < best.score[i]) return best;
    }
    return best;
  }).peek;
}

// Exposed for the test file only — not part of the public surface.
export const _internals = {
  RENDITION_PATTERNS,
  _renditionMatchCount,
  _richestSubfolderImages,
};
