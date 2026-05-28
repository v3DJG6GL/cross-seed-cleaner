#!/usr/bin/env python3
"""Generate a realistic ~10k-torrent HTML report for hands-on UX testing.

Usage:
    python3 tests/render_smoke.py            # ~2000 groups (default)
    python3 tests/render_smoke.py 5000       # 5000 groups

Prints the absolute path of the generated HTML on the last line.
Deterministic via random.seed(42), so successive runs produce the same file
content (different filename only because of the timestamp).

Generates groups that exercise every rejection-reason path:
- LOW_SEEDS, SMALL_SIZE, LOW_TIME, TOO_MANY, EXTERNAL_LINK, CATEGORY_FILTER
- DELETE-eligible (no rejection reason)
- Mixed reasons (multiple icons on one badge)
- ORPHAN groups (no cross-seeds), groups with EXT row, big groups (>= MAX)

Not part of pytest — this is a developer tool for visually exercising the
filter / sort / popover / slider behaviour against a realistic dataset.
"""
import os
import sys
import glob
import random
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Capture user args BEFORE we wipe sys.argv to keep cross_seed_cleaner's
# argparse import-time behaviour from blowing up.
_USER_ARGS = sys.argv[1:]
sys.argv = ["render_smoke.py"]

import cross_seed_cleaner as csc  # noqa: E402
from fixtures import gen_group  # noqa: E402

# Set tunables explicitly (not via argparse) so the demo deterministically
# exercises every rejection reason regardless of the user's environment.
csc.MIN_SEEDERS = 10
csc.MAX_TORRENTS_IN_GROUP = 5
csc.MIN_ORIGINAL_SEED_TIME_DAYS = 30
csc.MIN_ORIGINAL_SEED_TIME_SECONDS = csc.MIN_ORIGINAL_SEED_TIME_DAYS * 86400
csc.MIN_SIZE_GIB = 2
csc.MIN_SIZE_BYTES = csc.MIN_SIZE_GIB * 1024 ** 3
csc.CATEGORY_BLOCKLIST = ["games"]
csc.CATEGORY_ALLOWLIST = []
csc.CATEGORY_FILTER_MODE = "block"
# category_allowed reads pre-compiled spec lists; re-compile after override.
csc._CATEGORY_BLOCKLIST_SPECS = csc._compile_specs(csc.CATEGORY_BLOCKLIST, "CATEGORY_BLOCKLIST")
csc._CATEGORY_ALLOWLIST_SPECS = csc._compile_specs(csc.CATEGORY_ALLOWLIST, "CATEGORY_ALLOWLIST")
csc.NO_HARD_LINKS_MODE = False
csc.UNRELIABLE_TRACKERS = ["torrentday.com"]


def main():
    n_groups = int(_USER_ARGS[0]) if _USER_ARGS else 2000
    rng = random.Random(42)

    # Compose the dataset with a deliberate mix of every kind so each
    # rejection-reason icon shows up at least dozens of times.
    weighted_kinds = (
        ["normal"]          * 50 +
        ["low_seeds"]       * 12 +
        ["small_size"]      * 10 +
        ["low_time"]        * 10 +
        ["too_many"]        * 4  +
        ["external_link"]   * 5  +
        ["category_filter"] * 7  +
        ["multi_reason"]    * 5  +
        ["orphan"]          * 7
    )
    sorted_items = []
    for i in range(n_groups):
        kind = rng.choice(weighted_kinds)
        sorted_items.append(gen_group(rng, i, kind, csc))

    # Run the real evaluator on every group so eligibility + reasons are
    # computed exactly the way the production pipeline does it.
    eligible_ids = set()
    reason_counts = {}
    for idx, (_gid, d) in enumerate(sorted_items):
        ev = csc.evaluate_group(d)
        d["_evaluation"] = ev
        if ev["eligible"]:
            eligible_ids.add(idx)
        for r in ev["reasons"]:
            reason_counts[r] = reason_counts.get(r, 0) + 1

    tmp = tempfile.mkdtemp(prefix="render_smoke_")
    csc.HTML_EXPORT = os.path.join(tmp, "report.html")
    csc.CSV_EXPORT = os.path.join(tmp, "report.csv")

    n_torrents = sum(
        1 + len(r[1]["crossseeds"]) + (1 if r[1]["original"].get("_external_path") else 0)
        for r in sorted_items
    )
    print(f"# Generating {n_groups} groups, ~{n_torrents} individual torrent rows…", file=sys.stderr)
    print(f"# Eligible (DELETE): {len(eligible_ids)}   KEEP: {n_groups - len(eligible_ids)}", file=sys.stderr)
    print(f"# Reason counts: {sorted(reason_counts.items())}", file=sys.stderr)

    csc.export_reports(sorted_items=sorted_items, eligible_ids=eligible_ids)

    out = sorted(glob.glob(os.path.join(tmp, "report*.html")), key=os.path.getmtime)[-1]
    print(out)


if __name__ == "__main__":
    main()
