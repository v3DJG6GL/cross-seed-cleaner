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
    # A name whose first char is a spreadsheet formula trigger is prefixed with
    # a single quote so it can't execute when the CSV opens in Excel/Sheets; an
    # ordinary name (incl. a '-' mid-string) is left untouched.
    import csv as _csv, io
    std(csc)
    items = [("g0", {"original": t("=2+5"), "crossseeds": [t("Plain-Name")]})]
    csv_text = render_csv(csc, items, evaluate(csc, items), tmp_path)
    names = {r["Name"] for r in _csv.DictReader(io.StringIO(csv_text))}
    assert "'=2+5" in names          # leading = neutralized
    assert "Plain-Name" in names     # interior '-' unchanged


def test_csv_safe_unit(csc):
    assert csc.csv_safe("=cmd") == "'=cmd"
    assert csc.csv_safe("+1") == "'+1"
    assert csc.csv_safe("-rm") == "'-rm"
    assert csc.csv_safe("@x") == "'@x"
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


# ─── escaping (combined HTML + JS sinks) ─────────────────────────────────────

def test_combined_payload_escaped_in_both_sinks(csc, tmp_path):
    std(csc)
    payload = '</script><script>alert("x")</script>'
    items = [("g0", {"original": t("A", tr=payload, cat="movies"),
                     "crossseeds": [t("B")]})]
    html = render_html(csc, items, evaluate(csc, items), tmp_path)
    assert '<script>alert("x")' not in html            # not raw anywhere
    assert "<\\/script>" in html                         # JS-escaped in inline JS
