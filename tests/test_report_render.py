"""Python-assertable coverage of the HTML/CSV report generator
(cross_seed_cleaner.py:1104-2924): the data-attribute contract the JS depends
on, injected JS constants, render branches, CSV format, and escaping sinks.

eligible_ids are 1-BASED group indices (export_reports enumerates with start=1,
matching _run_analyze_and_finalize)."""
import re

import pytest

from conftest import reconfigure, render_html, render_csv, ReportHTML

GIB = 1024 ** 3


def t(name, seeds=10, size=3 * GIB, seeded=20 * 86400, cat="movies", tr="aither.cc", **extra):
    d = dict(name=name, content_path="/d/" + name, category=cat,
             tracker=f"http://{tr}/a", _tracker_domain=tr, size=size, ratio=1.5,
             uploaded=size, seeding_time=seeded, added_on=1700000000,
             num_complete=seeds, _seeder_count=seeds, hash=name)
    d.update(extra)
    return d


def evaluate(csc, items):
    """Return 1-based eligible_ids, stamping _evaluation like the real pipeline."""
    elig = set()
    for i, (_gid, d) in enumerate(items, 1):
        ev = csc.evaluate_group(d)
        d["_evaluation"] = ev
        if ev["eligible"]:
            elig.add(i)
    return elig


def std(csc):
    return reconfigure(csc, MIN_SEEDERS=5, MAX_TORRENTS_IN_GROUP=3, MIN_SIZE_GIB=2,
                       MIN_ORIGINAL_SEED_TIME_DAYS=10, CATEGORY_FILTER_MODE="none",
                       MISSING_HARD_LINKS_MODE=False)


def data_rows(parser):
    return [a for (_tag, a) in parser.tags if "data-sk-0" in a]


# ─── data-attribute contract ─────────────────────────────────────────────────

def test_every_data_row_has_all_sort_keys(csc, tmp_path):
    std(csc)
    items = [("g0", {"original": t("A"), "crossseeds": [t("B")]})]
    html = render_html(csc, items, evaluate(csc, items), tmp_path)
    rows = data_rows(ReportHTML(html))
    assert rows, "no data rows emitted"
    for a in rows:
        for col in range(12):
            assert f"data-sk-{col}" in a, f"row missing data-sk-{col}"


def test_numeric_sort_key_formatting(csc, tmp_path):
    std(csc)
    items = [("g0", {"original": t("A", seeds=10, size=3 * GIB, seeded=1728000), "crossseeds": [t("B")]})]
    html = render_html(csc, items, evaluate(csc, items), tmp_path)
    orig_row = next(a for a in data_rows(ReportHTML(html)) if a["data-sk-10"] == "a")
    assert orig_row["data-sk-2"] == "10"              # seeds: int
    assert orig_row["data-sk-3"] == "1.5000"          # ratio: .4f
    assert orig_row["data-sk-4"] == str(3 * GIB)      # size: bytes
    assert orig_row["data-sk-6"] == "1728000"         # seeded: seconds
    assert orig_row["data-sk-7"] == "1700000000"      # added: epoch


def test_cols_template_has_twelve_tracks(csc, tmp_path):
    std(csc)
    items = [("g0", {"original": t("A"), "crossseeds": [t("B")]})]
    html = render_html(csc, items, evaluate(csc, items), tmp_path)
    cols = re.search(r"--cols:(.*?);", html, re.DOTALL).group(1)
    assert cols.count("max-content") == 8
    assert "140px" in cols and "130px" in cols
    assert "minmax(200px, 1fr)" in cols and "minmax(220px, 2fr)" in cols


def test_group_data_status(csc, tmp_path):
    std(csc)
    items = [("g0", {"original": t("Del"), "crossseeds": [t("DelX")]}),
             ("g1", {"original": t("Keep", seeds=1), "crossseeds": []})]
    html = render_html(csc, items, evaluate(csc, items), tmp_path)
    assert re.findall(r'data-status="(\w+)"', html) == ["delete", "keep"]


# ─── injected JS constants ───────────────────────────────────────────────────

def test_injected_constants(csc, tmp_path):
    std(csc)
    items = [("g0", {"original": t("A", tr="zeta.cc", cat="movies"),
                     "crossseeds": [t("B", tr="alpha.cc", cat="tv")]})]
    html = render_html(csc, items, evaluate(csc, items), tmp_path)
    assert "const NUMERIC_SK = new Set([2, 3, 4, 5, 6, 7]);" in html
    assert "const NARROW = 8;" in html
    assert "const TOTAL_GROUPS = 1;" in html
    assert "const TOTAL_TORRENTS = 2;" in html
    assert 'const UNIQUE_TRACKERS = ["alpha.cc", "zeta.cc"];' in html   # sorted
    assert 'const UNIQUE_CATEGORIES = ["movies", "tv"];' in html


