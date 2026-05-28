"""Shared torrent/group fixture generators.

Extracted from render_smoke.py so both the visual smoke demo and the pytest
suite build torrent dicts the same way. gen_torrent is independent of the
cross_seed_cleaner module; gen_group needs MIN_SEEDERS / MAX_TORRENTS_IN_GROUP,
so the loaded module is passed in as `csc`.
"""
import math

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


def gen_group(rng, idx, kind, csc):
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

    overrides = {
        "size": rng.randint(int(2.5 * GIB), 80 * GIB),
        "seeded_secs": rng.randint(45 * 86400, 1500 * 86400),
        "seeds": rng.randint(20, 400),
    }
    if kind == "low_seeds":
        overrides["seeds"] = rng.randint(0, 9)
    elif kind == "small_size":
        overrides["size"] = rng.randint(50 * 1024 * 1024, 1500 * 1024 * 1024)
    elif kind == "low_time":
        overrides["seeded_secs"] = rng.randint(86400, 25 * 86400)
    elif kind == "category_filter":
        primary_cat = "games"
    elif kind == "multi_reason":
        overrides["seeds"] = rng.randint(0, 9)
        overrides["size"] = rng.randint(100 * 1024 * 1024, 1500 * 1024 * 1024)
        overrides["seeded_secs"] = rng.randint(86400, 25 * 86400)

    original = gen_torrent(rng, name, tracker=primary_tracker, category=primary_cat, **overrides)

    if kind == "orphan":
        n_cross = 0
    elif kind == "too_many":
        n_cross = csc.MAX_TORRENTS_IN_GROUP
    else:
        n_cross = rng.choices([0, 1, 2, 3], weights=[15, 40, 30, 15])[0]

    crossseeds = []
    used_groups = {name.rsplit("-", 1)[-1]}
    for _ in range(n_cross):
        cs_cat = "games" if (kind == "category_filter" and rng.random() < 0.4) else (
            "cross-seed-link" if rng.random() < 0.6 else rng.choice(CATEGORIES))
        prefix = name.rsplit("-", 1)[0]
        alt_group = next(g for g in (rng.choice(GROUPS) for _ in range(20)) if g not in used_groups)
        used_groups.add(alt_group)
        if rng.random() < 0.3:
            alt_q = rng.choice([q for q in QUALITIES if q != prefix.rsplit(".", 1)[-1]])
            prefix = prefix.rsplit(".", 1)[0] + "." + alt_q
        cs_name = f"{prefix}-{alt_group}"
        orig_seeds = original["_seeder_count"]
        if orig_seeds < csc.MIN_SEEDERS:
            cs_seeds = rng.randint(0, max(0, orig_seeds + 5))
        else:
            cs_seeds = max(csc.MIN_SEEDERS, orig_seeds + rng.randint(-orig_seeds // 3, orig_seeds // 2 + 1))
        cs = gen_torrent(rng, cs_name, tracker=rng.choice(TRACKERS), category=cs_cat,
                         seeds=cs_seeds)
        cs["size"] = original["size"]
        crossseeds.append(cs)

    if kind == "external_link":
        original["_external_hardlink"] = True
        original["_external_path"] = f"/mnt/library/{primary_cat}/{name}"
    elif kind != "orphan" and rng.random() < 0.04:
        original["_external_hardlink"] = True
        original["_external_path"] = f"/mnt/library/{primary_cat}/{name}"

    return (f"g{idx}", {"original": original, "crossseeds": crossseeds})
