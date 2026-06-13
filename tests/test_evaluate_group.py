"""Exhaustive coverage of evaluate_group — the safety-critical eligibility
decision that determines which torrents get deleted. Pins every reason code,
the exact boundary operators/units, multi-reason ordering, and the normal vs
missing-hard-links mode split (cross_seed_cleaner.py:evaluate_group)."""
import pytest

from conftest import reconfigure

MIN_SEEDERS = 5
MAX_GROUP = 3
MIN_GIB = 2
MIN_DAYS = 10
MIN_BYTES = MIN_GIB * 1024 ** 3
MIN_SECS = MIN_DAYS * 86400


@pytest.fixture
def ev(csc):
    """Module configured with known thresholds, standard mode, no category filter."""
    return reconfigure(
        csc,
        MIN_SEEDERS=MIN_SEEDERS,
        MAX_TORRENTS_IN_GROUP=MAX_GROUP,
        MIN_SIZE_GIB=MIN_GIB,
        MIN_ORIGINAL_SEED_TIME_DAYS=MIN_DAYS,
        CATEGORY_FILTER_MODE="none",
        MISSING_HARD_LINKS_MODE=False,
    )


def t(seeds=MIN_SEEDERS, size=MIN_BYTES, seeded=MIN_SECS, category="movies", **extra):
    d = {"_seeder_count": seeds, "size": size, "seeding_time": seeded, "category": category}
    d.update(extra)
    return d


def grp(original, crossseeds=()):
    return {"original": original, "crossseeds": list(crossseeds)}


# ─── happy path & invariant ──────────────────────────────────────────────────

def test_all_pass_is_eligible(ev):
    r = ev.evaluate_group(grp(t(), [t()]))
    assert r["eligible"] is True
    assert r["reasons"] == []
    assert r["all_torrents"] == [grp(t(), [t()])["original"]] + [t()]  # orig + crossseeds


def test_eligible_iff_no_reasons(ev):
    # Any single failure flips eligible to False.
    r = ev.evaluate_group(grp(t(seeds=MIN_SEEDERS - 1)))
    assert r["eligible"] == (r["reasons"] == []) == False  # noqa: E712


# ─── per-reason boundaries (each isolates one code) ──────────────────────────

@pytest.mark.parametrize("seeds,expected", [
    (MIN_SEEDERS, []),            # == threshold passes (>=)
    (MIN_SEEDERS - 1, ["LOW_SEEDS"]),
    (0, ["LOW_SEEDS"]),
])
def test_seeds_boundary(ev, seeds, expected):
    assert ev.evaluate_group(grp(t(seeds=seeds)))["reasons"] == expected


@pytest.mark.parametrize("size,expected", [
    (MIN_BYTES, []),
    (MIN_BYTES - 1, ["SMALL_SIZE"]),
    (0, ["SMALL_SIZE"]),
])
def test_size_boundary(ev, size, expected):
    assert ev.evaluate_group(grp(t(size=size)))["reasons"] == expected


@pytest.mark.parametrize("seeded,expected", [
    (MIN_SECS, []),
    (MIN_SECS - 1, ["LOW_TIME"]),
    (0, ["LOW_TIME"]),
])
def test_time_boundary(ev, seeded, expected):
    assert ev.evaluate_group(grp(t(seeded=seeded)))["reasons"] == expected


@pytest.mark.parametrize("n_cross,expected", [
    (MAX_GROUP - 2, []),               # group size == MAX-1 -> ok
    (MAX_GROUP - 1, ["TOO_MANY"]),     # group size == MAX -> TOO_MANY (strict <)
    (MAX_GROUP, ["TOO_MANY"]),
])
def test_group_size_boundary(ev, n_cross, expected):
    assert ev.evaluate_group(grp(t(), [t() for _ in range(n_cross)]))["reasons"] == expected


def test_external_hardlink_on_original(ev):
    assert ev.evaluate_group(grp(t(_external_hardlink=True)))["reasons"] == ["EXTERNAL_LINK"]


def test_external_hardlink_on_crossseed_flags_group(ev):
    r = ev.evaluate_group(grp(t(), [t(_external_hardlink=True)]))
    assert r["reasons"] == ["EXTERNAL_LINK"]
    assert r["externally_linked"] is True


# ─── the all-torrents seeds rule ─────────────────────────────────────────────

def test_one_low_crossseed_blocks_whole_group(ev):
    # Original healthy, a single cross-seed below MIN -> LOW_SEEDS (all() rule).
    r = ev.evaluate_group(grp(t(seeds=100), [t(seeds=MIN_SEEDERS - 1)]))
    assert r["reasons"] == ["LOW_SEEDS"]


# ─── category filtering over the set of all members ──────────────────────────

def test_blocked_category_on_any_member(csc):
    reconfigure(csc, MIN_SEEDERS=MIN_SEEDERS, MAX_TORRENTS_IN_GROUP=MAX_GROUP,
                MIN_SIZE_GIB=MIN_GIB, MIN_ORIGINAL_SEED_TIME_DAYS=MIN_DAYS,
                MISSING_HARD_LINKS_MODE=False,
                CATEGORY_FILTER_MODE="block", CATEGORY_BLOCKLIST=["games"])
    r = csc.evaluate_group(grp(t(category="movies"), [t(category="games")]))
    assert r["reasons"] == ["CATEGORY_FILTER"]


# ─── missing fields default to 0 ─────────────────────────────────────────────

