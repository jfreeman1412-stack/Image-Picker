// 2026-06-29 pin test for the low-confidence match suggestion gate.
// Run with: node frontend/src/utils/lowConfidenceSuggestion.test.mjs
//
// Zero new tooling — uses Node's built-in `assert` module. Same pattern
// as pickRepresentativeTeam.test.mjs. The gate predicate is the load-
// bearing piece — if a future refactor silently changes "real match
// below high" semantics, this catches it.

import { strict as assert } from 'node:assert';
import { lowConfidenceSuggestion } from './lowConfidenceSuggestion.js';

let passed = 0;
let failed = 0;
function test(name, fn) {
  try { fn(); passed++; console.log(`  ok  ${name}`); }
  catch (e) { failed++; console.log(`  FAIL  ${name}`); console.log(`    ${e.message}`); }
}

console.log('lowConfidenceSuggestion — pin tests\n');

// Fixture builders.
const cluster = (matchOverrides = {}) => ({
  cluster_id: 7137,
  match: {
    player_id: null,
    player_name: null,
    confidence: null,
    tier: 'none',
    scope: null,
    roster_team: null,
    team_mismatch: false,
    ...matchOverrides,
  },
});
const team = (name, opts = {}) => ({
  name,
  norm_name: name.toLowerCase().replace(/[^a-z0-9]/g, ''),
  session_id: opts.session_id ?? null,
  archived: opts.archived ?? false,
});

// ── Case 1: low-tier + roster_team + findable suggested team → fires ──
test('low-tier match with roster_team + findable team → returns {match, target}', () => {
  const c = cluster({
    tier: 'low', player_id: 6288, player_name: 'Etta-Rakotz',
    confidence: 0.7678, roster_team: 'GU12-Rec-Otsego-ROYAL', scope: 'roster',
  });
  const opts = [
    team('Some Other Team', { session_id: 100 }),
    team('GU12-Rec-Otsego-ROYAL', { session_id: 764 }),
    team('Yet Another', { session_id: 101 }),
  ];
  const result = lowConfidenceSuggestion(c, opts);
  assert.ok(result, 'expected a suggestion');
  assert.equal(result.match.player_name, 'Etta-Rakotz');
  assert.equal(result.target.name, 'GU12-Rec-Otsego-ROYAL');
  assert.equal(result.target.session_id, 764);
});

// ── Case 2: high-tier → returns null (smart panel owns this) ──
test('high-tier match → null (smart panel owns this; we MUST NOT also fire)', () => {
  const c = cluster({
    tier: 'high', player_id: 6288, player_name: 'Etta-Rakotz',
    confidence: 0.85, roster_team: 'GU12-Rec-Otsego-ROYAL',
  });
  const opts = [team('GU12-Rec-Otsego-ROYAL', { session_id: 764 })];
  assert.equal(lowConfidenceSuggestion(c, opts), null);
});

// ── Case 3: none-tier / null player → returns null ──
test('tier=none → null (no real match)', () => {
  const c = cluster({ tier: 'none', player_id: null, roster_team: null });
  assert.equal(lowConfidenceSuggestion(c, []), null);
});

test('player_id=null even at tier=low → null (defensive)', () => {
  // Shouldn't happen per matching.py semantics (none always nulls player_id,
  // low always populates it), but pin the defensive guard anyway.
  const c = cluster({
    tier: 'low', player_id: null, roster_team: 'GU12-Rec-Otsego-ROYAL',
  });
  const opts = [team('GU12-Rec-Otsego-ROYAL', { session_id: 764 })];
  assert.equal(lowConfidenceSuggestion(c, opts), null);
});

// ── Case 4: low-tier but suggested team archived/missing → null (fallback) ──
test('low-tier but suggested team is ARCHIVED → null (graceful fallback)', () => {
  const c = cluster({
    tier: 'low', player_id: 6288, player_name: 'Etta-Rakotz',
    roster_team: 'GU12-Rec-Otsego-ROYAL',
  });
  const opts = [team('GU12-Rec-Otsego-ROYAL', { session_id: 764, archived: true })];
  assert.equal(lowConfidenceSuggestion(c, opts), null);
});

test('low-tier but suggested team NOT in teamOptions → null', () => {
  const c = cluster({
    tier: 'low', player_id: 6288, player_name: 'Etta-Rakotz',
    roster_team: 'Nonexistent Team',
  });
  const opts = [team('GU12-Rec-Otsego-ROYAL', { session_id: 764 })];
  assert.equal(lowConfidenceSuggestion(c, opts), null);
});

// ── Edge: scope='global' (player not in this job's roster) → roster_team is null ──
test('scope=global with null roster_team → null (no actionable suggestion)', () => {
  const c = cluster({
    tier: 'low', player_id: 9999, player_name: 'Stranger',
    confidence: 0.55, roster_team: null, scope: 'global',
  });
  assert.equal(lowConfidenceSuggestion(c, []), null);
});

// ── Edge: multi-team joined roster_team → smartTarget lookup fails gracefully ──
test('multi-team comma-joined roster_team → null (lookup fails on joined string)', () => {
  // When a player is on multiple teams the API joins them: "Team A, Team B".
  // The lookup compares to a single team name → no match → falls back to
  // the generic trigger. Same caveat as the smart panel (pre-existing).
  const c = cluster({
    tier: 'low', player_id: 6288, player_name: 'Etta-Rakotz',
    roster_team: 'GU12-Rec-Otsego-ROYAL, GU10-Rec-Otsego-ROYAL',
  });
  const opts = [
    team('GU12-Rec-Otsego-ROYAL', { session_id: 764 }),
    team('GU10-Rec-Otsego-ROYAL', { session_id: 720 }),
  ];
  assert.equal(lowConfidenceSuggestion(c, opts), null);
});

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
