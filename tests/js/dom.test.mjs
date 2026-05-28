// DOM-interaction tests: load a real generated report into jsdom and exercise
// the report's own sort/filter code. This also verifies the JS refactor
// (extracting helpers into report-logic.js) is behavior-preserving.
//
// Layout-dependent behavior (column-width measurement, drag-resize, tooltip
// overflow, popover positioning, Chart.js rendering) needs a real layout engine
// and is NOT covered here — that would require Playwright/headless Chrome.
import { test, before } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { JSDOM } from 'jsdom';
import os from 'node:os';
import path from 'node:path';
import fs from 'node:fs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');

let HTML;

before(() => {
  const out = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'csc-')), 'report.html');
  execFileSync('python3', [path.join(ROOT, 'tests', 'js', 'make_report.py'), out]);
  HTML = fs.readFileSync(out, 'utf-8');
});

function load() {
  const dom = new JSDOM(HTML, {
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    beforeParse(w) { w.Chart = function () { return {}; }; },  // stub charts
  });
  return dom;
}

function visibleGroups(doc) {
  return [...doc.querySelectorAll('.group')].filter(g => !g.classList.contains('filtered-hidden'));
}

function groupSeedKeys(doc) {
  // each group's outer key is its first .grid-row's data-sk-2 (seeds)
  return [...doc.querySelectorAll('.group')].map(g => {
    const row = g.querySelector(':scope > .grid-row');
    return parseFloat(row.getAttribute('data-sk-2'));
  });
}

test('report loads under jsdom without throwing and exposes globals', () => {
  const { window } = load();
  assert.equal(typeof window.parseCols, 'function');
  assert.equal(typeof window.numericInRange, 'function');
  assert.equal(typeof window.compareSortKeys, 'function');
  assert.equal(typeof window.sortTable, 'function');
  assert.ok(window.document.querySelectorAll('.group').length === 3);
});

test('initial DELETE filter hides keep groups', () => {
  const { window } = load();
  // DELETE is pre-checked; only the one eligible group should be visible.
  const vis = visibleGroups(window.document);
  assert.equal(vis.length, 1);
});

test('sortTable(2) orders groups by seeds ascending then descending', () => {
  const { window } = load();
  window.sortTable(2);                 // seeds, first click = ascending
  const asc = groupSeedKeys(window.document);
  assert.deepEqual(asc, [...asc].sort((a, b) => a - b));
  window.sortTable(2);                 // same column toggles to descending
  const desc = groupSeedKeys(window.document);
  assert.deepEqual(desc, [...desc].sort((a, b) => b - a));
});
