// Unit tests for the pure report helpers in vendor/report/report-logic.js.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const require = createRequire(import.meta.url);
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const L = require(path.join(ROOT, 'vendor', 'report', 'report-logic.js'));

const REAL_COLS =
  'max-content max-content max-content max-content ' +
  'max-content max-content max-content max-content ' +
  '140px 130px minmax(200px, 1fr) minmax(220px, 2fr)';

test('parseCols keeps minmax() atomic and yields 12 tracks', () => {
  const cols = L.parseCols(REAL_COLS);
  assert.equal(cols.length, 12);
  assert.equal(cols[10], 'minmax(200px, 1fr)');   // space inside parens preserved
  assert.equal(cols[11], 'minmax(220px, 2fr)');
});

test('parseCols does not split inside parentheses', () => {
  const cols = L.parseCols('a minmax(200px, 1fr) b');
  assert.deepEqual(cols, ['a', 'minmax(200px, 1fr)', 'b']);
});

test('parseCols empty string -> empty array', () => {
  assert.deepEqual(L.parseCols(''), []);
});

test('colMinFromCols extracts first px number', () => {
  assert.equal(L.colMinFromCols(['140px'], 0), 140);
  assert.equal(L.colMinFromCols(['minmax(200px, 1fr)'], 0), 200);
  assert.equal(L.colMinFromCols(['max-content'], 0), 30);  // no number -> fallback
  assert.equal(L.colMinFromCols([], 5), 30);               // missing token
});

test('numericInRange null bounds are unbounded', () => {
  assert.equal(L.numericInRange('5', null, null), true);
});

test('numericInRange inclusive bounds', () => {
  assert.equal(L.numericInRange('5', 5, 10), true);
  assert.equal(L.numericInRange('10', 5, 10), true);
  assert.equal(L.numericInRange('4', 5, 10), false);
  assert.equal(L.numericInRange('11', 5, 10), false);
});

test('numericInRange NaN fails when a bound is set, passes when unbounded', () => {
  assert.equal(L.numericInRange('-', 1, null), false);   // EXT-row empty cell
  assert.equal(L.numericInRange('-', null, null), true);
});

test('numericInRange falsy zero is a real value', () => {
  assert.equal(L.numericInRange('0', 0, 10), true);
  assert.equal(L.numericInRange('0', 1, 10), false);
});

test('parseSortKey null defaults', () => {
  assert.equal(L.parseSortKey(null, true), 0);
  assert.equal(L.parseSortKey(null, false), '');
  assert.equal(L.parseSortKey('42', true), 42);
  assert.equal(L.parseSortKey('Foo', false), 'Foo');
});

test('compareSortKeys numeric vs string', () => {
  assert.ok(L.compareSortKeys(2, 10, true) < 0);     // numeric, not lexical
  assert.ok(L.compareSortKeys('b', 'a', false) > 0);
  assert.equal(L.compareSortKeys('a', 'a', false), 0);
});

test('matchesReasonFilter empty selection always passes', () => {
  assert.equal(L.matchesReasonFilter(new Set(['CATEGORY_FILTER']), new Set(), 'any'),  true);
  assert.equal(L.matchesReasonFilter(new Set(['CATEGORY_FILTER']), new Set(), 'only'), true);
  // even an eligible torrent (no reasons) passes when no filter is active
  assert.equal(L.matchesReasonFilter(new Set(), new Set(), 'any'),  true);
});

test('matchesReasonFilter eligible torrent never passes an active filter', () => {
  const sel = new Set(['CATEGORY_FILTER']);
  assert.equal(L.matchesReasonFilter(new Set(), sel, 'any'),  false);
  assert.equal(L.matchesReasonFilter(new Set(), sel, 'only'), false);
});

test('matchesReasonFilter any-mode: at least one overlap', () => {
  const sel = new Set(['CATEGORY_FILTER']);
  assert.equal(L.matchesReasonFilter(new Set(['CATEGORY_FILTER']),                  sel, 'any'), true);
  assert.equal(L.matchesReasonFilter(new Set(['CATEGORY_FILTER', 'TRACKER_ALIVE']), sel, 'any'), true);
  assert.equal(L.matchesReasonFilter(new Set(['TRACKER_ALIVE']),                    sel, 'any'), false);
});

test('matchesReasonFilter only-mode: every group reason must be selected', () => {
  const sel = new Set(['CATEGORY_FILTER']);
  assert.equal(L.matchesReasonFilter(new Set(['CATEGORY_FILTER']),                  sel, 'only'), true);
  assert.equal(L.matchesReasonFilter(new Set(['CATEGORY_FILTER', 'TRACKER_ALIVE']), sel, 'only'), false);
  assert.equal(L.matchesReasonFilter(new Set(['TRACKER_ALIVE']),                    sel, 'only'), false);
});

test('matchesReasonFilter only-mode with multiple selected: subset wins', () => {
  const sel = new Set(['CATEGORY_FILTER', 'TRACKER_ALIVE']);
  assert.equal(L.matchesReasonFilter(new Set(['CATEGORY_FILTER']),                  sel, 'only'), true);
  assert.equal(L.matchesReasonFilter(new Set(['TRACKER_ALIVE']),                    sel, 'only'), true);
  assert.equal(L.matchesReasonFilter(new Set(['CATEGORY_FILTER', 'TRACKER_ALIVE']), sel, 'only'), true);
  assert.equal(L.matchesReasonFilter(new Set(['CATEGORY_FILTER', 'LOW_SEEDS']),     sel, 'only'), false);
});
