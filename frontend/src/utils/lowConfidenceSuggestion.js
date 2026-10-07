// 2026-06-29 low-confidence match suggestion gate.
//
// Encapsulates the cluster-data-driven part of the gate predicate for the
// "🔍 Possible match — verify before moving" panel. ClusterCard combines
// this with UI-state predicates (!guest, !smartPanelTriggered, hasMoveHandler)
// at render time.
//
// What "low confidence" means in this codebase (matching.py:144-163):
//   - tier='high'  → score ≥ 0.6 AND margin ≥ 0.05 → auto-labeled, no review
//   - tier='low'   → real match (player_id populated) but uncertain. Two paths:
//                    (a) ambiguous_margin: score ≥ 0.6 but runner-up too close
//                    (b) low_confidence:   0.4 ≤ score < 0.6
//   - tier='none'  → score < 0.4 → no player named (player_id = null)
//
// "Real match below high" cleanly = tier === 'low' AND player_id != null.
// tier='none' never has a player_id, so the second clause is redundant when
// tier='low' but it's belt-and-suspenders against a future tier rename.
//
// The smart panel (smartPanelTriggered) handles tier='high' with team
// mismatch. This helper handles below-high matches WHERE we have a findable
// suggested team. When the suggested team is missing/archived/multi-team-
// joined, the helper returns null and the operator falls through to the
// generic "Move to another team…" trigger.
//
// Pure function — no React, no fetch, no side effects. Pinned by
// lowConfidenceSuggestion.test.mjs (run with `node`).

/**
 * Compute the low-confidence suggestion for a cluster, or null when no
 * actionable suggestion can be made.
 *
 * @param {Object} cluster - cluster row from list_clusters API. Reads:
 *   match.tier, match.player_id, match.player_name, match.roster_team,
 *   match.confidence.
 * @param {Array} teamOptions - move-targets list. Reads: name, norm_name,
 *   session_id, archived.
 * @returns {{match, target}|null} The suggestion, or null when the gate
 *   doesn't fire. `target` is the teamOption (with session_id+archived
 *   for Case-1 vs Case-2 routing). `match` is passed through verbatim so
 *   the render block can read player_name + confidence directly.
 */
export function lowConfidenceSuggestion(cluster, teamOptions) {
  const match = cluster?.match;
  if (!match) return null;
  if (match.tier !== 'low') return null;
  if (match.player_id == null) return null;
  if (!match.roster_team) return null;
  const rosterTeamLower = match.roster_team.toLowerCase();
  const target = (teamOptions || []).find(
    (o) => !o.archived && o.name.toLowerCase() === rosterTeamLower,
  );
  if (!target) return null;
  return { match, target };
}