def test_empty_original_defaults_to_zero(ev):
    r = ev.evaluate_group(grp({}))
    assert set(r["reasons"]) == {"LOW_SEEDS", "SMALL_SIZE", "LOW_TIME"}
    assert "TOO_MANY" not in r["reasons"]
    assert "EXTERNAL_LINK" not in r["reasons"]


# ─── "no limit" thresholds ───────────────────────────────────────────────────

def test_min_size_zero_is_no_limit(csc):
    reconfigure(csc, MIN_SEEDERS=0, MAX_TORRENTS_IN_GROUP=99, MIN_SIZE_GIB=0,
                MIN_ORIGINAL_SEED_TIME_DAYS=0, CATEGORY_FILTER_MODE="none",
                MISSING_HARD_LINKS_MODE=False)
    assert csc.evaluate_group(grp(t(size=0, seeded=0, seeds=0)))["reasons"] == []


# ─── multi-reason ordering (normal mode) ─────────────────────────────────────

def test_multi_reason_order_normal(csc):
    reconfigure(csc, MIN_SEEDERS=MIN_SEEDERS, MAX_TORRENTS_IN_GROUP=2,
                MIN_SIZE_GIB=MIN_GIB, MIN_ORIGINAL_SEED_TIME_DAYS=MIN_DAYS,
                MISSING_HARD_LINKS_MODE=False,
                CATEGORY_FILTER_MODE="block", CATEGORY_BLOCKLIST=["games"])
    bad = t(seeds=0, size=0, seeded=0, category="games", _external_hardlink=True)
    # group of 3 (orig + 2 cross) >= MAX(2) -> TOO_MANY too
    r = csc.evaluate_group(grp(bad, [t(category="games"), t(category="games")]))
    assert r["reasons"] == [
        "EXTERNAL_LINK", "LOW_SEEDS", "SMALL_SIZE", "LOW_TIME", "TOO_MANY", "CATEGORY_FILTER",
    ]
    assert r["eligible"] is False


# ─── missing-hard-links mode ─────────────────────────────────────────────────

@pytest.fixture
def nhl(csc):
    return reconfigure(
        csc,
        MIN_SEEDERS=MIN_SEEDERS,
        MAX_TORRENTS_IN_GROUP=MAX_GROUP,
        MIN_SIZE_GIB=MIN_GIB,
        MIN_ORIGINAL_SEED_TIME_DAYS=MIN_DAYS,
        CATEGORY_FILTER_MODE="none",
        MISSING_HARD_LINKS_MODE=True,
    )


def test_nhl_ignores_crossseeds(nhl):
    # Only the original is evaluated; a dead cross-seed does not block.
    r = nhl.evaluate_group(grp(t(), [t(seeds=0), t(seeds=0), t(seeds=0)]))
    assert r["eligible"] is True
    assert r["all_torrents"] == [grp(t())["original"]]


def test_nhl_no_too_many(nhl):
    # Group-size cap is disabled in NHL mode even with many cross-seeds.
    r = nhl.evaluate_group(grp(t(), [t() for _ in range(10)]))
    assert "TOO_MANY" not in r["reasons"]


def test_nhl_path_error(nhl):
    r = nhl.evaluate_group(grp(t(_path_error=True)))
    assert r["reasons"] == ["PATH_ERROR"]


def test_nhl_path_error_only_in_nhl(ev):
    # _path_error is ignored in standard mode.
    assert "PATH_ERROR" not in ev.evaluate_group(grp(t(_path_error=True)))["reasons"]


def test_nhl_external_link(nhl):
    assert nhl.evaluate_group(grp(t(_external_hardlink=True)))["reasons"] == ["EXTERNAL_LINK"]


def test_reason_icon_and_text_maps_stay_in_sync(csc):
    # The HTML report degrades gracefully for an unmapped code, but a reason code
    # added to an evaluator without an icon would then ship a silent neutral
    # marker. Pin the icon map to the canonical code set and require each to have
    # a custom _reason_text, so adding a code forces updating all three.
    expected = {
        "EXTERNAL_LINK", "PATH_ERROR", "LOW_SEEDS", "SMALL_SIZE", "LOW_TIME",
        "TOO_MANY", "CATEGORY_FILTER", "TRACKER_ALIVE", "RECENTLY_ADDED",
        "TRACKER_UPDATING", "NO_REAL_TRACKERS", "NO_ADDED_TIME", "RECENT_ACTIVITY",
    }
    assert set(csc._REASON_HTML_ICON) == expected
    for code in expected:
        assert csc._reason_text(code) != code, f"{code} missing a custom _reason_text"


def test_nhl_multi_reason_order(csc):
    reconfigure(csc, MIN_SEEDERS=MIN_SEEDERS, MAX_TORRENTS_IN_GROUP=MAX_GROUP,
                MIN_SIZE_GIB=MIN_GIB, MIN_ORIGINAL_SEED_TIME_DAYS=MIN_DAYS,
                MISSING_HARD_LINKS_MODE=True,
                CATEGORY_FILTER_MODE="block", CATEGORY_BLOCKLIST=["games"])
    bad = t(seeds=0, size=0, seeded=0, category="games",
            _external_hardlink=True, _path_error=True)
    r = csc.evaluate_group(grp(bad))
    assert r["reasons"] == [
        "EXTERNAL_LINK", "PATH_ERROR", "LOW_SEEDS", "SMALL_SIZE", "LOW_TIME", "CATEGORY_FILTER",
    ]
