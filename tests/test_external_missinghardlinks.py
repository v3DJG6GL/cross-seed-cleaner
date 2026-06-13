"""External-library hardlink scanning and missing-hard-links orphan detection
(cross_seed_cleaner.py:3011-3166)."""
import os

import pytest

from conftest import reconfigure, FakeClient


def _inode(path):
    st = os.stat(path)
    return (st.st_dev, st.st_ino)


# ─── scan_external_libraries ─────────────────────────────────────────────────

def test_scan_empty_paths(csc):
    assert csc.scan_external_libraries([]) == {}


def test_scan_records_hardlinked_file(csc, tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    f = lib / "movie.mkv"
    f.write_bytes(b"x" * 100)
    os.link(str(f), str(lib / "movie.hardlink.mkv"))   # nlink now 2
    result = csc.scan_external_libraries([str(lib)])
    assert _inode(str(f)) in result


def test_scan_ignores_unlinked_file(csc, tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    f = lib / "solo.mkv"
    f.write_bytes(b"x" * 100)            # nlink == 1
    assert csc.scan_external_libraries([str(lib)]) == {}


def test_scan_wildcard_zero_matches(csc, tmp_path):
    assert csc.scan_external_libraries([str(tmp_path / "nope" / "*")]) == {}


def test_scan_brace_expansion(csc, tmp_path):
    base = tmp_path / "base"
    for sub in ("a", "b"):
        d = base / sub
        d.mkdir(parents=True)
        f = d / "f.mkv"
        f.write_bytes(b"x" * 100)
        os.link(str(f), str(d / "f.link.mkv"))
    result = csc.scan_external_libraries([str(base) + "/{a,b}"])
    assert _inode(str(base / "a" / "f.mkv")) in result
    assert _inode(str(base / "b" / "f.mkv")) in result


# ─── check_missing_hard_links orphan detection ───────────────────────────────

def _setup_mhl(csc, monkeypatch, torrents, categories, external=None):
    """Wire check_missing_hard_links phases and capture the all_groups it produces."""
    reconfigure(csc, MISSING_HARD_LINKS_CATEGORIES=categories)
    csc.MISSING_HARD_LINKS_MODE = True
    monkeypatch.setattr(csc, "_fetch_and_filter_torrents", lambda client: torrents)
    monkeypatch.setattr(csc, "_scan_external_libs_phase", lambda: external or {})
    monkeypatch.setattr(csc, "_fetch_seeders_phase", lambda client, t: None)
    monkeypatch.setattr(csc, "get_path_identity", lambda t: t["_identity"])
    captured = {}
    monkeypatch.setattr(csc, "_run_analyze_and_finalize",
                        lambda client, all_groups: captured.update(all_groups))
    return captured


def _tor(h, identity, category, **extra):
    d = {"hash": h, "name": h, "_identity": identity, "category": category}
    d.update(extra)
    return d


def test_mhl_no_categories_early_return(csc, monkeypatch):
    reconfigure(csc, MISSING_HARD_LINKS_CATEGORIES=[])
    called = []
    monkeypatch.setattr(csc, "_fetch_and_filter_torrents", lambda c: called.append(1) or [])
    csc.check_missing_hard_links(FakeClient())
    assert called == []                  # returned before fetching


def test_mhl_target_category_regex_and_literal(csc, monkeypatch):
    tors = [
        _tor("a", "inode:1:1", "cross-seed"),     # literal target
        _tor("b", "inode:2:2", "autobrr-x"),      # regex target
        _tor("c", "inode:3:3", "movies"),         # not target
    ]
    cap = _setup_mhl(csc, monkeypatch, tors, ["cross-seed", "r:autobrr-.*"])
    csc.check_missing_hard_links(FakeClient())
    assert set(cap) == {"a", "b"}


def test_mhl_category_regex_case_insensitive_and_metachar_preserved(csc, monkeypatch):
    # A regex pattern must match the category case-insensitively and keep its
    # metacharacters intact. Lowercasing the pattern string would both break the
    # uppercase literal "TV-" against a real category and flip \D (non-digit)
    # into \d (digit), so "Sonarr" would stop matching and "1080" would start.
    tors = [
        _tor("a", "inode:1:1", "TV-Sonarr"),     # \D+ matches "Sonarr"
        _tor("b", "inode:2:2", "TV-1080"),        # digits -> \D+ must NOT match
    ]
    cap = _setup_mhl(csc, monkeypatch, tors, [r"r:TV-\D+"])
    csc.check_missing_hard_links(FakeClient())
    assert set(cap) == {"a"}


def test_mhl_sibling_identity_skipped(csc, monkeypatch):
    # Target torrent shares an identity with another torrent -> has a sibling -> not orphan.
    tors = [
        _tor("a", "inode:5:5", "cross-seed"),
        _tor("b", "inode:5:5", "movies"),          # non-category sibling, same identity
    ]
    cap = _setup_mhl(csc, monkeypatch, tors, ["cross-seed"])
    csc.check_missing_hard_links(FakeClient())
    assert cap == {}                                # 'a' skipped (sibling count >= 2)


def test_mhl_heuristic_sets_path_error(csc, monkeypatch):
    tors = [_tor("a", "heuristic:100:a", "cross-seed")]
    cap = _setup_mhl(csc, monkeypatch, tors, ["cross-seed"])
    csc.check_missing_hard_links(FakeClient())
    assert cap["a"]["original"]["_path_error"] is True


def test_mhl_external_match_preserved(csc, monkeypatch):
    tors = [_tor("a", "inode:1:2", "cross-seed")]
    cap = _setup_mhl(csc, monkeypatch, tors, ["cross-seed"], external={(1, 2): "/mnt/lib/x"})
    csc.check_missing_hard_links(FakeClient())
    assert cap["a"]["original"]["_external_hardlink"] is True
    assert cap["a"]["original"]["_external_path"] == "/mnt/lib/x"


def test_mhl_plain_orphan(csc, monkeypatch):
    tors = [_tor("a", "inode:9:9", "cross-seed")]
    cap = _setup_mhl(csc, monkeypatch, tors, ["cross-seed"])
    csc.check_missing_hard_links(FakeClient())
    orig = cap["a"]["original"]
    assert orig["_external_hardlink"] is False
    assert "_path_error" not in orig