def test_unique_categories_includes_empty(csc, tmp_path):
    std(csc)
    items = [("g0", {"original": t("A", cat=""), "crossseeds": [t("B", cat="movies")]})]
    html = render_html(csc, items, evaluate(csc, items), tmp_path)
    assert 'const UNIQUE_CATEGORIES = ["", "movies"];' in html


# ─── render branches ─────────────────────────────────────────────────────────

def test_mode_label_standard(csc, tmp_path):
    std(csc)
    items = [("g0", {"original": t("A"), "crossseeds": [t("B")]})]
    html = render_html(csc, items, evaluate(csc, items), tmp_path)
    assert "STANDARD" in html and "MISSING HARD LINKS" not in html


def test_mode_label_missing_hard_links(csc, tmp_path):
    reconfigure(csc, MIN_SEEDERS=5, MAX_TORRENTS_IN_GROUP=3, MIN_SIZE_GIB=2,
                MIN_ORIGINAL_SEED_TIME_DAYS=10, CATEGORY_FILTER_MODE="none",
                MISSING_HARD_LINKS_MODE=True)
    items = [("g0", {"original": t("A"), "crossseeds": []})]
    html = render_html(csc, items, evaluate(csc, items), tmp_path)
    assert "MISSING HARD LINKS" in html


def test_dry_run_vs_live_class(csc, tmp_path):
    std(csc)
    items = [("g0", {"original": t("A"), "crossseeds": [t("B")]})]
    csc.DRY_RUN = True
    assert 'dry-run' in render_html(csc, items, evaluate(csc, items), tmp_path)
    csc.DRY_RUN = False
    assert 'live-mode' in render_html(csc, items, evaluate(csc, items), tmp_path)


def test_rejection_icons_only_on_keep_with_reasons(csc, tmp_path):
    std(csc)
    # one keep group failing LOW_SEEDS
    items = [("g0", {"original": t("Keep", seeds=1), "crossseeds": []})]
    html = render_html(csc, items, evaluate(csc, items), tmp_path)
    icons = ReportHTML(html).with_class("rejection-icon")
    assert icons, "keep-with-reason group should show rejection icons"
    assert any("Low seeder count" in a.get("data-tip", "") for _tag, a in icons)


def test_no_rejection_icons_when_all_eligible(csc, tmp_path):
    std(csc)
    items = [("g0", {"original": t("A"), "crossseeds": [t("B")]})]
    html = render_html(csc, items, evaluate(csc, items), tmp_path)
    assert ReportHTML(html).with_class("rejection-icon") == []


def test_ext_row_rendered(csc, tmp_path):
    std(csc)
    items = [("g0", {"original": t("Ext", _external_hardlink=True, _external_path="/mnt/lib/Ext"),
                     "crossseeds": [t("ExtX")]})]
    html = render_html(csc, items, evaluate(csc, items), tmp_path)
    assert "External Library" in html
    assert "/mnt/lib/Ext" in html


def test_empty_dataset(csc, tmp_path):
    std(csc)
    html = render_html(csc, [], set(), tmp_path)
    assert "const TOTAL_GROUPS = 0;" in html
    assert data_rows(ReportHTML(html)) == []


def test_initial_sort_from_config(csc, tmp_path):
    std(csc)
    csc.SORT_BY = "seeders"
    csc.SORT_ORDER = "desc"
    items = [("g0", {"original": t("A"), "crossseeds": [t("B")]})]
    html = render_html(csc, items, evaluate(csc, items), tmp_path)
    assert "let lastSortedCol = 2;" in html      # seeders -> col 2
    assert "let sortDirection = -1;" in html      # desc


# ─── CSV ─────────────────────────────────────────────────────────────────────

def test_csv_header_and_types(csc, tmp_path):
    std(csc)
    items = [("g0", {"original": t("Ext", _external_hardlink=True, _external_path="/mnt/lib/Ext"),
                     "crossseeds": [t("ExtX")]})]
    csv_text = render_csv(csc, items, evaluate(csc, items), tmp_path)
    assert csv_text.splitlines()[0] == (
        "Group ID,Status,Type,Name,Size,Tracker,Category,Added,Seeding Time,Ratio,Seeders,Reasons,Path")
    # \b so the EXT type value isn't matched inside reason codes like EXTERNAL_LINK.
    # The cross-seed row is labelled "CROSS" to match the HTML badge and CLI table.
    types = set(re.findall(r"\b(ORIGINAL|CROSS|EXT)\b", csv_text))
    assert types == {"ORIGINAL", "CROSS", "EXT"}


