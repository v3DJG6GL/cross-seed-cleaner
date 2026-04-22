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
import math
import glob
import random
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
# Capture user args BEFORE we wipe sys.argv to keep cross_seed_cleaner's
# argparse import-time behaviour from blowing up.
_USER_ARGS = sys.argv[1:]
sys.argv = ["render_smoke.py"]

import cross_seed_cleaner as csc  # noqa: E402

# ---- Tunables that shape the dataset & the eligibility decision -----------
# These mirror what the user might run cross_seed_cleaner with. Set explicitly
# (rather than inheriting argparse defaults) so the demo report deterministically
# exercises every rejection reason regardless of the user's environment.
csc.MIN_SEEDERS = 10
csc.MAX_TORRENTS_IN_GROUP = 5
csc.MIN_ORIGINAL_SEED_TIME_DAYS = 30
csc.MIN_ORIGINAL_SEED_TIME_HOURS = csc.MIN_ORIGINAL_SEED_TIME_DAYS * 24
csc.MIN_SIZE_GIB = 2
csc.MIN_SIZE_BYTES = csc.MIN_SIZE_GIB * 1024 ** 3
# Block one common category so CATEGORY_FILTER fires on matching torrents.
csc.CATEGORY_BLOCKLIST = ["games"]
csc.CATEGORY_ALLOWLIST = []
csc.CATEGORY_FILTER_MODE = "block"
# `category_allowed` reads the *compiled* spec lists (built once at module
# load), so re-compile after the override.
csc._CATEGORY_BLOCKLIST_SPECS = csc._compile_specs(csc.CATEGORY_BLOCKLIST, "CATEGORY_BLOCKLIST")
csc._CATEGORY_ALLOWLIST_SPECS = csc._compile_specs(csc.CATEGORY_ALLOWLIST, "CATEGORY_ALLOWLIST")
csc.NO_HARD_LINKS_MODE = False
csc.UNRELIABLE_TRACKERS = ["torrentday.com"]
# ---------------------------------------------------------------------------

CATEGORIES = [
    "movies", "tv", "music", "books", "games",
    "autobrr-cinemaz", "autobrr-blutopia", "autobrr-fearnopeer",
    "cross-seed-link", "radarr-imported", "sonarr-imported", "manual",
]

TRACKERS = [
    "aither.cc", "blutopia.cc", "cinemaz.to", "fearnopeer.com", "lst.gg",
    "reelflix.cc", "torrentleech.org", "tleechreload.org", "seedpool.org",
    "iptorrents.com", "myanonamouse.net", "passthepopcorn.me", "broadcasthe.net",
    "redacted.ch", "orpheus.network", "milkie.cc", "anthelion.me",
    "hdts-announce.ru", "fileapi.uk", "torrentday.com",
]

WORDS_NOUN = [
    "Falcon", "Shadow", "River", "Mountain", "Galaxy", "Phoenix", "Storm",
    "Eclipse", "Dragon", "Forest", "Atlas", "Comet", "Meridian", "Winter",
    "Summer", "Autumn", "Spring", "Pioneer", "Voyager", "Horizon",
]
WORDS_ADJ = [
    "Hidden", "Silent", "Distant", "Crimson", "Golden", "Frozen",
    "Burning", "Lost", "Sacred", "Wild", "Ancient", "Forgotten",
]

QUALITIES = ["1080p", "2160p", "720p", "WEB-DL", "Bluray", "REMUX"]
GROUPS = ["FraMeSToR", "RAWR", "TAoE", "ZZG", "DON", "TBS", "PSA"]

GIB = 1024 ** 3


def gen_torrent(rng, base_name, tracker=None, category=None,
                size=None, seeds=None, seeded_secs=None, ratio=None):
    if size is None:
        size = int(math.exp(rng.gauss(22.5, 1.4)))
        size = max(50 * 1024 * 1024, min(80 * GIB, size))
    if seeds is None:
        seeds = max(0, int(rng.lognormvariate(2.0, 1.4)))
        seeds = min(seeds, 600)
    if ratio is None:
        ratio = round(max(0.0, rng.lognormvariate(-0.5, 1.1)), 2)
        if rng.random() < 0.05:
            ratio = 0.0
    if seeded_secs is None:
        seeded_secs = rng.randint(86400, 86400 * 4 * 365)
    uploaded = int(size * ratio)
    added_on = 1775073600 - rng.randint(0, 86400 * 4 * 365)
    category = category or rng.choice(CATEGORIES)
    tracker = tracker or rng.choice(TRACKERS)

    return {
        "name": base_name,
        "content_path": f"/data/downloads/{category}/{base_name}",
        "category": category,
        "tracker": f"http://{tracker}/announce",
        "_tracker_domain": tracker,
        "size": size,
        "ratio": ratio,
        "uploaded": uploaded,
        "seeding_time": seeded_secs,
        "added_on": added_on,
        "num_complete": seeds,
        "_seeder_count": seeds,
        "hash": f"{rng.randrange(16 ** 16):016x}",
    }


