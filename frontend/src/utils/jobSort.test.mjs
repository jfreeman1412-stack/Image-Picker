// 2026-08-12 pin test for the job-list sort comparator + sortJobs.
// Run with: node frontend/src/utils/jobSort.test.mjs
//
// Zero new tooling — Node's built-in `assert`, same pattern as
// lowConfidenceSuggestion.test.mjs / pickRepresentativeTeam.test.mjs.
//
// The load-bearing behaviours pinned here: numeric-aware ordering
// ("Team 2" < "Team 10"), case-insensitivity, blank-names-last, input
// immutability, and 'newest' preserving the incoming (backend) order.
// The capture copy (capture/src/jobSort.js) is identical, so this test
// guards both apps.

import { strict as assert } from 'node:assert';
import { sortJobs, compareJobsByName, isSortMode, DEFAULT_SORT_MODE } from './jobSort.js';

let passed = 0;
let failed = 0;
function test(name, fn) {
  try { fn(); passed++; console.log(`  ok  ${name}`); }
  catch (e) { failed++; console.log(`  FAIL  ${name}`); console.log(`    ${e.message}`); }
}

console.log('jobSort — pin tests\n');

const names = (jobs) => jobs.map((j) => j.name);

// ── Numeric-aware: the headline requirement ──────────────────────────────
test('numeric-aware: "Team 2" sorts before "Team 10" (not naive string order)', () => {
  const jobs = [{ name: 'Team 10' }, { name: 'Team 2' }, { name: 'Team 1' }];
  assert.deepEqual(names(sortJobs(jobs, 'name')), ['Team 1', 'Team 2', 'Team 10']);
});

// ── Case-insensitive ─────────────────────────────────────────────────────
test('case-insensitive: lowercase and uppercase interleave by letter', () => {
  const jobs = [{ name: 'banana' }, { name: 'Apple' }, { name: 'cherry' }, { name: 'Blueberry' }];
  assert.deepEqual(names(sortJobs(jobs, 'name')), ['Apple', 'banana', 'Blueberry', 'cherry']);
});

// ── Blank / missing names pushed to the end (graceful, no throw) ──────────
test('blank and missing names sort to the end', () => {
  const jobs = [{ name: '' }, { name: 'Zebra' }, {}, { name: 'Alpha' }, { name: null }];
  const sorted = sortJobs(jobs, 'name');
  assert.deepEqual(names(sorted).slice(0, 2), ['Alpha', 'Zebra']);
  // The three blank/missing entries occupy the tail (order among them unspecified).
  assert.equal(sorted.length, 5);
  assert.ok(sorted.slice(2).every((j) => !j.name), 'tail entries should all be blank/missing');
});

test('whitespace-only name is treated as blank → end', () => {
  const jobs = [{ name: '   ' }, { name: 'Alpha' }];
  assert.deepEqual(names(sortJobs(jobs, 'name')), ['Alpha', '   ']);
});

// ── 'newest' / default-arg / unknown mode preserve incoming order ─────────
test("mode 'newest' preserves the incoming (backend created_at DESC) order", () => {
  const jobs = [{ name: 'Charlie' }, { name: 'Alpha' }, { name: 'Bravo' }];
  assert.deepEqual(names(sortJobs(jobs, 'newest')), ['Charlie', 'Alpha', 'Bravo']);
});

test('unknown mode falls back to preserving incoming order', () => {
  const jobs = [{ name: 'Charlie' }, { name: 'Alpha' }];
  assert.deepEqual(names(sortJobs(jobs, 'whatever')), ['Charlie', 'Alpha']);
});

// ── Immutability: sortJobs never mutates its input ───────────────────────
test('sortJobs returns a new array and does not mutate the input', () => {
  const jobs = [{ name: 'Team 10' }, { name: 'Team 2' }];
  const before = names(jobs);
  const sorted = sortJobs(jobs, 'name');
  assert.notEqual(sorted, jobs, 'should return a new array reference');
  assert.deepEqual(names(jobs), before, 'input order must be unchanged');
});

// ── Stability: equal names keep incoming relative order ──────────────────
test('stable: equal names keep their incoming (newest-first) order', () => {
  const jobs = [
    { name: 'Dup', id: 1 }, { name: 'Dup', id: 2 }, { name: 'Dup', id: 3 },
  ];
  assert.deepEqual(sortJobs(jobs, 'name').map((j) => j.id), [1, 2, 3]);
});

// ── Non-array / empty input → [] (loading state passes null) ─────────────
test('non-array input returns []', () => {
  assert.deepEqual(sortJobs(null, 'name'), []);
  assert.deepEqual(sortJobs(undefined, 'newest'), []);
});

// ── Comparator direct ────────────────────────────────────────────────────
test('compareJobsByName sign contract', () => {
  assert.ok(compareJobsByName({ name: 'a' }, { name: 'b' }) < 0);
  assert.ok(compareJobsByName({ name: 'b' }, { name: 'a' }) > 0);
  assert.equal(compareJobsByName({ name: 'x' }, { name: 'X' }), 0);
});

// ── Metadata sanity ──────────────────────────────────────────────────────
test('isSortMode + DEFAULT_SORT_MODE', () => {
  assert.ok(isSortMode('name'));
  assert.ok(isSortMode('newest'));
  assert.ok(!isSortMode('nope'));
  assert.ok(!isSortMode(undefined));
  assert.equal(DEFAULT_SORT_MODE, 'name');
});

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