def test_csv_neutralizes_formula_injection(csc, tmp_path):
    # A field whose first char is a spreadsheet formula trigger is prefixed with
    # a single quote so it can't execute when the CSV opens in Excel/Sheets; an
    # ordinary value (incl. a '-' mid-string) is left untouched. All four
    # attacker-controlled string columns are guarded, not just Name.
    import csv as _csv, io
    std(csc)
    items = [("g0", {"original": t("=2+5", tr="=HYPERLINK(1)", cat="+danger",
                                    content_path="@evil"),
                     "crossseeds": [t("Plain-Name")]})]
    csv_text = render_csv(csc, items, evaluate(csc, items), tmp_path)
    rows = list(_csv.DictReader(io.StringIO(csv_text)))
    names = {r["Name"] for r in rows}
    assert "'=2+5" in names          # leading = neutralized
    assert "Plain-Name" in names     # interior '-' unchanged
    orig = next(r for r in rows if r["Name"] == "'=2+5")
    assert orig["Tracker"] == "'=HYPERLINK(1)"   # tracker domain neutralized
    assert orig["Category"] == "'+danger"        # category neutralized
    assert orig["Path"] == "'@evil"              # content path neutralized


def test_csv_safe_unit(csc):
    assert csc.csv_safe("=cmd") == "'=cmd"
    assert csc.csv_safe("+1") == "'+1"
    assert csc.csv_safe("-rm") == "'-rm"
    assert csc.csv_safe("@x") == "'@x"
    assert csc.csv_safe("\tcmd") == "'\tcmd"                  # leading tab (0x09)
    assert csc.csv_safe("\rcmd") == "'\rcmd"                  # leading CR (0x0D)
    assert csc.csv_safe("\n=cmd") == "'\n=cmd"                # leading LF (0x0A) — OWASP trigger
    assert csc.csv_safe("＝1+1") == "'＝1+1"          # full-width '=' (U+FF1D)
    assert csc.csv_safe("＠cmd") == "'＠cmd"          # full-width '@' (U+FF20)
    assert csc.csv_safe(" =cmd") == " =cmd"                   # leading space keeps it text, not a trigger
    assert csc.csv_safe("My-Movie 2024") == "My-Movie 2024"   # interior trigger untouched
    assert csc.csv_safe("") == ""
    assert csc.csv_safe(None) == ""


def test_html_badge_tooltips(csc, tmp_path):
    # The abbreviated type badges carry a hover tooltip spelling out the full term.
    std(csc)
    items = [("g0", {"original": t("Ext", _external_hardlink=True, _external_path="/mnt/lib/Ext"),
                     "crossseeds": [t("ExtX")]})]
    html = render_html(csc, items, evaluate(csc, items), tmp_path)
    titles = {a.get("title") for (_tag, a) in ReportHTML(html).with_class("type-badge")}
    assert "Cross-seed" in titles        # CROSS badge tooltip
    assert "External library" in titles  # EXT badge tooltip


# ─── mode-aware threshold cell coloring (HTML) ───────────────────────────────

def _seeds_cell_class(html):
    """Class on the Seeds-cell span of the only 0-seeder row in the report."""
    m = re.search(r'<div class="cell"><span class="([^"]*)">0</span></div>', html)
    assert m, "0-seeder Seeds cell not found in report"
    return m.group(1)


def test_html_tracker_error_mode_neutralizes_seeds_color(csc, tmp_path):
    # Tracker-error mode bypasses MIN_SEEDERS, so an eligible 0-seeder dead
    # torrent must NOT render its Seeds cell red in the HTML report (mirrors the
    # CLI print_group rule). Otherwise the cell contradicts the green DELETE/DEAD
    # row it sits in.
    reconfigure(csc, TRACKER_ERROR_MODE=True, MISSING_HARD_LINKS_MODE=False,
                MIN_SEEDERS=5, MAX_TORRENTS_IN_GROUP=3, MIN_SIZE_GIB=2,
                MIN_ORIGINAL_SEED_TIME_DAYS=10, CATEGORY_FILTER_MODE="none")
    items = [("g0", {"original": t("Dead", seeds=0, size=5 * GIB), "crossseeds": []})]
    html = render_html(csc, items, {1}, tmp_path)
    assert "text-danger" not in _seeds_cell_class(html)


