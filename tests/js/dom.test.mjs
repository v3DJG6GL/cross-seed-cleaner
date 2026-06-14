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

test('sortTable orders cross-seeds within a group and pins the EXT row last', () => {
  const { window } = load();
  const doc = window.document;

  // g2 is the only fixture group with >1 cross-seed plus an EXT pseudo-row, so
  // it is the only one that reaches sortTable's per-group `middle.sort` /
  // trailing-EXT branch (every other group hits the <=2-row early return). Find
  // it by its EXT trailing row.
  const g2 = [...doc.querySelectorAll('.group')].find(
    g => [...g.children].some(r => r.getAttribute && r.getAttribute('data-sk-1') === 'ext'));
  assert.ok(g2, 'no group with an EXT row in the fixture');

  const rowsOf = () => [...g2.children].filter(c => c.classList && c.classList.contains('grid-row'));
  const seedsOf = (r) => r.getAttribute('data-sk-2');

  // Sanity: original first, EXT last, two cross-seeds in the middle.
  const start = rowsOf();
  assert.equal(start.length, 4, 'expected original + 2 cross-seeds + EXT');
  assert.equal(start[0].getAttribute('data-sk-1'), 'original');
  assert.equal(start[start.length - 1].getAttribute('data-sk-1'), 'ext');

  window.sortTable(2);  // seeds ascending
  let rows = rowsOf();
  // Original stays pinned at index 0; EXT stays pinned last regardless of its
  // (zero) seed key; the two middle cross-seeds sort ascending by seeds.
  assert.equal(rows[0].getAttribute('data-sk-1'), 'original');
  assert.equal(rows[rows.length - 1].getAttribute('data-sk-1'), 'ext');
  const midAsc = rows.slice(1, -1).map(seedsOf);
  assert.deepEqual(midAsc, [...midAsc].sort((a, b) => a - b),
    'middle cross-seeds not sorted ascending');
  assert.deepEqual(midAsc, ['10', '60']);

  window.sortTable(2);  // toggle to descending
  rows = rowsOf();
  assert.equal(rows[0].getAttribute('data-sk-1'), 'original');
  assert.equal(rows[rows.length - 1].getAttribute('data-sk-1'), 'ext');
  const midDesc = rows.slice(1, -1).map(seedsOf);
  assert.deepEqual(midDesc, ['60', '10'],
    'middle cross-seeds did not flip to descending (or EXT row escaped the bottom)');
});

test('name and path search boxes are scoped to their own column', async () => {
  const { window } = load();
  const doc = window.document;

  // Drop the default DELETE filter so all three fixture groups are candidates.
  const deleteCb = doc.querySelector('[data-filter-status][value="delete"]');
  deleteCb.checked = false;
  deleteCb.dispatchEvent(new window.Event('change', { bubbles: true }));

  const nameInput = doc.querySelector('[data-filter="name"]');
  const pathInput = doc.querySelector('[data-filter="path"]');
  // The text boxes apply on a 120ms debounce; fire the real 'input' event and
  // wait it out (drives the report's actual filter path, no internal hooks).
  async function search(input, q) {
    input.value = q;
    input.dispatchEvent(new window.Event('input', { bubbles: true }));
    await new Promise(r => setTimeout(r, 170));
  }

  // Fixture g1 is the torrent "KeepLow" on tracker ccc.cc, category music,
  // content path /d/KeepLow.
  await search(nameInput, 'keeplow');           // real name → matches
  assert.equal(visibleGroups(doc).length, 1);

  await search(nameInput, 'ccc.cc');            // its TRACKER → must not match a name
  assert.equal(visibleGroups(doc).length, 0,
    'tracker text leaked into the name search (boxes share a blob)');

  await search(nameInput, 'music');             // its CATEGORY → must not match a name
  assert.equal(visibleGroups(doc).length, 0,
    'category text leaked into the name search');

  await search(nameInput, '/d/keeplow');        // its PATH → must not match a name
  assert.equal(visibleGroups(doc).length, 0,
    'path text leaked into the name search');

  await search(nameInput, '');                  // clear the name box
  await search(pathInput, '/d/keeplow');        // real path → matches in the path box
  assert.equal(visibleGroups(doc).length, 1);

  await search(pathInput, '/mnt/lib');          // g2's external_path /mnt/lib/ExtO
  assert.equal(visibleGroups(doc).length, 1);
});

// ── Range / date / dropdown filters ─────────────────────────────────────────
// Exercise the numeric range inputs (seeds/ratio/size/uploaded/seeded), the date
// filter (incl. its inclusive end-of-day), and the tracker/category dropdowns —
// paths the smoke tests above never touched. The three fixture-group originals
// carry distinct values for each dimension (see make_report.py).
function dropDelete(doc, window) {
  // Remove the pre-checked DELETE status filter so all three groups are candidates.
  const cb = doc.querySelector('[data-filter-status][value="delete"]');
  cb.checked = false;
  cb.dispatchEvent(new window.Event('change', { bubbles: true }));
}

async function setInput(window, doc, name, value) {
  // Range/date inputs apply on the same 120ms debounce as the text boxes.
  const el = doc.querySelector(`[data-filter="${name}"]`);
  el.value = value;
  el.dispatchEvent(new window.Event('input', { bubbles: true }));
  await new Promise(r => setTimeout(r, 170));
}

function selectDropdown(window, doc, panel, value) {
  const cb = [...doc.querySelectorAll(`[data-filter-panel="${panel}"] input[type="checkbox"]`)]
    .find(c => c.value === value);
  assert.ok(cb, `no ${panel} dropdown option for "${value}"`);
  cb.checked = true;
  cb.dispatchEvent(new window.Event('change', { bubbles: true }));
}