def gen_group(rng, idx, kind):
    """`kind` chooses which rejection reason(s) the generated group should hit.

    Possible values: 'normal' (DELETE-eligible), 'low_seeds', 'small_size',
    'low_time', 'too_many', 'external_link', 'category_filter',
    'multi_reason', 'orphan'.
    """
    name = (
        f"{rng.choice(WORDS_ADJ)}.{rng.choice(WORDS_NOUN)}.{rng.randint(1980, 2025)}."
        f"{rng.choice(QUALITIES)}-{rng.choice(GROUPS)}"
    )
    primary_cat = rng.choice([c for c in CATEGORIES if c != "games"])
    primary_tracker = rng.choice(TRACKERS)

    # Defaults that pass every check (= eligible for deletion)
    overrides = {
        "size": rng.randint(int(2.5 * GIB), 80 * GIB),               # >= 2 GiB
        "seeded_secs": rng.randint(45 * 86400, 1500 * 86400),        # >= 30 days
        "seeds": rng.randint(20, 400),                                # >= MIN_SEEDERS
    }
    if kind == "low_seeds":
        overrides["seeds"] = rng.randint(0, 9)                        # < MIN_SEEDERS
    elif kind == "small_size":
        overrides["size"] = rng.randint(50 * 1024 * 1024, 1500 * 1024 * 1024)
    elif kind == "low_time":
        overrides["seeded_secs"] = rng.randint(86400, 25 * 86400)     # < 30 days
    elif kind == "category_filter":
        primary_cat = "games"                                          # in BLOCKLIST
    elif kind == "multi_reason":
        overrides["seeds"] = rng.randint(0, 9)
        overrides["size"] = rng.randint(100 * 1024 * 1024, 1500 * 1024 * 1024)
        overrides["seeded_secs"] = rng.randint(86400, 25 * 86400)

    original = gen_torrent(rng, name, tracker=primary_tracker, category=primary_cat, **overrides)

    # Cross-seed count: TOO_MANY needs >= MAX_TORRENTS_IN_GROUP. Orphan = 0.
    if kind == "orphan":
        n_cross = 0
    elif kind == "too_many":
        n_cross = csc.MAX_TORRENTS_IN_GROUP                           # original + N >= MAX
    else:
        n_cross = rng.choices([0, 1, 2, 3], weights=[15, 40, 30, 15])[0]

    crossseeds = []
    used_groups = {name.rsplit("-", 1)[-1]}
    for _ in range(n_cross):
        cs_cat = "games" if (kind == "category_filter" and rng.random() < 0.4) else (
            "cross-seed-link" if rng.random() < 0.6 else rng.choice(CATEGORIES))
        # Real cross-seed names differ from the original — usually the release
        # group differs, sometimes the quality tag too. Same content, different
        # encoding/scene release.
        prefix = name.rsplit("-", 1)[0]
        alt_group = next(g for g in (rng.choice(GROUPS) for _ in range(20)) if g not in used_groups)
        used_groups.add(alt_group)
        if rng.random() < 0.3:
            # 30% chance the quality tag differs too (e.g. WEB-DL vs Bluray)
            alt_q = rng.choice([q for q in QUALITIES if q != prefix.rsplit(".", 1)[-1]])
            prefix = prefix.rsplit(".", 1)[0] + "." + alt_q
        cs_name = f"{prefix}-{alt_group}"
        # Cross-seed seed count is correlated with the original's — same content,
        # different tracker, but same general popularity tier. Without this,
        # evaluate_group's all-torrents-in-group rule for LOW_SEEDS fires on
        # almost every group.
        orig_seeds = original["_seeder_count"]
        if orig_seeds < csc.MIN_SEEDERS:
            cs_seeds = rng.randint(0, max(0, orig_seeds + 5))
        else:
            cs_seeds = max(csc.MIN_SEEDERS, orig_seeds + rng.randint(-orig_seeds // 3, orig_seeds // 2 + 1))
        cs = gen_torrent(rng, cs_name, tracker=rng.choice(TRACKERS), category=cs_cat,
                         seeds=cs_seeds)
        cs["size"] = original["size"]                                 # cross-seed shares size
        crossseeds.append(cs)

    if kind == "external_link":
        # Eligibility check: any torrent with _external_hardlink → EXTERNAL_LINK reason.
        original["_external_hardlink"] = True
        # Also stamp _external_path so the EXT row renders.
        original["_external_path"] = f"/mnt/library/{primary_cat}/{name}"
    elif kind != "orphan" and rng.random() < 0.04:
        # Sprinkle EXT rows on a few "normal" (non-orphan) groups. STANDARD-
        # mode production drops singletons entirely (cross_seed_cleaner.py:645),
        # so a real ORPHAN group never reaches the HTML with an EXT row;
        # don't generate that combination here either. _external_path and
        # _external_hardlink are always set together in production
        # (cross_seed_cleaner.py:770-774) — an EXT row implies EXTERNAL_LINK KEEP.
        original["_external_hardlink"] = True
        original["_external_path"] = f"/mnt/library/{primary_cat}/{name}"

    return (f"g{idx}", {"original": original, "crossseeds": crossseeds})


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
        sorted_items.append(gen_group(rng, i, kind))

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
