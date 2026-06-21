"""Pattern compilation, category filtering, tracker-domain normalization, and
seeder counting (cross_seed_cleaner.py: _compile_specs / matches_pattern
/ category_allowed / _domain_from_tracker_url / get_seeder_count)."""
import pytest

from conftest import reconfigure, FakeClient


# ─── _compile_specs / matches_pattern ────────────────────────────────────────

def test_compile_invalid_regex_exits(csc):
    with pytest.raises(SystemExit):
        csc._compile_specs(["r:["], "X")


def test_matches_pattern_fullmatch_vs_literal(csc):
    rx = csc._compile_specs(["r:tv"], "X")[0]
    assert csc.matches_pattern("tv", rx) is True
    assert csc.matches_pattern("tvshows", rx) is False        # fullmatch, not prefix
    rx2 = csc._compile_specs(["r:tv.*"], "X")[0]
    assert csc.matches_pattern("tvshows", rx2) is True
    lit = csc._compile_specs(["movies"], "X")[0]
    assert csc.matches_pattern("movies", lit) is True
    assert csc.matches_pattern("movies-4k", lit) is False


def test_compile_lower_flag(csc):
    lit = csc._compile_specs(["MyTracker.Org"], "X", lower=True)[0]
    assert lit == (None, "mytracker.org")
    rx = csc._compile_specs(["r:ABC"], "X", lower=True)[0]
    assert csc.matches_pattern("abc", rx) is True             # IGNORECASE


# ─── category_allowed ────────────────────────────────────────────────────────

def _cat(csc, mode, allow=(), block=()):
    return reconfigure(csc, CATEGORY_FILTER_MODE=mode,
                       CATEGORY_ALLOWLIST=list(allow), CATEGORY_BLOCKLIST=list(block))


@pytest.mark.parametrize("cat", ["movies", "games", "", "anything"])
def test_mode_none_allows_everything(csc, cat):
    _cat(csc, "none", allow=["movies"], block=["games"])
    assert csc.category_allowed(cat) is True


def test_mode_allow(csc):
    _cat(csc, "allow", allow=["movies"])
    assert csc.category_allowed("movies") is True
    assert csc.category_allowed("tv") is False
    assert csc.category_allowed("") is False


def test_mode_allow_empty_list_blocks_all(csc):
    _cat(csc, "allow", allow=[])
    assert csc.category_allowed("movies") is False


def test_mode_block(csc):
    _cat(csc, "block", block=["games"])
    assert csc.category_allowed("games") is False
    assert csc.category_allowed("movies") is True


def test_mode_block_empty_list_allows_all(csc):
    _cat(csc, "block", block=[])
    assert csc.category_allowed("games") is True


def test_mode_both_overlap_block_wins(csc):
    _cat(csc, "both", allow=["movies", "games"], block=["games"])
    assert csc.category_allowed("movies") is True
    assert csc.category_allowed("games") is False     # in allow AND block -> blocked
    assert csc.category_allowed("tv") is False         # not in allow


def test_category_fullmatch_anchoring(csc):
    _cat(csc, "allow", allow=["r:movie"])
    assert csc.category_allowed("movies") is False     # fullmatch
    _cat(csc, "allow", allow=["r:movie.*"])
    assert csc.category_allowed("movies") is True


def test_category_case_sensitive(csc):
    _cat(csc, "allow", allow=["Movies"])
    assert csc.category_allowed("movies") is False


# ─── _domain_from_tracker_url ────────────────────────────────────────────────

@pytest.mark.parametrize("url,expected", [
    ("http://tracker.opensharing.org:8080/announce", "opensharing.org"),
    ("https://www.example.com/announce", "example.com"),
    ("http://my-tracker.org/announce", "my-tracker.org"),    # substring NOT stripped
    ("http://tracker.org/announce", "tracker.org"),          # not over-stripped to bare TLD
    ("http://www.tracker.x.org/announce", "x.org"),
    ("", None),
    ("tracker.foo.bar", None),                                # no scheme
    ("**[DHT]**", None),                                      # pseudo-tracker
    ("http:///path", None),                                   # no host
])
def test_domain_from_tracker_url(csc, url, expected):
    assert csc._domain_from_tracker_url(url) == expected


def test_domain_from_tracker_url_bounds_pathological_host(csc):
    # A crafted announce URL with an absurdly long "www."-repeated host must not
    # drive the normalize loop into quadratic-time CPU exhaustion. Hosts past the
    # 253-char DNS limit are returned unprocessed (and instantly) — if the strip
    # loop ran on this ~80 KB host the test would hang for seconds.
    host = "www." * 20000 + "x.org"
    out = csc._domain_from_tracker_url("http://" + host + "/announce")
    assert out == host


# ─── is_unreliable_tracker ───────────────────────────────────────────────────

def test_unreliable_empty_config(csc):
    reconfigure(csc, UNRELIABLE_TRACKERS=[])
    assert csc.is_unreliable_tracker("anything.com") is False


def test_unreliable_empty_domain(csc):
    reconfigure(csc, UNRELIABLE_TRACKERS=["x.com"])
    assert csc.is_unreliable_tracker(None) is False
    assert csc.is_unreliable_tracker("") is False


def test_unreliable_literal_case_insensitive(csc):
    reconfigure(csc, UNRELIABLE_TRACKERS=["MyTracker.org"])
    assert csc.is_unreliable_tracker("mytracker.org") is True
    assert csc.is_unreliable_tracker("other.com") is False


def test_unreliable_regex_fullmatch(csc):
    reconfigure(csc, UNRELIABLE_TRACKERS=[r"r:.*\.ru"])
    assert csc.is_unreliable_tracker("x.ru") is True
    assert csc.is_unreliable_tracker("x.ru.com") is False