function soleVisibleName(doc) {
  const vis = visibleGroups(doc);
  assert.equal(vis.length, 1, `expected exactly one visible group, got ${vis.length}`);
  return vis[0].querySelector('.name-cell').textContent.trim();
}

test('seeds range filter selects by the group worst-case seed count', async () => {
  const { window } = load();
  const doc = window.document;
  dropDelete(doc, window);
  // group seeds-min: g0=80, g1=1, g2=1 → min 50 keeps only g0.
  await setInput(window, doc, 'seedsMin', '50');
  const vis = visibleGroups(doc);
  assert.equal(vis.length, 1);
  assert.equal(vis[0].dataset.seedsMin, '80');
});

test('size range filter reads the group original size in GiB', async () => {
  const { window } = load();
  const doc = window.document;
  dropDelete(doc, window);
  // original sizes: g0=3, g1=8, g2=20 GiB → min 10 keeps only g2.
  await setInput(window, doc, 'sizeMin', '10');
  assert.ok(soleVisibleName(doc).includes('ExtO'));
});

test('ratio range filter reads the group original ratio', async () => {
  const { window } = load();
  const doc = window.document;
  dropDelete(doc, window);
  // original ratios: g0=1.5, g1=0.5, g2=3.0 → min 2 keeps only g2.
  await setInput(window, doc, 'ratioMin', '2');
  assert.ok(soleVisibleName(doc).includes('ExtO'));
});

test('uploaded range filter reads the group original uploaded total', async () => {
  const { window } = load();
  const doc = window.document;
  dropDelete(doc, window);
  // original uploaded: g0=3, g1=1, g2=10 GiB → min 5 keeps only g2.
  await setInput(window, doc, 'upMin', '5');
  assert.ok(soleVisibleName(doc).includes('ExtO'));
});

test('seeded-time range filter reads the group original seed time in days', async () => {
  const { window } = load();
  const doc = window.document;
  dropDelete(doc, window);
  // original seed days: g0=30, g1=100, g2=5 → min 50 keeps only g1.
  await setInput(window, doc, 'seededMin', '50');
  assert.ok(soleVisibleName(doc).includes('KeepLow'));
});

test('date "from" filter keeps groups added on or after the date', async () => {
  const { window } = load();
  const doc = window.document;
  dropDelete(doc, window);
  // added: g0=2023-11, g1=2017-07, g2=2020-09 → from 2023-01-01 keeps only g0.
  await setInput(window, doc, 'addedFrom', '2023-01-01');
  assert.ok(soleVisibleName(doc).includes('DelA'));
});

test('date "to" filter includes events on the end date itself (inclusive end-of-day)', async () => {
  const { window } = load();
  const doc = window.document;
  dropDelete(doc, window);
  // g2 was added 2020-09-13 12:26:40 UTC — AFTER that day's midnight, so it only
  // survives a "to 2020-09-13" bound because the filter extends it to end-of-day.
  await setInput(window, doc, 'addedFrom', '2020-01-01');
  await setInput(window, doc, 'addedTo', '2020-09-13');
  assert.ok(soleVisibleName(doc).includes('ExtO'));
});

test('tracker dropdown shows only groups with a row on the chosen tracker', async () => {
  const { window } = load();
  const doc = window.document;
  dropDelete(doc, window);
  // ccc.cc is g1's only tracker (KeepLow); g0=aaa/bbb, g2=ddd/eee.
  selectDropdown(window, doc, 'tracker', 'ccc.cc');
  await new Promise(r => setTimeout(r, 10));
  assert.ok(soleVisibleName(doc).includes('KeepLow'));
});

test('category dropdown shows only groups with a row in the chosen category', async () => {
  const { window } = load();
  const doc = window.document;
  dropDelete(doc, window);
  // "games" is ExtO's category in g2; g0=movies/tv, g1=music.
  selectDropdown(window, doc, 'category', 'games');
  await new Promise(r => setTimeout(r, 10));
  assert.ok(soleVisibleName(doc).includes('ExtO'));
});

// The type/EXT badges carry a native `title`; the generic overflow tooltip also
// fires on any clipped cell. Without a guard, a hand-narrowed Type column would
// show both at once. jsdom has no layout engine, so stub the width getters to
// simulate clipping and drive the real mouseover handler.
function forceClipped(el) {
  Object.defineProperty(el, 'scrollWidth', { value: 200, configurable: true });
  Object.defineProperty(el, 'clientWidth', { value: 20, configurable: true });
}

test('overflow tooltip is suppressed on a clipped cell that has a native title', () => {
  const { window } = load();
  const doc = window.document;
  const tip = doc.getElementById('rsnTip');
  assert.ok(tip, '#rsnTip not created');

  const badge = doc.querySelector('.type-badge[title]');   // CROSS or EXT
  assert.ok(badge, 'no titled type badge in fixture');
  forceClipped(badge.closest('.cell'));

  badge.dispatchEvent(new window.MouseEvent('mouseover', { bubbles: true }));
  assert.equal(tip.classList.contains('visible'), false,
    'custom overflow tooltip fired on a cell that already has a native title (double tooltip)');
});

test('overflow tooltip still fires on a clipped cell with no native title', () => {
  const { window } = load();
  const doc = window.document;
  const tip = doc.getElementById('rsnTip');

  const nameCell = doc.querySelector('.name-cell');
  assert.ok(nameCell, 'no name cell in fixture');
  assert.equal(nameCell.querySelector('[title]'), null, 'name cell unexpectedly has a titled child');
  forceClipped(nameCell);

  nameCell.dispatchEvent(new window.MouseEvent('mouseover', { bubbles: true }));
  assert.equal(tip.classList.contains('visible'), true,
    'overflow tooltip should still work for cells without a native title');
  assert.ok(tip.textContent.length > 0, 'overflow tooltip text empty');
});
