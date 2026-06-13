"""Shared pytest harness for the cross-seed-cleaner suite.

The module under test runs config resolution (argparse + env + _validate_config
+ Chart.js load) AT IMPORT TIME, with no __main__ guard. So tests must stub
sys.argv, and to vary import-time config they must set env vars then re-import.
Several config-derived globals are computed once at import and are NOT
recomputed when the source constant is reassigned — reconfigure() keeps the two
in sync for in-process tweaks. See cross_seed_cleaner.py L191-234.
"""
import importlib
import os
import sys
import glob
from html.parser import HTMLParser

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


# ─── module loading ──────────────────────────────────────────────────────────

def load_module(env=None, argv=None):
    """Import a FRESH cross_seed_cleaner with stubbed argv and optional env.

    env: dict of environment overrides applied before import (None value = unset).
    argv: list used as sys.argv (default ["cross_seed_cleaner.py"], i.e. no flags).
    Restores argv/env afterwards. Use this for import-time config-matrix and
    SystemExit/validation tests.
    """
    saved_argv = sys.argv
    env = env or {}
    saved_env = {k: os.environ.get(k) for k in env}
    sys.argv = list(argv) if argv is not None else ["cross_seed_cleaner.py"]
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        sys.modules.pop("cross_seed_cleaner", None)
        return importlib.import_module("cross_seed_cleaner")
    finally:
        sys.argv = saved_argv
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# Keys whose module global is derived from another constant at import time.
# reconfigure() recomputes the derived global so consumers stay consistent.
def reconfigure(csc, **overrides):
    """Set constants in-process AND recompute their import-time-derived globals."""
    for key, val in overrides.items():
        setattr(csc, key, val)
        if key == "MIN_SIZE_GIB":
            csc.MIN_SIZE_BYTES = val * 1024 * 1024 * 1024
        elif key == "MIN_ORIGINAL_SEED_TIME_DAYS":
            csc.MIN_ORIGINAL_SEED_TIME_SECONDS = val * 86400
        elif key == "PATH_MAPPINGS":
            csc._SORTED_PATH_MAPPING_PREFIXES = sorted(val.keys(), key=len, reverse=True)
        elif key == "CATEGORY_ALLOWLIST":
            csc._CATEGORY_ALLOWLIST_SPECS = csc._compile_specs(val, "CATEGORY_ALLOWLIST")
        elif key == "CATEGORY_BLOCKLIST":
            csc._CATEGORY_BLOCKLIST_SPECS = csc._compile_specs(val, "CATEGORY_BLOCKLIST")
        elif key == "UNRELIABLE_TRACKERS":
            csc._UNRELIABLE_TRACKERS_SPECS = csc._compile_specs(val, "UNRELIABLE_TRACKERS", lower=True)
        elif key == "MISSING_HARD_LINKS_CATEGORIES":
            csc._MISSING_HARD_LINKS_CATEGORY_SPECS = csc._compile_specs(val, "MISSING_HARD_LINKS_CATEGORIES", lower=True)
        elif key == "CATEGORY_FILTER_MODE":
            csc._CATEGORY_FILTER_MODE_LC = val.lower()
        elif key == "DEAD_TRACKER_STATUSES":
            # accept a CSV string (matches config.py form) or any iterable of ints
            csc.DEAD_TRACKER_STATUSES = (
                csc._parse_dead_statuses(val) if isinstance(val, str) else frozenset(val)
            )
    return csc


@pytest.fixture
def csc():
    """A freshly-imported module with default config (no CLI flags)."""
    return load_module()


# ─── report rendering ────────────────────────────────────────────────────────

def render_html(csc, sorted_items, eligible_ids, tmp_path):
    """Render the HTML report to tmp and return its text."""
    csc.HTML_EXPORT = os.path.join(str(tmp_path), "report.html")
    csc.CSV_EXPORT = ""
    csc.export_reports(sorted_items=sorted_items, eligible_ids=eligible_ids)
    matches = sorted(glob.glob(os.path.join(str(tmp_path), "report*.html")),
                     key=os.path.getmtime)
    assert matches, "export_reports produced no HTML output"
    with open(matches[-1], "r", encoding="utf-8") as f:
        return f.read()


