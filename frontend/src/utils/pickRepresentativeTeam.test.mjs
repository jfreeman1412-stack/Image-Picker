// 2026-06-28 pin test for the wizard's representative-folder picker.
// Run with: node frontend/src/utils/pickRepresentativeTeam.test.mjs
//
// Zero new tooling — uses Node's built-in `assert` module. This is the
// load-bearing picker whose silent drift would reintroduce the wizard's
// silent-under-ingest bug, so it's worth pinning even though the rest of
// the wizard work is eyeball-verified per cycle precedent.

import { strict as assert } from 'node:assert';
import { pickRepresentativeTeam } from './pickRepresentativeTeam.js';

let passed = 0;
let failed = 0;
function test(name, fn) {
  try { fn(); passed++; console.log(`  ok  ${name}`); }
  catch (e) { failed++; console.log(`  FAIL  ${name}`); console.log(`    ${e.message}`); }
}

console.log('pickRepresentativeTeam — pin tests\n');

// Fixture builders.
const peek = (name, subfolders = [], image_count = 0, raw_count = 0) =>
  ({ name, subfolders, image_count, raw_count });
const sub = (name, image_count = 0) => ({ name, image_count });

// ── Case 1: all-equivalent → peeks[0] (the regression-fallback guarantee) ──
test('all peeks structurally equivalent → returns peeks[0] (alphabetic-first fallback)', () => {
  const peeks = [
    peek('1st Grade Blue',  [sub('Adjusted', 50)]),
    peek('2nd Grade Red',   [sub('Adjusted', 50)]),
    peek('3rd Grade Green', [sub('Adjusted', 50)]),
  ];
  const result = pickRepresentativeTeam(peeks);
  assert.equal(result.name, '1st Grade Blue',
    'expected peeks[0] when all equivalent');
});

// ── Case 2: one-distinct (the swatch-sorts-first bug) → distinct one ──
test('one swatch-shape folder + many real-team folders → picks a real team, not the swatch', () => {
  const peeks = [
    peek('Color Swatch',    [],                 12, 0),   // sorts first
    peek('Eagles',          [sub('Adjusted', 60)]),
    peek('Tigers',          [sub('Adjusted', 55)]),
    peek('Wildcats',        [sub('Adjusted', 45)]),
  ];
  const result = pickRepresentativeTeam(peeks);
  assert.notEqual(result.name, 'Color Swatch',
    'must not pick the swatch — it has no rendition subfolder');
  // Among the three real teams, all score equally on rendition+total+
  // richest counts EXCEPT richest-image-count. Eagles wins via image
  // weight, but acceptable is any of the three (the test guards against
  // the bug, not the specific winner among equivalents).
  assert.ok(['Eagles', 'Tigers', 'Wildcats'].includes(result.name),
    `expected a real team, got ${result.name}`);
});

// ── Case 3: mixed structure → richest structure wins ──
test('mixed structure → folder with most rendition-matching subfolders wins', () => {
  const peeks = [
    peek('Team A', [sub('misc', 10), sub('photos', 5)]),                // 2 subs, 0 rendition
    peek('Team B', [sub('Adjusted', 80)]),                              // 1 sub, 1 rendition
    peek('Team C', [sub('Adjusted', 60), sub('JPG', 40), sub('raw', 0)]), // 3 subs, 2 rendition
  ];
  const result = pickRepresentativeTeam(peeks);
  assert.equal(result.name, 'Team C',
    'expected Team C (most rendition matches: 2)');
});

// ── Extra: alphabetic-first tiebreaker when rendition+total+richest all tie ──
test('three identical-structure teams → returns peeks[0] (alphabetic-first)', () => {
  const peeks = [
    peek('Bandits', [sub('Adjusted', 50)]),
    peek('Eagles',  [sub('Adjusted', 50)]),
    peek('Tigers',  [sub('Adjusted', 50)]),
  ];
  const result = pickRepresentativeTeam(peeks);
  assert.equal(result.name, 'Bandits');
});

// ── Extra: empty / single edge cases ──
test('empty peeks → null', () => {
  assert.equal(pickRepresentativeTeam([]), null);
  assert.equal(pickRepresentativeTeam(null), null);
});

test('single peek → returns that peek (no comparisons needed)', () => {
  const p = peek('Only Team', [sub('Adjusted', 30)]);
  assert.equal(pickRepresentativeTeam([p]), p);
});

// ── Extra: swatch beats real teams ONLY if real teams have no structure ──
// (Documents the limit: if no team has rendition subfolders, the picker
//  has nothing to anchor on and falls back to alphabetic-first.)
test('all folders structureless → returns peeks[0] (fallback)', () => {
  const peeks = [
    peek('Swatch', [],         12),
    peek('Eagles', [],         60),
    peek('Tigers', [],         55),
  ];
  // Without rendition subfolders ANYWHERE, the picker can't distinguish
  // the swatch from real teams structurally — it returns peeks[0]. The
  // zero-images guard in the create step is the safety net for this case.
  const result = pickRepresentativeTeam(peeks);
  assert.equal(result.name, 'Swatch');
});

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
