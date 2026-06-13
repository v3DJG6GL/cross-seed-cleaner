"""Formatting helpers, sort_torrents, and calculate_stats
(cross_seed_cleaner.py:750-779, 886-894, 1061-1076)."""
import pytest


# ─── format_size_smart ───────────────────────────────────────────────────────

@pytest.mark.parametrize("size,expected", [
    (0, "0 B"),
    (1023, "1023.00 B"),
    (1024, "1.00 KiB"),
    (1024 ** 3, "1.00 GiB"),
    (1024 ** 5, "1.00 PiB"),
    (1024 ** 6, "1024.00 PiB"),   # caps at PiB
    (-5, "-5.00 B"),              # negative: no scaling loop
    (1024 ** 2 - 1, "1.00 MiB"),  # rounds up to a unit boundary -> roll over
    (1024 ** 3 - 1, "1.00 GiB"),
])
def test_format_size_smart(csc, size, expected):
    assert csc.format_size_smart(size) == expected


# ─── format_duration ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("seconds,fmt,expected", [
    (0, "d:hh", "0:00"),
    (-100, "d:hh", "0:00"),
    (0, "d:hh:mm", "0:00:00"),
    (0, "days", "0.0 days"),
    (30, "d:hh", "0:00"),
    (86400, "d:hh", "1:00"),
    (86400 + 3600 * 5, "d:hh", "1:05"),
    (86400 + 3600 * 5 + 60 * 7, "d:hh:mm", "1:05:07"),
    (86400 * 3 + 43200, "days", "3.5 days"),
])
def test_format_duration(csc, seconds, fmt, expected):
    assert csc.format_duration(seconds, fmt) == expected


def test_format_duration_unknown_fmt(csc):
    # Same error type regardless of the seconds sign (the zero/negative path
    # used to raise KeyError instead of ValueError).
    for seconds in (100, 0, -5):
        with pytest.raises(ValueError):
            csc.format_duration(seconds, "bogus")


# ─── format_timestamp ────────────────────────────────────────────────────────

def test_format_timestamp_zero_and_negative(csc):
    assert csc.format_timestamp(0) == "N/A"
    assert csc.format_timestamp(-1) == "N/A"


def test_format_timestamp_positive(csc):
    out = csc.format_timestamp(1700000000)
    assert out != "N/A" and "|" in out


# ─── sort_torrents ───────────────────────────────────────────────────────────

def _t(name, seeds):
    return {"name": name, "_seeder_count": seeds}


def test_sort_original_always_first(csc):
    orig = _t("ZZZ", 0)
    xs = [_t("a", 5), _t("b", 1)]
    result = csc.sort_torrents(orig, xs, "seeds", "asc")
    assert result[0] is orig
    assert [t["_seeder_count"] for t in result[1:]] == [1, 5]


def test_sort_desc_reverses_crossseeds(csc):
    orig = _t("orig", 99)
    xs = [_t("a", 1), _t("b", 9)]
    result = csc.sort_torrents(orig, xs, "seeds", "desc")
    assert result[0] is orig
    assert [t["_seeder_count"] for t in result[1:]] == [9, 1]


def test_sort_name_case_insensitive(csc):
    orig = _t("orig", 0)
    xs = [_t("banana", 0), _t("Apple", 0)]
    result = csc.sort_torrents(orig, xs, "name", "asc")
    assert [t["name"] for t in result[1:]] == ["Apple", "banana"]


def test_sort_missing_field_defaults_zero(csc):
    orig = _t("orig", 0)
    xs = [{"name": "x"}, _t("y", 5)]   # first has no _seeder_count -> 0
    result = csc.sort_torrents(orig, xs, "seeds", "asc")
    assert result[1]["name"] == "x"


# ─── calculate_stats ─────────────────────────────────────────────────────────

def test_calculate_stats(csc):
    GIB = 1024 ** 3
    all_groups = {
        "h1": {"original": {"size": 4 * GIB}, "crossseeds": [{"size": 4 * GIB}, {"size": 4 * GIB}]},
        "h2": {"original": {"size": 1 * GIB}, "crossseeds": []},
    }
    # eligible_map values are all_torrents lists; size_del counts ts[0] (original) only.
    eligible_map = {0: [{"size": 4 * GIB}, {"size": 4 * GIB}, {"size": 4 * GIB}]}
    s = csc.calculate_stats(all_groups, eligible_map)
    assert s["groups_total"] == 2
    assert s["size_total"] == 5 * GIB
    assert s["torrents_orig"] == 2
    assert s["torrents_xs"] == 2
    assert s["torrents_total"] == 4
    assert s["groups_del"] == 1
    assert s["torrents_del"] == 3
    assert s["size_del"] == 4 * GIB          # one copy per eligible group
    assert s["groups_keep"] == 1
    assert s["torrents_keep"] == 1
    assert s["size_keep"] == 1 * GIB


def test_calculate_stats_empty_eligible(csc):
    all_groups = {"h1": {"original": {"size": 100}, "crossseeds": []}}
    s = csc.calculate_stats(all_groups, {})
    assert s["groups_del"] == 0
    assert s["size_del"] == 0
    assert s["groups_keep"] == 1