def render_csv(csc, sorted_items, eligible_ids, tmp_path):
    """Render the CSV report to tmp and return its text."""
    csc.HTML_EXPORT = ""
    csc.CSV_EXPORT = os.path.join(str(tmp_path), "report.csv")
    csc.export_reports(sorted_items=sorted_items, eligible_ids=eligible_ids)
    matches = sorted(glob.glob(os.path.join(str(tmp_path), "report*.csv")),
                     key=os.path.getmtime)
    assert matches, "export_reports produced no CSV output"
    with open(matches[-1], "r", encoding="utf-8") as f:
        return f.read()


class ReportHTML(HTMLParser):
    """Collect every start tag's attributes so tests can query the markup
    without a third-party HTML parser. tags = list of (tag, {attr: value})."""

    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    handle_startendtag = handle_starttag

    def with_class(self, cls):
        """All (tag, attrs) whose class attribute contains the token `cls`."""
        return [(t, a) for (t, a) in self.tags
                if cls in (a.get("class") or "").split()]

    def grid_rows(self):
        return self.with_class("grid-row")

    def groups(self):
        return self.with_class("group")


# ─── fake qBittorrent client ─────────────────────────────────────────────────

class FakeClient:
    """Stub client for deletion-path tests. delete_results is a list consumed
    per call (e.g. "dry_run", None for failure, "" for success); a non-list
    value is returned for every call.

    For tracker-error mode tests:
      trackers_by_hash maps a torrent hash to its raw tracker list (matching
      what /api/v2/torrents/trackers returns — sticky DHT/PeX/LSD entries
      with URLs starting "**" are filtered out by the production code).
      webapi_version returns whatever string the test wants; the production
      get_torrents_with_trackers uses it to decide bulk vs per-torrent.
      bulk_calls and per_hash_calls count which code path was taken."""

    def __init__(self, delete_results="", torrents=None, trackers=None,
                 trackers_by_hash=None, webapi_version="2.11.0"):
        self._delete_results = list(delete_results) if isinstance(delete_results, list) else delete_results
        self.deleted = []
        self._torrents = torrents or []
        self._trackers = trackers or []
        self._trackers_by_hash = trackers_by_hash or {}
        self._webapi_version = webapi_version
        self.bulk_calls = 0
        self.per_hash_calls = 0

    def delete_torrents(self, hashes, delete_files=True):
        self.deleted.append((list(hashes), delete_files))
        if isinstance(self._delete_results, list):
            return self._delete_results.pop(0) if self._delete_results else ""
        return self._delete_results

    def get_torrents(self):
        return self._torrents

    def get_torrent_trackers(self, torrent_hash):
        self.per_hash_calls += 1
        if torrent_hash in self._trackers_by_hash:
            return list(self._trackers_by_hash[torrent_hash])
        return self._trackers

    def webapi_version(self):
        return self._webapi_version

    def get_torrents_with_trackers(self, max_workers=8):
        """Mirrors the production method: stash `_trackers` (sticky entries
        stripped) on each torrent, prefer the bulk path on a recent API.
        Counts calls so tests can assert which path was taken."""
        # Use production's numeric version comparator so "2.9.0" < "2.11.0"
        # routes to the fallback path (lex compare would flip "9" > "1").
        from cross_seed_cleaner import _version_at_least
        if _version_at_least(self._webapi_version, (2, 11, 0)):
            self.bulk_calls += 1
            for t in self._torrents:
                raw = self._trackers_by_hash.get(t['hash'], [])
                t['_trackers'] = [tr for tr in raw if not str(tr.get('url', '')).startswith('**')]
            return self._torrents
        for t in self._torrents:
            raw = self.get_torrent_trackers(t['hash'])
            t['_trackers'] = [tr for tr in raw if not str(tr.get('url', '')).startswith('**')]
        return self._torrents
