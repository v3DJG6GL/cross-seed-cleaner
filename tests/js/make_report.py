#!/usr/bin/env python3
"""Generate a small deterministic HTML report for the jsdom DOM tests.

Usage: python3 tests/js/make_report.py <output.html>
Writes the report to the given path (Chart.js source blanked so jsdom can stub
window.Chart without pulling in canvas). The dataset has one delete-eligible
group and two keep groups (one with an EXT row) across distinct trackers,
categories, and seed counts so sort/filter behavior is observable.
"""
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)
OUT = sys.argv[1]
sys.argv = ["make_report.py"]

import cross_seed_cleaner as csc  # noqa: E402

csc.MIN_SEEDERS = 5
csc.MAX_TORRENTS_IN_GROUP = 10
csc.MIN_SIZE_GIB = 1
csc.MIN_SIZE_BYTES = 1 * 1024 ** 3
csc.MIN_ORIGINAL_SEED_TIME_DAYS = 1
csc.MIN_ORIGINAL_SEED_TIME_SECONDS = 86400
csc.CATEGORY_FILTER_MODE = "none"
csc._CATEGORY_FILTER_MODE_LC = "none"
csc.NO_HARD_LINKS_MODE = False
csc.DRY_RUN = True
csc.CHARTJS_SOURCE = "/* chart.js stripped for tests */"

GIB = 1024 ** 3


def t(name, seeds, tr, cat, **extra):
    d = dict(name=name, content_path="/d/" + name, category=cat,
             tracker=f"http://{tr}/a", _tracker_domain=tr, size=3 * GIB, ratio=1.5,
             uploaded=3 * GIB, seeding_time=30 * 86400, added_on=1700000000,
             num_complete=seeds, _seeder_count=seeds, hash=name)
    d.update(extra)
    return d


items = [
    ("g0", {"original": t("DelA", 100, "aaa.cc", "movies"),
            "crossseeds": [t("DelB", 80, "bbb.cc", "tv")]}),
    ("g1", {"original": t("KeepLow", 1, "ccc.cc", "music"), "crossseeds": []}),
    ("g2", {"original": t("ExtO", 50, "ddd.cc", "games",
                          _external_hardlink=True, _external_path="/mnt/lib/ExtO"),
            "crossseeds": [t("ExtX", 60, "eee.cc", "books")]}),
]

eligible = set()
for i, (_gid, d) in enumerate(items, 1):
    ev = csc.evaluate_group(d)
    d["_evaluation"] = ev
    if ev["eligible"]:
        eligible.add(i)

# export_reports appends a timestamp before the extension; redirect to OUT.
import glob  # noqa: E402
import tempfile  # noqa: E402

tmp = tempfile.mkdtemp()
csc.HTML_EXPORT = os.path.join(tmp, "r.html")
csc.CSV_EXPORT = ""
csc.export_reports(sorted_items=items, eligible_ids=eligible)
produced = sorted(glob.glob(os.path.join(tmp, "r*.html")))[-1]
with open(produced, "r", encoding="utf-8") as f:
    html = f.read()
with open(OUT, "w", encoding="utf-8") as f:
    f.write(html)
