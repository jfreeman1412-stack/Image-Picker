// 2026-08 pin test for the player-card (cluster) sort.
// Run with: node frontend/src/utils/clusterSort.test.mjs
//
// Same zero-tooling pattern as jobSort.test.mjs. Pins: numeric-aware name
// order, blank-labels-last, guests-always-last, photographed order by
// capture_time then filename, and the buddy-frame exclusion.

import { strict as assert } from 'node:assert';
import { sortClusters, isClusterSortMode, DEFAULT_CLUSTER_SORT } from './clusterSort.js';

let passed = 0, failed = 0;
function test(name, fn) {
  try { fn(); passed++; console.log(`  ok  ${name}`); }
  catch (e) { failed++; console.log(`  FAIL  ${name}`); console.log(`    ${e.message}`); }
}

console.log('clusterSort — pin tests\n');

const labels = (cs) => cs.map((c) => c.label);
// img(filename, capture_time, role)
const img = (filename, t = null, role = 'individual') => ({ filename, capture_time: t, role });
const cl = (label, images = [], guest_of = null) => ({ label, images, guest_of, cluster_id: label });

// ── Name mode ────────────────────────────────────────────────────────────
test('name: numeric-aware ("Player 2" before "Player 10")', () => {
  const cs = [cl('Player 10'), cl('Player 2'), cl('Aaron')];
  assert.deepEqual(labels(sortClusters(cs, 'name')), ['Aaron', 'Player 2', 'Player 10']);
});

test('name: case-insensitive', () => {
  const cs = [cl('zeb'), cl('Ana'), cl('bo')];
  assert.deepEqual(labels(sortClusters(cs, 'name')), ['Ana', 'bo', 'zeb']);
});

test('name: blank labels sort last', () => {
  const cs = [cl(''), cl('Kai'), cl(null)];
  assert.equal(labels(sortClusters(cs, 'name'))[0], 'Kai');
});

test('default mode is name', () => {
  assert.equal(DEFAULT_CLUSTER_SORT, 'name');
  const cs = [cl('B'), cl('A')];
  assert.deepEqual(labels(sortClusters(cs)), ['A', 'B']);
});

// ── Photographed mode ──────────────────────────────────────────────────────
test('photographed: by capture_time ascending', () => {
  const cs = [
    cl('third', [img('c.jpg', '2026-08-15T10:03:00')]),
    cl('first', [img('a.jpg', '2026-08-15T10:01:00')]),
    cl('second', [img('b.jpg', '2026-08-15T10:02:00')]),
  ];
  assert.deepEqual(labels(sortClusters(cs, 'photographed')), ['first', 'second', 'third']);
});

test('photographed: filename breaks ties / untimed uses filename order', () => {
  const cs = [
    cl('x', [img('TAYR3410.png')]),
    cl('y', [img('TAYR3352.png')]),
    cl('z', [img('TAYR3376.png')]),
  ];
  assert.deepEqual(labels(sortClusters(cs, 'photographed')), ['y', 'z', 'x']);
});

test('photographed: buddy/rejected frames excluded from the shoot key', () => {
  // Cluster A's own block starts at 10:05; it also appears in an earlier buddy
  // shot (10:00) from B's block. That buddy frame must NOT pull A ahead of B.
  const a = cl('A', [img('buddy.jpg', '2026-08-15T10:00:00', 'buddy'),
                     img('a_own.jpg', '2026-08-15T10:05:00', 'individual')]);
  const b = cl('B', [img('b_own.jpg', '2026-08-15T10:02:00', 'team')]);
  assert.deepEqual(labels(sortClusters([a, b], 'photographed')), ['B', 'A']);
});

test('photographed: cluster with only buddy frames falls back to its earliest', () => {
  const a = cl('A', [img('a.jpg', '2026-08-15T10:09:00', 'buddy')]);
  const b = cl('B', [img('b.jpg', '2026-08-15T10:01:00', 'individual')]);
  assert.deepEqual(labels(sortClusters([a, b], 'photographed')), ['B', 'A']);
});

// ── Guests always last (both modes) ─────────────────────────────────────────
test('guests sink to the end regardless of name order', () => {
  const cs = [
    cl('Zoe'),
    { ...cl('Amy'), guest_of: { team: 'Other' } },  // guest, alphabetically first
    cl('Max'),
  ];
  const out = labels(sortClusters(cs, 'name'));
  assert.deepEqual(out, ['Max', 'Zoe', 'Amy']);   // Amy (guest) last despite A
});

test('guests last in photographed mode too', () => {
  const cs = [
    cl('late', [img('a.jpg', '2026-08-15T10:09:00')]),
    { ...cl('guest-early', [img('g.jpg', '2026-08-15T10:00:00')]), guest_of: { team: 'X' } },
  ];
  assert.deepEqual(labels(sortClusters(cs, 'photographed')), ['late', 'guest-early']);
});

// ── Misc ────────────────────────────────────────────────────────────────────
test('non-array input → []', () => {
  assert.deepEqual(sortClusters(null, 'name'), []);
});

test('does not mutate input', () => {
  const cs = [cl('B'), cl('A')];
  const before = labels(cs);
  sortClusters(cs, 'name');
  assert.deepEqual(labels(cs), before);
});

test('isClusterSortMode', () => {
  assert.ok(isClusterSortMode('name'));
  assert.ok(isClusterSortMode('photographed'));
  assert.ok(!isClusterSortMode('nope'));
});

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
