"""Path mapping, representative-inode selection, identity parsing, and the
grouping pipeline (cross_seed_cleaner.py:342-449, 611-723)."""
import os

import pytest

from conftest import reconfigure, FakeClient


# ─── apply_path_mapping ──────────────────────────────────────────────────────

@pytest.fixture
def mapped(csc):
    return reconfigure(csc, PATH_MAPPINGS={"/data": "/mnt/storage", "/data/movies": "/mnt/films"})


@pytest.mark.parametrize("remote,expected", [
    ("", ""),
    ("/data/tv/x", "/mnt/storage/tv/x"),
    ("/data/movies/x", "/mnt/films/x"),       # longest prefix wins
    ("/data", "/mnt/storage"),                 # exact match
    ("/database/z", "/database/z"),            # boundary: must NOT match /data
    ("/unmapped/y", "/unmapped/y"),
])
def test_apply_path_mapping(mapped, remote, expected):
    assert mapped.apply_path_mapping(remote) == expected


def test_apply_path_mapping_none(mapped):
    assert mapped.apply_path_mapping(None) == ""


def test_apply_path_mapping_trailing_slash_prefix(csc):
    reconfigure(csc, PATH_MAPPINGS={"/data/": "/mnt/x/"})
    assert csc.apply_path_mapping("/data/foo") == "/mnt/x/foo"


# ─── get_representative_inode (real files) ───────────────────────────────────

def _inode(path):
    st = os.stat(path)
    return (st.st_dev, st.st_ino)


def test_inode_single_file(csc, tmp_path):
    f = tmp_path / "movie.mkv"
    f.write_bytes(b"x" * 100)
    assert csc.get_representative_inode(str(f)) == _inode(str(f))


def test_inode_nonexistent(csc, tmp_path):
    assert csc.get_representative_inode(str(tmp_path / "nope")) is None


def test_inode_largest_nonmetadata(csc, tmp_path):
    (tmp_path / "big.mkv").write_bytes(b"x" * 1000)
    (tmp_path / "small.mkv").write_bytes(b"x" * 10)
    (tmp_path / "info.nfo").write_bytes(b"x" * 5000)   # filtered despite being largest
    assert csc.get_representative_inode(str(tmp_path)) == _inode(str(tmp_path / "big.mkv"))


def test_inode_sample_file_skipped(csc, tmp_path):
    (tmp_path / "real.mkv").write_bytes(b"x" * 100)
    (tmp_path / "sample.mkv").write_bytes(b"x" * 9999)
    assert csc.get_representative_inode(str(tmp_path)) == _inode(str(tmp_path / "real.mkv"))


def test_inode_sample_dir_pruned(csc, tmp_path):
    (tmp_path / "main.mkv").write_bytes(b"x" * 50)
    sample = tmp_path / "Sample"
    sample.mkdir()
    (sample / "huge.mkv").write_bytes(b"x" * 9999)
    assert csc.get_representative_inode(str(tmp_path)) == _inode(str(tmp_path / "main.mkv"))


def test_inode_tie_break_lexicographic(csc, tmp_path):
    (tmp_path / "b.bin").write_bytes(b"x" * 500)
    (tmp_path / "a.bin").write_bytes(b"x" * 500)
    assert csc.get_representative_inode(str(tmp_path)) == _inode(str(tmp_path / "a.bin"))


def test_inode_tie_break_same_basename_is_walk_order_independent(csc, tmp_path, monkeypatch):
    # Two distinct files with the SAME size AND the SAME basename in different
    # subdirs: the basename tie is broken by full path, so the representative
    # inode (and thus the grouping identity) is stable no matter what order the
    # directory walk happens to return — otherwise cross-seeds of one release
    # could pick different inodes and fail to group.
    (tmp_path / "Disc1").mkdir()
    (tmp_path / "Disc2").mkdir()
    (tmp_path / "Disc1" / "video.mkv").write_bytes(b"a" * 500)
    (tmp_path / "Disc2" / "video.mkv").write_bytes(b"b" * 500)
    winner = _inode(str(tmp_path / "Disc1" / "video.mkv"))   # smaller full path
    assert csc.get_representative_inode(str(tmp_path)) == winner

    # Force the opposite traversal order (Disc2 before Disc1); winner must hold.
    real_walk = os.walk
    def reversed_walk(top, *a, **k):
        for root, dirs, files in real_walk(top, *a, **k):
            dirs.sort(reverse=True)
            yield root, dirs, files
    monkeypatch.setattr(os, "walk", reversed_walk)
    assert csc.get_representative_inode(str(tmp_path)) == winner


