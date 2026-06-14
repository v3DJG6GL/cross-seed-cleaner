"""External-library hardlink scanning and missing-hard-links orphan detection
(cross_seed_cleaner.py: scan_external_libraries / check_missing_hard_links)."""
import errno
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
    # A configured wildcard that resolves to nothing (the usual symptom of an
    # unmounted media volume) must fail closed: an empty inode set would make
    # every torrent look orphaned, so the scan is flagged incomplete to block
    # deletion downstream.
    assert csc.scan_external_libraries([str(tmp_path / "nope" / "*")]) == {}
    assert csc.SCAN_STATS['scan_incomplete'] is True


def test_scan_partial_wildcard_zero_match_flags_incomplete(csc, tmp_path):
    # One good library plus a zero-match wildcard: real inodes are still found
    # (final_paths is non-empty, so the "no valid paths" guard never fires), but
    # the missing wildcard location alone must still flag the scan incomplete,
    # exactly as a literal missing path does via os.walk's error handler.
    lib = tmp_path / "lib"
    lib.mkdir()
    f = lib / "movie.mkv"
    f.write_bytes(b"x" * 100)
    os.link(str(f), str(lib / "movie.hardlink.mkv"))
    result = csc.scan_external_libraries([str(lib), str(tmp_path / "gone" / "*")])
    assert _inode(str(f)) in result            # the good library was scanned
    assert csc.SCAN_STATS['scan_incomplete'] is True


def test_scan_clean_walk_not_flagged_incomplete(csc, tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    f = lib / "movie.mkv"
    f.write_bytes(b"x" * 100)
    os.link(str(f), str(lib / "movie.hardlink.mkv"))
    csc.scan_external_libraries([str(lib)])
    assert csc.SCAN_STATS['scan_incomplete'] is False


def test_scan_aborted_walk_flags_incomplete(csc, tmp_path, monkeypatch):
    # A mid-walk failure (not a per-entry OSError, which os.walk routes to the
    # error handler) must mark the scan incomplete so deletion can be blocked.
    lib = tmp_path / "lib"
    lib.mkdir()

    def _boom(*a, **k):
        raise RuntimeError("walk blew up")

    monkeypatch.setattr(csc.os, "walk", _boom)
    csc.scan_external_libraries([str(lib)])
    assert csc.SCAN_STATS['scan_incomplete'] is True


def test_scan_unreadable_dir_routed_to_onerror_flags_incomplete(csc, tmp_path, monkeypatch):
    # os.walk does NOT raise when it can't enter a directory — it calls the
    # onerror callback and skips that subtree. The skipped subtree's protected
    # inodes are missing, so the scan must still be flagged incomplete.
    lib = tmp_path / "lib"
    lib.mkdir()

    def _walk_with_dir_error(path, **kwargs):
        onerror = kwargs.get("onerror")
        if onerror is not None:
            onerror(OSError(errno.EACCES, "Permission denied", str(lib / "locked")))
        return iter(())

    monkeypatch.setattr(csc.os, "walk", _walk_with_dir_error)
    csc.scan_external_libraries([str(lib)])
    assert csc.SCAN_STATS['scan_incomplete'] is True


def test_scan_unreadable_file_flags_incomplete(csc, tmp_path, monkeypatch):
    # A file that exists but can't be stat'd (permission denied / stale NFS /
    # I/O error) may have been a protected hardlink we're now missing.
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "movie.mkv").write_bytes(b"x" * 100)

    def _stat_denied(path, *a, **k):
        raise OSError(errno.EACCES, "Permission denied", str(path))

    monkeypatch.setattr(csc.os, "stat", _stat_denied)
    csc.scan_external_libraries([str(lib)])
    assert csc.SCAN_STATS['scan_incomplete'] is True


def test_scan_missing_file_enoent_not_flagged(csc, tmp_path, monkeypatch):
    # A broken symlink / a file that vanished mid-walk (ENOENT) protects
    # nothing, so it must NOT block deletion with a false positive.
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "movie.mkv").write_bytes(b"x" * 100)

    def _stat_missing(path, *a, **k):
        raise OSError(errno.ENOENT, "No such file", str(path))

    monkeypatch.setattr(csc.os, "stat", _stat_missing)
    csc.scan_external_libraries([str(lib)])
    assert csc.SCAN_STATS['scan_incomplete'] is False


def test_finalize_deletion_blocked_on_incomplete_scan(csc, monkeypatch, capsys):
    # Even in live mode with eligible groups, an incomplete library scan must
    # block deletion entirely (the gate returns before any prompt or API call).
    csc.DRY_RUN = False
    csc.MANUAL_MODE = False
    csc.SCAN_STATS['scan_incomplete'] = True
    monkeypatch.setattr(csc, "input", lambda *a, **k: pytest.fail("must not prompt"), raising=False)
    client = FakeClient()
    csc._finalize_deletion(client, {1: [{"hash": "a"}]})
    assert client.deleted == []                  # nothing deleted
    assert "DISABLED" in capsys.readouterr().out  # and the user is told why


