"""Formatting helpers, sort_torrents, and calculate_stats (cross_seed_cleaner.py:
format_size_smart / format_duration / format_timestamp / sort_torrents /
calculate_stats)."""
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


def test_format_timestamp_out_of_range(csc):
    # A corrupt/out-of-range epoch must degrade to N/A, not abort the report.
    assert csc.format_timestamp(99999999999999) == "N/A"


def test_format_timestamp_none(csc):
    # A missing added_on (None) must degrade to N/A, not raise TypeError on the
    # `<= 0` comparison and abort the whole report/print run.
    assert csc.format_timestamp(None) == "N/A"


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


def test_sort_null_field_treated_as_zero(csc):
    # A present-but-null sort field (added_on can be an explicit null) must not
    # crash the configured sort that orders CLI/HTML/CSV output, mirroring the
    # group-sort guard. The null sorts as 0 (oldest) like a missing key.
    orig = _t("orig", 0)
    xs = [{"name": "n", "added_on": None}, {"name": "v", "added_on": 5}]
    result = csc.sort_torrents(orig, xs, "added", "asc")
    assert [t["name"] for t in result[1:]] == ["n", "v"]


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


# ─── print_config: mode-irrelevant rows are dimmed ───────────────────────────

TE_ONLY = {"Dead Statuses", "Min Age", "Min Inactivity", "Ignore Cat Filter"}
CAT_ROWS = {"Category Mode", "Cat Allowlist", "Cat Blocklist"}


def _set_mode(monkeypatch, csc, tracker_error=False, missing_hl=False, ignore_cat=False):
    monkeypatch.setattr(csc, "TRACKER_ERROR_MODE", tracker_error)
    monkeypatch.setattr(csc, "MISSING_HARD_LINKS_MODE", missing_hl)
    monkeypatch.setattr(csc, "TRACKER_ERROR_MODE_IGNORE_CATEGORY_FILTER", ignore_cat)


def test_inactive_labels_standard_mode(csc, monkeypatch):
    _set_mode(monkeypatch, csc)
    assert csc._inactive_config_labels() == TE_ONLY | {"Missing Hard Links Cat"}


def test_inactive_labels_missing_hardlinks_mode(csc, monkeypatch):
    _set_mode(monkeypatch, csc, missing_hl=True)
    inactive = csc._inactive_config_labels()
    assert inactive == TE_ONLY | {"Max Group Size"}
    # Seeder/size/time limits and category filter are still live here.
    assert not inactive & {"Min Seeders", "Min Size", "Min Seed Time", "External Media Paths", "Path Mappings"} | (inactive & CAT_ROWS)


def test_inactive_labels_tracker_error_mode(csc, monkeypatch):
    _set_mode(monkeypatch, csc, tracker_error=True)
    inactive = csc._inactive_config_labels()
    assert inactive == {"Min Seeders", "Min Seed Time", "Min Size", "Max Group Size",
                        "Missing Hard Links Cat", "External Media Paths", "Path Mappings"}
    # Category filter is honored unless the ignore modifier is set; exclusions always apply.
    assert not inactive & (CAT_ROWS | {"Excluded Trackers", "Unreliable Trackers"})


def test_inactive_labels_tracker_error_ignore_category(csc, monkeypatch):
    _set_mode(monkeypatch, csc, tracker_error=True, ignore_cat=True)
    inactive = csc._inactive_config_labels()
    assert CAT_ROWS <= inactive
    assert "Excluded Trackers" not in inactive


def _config_row(csc, out, label):
    """Return the raw (ANSI-bearing) table line for `label`, or None."""
    for line in out.splitlines():
        if csc.strip_colors(line).startswith(f"│ {label} "):
            return line
    return None


def test_print_config_dims_rows_and_keeps_alignment(csc, monkeypatch, capsys):
    _set_mode(monkeypatch, csc, tracker_error=True)
    monkeypatch.setattr(csc, "EXTERNAL_MEDIA_PATHS", ["/media/a", "/media/b"])
    csc.print_config()
    out = capsys.readouterr().out

    # Dimmed: label wrapped in DIM, not bold.
    seeders = _config_row(csc, out, "Min Seeders")
    assert seeders is not None and csc.Colors.DIM in seeders and csc.Colors.BOLD not in seeders
    # Continuation row of a dimmed multi-line group is dimmed too.
    cont = next(l for l in out.splitlines() if "/media/b" in l)
    assert csc.Colors.DIM in cont
    # Live row keeps its bold label and its value's own color (not dimmed).
    dry = _config_row(csc, out, "Dry Run")
    assert dry is not None and csc.Colors.BOLD in dry and csc.Colors.DIM not in dry
    # Dimmed row's value has its GREEN/RED stripped (would cancel DIM mid-cell).
    ignore = _config_row(csc, out, "Missing Hard Links Cat")
    assert ignore is not None and csc.Colors.GREEN not in ignore and csc.Colors.RED not in ignore
    # Legend printed when something is dimmed.
    assert "dimmed = not applied in the current mode" in out
    # Every table line has identical visible width (dim codes must not skew padding).
    widths = {len(csc.strip_colors(l)) for l in out.splitlines() if l.startswith(("│", "┌", "├", "└"))}
    assert len(widths) == 1