def test_inode_empty_dir_falls_back_to_dir(csc, tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    assert csc.get_representative_inode(str(d)) == _inode(str(d))


def test_inode_only_metadata_falls_back_to_dir(csc, tmp_path):
    (tmp_path / "a.nfo").write_bytes(b"x" * 10)
    assert csc.get_representative_inode(str(tmp_path)) == _inode(str(tmp_path))


# ─── _parse_inode_identity ───────────────────────────────────────────────────

@pytest.mark.parametrize("identity,expected", [
    ("inode:5:99", (5, 99)),
    ("heuristic:100:name", None),
    ("inode:5", None),               # IndexError
    ("inode:a:b", None),             # ValueError
])
def test_parse_inode_identity(csc, identity, expected):
    assert csc._parse_inode_identity(identity) == expected


# ─── get_path_identity ───────────────────────────────────────────────────────

def test_path_identity_inode(csc, monkeypatch):
    monkeypatch.setattr(csc, "get_representative_inode", lambda p: (7, 42))
    assert csc.get_path_identity({"content_path": "/x", "name": "n", "size": 9}) == "inode:7:42"


def test_path_identity_heuristic(csc, monkeypatch):
    monkeypatch.setattr(csc, "get_representative_inode", lambda p: None)
    assert csc.get_path_identity({"content_path": "/x", "name": "n", "size": 9}) == "heuristic:9:n"


def test_path_identity_groups_same_inode(csc, monkeypatch):
    monkeypatch.setattr(csc, "get_representative_inode", lambda p: (1, 1))
    a = csc.get_path_identity({"content_path": "/a", "name": "A", "size": 1})
    b = csc.get_path_identity({"content_path": "/b", "name": "B", "size": 2})
    assert a == b   # same inode -> same identity -> grouped


# ─── load_and_group_torrents ─────────────────────────────────────────────────

def _wire(csc, monkeypatch, torrents, external=None):
    reconfigure(csc, PATH_MAPPINGS={})   # no remapping; normpath(content_path)
    monkeypatch.setattr(csc, "_fetch_and_filter_torrents", lambda client: torrents)
    monkeypatch.setattr(csc, "_scan_external_libs_phase", lambda: external or {})
    monkeypatch.setattr(csc, "_fetch_seeders_phase", lambda client, t: None)
    monkeypatch.setattr(csc, "get_path_identity", lambda t: t["_identity"])


def _tor(identity, h, added, **extra):
    d = {"_identity": identity, "hash": h, "added_on": added, "name": h, "content_path": f"/data/{h}"}
    d.update(extra)
    return d


def test_grouping_pairs_same_identity(csc, monkeypatch):
    tors = [_tor("inode:1:1", "a", 100), _tor("inode:1:1", "b", 200)]
    _wire(csc, monkeypatch, tors)
    groups = csc.load_and_group_torrents(FakeClient())
    assert len(groups) == 1
    g = next(iter(groups.values()))
    assert g["original"]["hash"] == "a"             # oldest added_on
    assert [c["hash"] for c in g["crossseeds"]] == ["b"]


def test_grouping_tolerates_null_added_on(csc, monkeypatch):
    # qBittorrent can report added_on as an explicit null (the codebase guards it
    # everywhere else: evaluate_dead_trackers / format_timestamp). A group mixing a
    # null and an int added_on must not crash the oldest-first sort that picks the
    # original; the null is treated as 0 (oldest), matching int(... or 0).
    tors = [_tor("inode:1:1", "a", None), _tor("inode:1:1", "b", 200)]
    _wire(csc, monkeypatch, tors)
    groups = csc.load_and_group_torrents(FakeClient())
    assert len(groups) == 1
    g = next(iter(groups.values()))
    assert g["original"]["hash"] == "a"             # null added_on sorts oldest
    assert [c["hash"] for c in g["crossseeds"]] == ["b"]


def test_grouping_drops_singletons(csc, monkeypatch):
    tors = [_tor("inode:1:1", "a", 100), _tor("inode:2:2", "lonely", 100)]
    _wire(csc, monkeypatch, tors + [_tor("inode:1:1", "b", 200)])
    groups = csc.load_and_group_torrents(FakeClient())
    assert len(groups) == 1
    assert all(g["original"]["hash"] != "lonely" for g in groups.values())


def test_external_unrelated_path_marks_linked(csc, monkeypatch):
    tors = [_tor("inode:1:2", "a", 100, content_path="/data/a"),
            _tor("inode:1:2", "b", 200, content_path="/data/a")]
    _wire(csc, monkeypatch, tors, external={(1, 2): "/mnt/library/elsewhere"})
    groups = csc.load_and_group_torrents(FakeClient())
    g = next(iter(groups.values()))
    assert g["original"]["_external_hardlink"] is True
    assert g["original"]["_external_path"] == "/mnt/library/elsewhere"


def test_external_self_match_not_linked(csc, monkeypatch):
    tors = [_tor("inode:1:2", "a", 100, content_path="/data/a"),
            _tor("inode:1:2", "b", 200, content_path="/data/a")]
    _wire(csc, monkeypatch, tors, external={(1, 2): "/data/a"})   # candidate == own path
    groups = csc.load_and_group_torrents(FakeClient())
    g = next(iter(groups.values()))
    assert g["original"]["_external_hardlink"] is False
