// 2026-08 — human labels for cluster review-reason flag codes.
//
// Shared by Settings.jsx (the flag-visibility toggles) and ClusterCard.jsx
// (the ⚠ badges on each cluster). Keep the keys in sync with the backend's
// KNOWN_FLAGS (app/api/settings.py) plus the read-time flags computed in
// /clusters (roster_mismatch, duplicate_auto_label, match_* ). Unmapped codes
// fall back to the raw string, so a missing entry is cosmetic, never a crash.

export const FLAG_LABELS = {
  team_pick_not_smiling: 'Team pick not smiling',
  // Pano selection prefers a non-smiling frame; this fires only when every
  // acceptable candidate was smiling and it fell back to one.
  pano_smiling_fallback: 'Pano: smiling — no neutral shot',
  no_clean_pano_pose: 'No clean pano pose',
  no_pano_candidate: 'No pano candidate',
  no_single_face_images: 'No single-face images',
  ambiguous_copyright: 'Ambiguous copyright',
  outlier_high: 'Outlier image count (high)',
  outlier_low: 'Outlier image count (low)',
  no_team_pick: 'No team pick',
  no_pano_pick: 'No pano pick',
  coach_no_solo_image: 'Coach has no solo image',
  empty_cluster: 'Empty cluster',
  roster_mismatch: 'Roster team mismatch',
  duplicate_auto_label: 'Duplicate label',
  match_label_conflict: 'Match / label conflict',
  low_confidence_match: 'Low-confidence match',
  match_team_mismatch: 'Matched to another team',
};

// Label for one flag code, falling back to the raw code.
export function flagLabel(code) {
  return FLAG_LABELS[code] || code;
}