def test_finalize_manual_live_blocked_on_incomplete_scan(csc, monkeypatch, capsys):
    # The highest-stakes cell: a manual LIVE run (--manual, not dry) with an
    # incomplete scan must be BLOCKED before the manual loop — the gate returns
    # before any prompt or delete, so a possibly-still-protected file is never
    # removed against a knowingly-incomplete inode set. (The gate is `not DRY_RUN`,
    # which is True here; a regression scoping it to auto-only would let this
    # through to a real deletion.)
    csc.DRY_RUN = False
    csc.MANUAL_MODE = True
    csc.SCAN_STATS['scan_incomplete'] = True
    monkeypatch.setattr(csc, "input", lambda *a, **k: pytest.fail("must not prompt"), raising=False)
    client = FakeClient()
    csc._finalize_deletion(client, {1: [{"hash": "a"}]})
    assert client.deleted == []                  # nothing deleted
    assert "DISABLED" in capsys.readouterr().out  # blocked, with the reason shown


def test_finalize_empty_set_incomplete_scan_says_nothing_to_delete(csc, monkeypatch, capsys):
    # A live run with an incomplete scan but NO eligible groups was at no risk
    # (nothing to delete), so it must say "Nothing to delete" — not the alarming
    # "Deletion is DISABLED" warning. The empty-set check runs before the
    # incomplete-scan block; the block still fires when there ARE candidates
    # (covered by the blocked-on-incomplete-scan tests above).
    csc.DRY_RUN = False
    csc.MANUAL_MODE = False
    csc.SCAN_STATS['scan_incomplete'] = True
    monkeypatch.setattr(csc, "input", lambda *a, **k: pytest.fail("must not prompt"), raising=False)
    client = FakeClient()
    csc._finalize_deletion(client, {})
    out = capsys.readouterr().out
    assert client.deleted == []
    assert "Nothing to delete" in out
    assert "DISABLED" not in out


def test_finalize_dry_run_incomplete_scan_no_disabled_warning(csc, capsys):
    # Dry-run deletes nothing regardless, so an incomplete scan must NOT claim
    # "Deletion is DISABLED" (a run that was never going to delete). The normal
    # dry-run notice is shown instead.
    csc.DRY_RUN = True
    csc.MANUAL_MODE = False
    csc.SCAN_STATS['scan_incomplete'] = True
    csc._finalize_deletion(FakeClient(), {1: [{"hash": "a"}]})
    out = capsys.readouterr().out
    assert "DISABLED" not in out
    assert "DRY RUN" in out


def test_finalize_manual_dry_run_incomplete_scan_enters_loop(csc, monkeypatch, capsys):
    # A --manual dry-run also deletes nothing (delete_torrents returns "dry_run"),
    # so an incomplete scan must NOT claim "Deletion is DISABLED" or skip the
    # manual preview — the user still gets the interactive loop. Only a run that
    # actually deletes (live, manual or auto) is blocked.
    csc.DRY_RUN = True
    csc.MANUAL_MODE = True
    csc.SCAN_STATS['scan_incomplete'] = True
    prompts = []
    def _quit(*a, **k):
        prompts.append(a[0] if a else "")
        return "q"  # leave the manual loop immediately
    monkeypatch.setattr(csc, "input", _quit, raising=False)
    client = FakeClient()
    csc._finalize_deletion(client, {1: [{"hash": "a"}]})
    out = capsys.readouterr().out
    assert "DISABLED" not in out   # not blocked as a deletion run
    assert prompts                 # the manual preview loop was actually entered
    assert client.deleted == []    # quit before deleting anything


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


def test_mhl_warns_when_no_external_paths(csc, monkeypatch, capsys):
    # No media library configured: the mode still runs (torrent-to-torrent /
    # cross-seed links are detected) but warns that media-library-only links
    # won't be protected.
    tors = [_tor("a", "inode:9:9", "cross-seed")]
    _setup_mhl(csc, monkeypatch, tors, ["cross-seed"])
    reconfigure(csc, EXTERNAL_MEDIA_PATHS=[])
    csc.check_missing_hard_links(FakeClient())
    assert "cross-seed) hard-links only" in capsys.readouterr().out


def test_mhl_no_warning_when_external_paths_set(csc, monkeypatch, capsys):
    tors = [_tor("a", "inode:9:9", "cross-seed")]
    _setup_mhl(csc, monkeypatch, tors, ["cross-seed"])
    reconfigure(csc, EXTERNAL_MEDIA_PATHS=["/mnt/lib"])
    csc.check_missing_hard_links(FakeClient())
    assert "cross-seed) hard-links only" not in capsys.readouterr().out
