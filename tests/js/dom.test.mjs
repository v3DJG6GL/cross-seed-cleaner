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
  // matchesReasonFilter is called as a bare global from the inline filter
  // code; if a vendor-sync ever drops it, the report's filter throws
  // ReferenceError in the browser but jsdom's smoke tests stay green
  // unless we assert its presence here too.
  assert.equal(typeof window.matchesReasonFilter, 'function');
  assert.ok(window.document.querySelectorAll('.group').length === 3);
});

test('initial DELETE filter hides keep groups', () => {
  const { window } = load();
  // DELETE is pre-checked; only the one eligible group should be visible.
  const vis = visibleGroups(window.document);
  assert.equal(vis.length, 1);
});

test('Any/Only reason toggle hides multi-reason groups in only-mode', () => {
  const { window } = load();
  const doc = window.document;

  function fire(el) {
    el.dispatchEvent(new window.Event('change', { bubbles: true }));
  }

  // Drop the default DELETE filter so kept-group visibility is governed by
  // the reason filter alone.
  const deleteCb = doc.querySelector('[data-filter-status][value="delete"]');
  deleteCb.checked = false;
  fire(deleteCb);

  // Tick LOW_SEEDS only.
  const lowSeeds = doc.querySelector('[data-filter-reason][value="LOW_SEEDS"]');
  lowSeeds.checked = true;
  fire(lowSeeds);

  // Default mode is "any": both single-reason and multi-reason groups whose
  // reasons overlap LOW_SEEDS should be visible. The fixture has one of each:
  // g1 (LOW_SEEDS only) and g2 (LOW_SEEDS + EXTERNAL_LINK).
  const anyMode = visibleGroups(doc).map(g => g.dataset.reasons);
  assert.equal(anyMode.length, 2);
  assert.ok(anyMode.some(r => r.split(/\s+/).includes('EXTERNAL_LINK')),
            'expected a multi-reason group to be visible in any-mode');

  // Flip to "only": only groups whose reasons are a subset of {LOW_SEEDS}
  // survive — the multi-reason g2 should now be hidden.
  const onlyRadio = doc.querySelector('[data-reason-match][value="only"]');
  onlyRadio.checked = true;
  fire(onlyRadio);

  const onlyMode = visibleGroups(doc).map(g => g.dataset.reasons.trim());
  assert.equal(onlyMode.length, 1);
  assert.equal(onlyMode[0], 'LOW_SEEDS');
});

test('Clear all keeps the Any/Only reason filter working', () => {
  const { window } = load();
  const doc = window.document;

  function fire(el) {
    el.dispatchEvent(new window.Event('change', { bubbles: true }));
  }

  // "Clear all" must not blank out the reason-match radios' value attribute;
  // they live inside a .filter-multi-panel, so a naive value-wipe would set
  // them to "" and the next toggle would feed an unknown mode to the filter.
  const clearBtn = doc.getElementById('filterClearBtn');
  clearBtn.click();

  const anyRadio = doc.querySelector('[data-reason-match][value="any"]');
  const onlyRadio = doc.querySelector('[data-reason-match][value="only"]');
  assert.equal(anyRadio.value, 'any');
  assert.equal(onlyRadio.value, 'only');

  // After clearing, select a reason then flip Any -> Only. This must not throw
  // (it would if the radio value had been wiped to "").
  const lowSeeds = doc.querySelector('[data-filter-reason][value="LOW_SEEDS"]');
  lowSeeds.checked = true;
  fire(lowSeeds);
  assert.doesNotThrow(() => { onlyRadio.checked = true; fire(onlyRadio); });

  // Only-mode with LOW_SEEDS selected leaves just the single-reason group.
  const onlyMode = visibleGroups(doc).map(g => g.dataset.reasons.trim());
  assert.equal(onlyMode.length, 1);
  assert.equal(onlyMode[0], 'LOW_SEEDS');
});

test('Clear all resets the Any/Only reason toggle back to its default', () => {
  const { window } = load();
  const doc = window.document;

  function fire(el) {
    el.dispatchEvent(new window.Event('change', { bubbles: true }));
  }

  const anyRadio = doc.querySelector('[data-reason-match][value="any"]');
  const onlyRadio = doc.querySelector('[data-reason-match][value="only"]');

  // User switches to "Only", then clicks "Clear all".
  onlyRadio.checked = true;
  fire(onlyRadio);
  assert.equal(onlyRadio.checked, true);
  assert.ok(onlyRadio.parentElement.classList.contains('is-on'));

  doc.getElementById('filterClearBtn').click();

  // "Clear all" must return the toggle to its default ("Any"): the Any radio
  // checked, the Only radio cleared, and the green highlight back on Any.
  assert.equal(anyRadio.checked, true);
  assert.equal(onlyRadio.checked, false);
  assert.ok(anyRadio.parentElement.classList.contains('is-on'));
  assert.ok(!onlyRadio.parentElement.classList.contains('is-on'));

  // The cleared mode must be "any": ticking one reason now shows both the
  // single- and multi-reason groups (only-mode would hide the multi one).
  const deleteCb = doc.querySelector('[data-filter-status][value="delete"]');
  deleteCb.checked = false;
  fire(deleteCb);
  const lowSeeds = doc.querySelector('[data-filter-reason][value="LOW_SEEDS"]');
  lowSeeds.checked = true;
  fire(lowSeeds);
  assert.equal(visibleGroups(doc).length, 2);
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