def test_html_standard_mode_low_seeds_color_red(csc, tmp_path):
    # Counterpart: standard mode genuinely applies MIN_SEEDERS, so the same
    # 0-seeder original keeps its red Seeds cell.
    std(csc)
    reconfigure(csc, TRACKER_ERROR_MODE=False)
    items = [("g0", {"original": t("Low", seeds=0, size=5 * GIB), "crossseeds": []})]
    html = render_html(csc, items, {1}, tmp_path)
    assert _seeds_cell_class(html) == "text-danger"


# ─── escaping (combined HTML + JS sinks) ─────────────────────────────────────

def test_combined_payload_escaped_in_both_sinks(csc, tmp_path):
    std(csc)
    payload = '</script><script>alert("x")</script>'
    items = [("g0", {"original": t("A", tr=payload, cat="movies"),
                     "crossseeds": [t("B")]})]
    html = render_html(csc, items, evaluate(csc, items), tmp_path)
    assert '<script>alert("x")' not in html            # not raw anywhere
    assert "<\\/script>" in html                         # JS-escaped in inline JS


# ─── within-group cross-seed ordering ────────────────────────────────────────

def _sorted_items(csc):
    return reconfigure(csc, MIN_SEEDERS=5, MAX_TORRENTS_IN_GROUP=9, MIN_SIZE_GIB=2,
                       MIN_ORIGINAL_SEED_TIME_DAYS=10, CATEGORY_FILTER_MODE="none",
                       MISSING_HARD_LINKS_MODE=False, SORT_BY="name", SORT_ORDER="asc")


def test_html_crossseeds_render_in_sort_order(csc, tmp_path):
    # The HTML report must order a group's cross-seeds by the configured sort
    # field (name asc here), with the original pinned first — matching the CLI.
    # Given out-of-order cross-seeds the report has to reorder them, not emit
    # them in their raw add-order.
    _sorted_items(csc)
    items = [("g0", {"original": t("Orig"),
                     "crossseeds": [t("Zeta"), t("alpha"), t("Mike")]})]
    html = render_html(csc, items, evaluate(csc, items), tmp_path)
    names = [a["data-sk-10"] for a in data_rows(ReportHTML(html))]
    assert names[0] == "orig"                          # original first
    assert names[1:] == ["alpha", "mike", "zeta"]      # cross-seeds name-sorted


def test_csv_crossseeds_render_in_sort_order(csc, tmp_path):
    # Same contract for the CSV export.
    import csv as _csv
    import io
    _sorted_items(csc)
    items = [("g0", {"original": t("Orig"),
                     "crossseeds": [t("Zeta"), t("alpha"), t("Mike")]})]
    csv_text = render_csv(csc, items, evaluate(csc, items), tmp_path)
    rows = list(_csv.DictReader(io.StringIO(csv_text)))
    names = [r["Name"] for r in rows]
    assert names[0] == "Orig"                          # original first
    assert names[1:] == ["alpha", "Mike", "Zeta"]      # cross-seeds name-sorted


# ─── unknown (unscraped) seeder counts ───────────────────────────────────────

def test_html_unknown_seeds_render_na(csc, tmp_path):
    # qBittorrent reports -1 for an unscraped torrent. The HTML cell must show
    # N/A (neutral, no color class), not a misleading negative number — while
    # the numeric sort key keeps the raw value and the seeds-slider lower bound
    # is clamped to 0 (never negative).
    std(csc)
    items = [("g0", {"original": t("A", seeds=-1, size=5 * GIB), "crossseeds": []})]
    html = render_html(csc, items, evaluate(csc, items), tmp_path)
    assert '<span class="">N/A</span>' in html       # neutral N/A cell
    assert ">-1</span>" not in html                   # no raw negative shown
    parser = ReportHTML(html)
    orig_row = next(a for a in data_rows(parser) if a["data-sk-10"] == "a")
    assert orig_row["data-sk-2"] == "-1"              # sort key still numeric/raw
    grp = next(a for (_tag, a) in parser.tags if "data-seeds-min" in a)
    assert grp["data-seeds-min"] == "0"               # slider bound clamped


def test_csv_unknown_seeds_left_empty(csc, tmp_path):
    # The CSV leaves an unscraped count blank (not "-1", not "N/A") so the
    # Seeders column stays numeric-sortable in a spreadsheet.
    import csv as _csv
    import io
    std(csc)
    items = [("g0", {"original": t("A", seeds=-1, size=5 * GIB), "crossseeds": []})]
    csv_text = render_csv(csc, items, evaluate(csc, items), tmp_path)
    rows = list(_csv.DictReader(io.StringIO(csv_text)))
    assert rows[0]["Seeders"] == ""