# ─── is_excluded_tracker / torrent_on_excluded_tracker ───────────────────────

def test_excluded_empty_config(csc):
    reconfigure(csc, EXCLUDED_TRACKERS=[])
    assert csc.is_excluded_tracker("anything.com") is False


def test_excluded_empty_domain(csc):
    reconfigure(csc, EXCLUDED_TRACKERS=["x.com"])
    assert csc.is_excluded_tracker(None) is False
    assert csc.is_excluded_tracker("") is False


def test_excluded_literal_case_insensitive(csc):
    reconfigure(csc, EXCLUDED_TRACKERS=["MyTracker.org"])
    assert csc.is_excluded_tracker("mytracker.org") is True
    assert csc.is_excluded_tracker("other.com") is False


def test_excluded_regex_fullmatch(csc):
    reconfigure(csc, EXCLUDED_TRACKERS=[r"r:.*\.example\.net"])
    assert csc.is_excluded_tracker("a.example.net") is True
    assert csc.is_excluded_tracker("a.example.net.evil.com") is False


def test_torrent_on_excluded_tracker_no_config(csc):
    reconfigure(csc, EXCLUDED_TRACKERS=[])
    tor = {"tracker": "http://protected.org/announce", "_trackers": []}
    assert csc.torrent_on_excluded_tracker(tor) is False


def test_torrent_on_excluded_tracker_primary(csc):
    reconfigure(csc, EXCLUDED_TRACKERS=["protected.org"])
    tor = {"tracker": "http://protected.org/announce", "_trackers": []}
    assert csc.torrent_on_excluded_tracker(tor) is True


def test_torrent_on_excluded_tracker_secondary_only(csc):
    # "Any tracker" scope: a torrent whose PRIMARY announce is a non-excluded
    # domain must still be protected when a SECONDARY tracker is excluded.
    reconfigure(csc, EXCLUDED_TRACKERS=["protected.org"])
    tor = {
        "tracker": "http://public.example/announce",
        "_trackers": [
            {"url": "http://public.example/announce"},
            {"url": "http://tracker.protected.org:8080/announce"},  # leading tracker. stripped
        ],
    }
    assert csc.torrent_on_excluded_tracker(tor) is True


def test_torrent_on_excluded_tracker_no_match(csc):
    reconfigure(csc, EXCLUDED_TRACKERS=["protected.org"])
    tor = {
        "tracker": "http://public.example/announce",
        "_trackers": [{"url": "http://another.example/announce"}],
    }
    assert csc.torrent_on_excluded_tracker(tor) is False


# ─── get_seeder_count ────────────────────────────────────────────────────────

def test_seeder_count_reliable(csc):
    reconfigure(csc, UNRELIABLE_TRACKERS=[])
    tor = {"tracker": "http://reliable.com/announce", "num_complete": 10, "num_incomplete": 5}
    assert csc.get_seeder_count(FakeClient(), tor) == 10
    assert tor["_tracker_domain"] == "reliable.com"


def test_seeder_count_unreliable_adds_incomplete(csc):
    reconfigure(csc, UNRELIABLE_TRACKERS=["unreliable.com"])
    tor = {"tracker": "http://unreliable.com/announce", "num_complete": 10, "num_incomplete": 5}
    assert csc.get_seeder_count(FakeClient(), tor) == 15


def test_seeder_count_unreliable_preserves_unscraped_sentinel(csc):
    # qBittorrent reports -1 for an unscraped (unknown) seeder count. On an
    # unreliable tracker, a server-supplied leecher total must NOT wash that -1
    # into a positive sum — doing so would silently defeat the LOW_SEEDS deletion
    # guard for a torrent whose seeders were never scraped. The unknown sentinel
    # is preserved (like the reliable branch) so LOW_SEEDS fails closed.
    reconfigure(csc, UNRELIABLE_TRACKERS=["unreliable.com"])
    tor = {"tracker": "http://unreliable.com/announce", "num_complete": -1, "num_incomplete": 99}
    assert csc.get_seeder_count(FakeClient(), tor) == -1


def test_seeder_count_unreliable_known_zero_still_recovers(csc):
    # No-op on real scraped data: num_complete == 0 means "scraped, zero seeders"
    # (NOT unknown), so the unreliable-tracker recovery from leechers must still
    # apply. Guards against an over-eager fix that blocks the legitimate case.
    reconfigure(csc, UNRELIABLE_TRACKERS=["unreliable.com"])
    tor = {"tracker": "http://unreliable.com/announce", "num_complete": 0, "num_incomplete": 8}
    assert csc.get_seeder_count(FakeClient(), tor) == 8


def test_seeder_count_missing_fields(csc):
    reconfigure(csc, UNRELIABLE_TRACKERS=[])
    assert csc.get_seeder_count(FakeClient(), {"tracker": "http://x.com/a"}) == 0


# ─── get_tracker_domain ──────────────────────────────────────────────────────

def test_tracker_domain_from_field_no_api_call(csc):
    client = FakeClient(trackers=[{"url": "http://other.example/announce"}])
    tor = {"tracker": "http://field.example/announce", "hash": "h"}
    assert csc.get_tracker_domain(client, tor) == "field.example"


def test_tracker_domain_api_fallback(csc):
    client = FakeClient(trackers=[{"url": "http://tracker.foo.com/announce"}])
    tor = {"tracker": "", "hash": "h"}
    assert csc.get_tracker_domain(client, tor) == "foo.com"


def test_tracker_domain_api_raises_returns_none(csc):
    import urllib.error

    class Raising:
        def get_torrent_trackers(self, h):
            raise urllib.error.URLError("boom")

    tor = {"tracker": "", "hash": "h"}
    assert csc.get_tracker_domain(Raising(), tor) is None
