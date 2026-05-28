// Pure, side-effect-free helpers for the cross-seed-cleaner HTML report.
// Inlined into the report as a classic <script> (so the functions become
// globals the report's main script calls) and also importable under Node via
// the UMD tail below, so the test suite can unit-test the logic directly.

// Split a --cols value on whitespace while keeping parenthesized segments
// (e.g. "minmax(200px, 1fr)") as single atomic tokens. A plain /\s+/ split
// corrupts those tokens because of the space after the comma, producing 14
// tokens for 12 tracks and mis-indexing the resize handlers.
function parseCols(s) {
    const out = []; let buf = ''; let depth = 0;
    for (const ch of s) {
        if (ch === '(') { depth++; buf += ch; }
        else if (ch === ')') { depth--; buf += ch; }
        else if (depth === 0 && /\s/.test(ch)) {
            if (buf) { out.push(buf); buf = ''; }
        } else { buf += ch; }
    }
    if (buf) out.push(buf);
    return out;
}

// Per-column drag floor: the first px number in its --cols token.
// "Xpx" -> X; "minmax(Xpx, Yfr)" -> X; missing token -> 30.
function colMinFromCols(cols, idx) {
    const c = cols[idx];
    if (!c) return 30;
    const m = c.match(/(\d+(?:\.\d+)?)/);
    return m ? parseFloat(m[1]) : 30;
}

// Inclusive numeric range predicate. A null bound means "unbounded". Uses
// !(n >= min) so NaN (e.g. an empty '-' cell on an EXT row) fails whenever a
// bound is set.
function numericInRange(val, min, max) {
    const n = parseFloat(val);
    if (min !== null && !(n >= min)) return false;
    if (max !== null && !(n <= max)) return false;
    return true;
}

// Parse a data-sk-* attribute into a sort key. Missing (null) sorts as 0 for
// numeric columns and '' for string columns.
function parseSortKey(v, isNum) {
    if (v === null) return isNum ? 0 : '';
    return isNum ? parseFloat(v) : v;
}

// Compare two sort keys: numeric subtraction or lexical comparison.
function compareSortKeys(a, b, isNum) {
    if (isNum) return a - b;
    return a < b ? -1 : a > b ? 1 : 0;
}

if (typeof module !== 'undefined' && module.exports) {
    module.exports = { parseCols, colMinFromCols, numericInRange, parseSortKey, compareSortKeys };
}
