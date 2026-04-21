#!/usr/bin/env python3
"""Generate a realistic ~10k-torrent HTML report for hands-on UX testing.

Usage:
    python3 tests/render_smoke.py            # ~2000 groups (default)
    python3 tests/render_smoke.py 5000       # 5000 groups

Prints the absolute path of the generated HTML on the last line.
Deterministic via random.seed(42), so successive runs produce the same file
content (different filename only because of the timestamp).

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


def gen_torrent(rng: random.Random, base_name: str, tracker: str | None = None,
                category: str | None = None) -> dict:
    # log-normal sizes from ~50 MiB to ~80 GiB
    size = int(math.exp(rng.gauss(22.5, 1.4)))
    size = max(50 * 1024 * 1024, min(80 * 1024 ** 3, size))

    # seeders: most torrents 0–30, long tail to ~500
    seeds = max(0, int(rng.lognormvariate(2.0, 1.4)))
    seeds = min(seeds, 600)

    ratio = round(max(0.0, rng.lognormvariate(-0.5, 1.1)), 2)
    if rng.random() < 0.05:                 # 5% never uploaded
        ratio = 0.0

    uploaded = int(size * ratio)

    # seed time: 1 day to 4 years
    seeded_secs = rng.randint(86400, 86400 * 4 * 365)

    # added: spread across last 4 years from a fixed reference (UTC 2026-04-01)
    added_on = 1775073600 - rng.randint(0, 86400 * 4 * 365)

    return {
        "name": base_name,
        "content_path": f"/data/downloads/{category or 'misc'}/{base_name}",
        "category": category or rng.choice(CATEGORIES),
        "tracker": f"http://{tracker or rng.choice(TRACKERS)}/announce",
        "_tracker_domain": tracker or rng.choice(TRACKERS),
        "size": size,
        "ratio": ratio,
        "uploaded": uploaded,
        "seeding_time": seeded_secs,
        "added_on": added_on,
        "num_complete": seeds,
        "_seeder_count": seeds,
        "hash": f"{rng.randrange(16 ** 16):016x}",
    }


def gen_group(rng: random.Random, idx: int, n_groups: int) -> tuple[str, dict]:
    name = (
        f"{rng.choice(WORDS_ADJ)}.{rng.choice(WORDS_NOUN)}.{rng.randint(1980, 2025)}."
        f"{rng.choice(QUALITIES)}-{rng.choice(GROUPS)}"
    )
    primary_cat = rng.choice(CATEGORIES)
    primary_tracker = rng.choice(TRACKERS)
    original = gen_torrent(rng, name, tracker=primary_tracker, category=primary_cat)

    # cross-seeds: 0..4, each with a different tracker / category
    n_cross = rng.choices([0, 1, 2, 3, 4], weights=[20, 35, 25, 15, 5])[0]
    crossseeds = []
    for _ in range(n_cross):
        cs = gen_torrent(
            rng, name + ".cross",
            tracker=rng.choice(TRACKERS),
            category="cross-seed-link" if rng.random() < 0.6 else rng.choice(CATEGORIES),
        )
        # share size with original — that's the realistic invariant
        cs["size"] = original["size"]
        crossseeds.append(cs)

    # ~5% have an external library hardlink
    if rng.random() < 0.05:
        original["_external_path"] = f"/mnt/library/{primary_cat}/{name}"

    return (f"g{idx}", {"original": original, "crossseeds": crossseeds})


def main():
    n_groups = int(_USER_ARGS[0]) if _USER_ARGS else 2000
    rng = random.Random(42)

    sorted_items = [gen_group(rng, i, n_groups) for i in range(n_groups)]

    # mark ~40% of groups as eligible-for-delete
    eligible_ids = {i for i in range(n_groups) if rng.random() < 0.40}

    tmp = tempfile.mkdtemp(prefix="render_smoke_")
    csc.HTML_EXPORT = os.path.join(tmp, "report.html")
    csc.CSV_EXPORT = os.path.join(tmp, "report.csv")
    csc.ELIGIBLE_ONLY = False

    n_torrents = sum(
        1 + len(r[1]["crossseeds"]) + (1 if r[1]["original"].get("_external_path") else 0)
        for r in sorted_items
    )
    print(f"# Generating {n_groups} groups, ~{n_torrents} individual torrent rows…", file=sys.stderr)

    csc.export_reports(sorted_items=sorted_items, eligible_ids=eligible_ids)

    out = sorted(glob.glob(os.path.join(tmp, "report*.html")), key=os.path.getmtime)[-1]
    print(out)


if __name__ == "__main__":
    main()
