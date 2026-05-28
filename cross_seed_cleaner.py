#!/usr/bin/env python3
"""
Cross-Seed Cleaner v2026.05.28 - Deduplicate and cleanup cross-seeded torrents in qBittorrent
"""

import urllib.request
import urllib.parse
import json
import sys
import re
import os
import argparse
import csv
import glob
import html
import threading
from collections import defaultdict
from datetime import datetime
from functools import lru_cache

# User-editable settings live in config.py. Env vars and CLI flags override
# these defaults at runtime (see get_config() below for the precedence chain).
from config import *


def str2bool(v):
    if isinstance(v, bool): return v
    return v.lower() in ('yes', 'true', 't', 'y', '1')


def _validate_config():
    valid_category_filter_modes = {"none", "allow", "block", "both"}
    valid_sort_by = {"seeders", "seeds", "ratio", "size", "uploaded", "added", "name", "time"}
    valid_sort_order = {"asc", "desc"}

    if CATEGORY_FILTER_MODE.lower() not in valid_category_filter_modes:
        sys.stderr.write(
            f"ERROR: CATEGORY_FILTER_MODE={CATEGORY_FILTER_MODE!r} is invalid. "
            f"Expected one of {sorted(valid_category_filter_modes)}.\n"
        )
        sys.exit(1)
    if SORT_BY not in valid_sort_by:
        sys.stderr.write(
            f"ERROR: SORT_BY={SORT_BY!r} is invalid. "
            f"Expected one of {sorted(valid_sort_by)}.\n"
        )
        sys.exit(1)
    if SORT_ORDER not in valid_sort_order:
        sys.stderr.write(
            f"ERROR: SORT_ORDER={SORT_ORDER!r} is invalid. Expected 'asc' or 'desc'.\n"
        )
        sys.exit(1)


def get_config():
    # Read fallbacks via globals().get so a commented-out / missing constant in
    # config.py degrades to a safe default instead of raising NameError (lets
    # API-key users drop USER/PASS, and password users drop API_KEY).
    env_host = os.environ.get("QBITTORRENT_HOST", globals().get("QBITTORRENT_HOST", "http://localhost:8080"))
    env_user = os.environ.get("QBITTORRENT_USER", globals().get("QBITTORRENT_USER", ""))
    env_pass = os.environ.get("QBITTORRENT_PASS", globals().get("QBITTORRENT_PASS", ""))
    env_api_key = os.environ.get("QBITTORRENT_API_KEY", globals().get("QBITTORRENT_API_KEY", ""))
    env_min_seeders = int(os.environ.get("MIN_SEEDERS", MIN_SEEDERS))
    env_max_group = int(os.environ.get("MAX_TORRENTS_IN_GROUP", MAX_TORRENTS_IN_GROUP))
    env_min_days = float(os.environ.get("MIN_ORIGINAL_SEED_TIME_DAYS", MIN_ORIGINAL_SEED_TIME_DAYS))
    env_min_size_gib = float(os.environ.get("MIN_SIZE_GIB", MIN_SIZE_GIB))
    env_debug = str2bool(os.environ.get("DEBUG_MODE", str(DEBUG_MODE)))
    env_dry_run = str2bool(os.environ.get("DRY_RUN", str(DRY_RUN)))
    env_html_export = os.environ.get("HTML_EXPORT", HTML_EXPORT)
    env_csv_export = os.environ.get("CSV_EXPORT", CSV_EXPORT)
    env_no_hard_links_mode = str2bool(os.environ.get("NO_HARD_LINKS_MODE", str(NO_HARD_LINKS_MODE)))
    env_no_hard_links_cats = os.environ.get("NO_HARD_LINKS_CATEGORIES", NO_HARD_LINKS_CATEGORIES)
    env_ext_media_paths = os.environ.get("EXTERNAL_MEDIA_PATHS", EXTERNAL_MEDIA_PATHS)

    parser = argparse.ArgumentParser(description='Cross-Seed Cleaner: Deduplicate and cleanup torrents.')
    parser.add_argument('--host', default=env_host, help='qBittorrent Host')
    parser.add_argument('--user', default=env_user, help='qBittorrent User')
    parser.add_argument('--password', default=env_pass, help='qBittorrent Password')
    parser.add_argument('--api-key', default=env_api_key, help='qBittorrent API key (v5.2.0+); overrides user/password when set')
    parser.add_argument('--min-seeders', type=int, default=env_min_seeders, help='Minimum seeders required')
    parser.add_argument('--max-group-size', type=int, default=env_max_group, help='Max torrents in group')
    parser.add_argument('--min-days', type=float, default=env_min_days, help='Min seed time in DAYS')
    parser.add_argument('--min-size-gib', type=float, default=env_min_size_gib, help='Min torrent size in GiB (0=no limit)')
    parser.add_argument('--debug', action=argparse.BooleanOptionalAction, default=env_debug, help='Enable debug logging')
    parser.add_argument('--manual', action='store_true', help='Enable Interactive Manual Deletion Mode')
    parser.add_argument('--html', type=str, default=env_html_export, help='Path to save HTML report')
    parser.add_argument('--csv', type=str, default=env_csv_export, help='Path to save CSV report')

    parser.add_argument('--no-hard-links-mode', action='store_true', default=env_no_hard_links_mode, help='Enable mode to check for torrents without hard links')
    parser.add_argument('--no-hard-links-categories', type=str, default=env_no_hard_links_cats, help='Comma-separated categories for no-hard-links mode; prefix "r:" for regex (e.g. "r:autobrr-.*")')
    parser.add_argument('--external-media-paths', type=str, default=env_ext_media_paths,
                        help='Paths to scan for hardlinks. Supports commas, wildcards (*), and braces ({a,b}). E.g., "/mnt/{movies,tv},/mnt/users/*"')

    group = parser.add_mutually_exclusive_group()
    group.add_argument('--dry-run', action='store_true', help='Force Dry Run')
    group.add_argument('--delete', action='store_true', help='Force Live Mode')

    args = parser.parse_args()

    final_dry_run = env_dry_run
    if args.dry_run:
        final_dry_run = True
    elif args.delete:
        final_dry_run = False

    return args, final_dry_run


def smart_split_paths(raw_str):
    """
    Splits paths by comma, but respects braces {a,b} to avoid splitting inside them.
    Input:  "/path/{a,b}, /path2"
    Output: ["/path/{a,b}", "/path2"]
    """
    if not raw_str: return []
    paths = []
    current = []
    depth = 0
    for char in raw_str:
        if char == '{':
            depth += 1
            current.append(char)
        elif char == '}':
            depth -= 1
            current.append(char)
        elif char == ',' and depth == 0:
            paths.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if current:
        paths.append("".join(current).strip())
    return [p for p in paths if p]

_BRACE_RE = re.compile(r'\{([^{}]+)\}')

def expand_braces(text):
    """
    Expands bash-style braces into a list of paths.
    Input:  "/data/{a,b}/media"
    Output: ["/data/a/media", "/data/b/media"]
    """
    match = _BRACE_RE.search(text)
    if not match:
        return [text]

    prefix = text[:match.start()]
    suffix = text[match.end():]
    options = match.group(1).split(',')

    results = []
    for option in options:
        results.extend(expand_braces(prefix + option.strip() + suffix))
    return results


class Colors:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    ORANGE = '\033[38;5;208m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    BOLD = '\033[1m'
    END = '\033[0m'
    DIM = '\033[2m'

def debug_log(message):
    if DEBUG_MODE:
        print(f"{Colors.DIM}  [DEBUG] {message}{Colors.END}")

_ANSI_RE = re.compile(r'\033\[[0-9;]+m')

def strip_colors(text):
    return _ANSI_RE.sub('', text)

def bold(text):
    return f"{Colors.BOLD}{text}{Colors.END}"

def _clear_progress_line():
    sys.stdout.write("\r\033[2K")
    sys.stdout.flush()

def html_escape(v):
    return html.escape("" if v is None else str(v), quote=True)

def js_string(v):
    return json.dumps(v).replace("</", "<\\/")

ARGS, DRY_RUN = get_config()

QBITTORRENT_HOST = ARGS.host
QBITTORRENT_USER = ARGS.user
QBITTORRENT_PASS = ARGS.password
QBITTORRENT_API_KEY = ARGS.api_key
MIN_SEEDERS = ARGS.min_seeders
MAX_TORRENTS_IN_GROUP = ARGS.max_group_size
MIN_ORIGINAL_SEED_TIME_DAYS = ARGS.min_days
MIN_ORIGINAL_SEED_TIME_SECONDS = MIN_ORIGINAL_SEED_TIME_DAYS * 86400
MIN_SIZE_GIB = ARGS.min_size_gib
MIN_SIZE_BYTES = MIN_SIZE_GIB * 1024 * 1024 * 1024
_SORTED_PATH_MAPPING_PREFIXES = sorted(PATH_MAPPINGS.keys(), key=len, reverse=True)
DEBUG_MODE = ARGS.debug
MANUAL_MODE = ARGS.manual
HTML_EXPORT = ARGS.html
CSV_EXPORT = ARGS.csv

CHARTJS_SOURCE = None
REPORT_LOGIC_SOURCE = None
if HTML_EXPORT:
    _VENDOR_CHARTJS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'vendor', 'chart.js', 'chart.umd.min.js')
    try:
        with open(_VENDOR_CHARTJS_PATH, 'r', encoding='utf-8') as _f:
            CHARTJS_SOURCE = _f.read()
        CHARTJS_SOURCE = re.sub(r'(?m)^\s*//[#@]\s*sourceMappingURL=.*$', '', CHARTJS_SOURCE)
        CHARTJS_SOURCE = re.sub(r'/\*[#@]\s*sourceMappingURL=.*?\*/', '', CHARTJS_SOURCE)
    except FileNotFoundError:
        sys.stderr.write(
            f"ERROR: vendored Chart.js not found at {_VENDOR_CHARTJS_PATH}.\n"
            f"Run from a full checkout of the repository (the vendor/chart.js/ directory must be present).\n"
        )
        sys.exit(1)

    _VENDOR_REPORT_LOGIC_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'vendor', 'report', 'report-logic.js')
    try:
        with open(_VENDOR_REPORT_LOGIC_PATH, 'r', encoding='utf-8') as _f:
            REPORT_LOGIC_SOURCE = _f.read()
    except FileNotFoundError:
        sys.stderr.write(
            f"ERROR: vendored report logic not found at {_VENDOR_REPORT_LOGIC_PATH}.\n"
            f"Run from a full checkout of the repository (the vendor/report/ directory must be present).\n"
        )
        sys.exit(1)


NO_HARD_LINKS_MODE = ARGS.no_hard_links_mode
NO_HARD_LINKS_CATEGORIES = [c.strip().lower() for c in ARGS.no_hard_links_categories.split(',') if c.strip()] if ARGS.no_hard_links_categories else []
EXTERNAL_MEDIA_PATHS = smart_split_paths(ARGS.external_media_paths) if ARGS.external_media_paths else []

CATEGORY_FILTER_MODE = os.environ.get("CATEGORY_FILTER_MODE", CATEGORY_FILTER_MODE)
SORT_BY = os.environ.get("SORT_BY", SORT_BY)
SORT_ORDER = os.environ.get("SORT_ORDER", SORT_ORDER)
UNRELIABLE_TRACKERS = [t.strip() for t in os.environ.get("UNRELIABLE_TRACKERS", UNRELIABLE_TRACKERS).split(",") if t.strip()]

_validate_config()

SCAN_STATS = {
    'files_scanned': 0,
    'unique_inodes': 0,
    'scan_duration': 0.0,
    'fetch_duration': 0.0,
    'group_duration': 0.0,
    'analyze_duration': 0.0
}


if not DRY_RUN and not MANUAL_MODE:
    print("\n" + "!"*80)
    print("WARNING: LIVE MODE ACTIVATED (Automatic Deletion)")
    print("!"*80)


class Table:
    @staticmethod
    def _pad(cell, width, align):
        padding = width - len(strip_colors(str(cell)))
        if align == 'r':
            return f" {' ' * padding}{cell} │"
        return f" {cell}{' ' * padding} │"

    @staticmethod
    def render(headers, rows, col_widths, aligns=None):
        aligns = aligns or ['l'] * len(headers)
        top = "┌" + "┬".join("─" * (w + 2) for w in col_widths) + "┐"
        sep = "├" + "┼".join("─" * (w + 2) for w in col_widths) + "┤"
        bottom = "└" + "┴".join("─" * (w + 2) for w in col_widths) + "┘"
        print(top)
        print("│" + "".join(Table._pad(h, w, a) for h, w, a in zip(headers, col_widths, aligns)))
        print(sep)
        for idx, row in enumerate(rows):
            print("│" + "".join(Table._pad(c, w, a) for c, w, a in zip(row, col_widths, aligns)))
            if idx < len(rows) - 1:
                print(sep)
        print(bottom)


class QBittorrentClient:
    def __init__(self, host, username, password, api_key=None):
        self.host = host.rstrip('/')
        self.cookie = None
        self.api_key = (api_key or "").strip() or None
        if self.api_key:
            self._verify_api_key()
        else:
            self.login(username, password)

    def _verify_api_key(self):
        # API keys can't hit auth/login; probe a lightweight endpoint to fail fast.
        if self._request('app/webapiVersion') is None:
            raise Exception(
                "API key authentication failed (check the key and that "
                "qBittorrent is v5.2.0+ / WebAPI v2.14.1+)"
            )

    def login(self, username, password):
        url = f"{self.host}/api/v2/auth/login"
        data = urllib.parse.urlencode({'username': username, 'password': password}).encode()
        request = urllib.request.Request(url, data=data)
        try:
            response = urllib.request.urlopen(request)
            cookie_header = response.headers.get('Set-Cookie')
            if cookie_header:
                self.cookie = cookie_header.split(';')[0]
            else:
                raise Exception("Failed to login")
        except Exception as e:
            raise Exception(f"Connection failed: {e}")

    def _request(self, endpoint, params=None, data=None):
        url = f"{self.host}/api/v2/{endpoint}"
        if params:
            url += '?' + urllib.parse.urlencode(params)
        request = urllib.request.Request(url)
        if self.api_key:
            request.add_header('Authorization', f'Bearer {self.api_key}')
        elif self.cookie:
            request.add_header('Cookie', self.cookie)
        if data:
            data = urllib.parse.urlencode(data).encode()
            request.data = data
        try:
            response = urllib.request.urlopen(request)
            content = response.read().decode('utf-8')
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                return content
        except (urllib.error.URLError, urllib.error.HTTPError, ConnectionError, TimeoutError) as e:
            debug_log(f"[HTTP] {endpoint} failed: {e}")
            return None

    def get_torrents(self):
        return self._request('torrents/info') or []

    def get_torrent_trackers(self, torrent_hash):
        return self._request('torrents/trackers', params={'hash': torrent_hash}) or []

    def delete_torrents(self, hashes, delete_files=True):
        if DRY_RUN:
            return "dry_run"
        return self._request('torrents/delete', data={'hashes': '|'.join(hashes), 'deleteFiles': 'true' if delete_files else 'false'})

def apply_path_mapping(remote_path):
    """Apply path mappings from qBittorrent remote paths to local paths"""
    if not remote_path:
        return ""

    for remote_prefix in _SORTED_PATH_MAPPING_PREFIXES:
        if not remote_path.startswith(remote_prefix):
            continue
        # Require a path boundary so '/data' doesn't match '/database'. Accept
        # both separators since the remote may be Windows while local is POSIX.
        rest = remote_path[len(remote_prefix):]
        if rest and rest[0] not in ('/', '\\') and remote_prefix[-1] not in ('/', '\\'):
            continue
        local_prefix = PATH_MAPPINGS[remote_prefix]
        local_path = os.path.normpath(remote_path.replace(remote_prefix, local_prefix, 1))
        debug_log(f"[GROUP]   > Mapping: '{remote_path}' -> '{local_path}'")
        return local_path

    debug_log(f"[GROUP]   > Mapping: No match. Using '{remote_path}'")
    return os.path.normpath(remote_path)


def get_representative_inode(path):
    """
    Recursively find the largest non-metadata file.
    """
    if os.path.isfile(path):
        try:
            stat = os.stat(path)
            inode = (stat.st_dev, stat.st_ino)
            debug_log(f"[GROUP]   > Inode: File found ({stat.st_size} bytes) -> {inode}")
            return inode
        except OSError as e:
            debug_log(f"[GROUP]   > Inode: Error stating file: {e}")
            return None

    largest_file = None
    largest_stat = None
    max_size = -1

    try:
        for root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if d.lower() not in ('sample', 'proof', 'screens')]
            for f in files:
                if 'sample' in f.lower() and f.lower().endswith(('.mkv', '.mp4', '.avi')): continue
                if f.lower().endswith(('.nfo', '.txt', '.jpg', '.png', '.jpeg', '.sfv', '.srr')): continue

                f_path = os.path.join(root, f)
                try:
                    stat = os.stat(f_path)  # follows symlinks; raises OSError on broken links
                    size = stat.st_size

                    if size > max_size:
                        max_size = size
                        largest_file = f_path
                        largest_stat = stat
                    elif size == max_size and largest_file:
                        if os.path.basename(f_path) < os.path.basename(largest_file):
                            largest_file = f_path
                            largest_stat = stat
                except OSError: continue
    except Exception as e:
        debug_log(f"  > Inode: Error walking dir: {e}")

    if largest_stat is not None:
        inode = (largest_stat.st_dev, largest_stat.st_ino)
        debug_log(f"[GROUP]   > Inode: Winner '{os.path.basename(largest_file)}' ({max_size} bytes) -> {inode}")
        return inode

    try:
        stat = os.stat(path)
        inode = (stat.st_dev, stat.st_ino)
        return inode
    except Exception as e:
        return None



def _parse_inode_identity(identity):
    """Parse an "inode:{dev}:{ino}" identity string; return (dev, ino) or None."""
    if not identity.startswith("inode:"):
        return None
    try:
        parts = identity.split(":")
        return (int(parts[1]), int(parts[2]))
    except (ValueError, IndexError):
        return None


def get_path_identity(torrent):
    """Generate identity string for grouping torrents by inode match"""
    remote_content_path = torrent.get('content_path', '')
    name = torrent.get('name', 'unknown')
    size = torrent.get('size', 0)


    local_content_path = apply_path_mapping(remote_content_path)

    inode_tuple = get_representative_inode(local_content_path)

    if inode_tuple:
        identity = f"inode:{inode_tuple[0]}:{inode_tuple[1]}"
        debug_log(f"[GROUP] '{name}' -> {identity} (Inode Found)")
        return identity

    identity = f"heuristic:{size}:{name}"
    debug_log(f"[GROUP] '{name}' -> {identity} (Heuristic/No File)")
    return identity


def _compile_specs(patterns, label, lower=False):
    """Build a list of (compiled_regex_or_None, literal_or_None) from raw r:…/exact patterns.

    lower=True makes matching case-insensitive — used for tracker domains, which
    are normalized to lowercase before comparison (DNS is case-insensitive).
    """
    specs = []
    for p in patterns:
        if p.startswith("r:"):
            try:
                specs.append((re.compile(p[2:], re.IGNORECASE if lower else 0), None))
            except re.error as e:
                sys.stderr.write(f"ERROR: invalid regex in {label}: {p!r} ({e})\n")
                sys.exit(1)
        else:
            specs.append((None, p.lower() if lower else p))
    return specs


def matches_pattern(text, spec):
    regex, literal = spec
    if regex is not None:
        return bool(regex.fullmatch(text))
    return text == literal


_CATEGORY_ALLOWLIST_SPECS = _compile_specs(CATEGORY_ALLOWLIST, "CATEGORY_ALLOWLIST")
_CATEGORY_BLOCKLIST_SPECS = _compile_specs(CATEGORY_BLOCKLIST, "CATEGORY_BLOCKLIST")
_UNRELIABLE_TRACKERS_SPECS = _compile_specs(UNRELIABLE_TRACKERS, "UNRELIABLE_TRACKERS", lower=True)
_NO_HARD_LINKS_CATEGORY_SPECS = _compile_specs(NO_HARD_LINKS_CATEGORIES, "NO_HARD_LINKS_CATEGORIES")
_CATEGORY_FILTER_MODE_LC = CATEGORY_FILTER_MODE.lower()

@lru_cache(maxsize=1024)
def _domain_from_tracker_url(url):
    """Normalize a tracker URL to a display domain, or return None."""
    if not url or '://' not in url or url.startswith('**'):
        return None
    host = urllib.parse.urlparse(url).hostname
    if not host:
        return None
    # Strip only leading 'www.'/'tracker.' labels (not substrings, so
    # 'my-tracker.org' is left intact).
    changed = True
    while changed:
        changed = False
        for prefix in ('www.', 'tracker.'):
            if host.startswith(prefix):
                host = host[len(prefix):]
                changed = True
    return host


def get_tracker_domain(client, torrent):
    """Get the primary tracker domain for a torrent.

    Prefers the URL already on the torrent dict (populated by /torrents/info).
    Falls back to a per-hash /torrents/trackers lookup only when that field
    is empty — e.g. immediately after add, before first announce.
    """
    domain = _domain_from_tracker_url(torrent.get('tracker', ''))
    if domain:
        return domain
    try:
        trackers = client.get_torrent_trackers(torrent['hash'])
        for tracker in trackers:
            domain = _domain_from_tracker_url(tracker.get('url', ''))
            if domain:
                return domain
    except (urllib.error.URLError, AttributeError, KeyError) as e:
        debug_log(f"[TRACKER] lookup failed for {torrent.get('hash')}: {e}")
    return None

def is_unreliable_tracker(tracker_domain):
    """Check if tracker is unreliable (misreports seeders as peers)"""
    if not tracker_domain or not UNRELIABLE_TRACKERS:
        return False

    for spec in _UNRELIABLE_TRACKERS_SPECS:
        if matches_pattern(tracker_domain, spec):
            debug_log(f"[FETCH] Tracker '{tracker_domain}' matches unreliable pattern")
            return True
    return False

def get_seeder_count(client, torrent):

    tracker_domain = get_tracker_domain(client, torrent)
    torrent['_tracker_domain'] = tracker_domain
    num_complete = torrent.get('num_complete', 0)
    num_incomplete = torrent.get('num_incomplete', 0)
    name = torrent.get('name', 'Unknown')

    if is_unreliable_tracker(tracker_domain):
        total = num_complete + num_incomplete
        debug_log(f"[FETCH] '{name}' on {tracker_domain}: Unreliable -> {num_complete} + {num_incomplete} = {total} Seeders")
        return total
    else:
        debug_log(f"[FETCH] '{name}' on {tracker_domain}: Reliable -> {num_complete} Seeders")
        return num_complete

def _fetch_and_filter_torrents(client):
    """Fetch torrents. Category filtering is done per-group in evaluate_group()."""
    t_start = datetime.now()
    print(f"{Colors.BOLD}[1/6]{Colors.END} Fetching torrents...")

    result = {}
    def _worker():
        result['torrents'] = client.get_torrents()
    thr = threading.Thread(target=_worker, daemon=True)
    thr.start()

    if not DEBUG_MODE:
        while thr.is_alive():
            elapsed = (datetime.now() - t_start).total_seconds()
            sys.stdout.write(f"\r{Colors.DIM}  ... Fetching torrents... {elapsed:.1f}s elapsed{Colors.END}")
            sys.stdout.flush()
            thr.join(timeout=0.3)
        _clear_progress_line()
    else:
        thr.join()

    torrents = result.get('torrents', [])

    SCAN_STATS['fetch_duration'] = (datetime.now() - t_start).total_seconds()
    print(f"{Colors.GREEN}  ✓ Fetching complete in {SCAN_STATS['fetch_duration']:.2f}s.{Colors.END}")
    print(f"{Colors.GREEN}  ✓ Found {len(torrents)} torrents.{Colors.END}\n")
    return torrents


def _scan_external_libs_phase():
    """Scan configured external paths; return {(dev,ino): path} dict."""
    t_start = datetime.now()
    external_inodes = {}
    msg = "Scanning external libraries..." if EXTERNAL_MEDIA_PATHS else "Skipping external libraries scan (Not Configured)..."
    print(f"{Colors.BOLD}[2/6]{Colors.END} {msg}")
    if EXTERNAL_MEDIA_PATHS:
        external_inodes = scan_external_libraries(EXTERNAL_MEDIA_PATHS)
    SCAN_STATS['scan_duration'] = (datetime.now() - t_start).total_seconds()
    return external_inodes


def _fetch_seeders_phase(client, torrents):
    """Populate _seeder_count on each torrent."""
    t_start = datetime.now()
    print(f"{Colors.BOLD}[3/6]{Colors.END} Fetching seeders...")

    total = len(torrents)
    for idx, t in enumerate(torrents, 1):
        if not DEBUG_MODE and idx % 50 == 0:
            sys.stdout.write(f"\r{Colors.DIM}  ... Fetched {idx}/{total} seeder counts...{Colors.END}")
            sys.stdout.flush()
        t['_seeder_count'] = get_seeder_count(client, t)

    if not DEBUG_MODE:
        _clear_progress_line()

    SCAN_STATS['meta_duration'] = (datetime.now() - t_start).total_seconds()
    print(f"{Colors.GREEN}  ✓ Metadata processed in {SCAN_STATS['meta_duration']:.2f}s.{Colors.END}\n")


def load_and_group_torrents(client):
    torrents = _fetch_and_filter_torrents(client)
    external_inodes = _scan_external_libs_phase()
    _fetch_seeders_phase(client, torrents)



    t_start = datetime.now()
    print(f"{Colors.BOLD}[4/6]{Colors.END} Grouping torrents by matching inodes...")

    debug_log(f"[GROUP] Starting identity check for {len(torrents)} torrents...")

    identity_groups = defaultdict(list)
    total_to_group = len(torrents)

    for idx, t in enumerate(torrents, 1):

        if not DEBUG_MODE and idx % 100 == 0:
            sys.stdout.write(f"\r{Colors.DIM}  ... Processed {idx}/{total_to_group} torrents...{Colors.END}")
            sys.stdout.flush()

        identity = get_path_identity(t)
        identity_groups[identity].append(t)

    if not DEBUG_MODE:
        _clear_progress_line()

    debug_log(f"[GROUP] Processing complete. Created {len(identity_groups)} unique identity groups.")

    final_groups = {}
    skipped_singles = 0
    protected_by_external = 0

    for identity, group in identity_groups.items():
        is_external_linked = False
        matched_path = None

        pair = _parse_inode_identity(identity) if external_inodes else None
        if pair and pair in external_inodes:
            candidate_path = external_inodes[pair]

            is_self_match = False
            candidate_norm = os.path.normpath(candidate_path)

            for t in group:
                local_t_path = apply_path_mapping(t.get('content_path', ''))
                local_norm = os.path.normpath(local_t_path)

                if candidate_norm == local_norm:
                    is_self_match = True
                    break

                if candidate_norm.startswith(local_norm + os.sep):
                    is_self_match = True
                    break

            if not is_self_match:
                is_external_linked = True
                matched_path = candidate_path
            else:
                debug_log(f"[GROUP]   > Ignoring External Match (Self-Reference): {candidate_path}")

        for t in group:
            t['_external_hardlink'] = is_external_linked
            t['_external_path'] = matched_path

        if is_external_linked:
            protected_by_external += 1

        if len(group) < 2:
            skipped_singles += 1
            s_name = group[0].get('name', 'Unknown')

            if is_external_linked:
                 debug_log(f"[GROUP] Singleton '{s_name}' ({identity}) matches external library (Ignored as singleton)")
            else:
                 debug_log(f"[GROUP] Skipping singleton: '{s_name}' ({identity})")
            continue

        group.sort(key=lambda t: t.get('added_on', 0))
        original = group[0]
        crossseeds = group[1:]

        total_items = 1 + len(crossseeds) + int(is_external_linked)
        ext_msg = " + 1 external library" if is_external_linked else ""

        debug_log(f"[GROUP] > Group {identity}: {total_items} items (1 original + {len(crossseeds)} cross-seeds{ext_msg})")
        debug_log(f"[GROUP]   + Original: {original.get('name')} @ {original.get('content_path')}")

        for i, xs in enumerate(crossseeds, 1):
            debug_log(f"[GROUP]   + Cross-Seed {i}: {xs.get('name')} @ {xs.get('content_path')}")

        if is_external_linked:
             debug_log(f"[GROUP]   + External Library: @ {matched_path}")

        final_groups[original['hash']] = {
            'original': original,
            'crossseeds': crossseeds,
            'name': original['name']
        }


    SCAN_STATS['group_duration'] = (datetime.now() - t_start).total_seconds()

    print(f"{Colors.GREEN}  ✓ Grouping Complete in {SCAN_STATS['group_duration']:.2f}s.{Colors.END}")
    print(f"{Colors.GREEN}  ✓ Grouped {len(final_groups)} sets (Found {len(identity_groups)} total identities).{Colors.END}")

    if external_inodes:
        print(f"{Colors.GREEN}  ✓ {protected_by_external} groups matched external hardlinks.{Colors.END}\n")

    debug_log(f"[GROUP] Skipped {skipped_singles} singletons without cross-seeds")

    return final_groups



def category_allowed(cat):
    mode = _CATEGORY_FILTER_MODE_LC
    if mode == "none":
        return True

    if mode == "allow":
        return any(matches_pattern(cat, s) for s in _CATEGORY_ALLOWLIST_SPECS)

    if mode == "block":
        return not any(matches_pattern(cat, s) for s in _CATEGORY_BLOCKLIST_SPECS)

    if mode == "both":
        if not any(matches_pattern(cat, s) for s in _CATEGORY_ALLOWLIST_SPECS):
            return False
        if any(matches_pattern(cat, s) for s in _CATEGORY_BLOCKLIST_SPECS):
            return False
        return True

    raise AssertionError(f"unreachable CATEGORY_FILTER_MODE: {mode!r}")




def format_size_smart(size_bytes):
    if size_bytes == 0:
        return "0 B"
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    i = 0
    p = size_bytes
    while p >= 1024 and i < len(units) - 1:
        p /= 1024
        i += 1
    return f"{p:.2f} {units[i]}"


def format_duration(seconds, fmt="d:hh"):
    """Format seconds as 'd:hh', 'd:hh:mm', or 'days' (e.g. '3.5 days')."""
    if seconds <= 0:
        return {"d:hh": "0:00", "d:hh:mm": "0:00:00", "days": "0.0 days"}[fmt]
    if fmt == "days":
        return f"{seconds / 86400:.1f} days"
    total_minutes, _sec = divmod(int(seconds), 60)
    h, m = divmod(total_minutes, 60)
    d, h = divmod(h, 24)
    if fmt == "d:hh":
        return f"{d}:{h:02d}"
    if fmt == "d:hh:mm":
        return f"{d}:{h:02d}:{m:02d}"
    raise ValueError(f"unknown duration fmt: {fmt!r}")


def format_timestamp(ts):
    return datetime.fromtimestamp(ts).strftime("%Y.%m.%d | %H:%M") if ts > 0 else "N/A"

def get_tracker_name(client, torrent):
    domain = get_tracker_domain(client, torrent)
    return domain[:30] if domain else "Unknown"


def evaluate_group(d):
    """Decide whether a group is eligible for deletion and why not.

    Returns a dict with:
      - eligible: bool
      - reasons: list[str] of semantic codes (EXTERNAL_LINK, PATH_ERROR,
        LOW_SEEDS, SMALL_SIZE, LOW_TIME, TOO_MANY, CATEGORY_FILTER)
      - all_torrents: torrents treated as the eligibility unit (what gets
        deleted if eligible)
      - externally_linked: bool
    """
    orig = d['original']
    xs = d.get('crossseeds', [])

    if NO_HARD_LINKS_MODE:
        all_t = [orig]
        externally_linked = bool(orig.get('_external_hardlink'))
        seeds_ok = orig.get('_seeder_count', 0) >= MIN_SEEDERS
        path_ok = not orig.get('_path_error')
        count_ok = True
    else:
        all_t = [orig] + xs
        externally_linked = any(t.get('_external_hardlink') for t in all_t)
        seeds_ok = all(t.get('_seeder_count', 0) >= MIN_SEEDERS for t in all_t)
        path_ok = True
        count_ok = len(all_t) < MAX_TORRENTS_IN_GROUP

    size_ok = orig.get('size', 0) >= MIN_SIZE_BYTES
    time_ok = orig.get('seeding_time', 0) >= MIN_ORIGINAL_SEED_TIME_SECONDS
    cat_ok = all(category_allowed(c) for c in {t.get('category', '') for t in all_t})

    reasons = []
    if externally_linked: reasons.append("EXTERNAL_LINK")
    if NO_HARD_LINKS_MODE and not path_ok: reasons.append("PATH_ERROR")
    if not seeds_ok: reasons.append("LOW_SEEDS")
    if not size_ok: reasons.append("SMALL_SIZE")
    if not time_ok: reasons.append("LOW_TIME")
    if not count_ok: reasons.append("TOO_MANY")
    if not cat_ok: reasons.append("CATEGORY_FILTER")

    return {
        "eligible": not reasons,
        "reasons": reasons,
        "all_torrents": all_t,
        "externally_linked": externally_linked,
    }


def _reason_text(code):
    if code == "EXTERNAL_LINK": return "Hardlinked to external library"
    if code == "PATH_ERROR": return "Path error — could not verify hardlinks"
    if code == "LOW_SEEDS": return f"Low seeder count (< {MIN_SEEDERS})"
    if code == "SMALL_SIZE": return f"Small size (< {MIN_SIZE_GIB} GiB)"
    if code == "LOW_TIME": return f"Short seed time (< {MIN_ORIGINAL_SEED_TIME_DAYS} days)"
    if code == "TOO_MANY": return f"Large group (≥ {MAX_TORRENTS_IN_GROUP} items)"
    if code == "CATEGORY_FILTER": return "Category not in cleanup allowlist"
    return code


_REASON_CLI_COLOR = {
    "EXTERNAL_LINK": Colors.BLUE,
    "PATH_ERROR": Colors.RED,
    "LOW_SEEDS": Colors.RED,
    "SMALL_SIZE": Colors.RED,
    "LOW_TIME": Colors.RED,
    "TOO_MANY": Colors.ORANGE,
    "CATEGORY_FILTER": Colors.ORANGE,
}

_REASON_HTML_ICON = {
    "EXTERNAL_LINK": "🔗",
    "PATH_ERROR": "⚠️",
    "LOW_SEEDS": "🌱",
    "SMALL_SIZE": "💾",
    "LOW_TIME": "⏳",
    "TOO_MANY": "📦",
    "CATEGORY_FILTER": "🏷️",
}

_SORT_KEY_MAP = {
    'seeds': '_seeder_count',
    'seeders': '_seeder_count',
    'ratio': 'ratio',
    'size': 'size',
    'uploaded': 'uploaded',
    'added': 'added_on',
    'name': 'name',
    'time': 'seeding_time',
}

_SORT_COL_MAP = {
    "seeders": 2, "seeds": 2,
    "ratio": 3,
    "size": 4,
    "uploaded": 5,
    "time": 6,
    "added": 7,
    "name": 10,
}

def _torrent_sort_key(torrent, by):
    field = _SORT_KEY_MAP[by]
    if field == 'name':
        return torrent.get('name', '').lower()
    return torrent.get(field, 0)

def sort_torrents(original, crossseeds, by, order):
    rev = (order == "desc")
    return [original] + sorted(crossseeds, key=lambda t: _torrent_sort_key(t, by), reverse=rev)

def _print_centered_banner(title, w=262):
    inner = w - 2 - len(title)
    left = inner // 2
    right = inner - left
    print(f"\n{Colors.BOLD}{Colors.CYAN}{'═' * w}{Colors.END}")
    print(f"{Colors.BOLD}{Colors.CYAN}║{' ' * left}{title}{' ' * right}║{Colors.END}")
    print(f"{Colors.BOLD}{Colors.CYAN}{'═' * w}{Colors.END}\n")


def print_header():
    _print_centered_banner("CROSS-SEED CLEANER v2026.05.28")


def _mode_label_and_color():
    if MANUAL_MODE:
        return ("MANUAL MODE (DRY RUN)", Colors.BLUE) if DRY_RUN else ("MANUAL MODE (LIVE - WILL DELETE)", Colors.RED)
    return ("DRY RUN (SAFE)", Colors.GREEN) if DRY_RUN else ("LIVE MODE - WILL DELETE!", Colors.RED)


def print_config():
    mode_text, mode_color = _mode_label_and_color()

    unreliable_str = ', '.join(UNRELIABLE_TRACKERS) if UNRELIABLE_TRACKERS else 'None'
    no_hard_links_cat = ', '.join(NO_HARD_LINKS_CATEGORIES) if NO_HARD_LINKS_CATEGORIES else 'None'
    cat_allow_str = ', '.join(CATEGORY_ALLOWLIST) if CATEGORY_ALLOWLIST else 'None'
    cat_block_str = ', '.join(CATEGORY_BLOCKLIST) if CATEGORY_BLOCKLIST else 'None'

    def c(val):
        s = str(val)
        if s == "True": return f"{Colors.GREEN}True{Colors.END}"
        if s == "False": return f"{Colors.RED}False{Colors.END}"
        if s == "Disabled": return f"{Colors.RED}Disabled{Colors.END}"
        if s == "None": return f"{Colors.DIM}None{Colors.END}"
        return s

    auth_method = "API Key" if (QBITTORRENT_API_KEY or "").strip() else "Username/Password"

    rows = [
        [bold("Execution Mode"), f"{mode_color}{mode_text}{Colors.END}"],
        [bold("Auth Method"), auth_method],
        [bold("Min Seeders"), str(MIN_SEEDERS)],
        [bold("Min Seed Time"), f"{MIN_ORIGINAL_SEED_TIME_DAYS} days"],
        [bold("Min Size"), f"{MIN_SIZE_GIB} GiB" + (" (no limit)" if MIN_SIZE_GIB == 0 else "")],
        [bold("Max Group Size"), str(MAX_TORRENTS_IN_GROUP)],
        [bold("Category Mode"), CATEGORY_FILTER_MODE],
        [bold("Cat Allowlist"), cat_allow_str],
        [bold("Cat Blocklist"), cat_block_str],
        [bold("Unreliable Trackers"), unreliable_str],
        [bold("Dry Run"), c(DRY_RUN)],
        [bold("Debug Mode"), c(DEBUG_MODE)],
        [bold("No Hard Links Mode"), c(NO_HARD_LINKS_MODE)],
        [bold("No Hard Links Cat"), no_hard_links_cat if NO_HARD_LINKS_CATEGORIES else c("None")],
        [bold("HTML Export"), c(HTML_EXPORT or "Disabled")],
        [bold("CSV Export"), c(CSV_EXPORT or "Disabled")],
    ]

    if EXTERNAL_MEDIA_PATHS:
        first = True
        for path in EXTERNAL_MEDIA_PATHS:
            while len(path) > 118:
                chunk = path[:118]
                path = path[118:]
                label = bold("External Media Paths") if first else ""
                rows.append([label, chunk])
                first = False

            if path:
                label = bold("External Media Paths") if first else ""
                rows.append([label, path])
                first = False
    else:
        rows.append([bold("External Media Paths"), c("None")])


    if PATH_MAPPINGS:
        first = True
        for k, v in PATH_MAPPINGS.items():
            label = bold("Path Mappings") if first else ""
            rows.append([label, f"{k} -> {v}"])
            first = False
    else:
        rows.append([bold("Path Mappings"), c("None")])

    print(f"\n{Colors.BOLD}CONFIGURATION:{Colors.END}")
    Table.render([f"{Colors.BOLD}Setting{Colors.END}", f"{Colors.BOLD}Value{Colors.END}"], rows, [25, 120])
    print()



def print_group(client, d, num, total):
    orig = d['original']
    xs = d['crossseeds']
    if '_evaluation' not in d:
        d['_evaluation'] = evaluate_group(d)
    result = d['_evaluation']
    eligible = result['eligible']
    all_t = result['all_torrents']
    is_externally_linked = result['externally_linked']

    if not eligible:
        return eligible, all_t

    print(f"\n{Colors.BOLD}{Colors.BLUE}{'─' * 262}{Colors.END}")
    print(f"{Colors.BOLD}Group {num}/{total}: "
          f"{Colors.CYAN}{orig.get('name')[:140]}{Colors.END} "
          f"({Colors.GREEN}✓ ELIGIBLE{Colors.END})")

    headers = ["Type", "Seeds", "Ratio", "Size", "Uploaded", "Seeded (D:H)", "Added", "Tracker", "Category", "Name"]
    widths  = [    13,       6,       6,     10,         11,             13,      18,        30,         20,    105]
    aligns  = [   'l',     'r',     'r',    'r',        'r',            'r',     'l',       'l',        'l',    'l']
    rows = []

    for t in sort_torrents(orig, xs, SORT_BY, SORT_ORDER):
        if '_tracker_cache' not in t:
            t['_tracker_cache'] = get_tracker_name(client, t)
        is_orig = (t == orig)
        seeders = t.get('_seeder_count', 0)
        size = t.get('size', 0)
        seed_time = t.get('seeding_time', 0)
        c_seeds = Colors.GREEN if seeders >= MIN_SEEDERS else Colors.RED
        c_size = Colors.END; c_time = Colors.END
        c_cat = Colors.GREEN if category_allowed(t.get('category', '')) else Colors.RED

        if is_orig:
            c_size = Colors.GREEN if size >= MIN_SIZE_BYTES else Colors.RED
            c_time = Colors.GREEN if seed_time >= MIN_ORIGINAL_SEED_TIME_SECONDS else Colors.RED
            if NO_HARD_LINKS_MODE and t.get('_path_error'):
                c_cat = Colors.RED

        name_str = t.get('name', '')[:105]

        if is_orig:
            type_label = f"{Colors.BOLD}[ORPHAN]{Colors.END}" if NO_HARD_LINKS_MODE else f"{Colors.BOLD}[ORIGINAL]{Colors.END}"
        else:
            type_label = f"{Colors.DIM}[CROSS]{Colors.END}"
        rows.append([
            type_label,
            f"{c_seeds}{seeders}{Colors.END}",
            f"{t.get('ratio', 0.0):.2f}",
            f"{c_size}{format_size_smart(size)}{Colors.END}",
            format_size_smart(t.get('uploaded', 0)),
            f"{c_time}{format_duration(seed_time)}{Colors.END}",
            format_timestamp(t.get('added_on', 0)),
            t['_tracker_cache'][:30],
            f"{c_cat}{t.get('category', '')[:20]}{Colors.END}",
            name_str
        ])

    if is_externally_linked:
        rows.append([
            f"{Colors.BLUE}{Colors.BOLD}[LIBRARY]{Colors.END}",
            f"{Colors.DIM}-{Colors.END}",
            f"{Colors.DIM}-{Colors.END}",
            f"{Colors.BOLD}{format_size_smart(orig.get('size', 0))}{Colors.END}",
            f"{Colors.DIM}-{Colors.END}",
            f"{Colors.DIM}-{Colors.END}",
            f"{Colors.DIM}-{Colors.END}",
            f"{Colors.DIM}-{Colors.END}",
            f"{Colors.DIM}-{Colors.END}",
            f"{Colors.BOLD}{orig.get('name', '')[:105]}{Colors.END}"
        ])

    Table.render(headers, rows, widths, aligns)
    return eligible, all_t

def calculate_stats(all_groups, eligible_map):
    s = defaultdict(int)
    s['groups_total'] = len(all_groups)
    for h, g in all_groups.items():
        s['size_total'] += g['original'].get('size', 0)
        s['torrents_orig'] += 1
        s['torrents_xs'] += len(g['crossseeds'])
    s['groups_del'] = len(eligible_map)
    for idx, ts in eligible_map.items():
        s['torrents_del'] += len(ts)
        if ts: s['size_del'] += ts[0].get('size', 0)
    s['groups_keep'] = s['groups_total'] - s['groups_del']
    s['torrents_total'] = s['torrents_orig'] + s['torrents_xs']
    s['torrents_keep'] = s['torrents_total'] - s['torrents_del']
    s['size_keep'] = s['size_total'] - s['size_del']
    return s

def print_summary(s):
    mode_text, mode_color = _mode_label_and_color()

    p_del = (s['size_del'] / s['size_total'] * 100) if s['size_total'] > 0 else 0
    p_keep = 100 - p_del

    rows = [
        [bold("Execution Mode"), f"{mode_color}{mode_text}{Colors.END}"],
        [bold("Groups Analyzed"), str(s['groups_total'])],
        [bold("Groups Eligible for Deletion"), f"{Colors.RED}{s['groups_del']}{Colors.END}"],
        [bold("Groups to Keep"), f"{Colors.GREEN}{s['groups_keep']}{Colors.END}"],
        [bold("Torrents Analyzed"), f"{s['torrents_total']} ({s['torrents_orig']} originals + {s['torrents_xs']} cross-seeds)"],
        [bold("Torrents to Delete"), f"{Colors.RED}{s['torrents_del']}{Colors.END}"],
        [bold("Torrents to Keep"), f"{Colors.GREEN}{s['torrents_keep']}{Colors.END}"],
        [bold("Total Size Analyzed"), format_size_smart(s['size_total'])],
        [bold("Size to Delete"), f"{Colors.RED}{format_size_smart(s['size_del'])} ({p_del:.1f}%){Colors.END}"],
        [bold("Size to Keep"), f"{Colors.GREEN}{format_size_smart(s['size_keep'])} ({p_keep:.1f}%){Colors.END}"],
    ]

    print()
    _print_centered_banner("SUMMARY & STATISTICS")
    Table.render([f"{Colors.BOLD}Metric{Colors.END}", f"{Colors.BOLD}Value{Colors.END}"], rows, [40, 105])
    print()



def export_reports(sorted_items, eligible_ids):
    def _mono_block(lines):
        return f"<div style='margin-top:2px; font-family:monospace; font-size:10px; color:#aaa; line-height:1.2; word-break:break-all;'>{'<br>'.join(lines)}</div>"

    _h = html_escape
    _js = js_string

    initial_sort_col = _SORT_COL_MAP.get(SORT_BY, -1)
    initial_sort_dir = 1 if SORT_ORDER == "asc" else -1
    initial_sort_class = (
        ("sorted-asc" if initial_sort_dir > 0 else "sorted-desc")
        if initial_sort_col >= 0 else ""
    )

    def _sk_lower(v):
        return _h((v or '').lower())

    def _sort_attrs(status_text, type_text, seeds, ratio, size, uploaded, seeded, added, tracker, category, name, path):
        return (
            f' data-sk-0="{_sk_lower(status_text)}"'
            f' data-sk-1="{_sk_lower(type_text)}"'
            f' data-sk-2="{int(seeds or 0)}"'
            f' data-sk-3="{float(ratio or 0):.4f}"'
            f' data-sk-4="{int(size or 0)}"'
            f' data-sk-5="{int(uploaded or 0)}"'
            f' data-sk-6="{int(seeded or 0)}"'
            f' data-sk-7="{int(added or 0)}"'
            f' data-sk-8="{_sk_lower(tracker)}"'
            f' data-sk-9="{_sk_lower(category)}"'
            f' data-sk-10="{_sk_lower(name)}"'
            f' data-sk-11="{_sk_lower(path)}"'
        )

    total_groups = len(sorted_items)
    del_groups_count = len(eligible_ids)
    keep_groups_count = total_groups - del_groups_count

    unique_trackers = set()
    unique_categories = set()

    total_size = 0
    del_size = 0
    keep_size = 0

    total_torrents = 0
    del_torrents = 0
    keep_torrents = 0

    stats_analyzed = {'ratio': 0, 'time': 0, 'up': 0, 'count': 0}
    stats_eligible = {'ratio': 0, 'time': 0, 'up': 0, 'count': 0}
    tracker_stats = {}

    group_size_stats_total = defaultdict(int)
    group_size_stats_del = defaultdict(int)

    report_rows = []

    for idx, (h, d) in enumerate(sorted_items, 1):
        is_del_group = idx in eligible_ids

        g_size = d['original'].get('size', 0)
        total_size += g_size
        if is_del_group:
            del_size += g_size
        else:
            keep_size += g_size

        group_torrents = [d['original']] + d['crossseeds']
        count = len(group_torrents)

        group_size_stats_total[count] += 1
        if is_del_group:
            group_size_stats_del[count] += 1

        total_torrents += count
        if is_del_group:
            del_torrents += count
        else:
            keep_torrents += count

        for t in group_torrents:
            ratio = t.get('ratio', 0)
            time_sec = t.get('seeding_time', 0)
            up = t.get('uploaded', 0)
            t_size = t.get('size', 0)

            stats_analyzed['ratio'] += ratio
            stats_analyzed['time'] += time_sec
            stats_analyzed['up'] += up
            stats_analyzed['count'] += 1

            if is_del_group:
                stats_eligible['ratio'] += ratio
                stats_eligible['time'] += time_sec
                stats_eligible['up'] += up
                stats_eligible['count'] += 1

            domain = t.get('_tracker_domain') or "Unknown"
            unique_trackers.add(domain.lower())
            unique_categories.add((t.get('category', '') or '').lower())

            if domain not in tracker_stats:
                tracker_stats[domain] = {'total_count': 0, 'total_size': 0, 'del_count': 0, 'del_size': 0}

            tracker_stats[domain]['total_count'] += 1
            tracker_stats[domain]['total_size'] += t_size
            if is_del_group:
                tracker_stats[domain]['del_count'] += 1
                tracker_stats[domain]['del_size'] += t_size

        rejection_reasons = []
        if not is_del_group:
            if '_evaluation' not in d:
                d['_evaluation'] = evaluate_group(d)
            for code in d['_evaluation']['reasons']:
                rejection_reasons.append({'code': code, 'icon': _REASON_HTML_ICON[code], 'text': _reason_text(code)})

        report_rows.append({
            'idx': idx,
            'is_del': is_del_group,
            'reasons': rejection_reasons,
            'data': d
        })

    del_pct = (del_size / total_size * 100) if total_size > 0 else 0.0
    keep_pct = (keep_size / total_size * 100) if total_size > 0 else 0.0
    del_torrents_pct = (del_torrents / total_torrents * 100) if total_torrents > 0 else 0.0
    keep_torrents_pct = (keep_torrents / total_torrents * 100) if total_torrents > 0 else 0.0

    def get_avg(stats_dict, key):
        return stats_dict[key] / stats_dict['count'] if stats_dict['count'] > 0 else 0

    avg_all_ratio = get_avg(stats_analyzed, 'ratio')
    avg_all_time = format_duration(get_avg(stats_analyzed, 'time'), "days")
    total_all_up = format_size_smart(stats_analyzed['up'])

    avg_del_ratio = get_avg(stats_eligible, 'ratio')
    avg_del_time = format_duration(get_avg(stats_eligible, 'time'), "days")
    total_del_up = format_size_smart(stats_eligible['up'])

    sorted_trackers = sorted(tracker_stats.keys(), key=lambda k: tracker_stats[k]['total_size'], reverse=True)
    chart_labels = sorted_trackers
    ds_count_total = [tracker_stats[k]['total_count'] for k in sorted_trackers]
    ds_count_del = [tracker_stats[k]['del_count'] for k in sorted_trackers]
    ds_size_total = [tracker_stats[k]['total_size'] / (1024**3) for k in sorted_trackers]
    ds_size_del = [tracker_stats[k]['del_size'] / (1024**3) for k in sorted_trackers]

    max_group_torrent_count = max(group_size_stats_total.keys(), default=1)
    group_chart_labels = list(range(max_group_torrent_count, 0, -1))

    ds_group_total = [group_size_stats_total.get(n, 0) for n in group_chart_labels]
    ds_group_del = [group_size_stats_del.get(n, 0) for n in group_chart_labels]


    ts_now = datetime.now()
    ts_str = ts_now.strftime("%Y.%m.%d_%H.%M.%S")
    ts_display = ts_now.strftime("%Y.%m.%d %H:%M:%S")

    mode_str = "NO HARD LINKS" if NO_HARD_LINKS_MODE else "STANDARD"
    dry_run_str = "DRY RUN" if DRY_RUN else "LIVE DELETION"
    dry_run_class = "dry-run" if DRY_RUN else "live-mode"

    total_size_fmt = format_size_smart(total_size)
    del_size_fmt = format_size_smart(del_size)
    keep_size_fmt = format_size_smart(keep_size)

    grad_del = f"linear-gradient(90deg, rgba(255, 82, 82, 0.15) {del_pct}%, transparent {del_pct}%)"
    grad_keep = f"linear-gradient(90deg, rgba(76, 175, 80, 0.15) {keep_pct}%, transparent {keep_pct}%)"
    grad_torrents_del = f"linear-gradient(90deg, rgba(255, 82, 82, 0.15) {del_torrents_pct}%, transparent {del_torrents_pct}%)"
    grad_torrents_keep = f"linear-gradient(90deg, rgba(76, 175, 80, 0.15) {keep_torrents_pct}%, transparent {keep_torrents_pct}%)"
    filtering_orphans_grouping_torrents_label = "Filtering for orphans" if NO_HARD_LINKS_MODE else "Grouping torrents & hardlinks"

    unreliable_str = _h(', '.join(UNRELIABLE_TRACKERS)) if UNRELIABLE_TRACKERS else 'None'
    no_hard_links_cat = _h(', '.join(NO_HARD_LINKS_CATEGORIES)) if NO_HARD_LINKS_CATEGORIES else 'None'
    cat_allow_str = _h(', '.join(CATEGORY_ALLOWLIST)) if CATEGORY_ALLOWLIST else 'None'
    cat_block_str = _h(', '.join(CATEGORY_BLOCKLIST)) if CATEGORY_BLOCKLIST else 'None'
    html_out_str = _h(HTML_EXPORT) if HTML_EXPORT else 'Disabled'
    csv_out_str = _h(CSV_EXPORT) if CSV_EXPORT else 'Disabled'

    external_media_paths_html = _mono_block([_h(path) for path in EXTERNAL_MEDIA_PATHS]) if EXTERNAL_MEDIA_PATHS else "None"
    mappings_html = _mono_block([f"{_h(k)} → {_h(v)}" for k, v in PATH_MAPPINGS.items()]) if PATH_MAPPINGS else "None"

    config_items = [
        f"<b>Min Seeders:</b> {MIN_SEEDERS}",
        f"<b>Min Seed Time:</b> {MIN_ORIGINAL_SEED_TIME_DAYS} days",
        f"<b>Min Size:</b> {MIN_SIZE_GIB} GiB",
        f"<b>Max Group Size:</b> {MAX_TORRENTS_IN_GROUP}",
        f"<b>Category Mode:</b> {CATEGORY_FILTER_MODE}",
        f"<b>Cat Allowlist:</b> {cat_allow_str}",
        f"<b>Cat Blocklist:</b> {cat_block_str}",
        f"<b>Unreliable Trackers:</b> {unreliable_str}",
        f"<b>Dry Run:</b> {DRY_RUN}",
        f"<b>Debug Mode:</b> {DEBUG_MODE}",
        f"<b>No Hard Links Mode:</b> {NO_HARD_LINKS_MODE}",
        f"<b>No Hard Links Cat:</b> {no_hard_links_cat}",
        f"<b>HTML Export:</b> {html_out_str}",
        f"<b>CSV Export:</b> {csv_out_str}",
        f"<b>External Media Paths:</b> {external_media_paths_html}",
        f"<b>Path Mappings:</b> {mappings_html}",
    ]

    config_html = "<ul class='config-ul'>" + "".join([f"<li>{item}</li>" for item in config_items]) + "</ul>"



    css_block = """
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background: #121212; color: #e0e0e0; margin: 0; padding: 20px; }
        .container { max-width: 95%; margin: 0 auto; }
        .card { background: #1e1e1e; border: 1px solid #333; border-radius: 6px; padding: 20px; margin-bottom: 20px; }

        .header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #333; padding-bottom: 15px; margin-bottom: 20px; }
        .header h1 { margin: 0; font-size: 24px; color: #fff; display: inline-block; margin-right: 15px; }
        .header-meta { text-align: right; font-size: 12px; color: #888; }

        .badges { display: block; margin-top: 5px; }
        .tag { display: inline-block; padding: 2px 8px; border-radius: 4px; font-weight: bold; font-size: 11px; margin-right: 5px; color: #000; }
        .dry-run { background: #ff9800; }
        .live-mode { background: #ff5252; color: #fff; }
        .mode-tag { background: #2196f3; color: #fff; }

        .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin-bottom: 20px; }

        .stat-box {
            background-color: #252525;
            padding: 15px; border-radius: 4px; text-align: center; border-top: 3px solid #555;
            position: relative; overflow: hidden; z-index: 1;
            background-repeat: no-repeat;
            background-size: 100% 100%;
        }
        .stat-box.danger { border-top-color: #ff5252; }
        .stat-box.success { border-top-color: #4caf50; }

        .stat-content { position: relative; z-index: 2; }
        .stat-value { font-size: 24px; font-weight: bold; display: block; margin: 8px 0; color: #fff; }
        .stat-label { font-size: 12px; color: #aaa; text-transform: uppercase; letter-spacing: 0.5px; }
        .stat-sub { font-size: 11px; color: #ccc; display: block; margin-top: 2px; }
        .stat-pct { font-size: 11px; font-weight: bold; margin-top: 5px; display: block; }

        .config-ul {
            text-align: left; margin: 0; padding: 0 0 0 15px; color: #ddd; font-size: 11px; columns: 1;
            list-style-type: square;
        }
        .config-ul li { margin-bottom: 2px; }
        .config-ul b { color: #888; font-weight: 600; }

        .charts-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px; margin-bottom: 20px; }
        .chart-col { background: #1e1e1e; padding: 15px; border-radius: 6px; border: 1px solid #333; }
        .chart-container { position: relative; height: 350px; width: 100%; }

        .metrics-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 20px; margin-bottom: 20px; }
        .metric-col { background: #1e1e1e; padding: 20px; border-radius: 6px; border: 1px solid #333; }

        .metric-title { font-size: 16px; font-weight: bold; margin-bottom: 15px; border-bottom: 1px solid #333; padding-bottom: 10px; }
        .metric-item { display: flex; justify-content: space-between; margin-bottom: 8px; font-size: 14px; }
        .metric-val { font-weight: bold; color: #fff; }

        /* overflow-x:auto so the grid scrolls horizontally INSIDE its card
           instead of pushing the body wider than the viewport. Per CSS, setting
           overflow-x to a non-visible value computes overflow-y to auto, which
           reassigns .grid-head's sticky containing block from the viewport to
           this container — the header is no longer viewport-sticky on page
           scroll, only sticky within this container. Accepted tradeoff so the
           table respects the card's horizontal bounds on narrow screens. */
        .table-container { overflow-x: auto; }

        /* Grid-based "table": divs all the way down so off-screen groups can use
           content-visibility:auto, which is forbidden on real <tbody>/<tr>/<td>.
           Initial --cols uses max-content for the 8 narrow columns; recomputeNarrowColumns()
           in the page script resets those columns to max-content, samples natural cell
           widths from the header, filter row and visible originals, then writes fixed px
           so every row shares the same tracks. The reset step is essential — cells use
           overflow:hidden, so once --cols is px, offsetWidth returns the clipped width
           and columns can never grow to fit a new sort arrow or extra rejection icon.
           (CSS subgrid would do this declaratively but conflicts with content-visibility:auto
           — see W3C csswg-drafts#7091). .grid-report stays visibility:hidden until JS runs. */
        .grid-report {
            --cols:
                max-content max-content max-content max-content
                max-content max-content max-content max-content
                140px 130px
                minmax(200px, 1fr) minmax(220px, 2fr);
            width: 100%; min-width: 0;
            font-size: 13px;
            visibility: hidden;
        }
        .grid-report.ready { visibility: visible; }
        .grid-row    { display: grid; grid-template-columns: var(--cols); align-items: stretch; }
        .cell        { padding: 8px; border-bottom: 1px solid #2a2a2a; color: #ddd; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; display: flex; align-items: center; }
        .grid-head   { position: sticky; top: 0; z-index: 3; background: #1e1e1e; }
        .filter-bar  { padding: 10px 12px; border-bottom: 1px solid #2a2a2a; }
        .hcell {
            position: relative;
            padding: 12px 8px; background: #252525; color: #aaa; font-weight: 600;
            border-bottom: 2px solid #333; cursor: pointer; user-select: none;
            white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
            display: flex; align-items: center;
        }
        .hcell:hover { color: #fff; background: #333; }
        .hcell.sorted-asc::after, .hcell.sorted-desc::after {
            content: "";
            display: inline-block;
            width: 0;
            height: 0;
            margin-left: 6px;
            vertical-align: middle;
            border-left: 5px solid transparent;
            border-right: 5px solid transparent;
            opacity: 0.9;
        }
        .hcell.sorted-asc::after  { border-bottom: 6px solid currentColor; }
        .hcell.sorted-desc::after { border-top: 6px solid currentColor; }
        .grid-row:hover > .cell { background: #2a2a2a; }
        .group { content-visibility: auto; contain-intrinsic-size: auto 80px; }
        .group.group-even .grid-row:not(:hover) > .cell { background: #242424; }
        .group .grid-row:last-child > .cell { border-bottom: 2px solid #444; }
        .group.filtered-hidden, .grid-row.filtered-hidden { display: none; }

        .grid-filterrow .fcell {
            background: #1a1a1a;
            padding: 4px 6px;
            border-bottom: 1px solid #333;
            cursor: default;
            display: flex; align-items: center;
        }
        .filter-row input[type="number"],
        .filter-row input[type="text"],
        .filter-row .filter-multi-btn {
            width: 100%;
            box-sizing: border-box;
            background: #0f0f0f;
            color: #ddd;
            border: 1px solid #333;
            border-radius: 3px;
            padding: 3px 6px;
            font: 12px system-ui, -apple-system, sans-serif;
        }
        .filter-row input:focus, .filter-row .filter-multi-btn:focus { outline: 1px solid #4caf50; outline-offset: -1px; }
        .filter-row input[type="number"] { -moz-appearance: textfield; appearance: textfield; }
        .filter-row input[type="number"]::-webkit-inner-spin-button,
        .filter-row input[type="number"]::-webkit-outer-spin-button {
            -webkit-appearance: none; appearance: none; margin: 0;
        }
        .filter-range { display: flex; gap: 3px; }
        .filter-range input { width: 50%; padding: 3px 4px; min-width: 0; text-align: center; }
        .filter-range input::placeholder { color: #555; }
        .filter-multi-btn { text-align: left; cursor: pointer; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .filter-multi-panel {
            position: fixed;
            background: #1e1e1e;
            border: 1px solid #444;
            border-radius: 4px;
            padding: 6px;
            z-index: 100;
            min-width: 180px;
            overflow-y: auto;
            display: none;
            box-shadow: 0 4px 10px rgba(0,0,0,0.5);
        }
        .filter-multi-panel.open { display: block; }
        .filter-multi-panel label { display: block; padding: 3px 2px; cursor: pointer; color: #ddd; font: 12px system-ui, sans-serif; white-space: nowrap; }
        .filter-multi-panel label:hover { background: #2a2a2a; }
        .filter-multi-panel input[type="checkbox"] { margin-right: 6px; vertical-align: middle; }
        .filter-range-panel { min-width: 200px; padding: 8px; }
        .filter-range-panel .range-row { display: flex; align-items: center; gap: 6px; padding: 3px 0; color: #ddd; font: 12px system-ui, sans-serif; }
        .filter-range-panel .range-row input { flex: 1; min-width: 0; background: #0f0f0f; color: #ddd; border: 1px solid #333; border-radius: 3px; padding: 4px 6px; font: 12px system-ui; }
        .filter-range-panel .range-row input::-webkit-inner-spin-button,
        .filter-range-panel .range-row input::-webkit-outer-spin-button { -webkit-appearance: none; appearance: none; margin: 0; }
        .filter-range-panel .range-row input[type="number"] { -moz-appearance: textfield; appearance: textfield; }
        .range-clear-btn { margin-top: 6px; background: #333; color: #ddd; border: 1px solid #444; border-radius: 3px; padding: 3px 10px; cursor: pointer; font: 11px system-ui; }
        .range-clear-btn:hover { background: #444; }
        .range-slider-note { color: #777; font: 11px system-ui; padding: 6px 0; text-align: center; }
        /* Dual-thumb range slider: two stacked <input type=range> elements
           share one visual track. The track and selected segment are drawn
           on the wrapper using two CSS variables (--p1, --p2) updated by JS. */
        .range-slider { position: relative; height: 22px; margin: 4px 2px 8px; --p1: 0%; --p2: 100%; }
        .range-slider::before {
            content: ""; position: absolute; left: 0; right: 0; top: 50%;
            transform: translateY(-50%); height: 4px; border-radius: 2px;
            background: linear-gradient(to right,
                #333 0,           #333 var(--p1),
                #4caf50 var(--p1),#4caf50 var(--p2),
                #333 var(--p2),   #333 100%);
        }
        .range-slider input[type="range"] {
            position: absolute; left: 0; top: 0; width: 100%; height: 22px;
            margin: 0; padding: 0; background: transparent;
            -webkit-appearance: none; appearance: none; pointer-events: none;
        }
        .range-slider input[type="range"]::-webkit-slider-runnable-track { background: transparent; height: 22px; border: 0; }
        .range-slider input[type="range"]::-moz-range-track { background: transparent; height: 22px; border: 0; }
        .range-slider input[type="range"]::-webkit-slider-thumb {
            -webkit-appearance: none; appearance: none; pointer-events: auto;
            width: 14px; height: 14px; border-radius: 50%; background: #ddd;
            border: 2px solid #4caf50; cursor: pointer; margin-top: 0;
        }
        .range-slider input[type="range"]::-moz-range-thumb {
            pointer-events: auto; width: 14px; height: 14px; border-radius: 50%;
            background: #ddd; border: 2px solid #4caf50; cursor: pointer;
        }
        .filter-group-label { color: #888; font: 11px system-ui; text-transform: uppercase; letter-spacing: 0.5px; padding: 4px 2px 2px; border-top: 1px solid #333; margin-top: 4px; }
        .filter-multi-panel > .filter-group-label:first-child { border-top: 0; margin-top: 0; }
        .filter-clear-btn {
            margin-left: 8px;
            background: #333;
            color: #ddd;
            border: 1px solid #444;
            border-radius: 3px;
            padding: 2px 8px;
            cursor: pointer;
            font: 11px system-ui;
        }
        .filter-clear-btn:hover { background: #444; }
        .filter-counts { color: #aaa; font-size: 12px; margin-left: 12px; display: inline-block; vertical-align: middle; }
        .filter-counts strong { color: #ddd; font-weight: 600; }
        .empty-state { display: none; padding: 40px 20px; text-align: center; color: #888; font-size: 13px; border-top: 1px solid #2a2a2a; }
        .empty-state.shown { display: block; }
        .resizer { position: absolute; right: 0; top: 0; height: 100%; width: 5px; cursor: col-resize; user-select: none; touch-action: none; }
        .resizer:hover, .resizing { background: #bb86fc; opacity: 0.5; }
        .status-badge { padding: 3px 8px; border-radius: 3px; font-size: 11px; font-weight: bold; text-transform: uppercase; margin-right: 6px; }
        .status-badge.status-delete { background: #3e2727; color: #ff5252; }
        .status-badge.status-keep { background: #273e27; color: #4caf50; }
        .type-badge { padding: 2px 6px; border-radius: 3px; font-size: 10px; font-weight: bold; }
        .type-orig { color: #bbdefb; }
        .type-orphan { color: #fff9c4; }
        .type-cross { color: #bdbdbd; }
        .name-cell { color: #fff; }
        .path-cell { color: #666; font-family: monospace; font-size: 11px; }
        .text-danger { color: #ff5252 !important; font-weight: bold; }
        .text-success { color: #4caf50 !important; }
        .status-container { display: flex; align-items: center; }
        .rejection-icon { cursor: default; margin-left: 2px; font-size: 14px; opacity: 0.8; }
        .rejection-icon:hover { opacity: 1; }
        #rsnTip {
            position: fixed;
            pointer-events: none;
            background: rgba(0,0,0,0.85);
            color: #fff;
            font: 500 12px system-ui, -apple-system, "Segoe UI", sans-serif;
            padding: 6px 10px;
            border-radius: 6px;
            box-shadow: 0 2px 6px rgba(0,0,0,0.4);
            white-space: nowrap;
            opacity: 0;
            transition: opacity 0.1s ease;
            z-index: 9999;
            left: 0;
            top: 0;
        }
        #rsnTip.visible { opacity: 1; }
    """

    html_parts = [f"""
    <div class="container">
        <div class="header">
            <div>
                <h1>Cross-Seed Cleaner Report</h1>
                <div class="badges">
                    <span class="tag mode-tag">{mode_str}</span>
                    <span class="tag {dry_run_class}">{dry_run_str}</span>
                </div>
            </div>
            <div class="header-meta">
                Generated: {ts_display}
            </div>
        </div>

        <div class="stats-grid" style="grid-template-columns: repeat(5, 1fr);">

            <div class="stat-box">
                <div class="stat-content">
                    <span class="stat-label">Total Analyzed</span>
                    <span class="stat-value" style="font-size: 20px;">{total_torrents} / {total_groups}</span>
                    <span class="stat-sub">Torrents / Groups</span>

                    <div style="margin-top: 10px; margin-bottom: 10px; border-top: 1px solid #444;"></div>

                    <span class="stat-label">Total Size</span>
                    <span class="stat-value" style="font-size: 20px;">{total_size_fmt}</span>

                    <div style="margin-top: 10px; margin-bottom: 10px; border-top: 1px solid #444;"></div>

                    <span class="stat-label">External Scan</span>
                    <span class="stat-value" style="font-size: 16px;">{SCAN_STATS['files_scanned']} / {SCAN_STATS['unique_inodes']}</span>
                    <span class="stat-sub">Files / Unique Inodes</span>

                    <div style="margin-top: 10px; margin-bottom: 10px; border-top: 1px solid #444;"></div>

                    <span class="stat-label">Execution Times</span>
                    <div style="font-size: 11px; color: #aaa; margin-top: 4px; text-align: left; padding-left: 20px;">
                        <div>• Fetching torrents: <span style="color:#fff; float:right">{SCAN_STATS.get('fetch_duration', 0):.2f}s</span></div>
                        <div>• Scanning external libs: <span style="color:#fff; float:right">{SCAN_STATS.get('scan_duration', 0):.2f}s</span></div>
                        <div>• Fetching seeders: <span style="color:#fff; float:right">{SCAN_STATS.get('meta_duration', 0):.2f}s</span></div>
                        <div>• {filtering_orphans_grouping_torrents_label}: <span style="color:#fff; float:right">{SCAN_STATS.get('group_duration', 0):.2f}s</span></div>
                        <div>• Analyze deletable: <span style="color:#fff; float:right">{SCAN_STATS.get('analyze_duration', 0):.2f}s</span></div>
                    </div>


                </div>
            </div>



            <div class="stat-box danger" style="background-image: {grad_del};">
                <div class="stat-content">
                    <span class="stat-label">Size to Delete</span>
                    <span class="stat-value">{del_size_fmt}</span>
                    <span class="stat-sub">{del_groups_count} groups / {del_torrents} torrents</span>
                    <span class="stat-pct" style="color:#ff5252">{del_pct:.1f}% of space</span>
                </div>
            </div>

            <div class="stat-box success" style="background-image: {grad_keep};">
                <div class="stat-content">
                    <span class="stat-label">Size to Keep</span>
                    <span class="stat-value">{keep_size_fmt}</span>
                    <span class="stat-sub">{keep_groups_count} groups / {keep_torrents} torrents</span>
                    <span class="stat-pct" style="color:#4caf50">{keep_pct:.1f}% of space</span>
                </div>
            </div>

             <div class="stat-box danger" style="background-image: {grad_torrents_del};">
                <div class="stat-content">
                    <span class="stat-label">Torrents to Delete</span>
                    <span class="stat-value">{del_torrents}</span>
                    <span class="stat-sub">{del_groups_count} Groups</span>
                    <span class="stat-pct" style="color:#ff5252">{del_torrents_pct:.1f}% of torrents</span>
                </div>
            </div>

             <div class="stat-box success" style="background-image: {grad_torrents_keep};">
                <div class="stat-content">
                    <span class="stat-label">Torrents to Keep</span>
                    <span class="stat-value">{keep_torrents}</span>
                    <span class="stat-sub">{keep_groups_count} Groups</span>
                    <span class="stat-pct" style="color:#4caf50">{keep_torrents_pct:.1f}% of torrents</span>
                </div>
            </div>
        </div>

        <div class="metrics-row">
            <div class="metric-col">
                <div class="metric-title">All Analyzed ({stats_analyzed['count']})</div>
                <div class="metric-item"><span>Average Ratio</span><span class="metric-val">{avg_all_ratio:.2f}</span></div>
                <div class="metric-item"><span>Average Seed Time</span><span class="metric-val">{avg_all_time}</span></div>
                <div class="metric-item"><span>Total Uploaded</span><span class="metric-val">{total_all_up}</span></div>
            </div>

            <div class="metric-col">
                <div class="metric-title">Configuration</div>
                {config_html}
            </div>

            <div class="metric-col" style="border-color: #ff5252;">
                <div class="metric-title" style="color: #ff5252;">Deleted ({stats_eligible['count']})</div>
                <div class="metric-item"><span>Average Ratio</span><span class="metric-val">{avg_del_ratio:.2f}</span></div>
                <div class="metric-item"><span>Average Seed Time</span><span class="metric-val">{avg_del_time}</span></div>
                <div class="metric-item"><span>Total Uploaded</span><span class="metric-val">{total_del_up}</span></div>
            </div>
        </div>

        <div class="charts-row">
            <div class="chart-col">
                <div class="chart-container"><canvas id="countChart"></canvas></div>
            </div>
            <div class="chart-col">
                <div class="chart-container"><canvas id="sizeChart"></canvas></div>
            </div>
            <div class="chart-col">
                <div class="chart-container"><canvas id="groupChart"></canvas></div>
            </div>
        </div>

        <div class="card">
            <div class="table-container">
            <div id="reportTable" class="grid-report">
                <div class="grid-head">
                    <div class="filter-bar">
                        <button type="button" id="filterClearBtn" class="filter-clear-btn">Clear all filters</button>
                        <span id="filterCounts" class="filter-counts"></span>
                    </div>
                    <div class="grid-row grid-headrow">
                        <div class="hcell" data-col="0">Status<div class="resizer"></div></div>
                        <div class="hcell" data-col="1">Type<div class="resizer"></div></div>
                        <div class="hcell" data-col="2">Seeds<div class="resizer"></div></div>
                        <div class="hcell" data-col="3">Ratio<div class="resizer"></div></div>
                        <div class="hcell" data-col="4">Size<div class="resizer"></div></div>
                        <div class="hcell" data-col="5">Uploaded<div class="resizer"></div></div>
                        <div class="hcell" data-col="6">Seeded (D:H)<div class="resizer"></div></div>
                        <div class="hcell" data-col="7">Added<div class="resizer"></div></div>
                        <div class="hcell" data-col="8">Tracker<div class="resizer"></div></div>
                        <div class="hcell" data-col="9">Category<div class="resizer"></div></div>
                        <div class="hcell" data-col="10">Name<div class="resizer"></div></div>
                        <div class="hcell" data-col="11">Path<div class="resizer"></div></div>
                    </div>
                    <div class="grid-row grid-filterrow filter-row">
                        <div class="fcell" data-col="0">
                            <button type="button" class="filter-multi-btn" data-filter="status">Any ▾</button>
                            <div class="filter-multi-panel" data-filter-panel="status">
                                <div class="filter-group-label">Status</div>
                                <label><input type="checkbox" value="keep" data-filter-status>KEEP</label>
                                <label><input type="checkbox" value="delete" data-filter-status checked>DELETE</label>
                                <div class="filter-group-label">Rejection reasons</div>
                                <label><input type="checkbox" value="LOW_SEEDS" data-filter-reason>🌱 Low seeders</label>
                                <label><input type="checkbox" value="SMALL_SIZE" data-filter-reason>💾 Small size</label>
                                <label><input type="checkbox" value="LOW_TIME" data-filter-reason>⏳ Short seed time</label>
                                <label><input type="checkbox" value="TOO_MANY" data-filter-reason>📦 Large group</label>
                                <label><input type="checkbox" value="EXTERNAL_LINK" data-filter-reason>🔗 External hardlink</label>
                                <label><input type="checkbox" value="PATH_ERROR" data-filter-reason>⚠️ Path error</label>
                                <label><input type="checkbox" value="CATEGORY_FILTER" data-filter-reason>🏷️ Category filter</label>
                            </div>
                        </div>
                        <div class="fcell" data-col="1"></div>
                        <div class="fcell" data-col="2">
                            <button type="button" class="filter-multi-btn" data-filter-trigger="seeds">Any ▾</button>
                            <div class="filter-multi-panel filter-range-panel" data-range-panel="seeds" data-range-unit="">
                                <div class="filter-group-label">Seeds (worst across group)</div>
                                <div class="range-slider" data-slider-for="seeds"></div>
                                <label class="range-row">Min <input type="number" data-filter="seedsMin"></label>
                                <label class="range-row">Max <input type="number" data-filter="seedsMax"></label>
                                <button type="button" class="range-clear-btn" data-range-clear="seeds">Clear</button>
                            </div>
                        </div>
                        <div class="fcell" data-col="3">
                            <button type="button" class="filter-multi-btn" data-filter-trigger="ratio">Any ▾</button>
                            <div class="filter-multi-panel filter-range-panel" data-range-panel="ratio" data-range-unit="">
                                <div class="filter-group-label">Ratio (ORIGINAL)</div>
                                <div class="range-slider" data-slider-for="ratio"></div>
                                <label class="range-row">Min <input type="number" step="0.01" data-filter="ratioMin"></label>
                                <label class="range-row">Max <input type="number" step="0.01" data-filter="ratioMax"></label>
                                <button type="button" class="range-clear-btn" data-range-clear="ratio">Clear</button>
                            </div>
                        </div>
                        <div class="fcell" data-col="4">
                            <button type="button" class="filter-multi-btn" data-filter-trigger="size">Any ▾</button>
                            <div class="filter-multi-panel filter-range-panel" data-range-panel="size" data-range-unit="GiB">
                                <div class="filter-group-label">Size (ORIGINAL, GiB)</div>
                                <div class="range-slider" data-slider-for="size"></div>
                                <label class="range-row">Min <input type="number" step="0.1" data-filter="sizeMin"> GiB</label>
                                <label class="range-row">Max <input type="number" step="0.1" data-filter="sizeMax"> GiB</label>
                                <button type="button" class="range-clear-btn" data-range-clear="size">Clear</button>
                            </div>
                        </div>
                        <div class="fcell" data-col="5">
                            <button type="button" class="filter-multi-btn" data-filter-trigger="up">Any ▾</button>
                            <div class="filter-multi-panel filter-range-panel" data-range-panel="up" data-range-unit="GiB">
                                <div class="filter-group-label">Uploaded (ORIGINAL, GiB)</div>
                                <div class="range-slider" data-slider-for="up"></div>
                                <label class="range-row">Min <input type="number" step="0.1" data-filter="upMin"> GiB</label>
                                <label class="range-row">Max <input type="number" step="0.1" data-filter="upMax"> GiB</label>
                                <button type="button" class="range-clear-btn" data-range-clear="up">Clear</button>
                            </div>
                        </div>
                        <div class="fcell" data-col="6">
                            <button type="button" class="filter-multi-btn" data-filter-trigger="seeded">Any ▾</button>
                            <div class="filter-multi-panel filter-range-panel" data-range-panel="seeded" data-range-unit="d">
                                <div class="filter-group-label">Seeded (ORIGINAL, days)</div>
                                <div class="range-slider" data-slider-for="seeded"></div>
                                <label class="range-row">Min <input type="number" data-filter="seededMin"> d</label>
                                <label class="range-row">Max <input type="number" data-filter="seededMax"> d</label>
                                <button type="button" class="range-clear-btn" data-range-clear="seeded">Clear</button>
                            </div>
                        </div>
                        <div class="fcell" data-col="7">
                            <button type="button" class="filter-multi-btn" data-filter-trigger="added">Any ▾</button>
                            <div class="filter-multi-panel filter-range-panel" data-range-panel="added" data-range-unit="date">
                                <div class="filter-group-label">Added (ORIGINAL)</div>
                                <label class="range-row">From <input type="date" data-filter="addedFrom"></label>
                                <label class="range-row">To <input type="date" data-filter="addedTo"></label>
                                <button type="button" class="range-clear-btn" data-range-clear="added">Clear</button>
                            </div>
                        </div>
                        <div class="fcell" data-col="8">
                            <button type="button" class="filter-multi-btn" data-filter="tracker">Any ▾</button>
                            <div class="filter-multi-panel" data-filter-panel="tracker"></div>
                        </div>
                        <div class="fcell" data-col="9">
                            <button type="button" class="filter-multi-btn" data-filter="category">Any ▾</button>
                            <div class="filter-multi-panel" data-filter-panel="category"></div>
                        </div>
                        <div class="fcell" data-col="10"><input type="text" data-filter="name" placeholder="search name"></div>
                        <div class="fcell" data-col="11"><input type="text" data-filter="path" placeholder="search path"></div>
                    </div>
                </div>
                <div class="grid-body">
    """]

    if initial_sort_col >= 0 and initial_sort_class:
        html_parts[0] = html_parts[0].replace(
            f'<div class="hcell" data-col="{initial_sort_col}"',
            f'<div class="hcell {initial_sort_class}" data-col="{initial_sort_col}"',
            1,
        )

    group_idx = 0
    total_torrents = 0
    ds_seeds_min = ds_seeds_max = None
    ds_ratio_min = ds_ratio_max = None
    ds_size_min = ds_size_max = None
    ds_up_min = ds_up_max = None
    ds_seeded_min = ds_seeded_max = None
    for row in report_rows:
        d = row['data']
        is_del_group = row['is_del']
        group_class = "group group-even" if group_idx % 2 == 1 else "group"
        group_idx += 1

        status_class = "status-delete" if is_del_group else "status-keep"
        status_text = "DELETE" if is_del_group else "KEEP"
        badge_html = f'<span class="status-badge {status_class}">{status_text}</span>'

        reasons_html = ""
        if not is_del_group:
            reasons = row.get('reasons', [])
            if reasons:
                reasons_html = "".join(
                    f'<span class="rejection-icon" data-tip="{_h(r["text"])}">{_h(r["icon"])}</span>'
                    for r in reasons
                )

        status_cell_content = f'<div class="status-container">{badge_html}{reasons_html}</div>'
        torrents_to_list = [d['original']] + d['crossseeds']

        status_attr = 'delete' if is_del_group else 'keep'
        reason_codes = ' '.join(r['code'] for r in row.get('reasons', []))
        min_seeds = min(t.get('_seeder_count', 0) for t in torrents_to_list)

        # Slider bounds must reflect what the FILTER actually compares against,
        # not every individual torrent in the dataset. Otherwise an extreme
        # cross-seed value pushes the slider's bound to a number no original
        # ever reaches → boundary positions silently exclude rows.
        #   Seeds filter reads the per-group worst (data-seeds-min) → bound source = min_seeds.
        #   Ratio/Size/Uploaded/Seeded filters read getAttr(orig, N) → bound source = original only.
        orig_t = d['original']
        rv = float(orig_t.get('ratio', 0) or 0)
        zv = orig_t.get('size', 0) or 0
        uv = orig_t.get('uploaded', 0) or 0
        tv = orig_t.get('seeding_time', 0) or 0
        if ds_seeds_max  is None or min_seeds > ds_seeds_max:  ds_seeds_max  = min_seeds
        if ds_seeds_min  is None or min_seeds < ds_seeds_min:  ds_seeds_min  = min_seeds
        if ds_ratio_max  is None or rv > ds_ratio_max:         ds_ratio_max  = rv
        if ds_ratio_min  is None or rv < ds_ratio_min:         ds_ratio_min  = rv
        if ds_size_max   is None or zv > ds_size_max:          ds_size_max   = zv
        if ds_size_min   is None or zv < ds_size_min:          ds_size_min   = zv
        if ds_up_max     is None or uv > ds_up_max:            ds_up_max     = uv
        if ds_up_min     is None or uv < ds_up_min:            ds_up_min     = uv
        if ds_seeded_max is None or tv > ds_seeded_max:        ds_seeded_max = tv
        if ds_seeded_min is None or tv < ds_seeded_min:        ds_seeded_min = tv

        # Pre-compute one lowercased searchable blob per group so the JS filter
        # can do a single dataset.search.includes(q) instead of walking rows.
        # The search legitimately scans every torrent (cross-seeds may have
        # different names/paths, and the user expects search to find them).
        search_tokens = []
        for t in torrents_to_list:
            search_tokens.append(t.get('name', '').lower())
            search_tokens.append(t.get('content_path', '').lower())
            search_tokens.append((t.get('_tracker_domain') or '').lower())
            search_tokens.append((t.get('category') or '').lower())
        ext_path_for_search = d['original'].get('_external_path')
        if ext_path_for_search:
            search_tokens.append(ext_path_for_search.lower())
        search_blob = _h(' '.join(search_tokens))

        row_count = len(torrents_to_list) + (1 if ext_path_for_search else 0)
        total_torrents += row_count

        html_parts.append(
            f'<div class="{group_class}" data-status="{status_attr}" '
            f'data-reasons="{_h(reason_codes)}" data-seeds-min="{min_seeds}" '
            f'data-row-count="{row_count}" data-search="{search_blob}">'
        )

        for i, t in enumerate(torrents_to_list):
            is_orig = (i == 0)
            if is_orig:
                type_badge = (
                    '<span class="type-badge type-orphan" style="border:1px solid #555;">ORPHAN</span>'
                    if len(d['crossseeds']) == 0
                    else '<span class="type-badge type-orig" style="border:1px solid #555;">ORIGINAL</span>'
                )
            else:
                type_badge = '<span class="type-badge type-cross" style="border:1px solid #555;">CROSS</span>'

            added_ts = format_timestamp(t.get('added_on', 0))
            tracker_clean = t.get('_tracker_domain') or "Unknown"

            cur_seeds = t.get('_seeder_count', 0)

            c_seeds = "text-success" if cur_seeds >= MIN_SEEDERS else "text-danger"
            c_size = ""
            c_time = ""
            c_cat = ""
            t_size = t.get('size', 0)
            t_time = t.get('seeding_time', 0)
            t_cat = t.get('category', '')

            c_cat = "text-success" if category_allowed(t_cat) else "text-danger"

            if is_orig:
                c_size = "text-success" if t_size >= MIN_SIZE_BYTES else "text-danger"
                c_time = "text-success" if t_time >= MIN_ORIGINAL_SEED_TIME_SECONDS else "text-danger"

                if t.get('_path_error'):
                    c_cat = "text-danger"

            t_name = t.get('name', '')
            t_path = t.get('content_path', '')
            name_h = _h(t_name)
            path_h = _h(t_path)
            type_text = ('ORPHAN' if (is_orig and len(d['crossseeds']) == 0)
                         else 'ORIGINAL' if is_orig else 'CROSS')
            sk = _sort_attrs(
                status_text, type_text, cur_seeds, t.get('ratio', 0),
                t_size, t.get('uploaded', 0), t_time, t.get('added_on', 0),
                tracker_clean, t_cat, t_name, t_path,
            )
            html_parts.append(f"""
            <div class="grid-row"{sk}>
                <div class="cell">{status_cell_content}</div>
                <div class="cell">{type_badge}</div>
                <div class="cell"><span class="{c_seeds}">{cur_seeds}</span></div>
                <div class="cell">{t.get('ratio', 0):.2f}</div>
                <div class="cell"><span class="{c_size}">{format_size_smart(t_size)}</span></div>
                <div class="cell">{format_size_smart(t.get('uploaded', 0))}</div>
                <div class="cell"><span class="{c_time}">{format_duration(t_time)}</span></div>
                <div class="cell" style="font-size:11px; color:#888;">{added_ts}</div>
                <div class="cell">{_h(tracker_clean)}</div>
                <div class="cell"><span class="{c_cat}">{_h(t_cat)}</span></div>
                <div class="cell name-cell">{name_h}</div>
                <div class="cell path-cell">{path_h}</div>
            </div>
            """)

        external_path = d['original'].get('_external_path')
        if external_path:
            orig_size = d['original'].get('size', 0)
            ext_sk = _sort_attrs(
                'KEEP', 'EXT', 0, 0, orig_size, 0, 0, 0,
                '', 'external library',
                d['original'].get('name', ''), external_path,
            )

            html_parts.append(f"""
            <div class="grid-row"{ext_sk}>
                <div class="cell">{status_cell_content}</div>
                <div class="cell"><span class="type-badge" style="color:#aaa; border:1px solid #555;">EXT</span></div>
                <div class="cell" style="text-align:center; color:#555; justify-content:center;">-</div>
                <div class="cell" style="text-align:center; color:#555; justify-content:center;">-</div>
                <div class="cell"><span class="text-success">{format_size_smart(orig_size)}</span></div>
                <div class="cell" style="text-align:center; color:#555; justify-content:center;">-</div>
                <div class="cell" style="text-align:center; color:#555; justify-content:center;">-</div>
                <div class="cell" style="text-align:center; color:#555; justify-content:center;">-</div>
                <div class="cell" style="text-align:center; color:#555; justify-content:center;">-</div>
                <div class="cell"><span style="color:#2196f3;">External Library</span></div>
                <div class="cell name-cell" style="color:#2196f3; font-style:italic;">{_h(d['original'].get('name', ''))}</div>
                <div class="cell path-cell">{_h(external_path)}</div>
            </div>
            """)


        html_parts.append("</div>")

    html_parts.append("""
                </div>
                <div id="emptyState" class="empty-state">No groups match the current filters.</div>
            </div>
            </div>
        </div>
    </div>
    """)
    html_body = "".join(html_parts)

    # Coerce dataset bounds to 0 when there were no torrents at all (empty report).
    ds_seeds_min  = ds_seeds_min  or 0; ds_seeds_max  = ds_seeds_max  or 0
    ds_ratio_min  = ds_ratio_min  if ds_ratio_min  is not None else 0
    ds_ratio_max  = ds_ratio_max  if ds_ratio_max  is not None else 0
    ds_size_min   = ds_size_min   or 0; ds_size_max   = ds_size_max   or 0
    ds_up_min     = ds_up_min     or 0; ds_up_max     = ds_up_max     or 0
    ds_seeded_min = ds_seeded_min or 0; ds_seeded_max = ds_seeded_max or 0

    full_html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'">
        <title>Cross-Seed Cleaner Report</title>
        <meta charset="UTF-8">
        <script>{CHARTJS_SOURCE}</script>
        <script>{REPORT_LOGIC_SOURCE}</script>
        <style>{css_block}</style>
    </head>
    <body>
        {html_body}
        <script>
            // parseCols / colMinFromCols / numericInRange / parseSortKey /
            // compareSortKeys are provided by the inlined vendor/report/report-logic.js.
            const readCols = (el) => parseCols(getComputedStyle(el).getPropertyValue('--cols').trim());
            const groupRows = (g) => Array.from(g.children).filter(c => c.classList.contains('grid-row'));
            // Sum of track *minimums* (first px value in each --cols token)
            // written as an inline min-width on .grid-report. The grid then
            // widens exactly when the container can't fit the tracks — no
            // always-on horizontal scroll at wide viewports, and Name/Path
            // truncate via ellipsis when there's no room to grow them. Also
            // ensures the .group paint-containment box covers all tracks,
            // so data cells render at every scroll position.
            function recomputeMinWidth() {{
                const grid = document.querySelector('.grid-report');
                if (!grid) return;
                const cols = readCols(grid);
                let sum = 0;
                for (const c of cols) {{
                    const m = c.match(/(\\d+(?:\\.\\d+)?)/);
                    if (m) sum += parseFloat(m[1]);
                }}
                grid.style.minWidth = sum + 'px';
            }}
            // Content-based px widths for the 8 narrow columns. Cells use
            // overflow:hidden + nowrap, so once --cols holds px values
            // offsetWidth returns the clipped column width — not the natural
            // content width. To grow a column we must first reset it to
            // max-content; only then does offsetWidth reveal what the cell
            // actually wants. We capture user-resized px values up front so
            // the reset doesn't lose them, sample header + filter row +
            // visible originals (capped, since .group has
            // content-visibility:auto and offsetWidth queries force layout
            // of off-screen subtrees), then apply per-column max as fixed px.
            // Recomputed on every sort and filter change so the sort ::after
            // arrow and extra rejection icons always fit.
            function recomputeNarrowColumns() {{
                const table = document.querySelector('.grid-report');
                if (!table) return;
                const NARROW = 8;
                const headCells = Array.from(table.querySelectorAll('.grid-headrow > .hcell'));

                const cur = readCols(table);
                const userWidths = new Array(NARROW).fill(null);
                for (let i = 0; i < NARROW; i++) {{
                    if (headCells[i] && headCells[i].dataset.userResized === 'true') {{
                        const v = parseInt(cur[i], 10);
                        if (Number.isFinite(v)) userWidths[i] = v;
                    }}
                }}

                const measureCols = [...cur];
                for (let i = 0; i < NARROW; i++) measureCols[i] = 'max-content';
                table.style.setProperty('--cols', measureCols.join(' '));

                // The filter-row buttons are width:100% + overflow:hidden, so their
                // label ("1 selected ▾", "Any ▾", etc.) clips inside the button itself
                // and never widens the .fcell — visible on narrow viewports where the
                // surrounding tracks are tight. Same risk for any clipped descendant.
                // Temporarily strip width/overflow on the buttons so their natural
                // label width pushes the cell's max-content out.
                const relaxedBtns = table.querySelectorAll('.filter-multi-btn');
                const btnRestore = [];
                relaxedBtns.forEach(b => {{
                    btnRestore.push([b, b.style.width, b.style.overflow, b.style.maxWidth]);
                    b.style.width = 'auto';
                    b.style.maxWidth = 'none';
                    b.style.overflow = 'visible';
                }});

                // Measure header + filter row + sampled visible originals. Each grid-row
                // is its own display:grid, so per-row offsetWidth reflects that row's
                // max-content; we take the max across rows to align all rows on the widest.
                const widths = new Array(NARROW).fill(0);
                const measureRow = (row) => {{
                    if (!row) return;
                    let i = 0;
                    for (const cell of row.children) {{
                        if (i >= NARROW) break;
                        // scrollWidth catches overflow that offsetWidth (clipped) misses;
                        // offsetWidth catches padding/border that scrollWidth omits.
                        let w = Math.max(cell.offsetWidth, cell.scrollWidth);
                        if (w > widths[i]) widths[i] = w;
                        i++;
                    }}
                }};
                measureRow(table.querySelector('.grid-headrow'));
                measureRow(table.querySelector('.grid-filterrow'));
                const visibleGroups = table.querySelectorAll('.group:not(.filtered-hidden)');
                const SAMPLE_CAP = 150;
                const stride = Math.max(1, Math.ceil(visibleGroups.length / SAMPLE_CAP));
                for (let gi = 0; gi < visibleGroups.length; gi += stride) {{
                    const firstRow = visibleGroups[gi].querySelector('.grid-row');
                    measureRow(firstRow);
                }}

                btnRestore.forEach(([b, w, ov, mw]) => {{
                    b.style.width = w; b.style.overflow = ov; b.style.maxWidth = mw;
                }});

                const next = [...measureCols];
                for (let i = 0; i < NARROW; i++) {{
                    const u = userWidths[i];
                    next[i] = (u !== null && u >= widths[i] ? u : widths[i]) + 'px';
                }}
                table.style.setProperty('--cols', next.join(' '));
                recomputeMinWidth();
            }}
            recomputeNarrowColumns();
            document.querySelector('.grid-report').classList.add('ready');

            const createResizableTable = function(table) {{
                const headCells = Array.from(table.querySelectorAll('.grid-headrow > .hcell'));
                if (!headCells.length) return;
                let cols = readCols(table);
                headCells.forEach((col, idx) => {{
                    const resizer = col.querySelector('.resizer');
                    if (!resizer) return;
                    let x = 0; let w = 0; let myMin = 30;
                    const mouseDownHandler = function(e) {{
                        x = e.clientX;
                        cols = readCols(table);
                        // Read the semantic min from the pre-fr-lock token so
                        // fr-backed columns (Name/Path) surface their minmax min
                        // (200/220) instead of their current fr allocation.
                        myMin = colMinFromCols(cols, idx);
                        // Lock 1fr columns to their measured pixel width before resizing,
                        // otherwise the grid won't honor a px change next to fr units.
                        // Matches both bare "1fr" and "minmax(200px, 1fr)" tokens;
                        // \\b anchors fr as a unit so identifiers like "frozen" won't match.
                        cols = cols.map((c, i) => {{
                            if (/\\bfr\\b/.test(c)) {{
                                const measured = headCells[i].getBoundingClientRect().width;
                                return Math.round(measured) + 'px';
                            }}
                            return c;
                        }});
                        w = parseInt(cols[idx], 10) || col.getBoundingClientRect().width;
                        document.addEventListener('mousemove', mouseMoveHandler);
                        document.addEventListener('mouseup', mouseUpHandler);
                        resizer.classList.add('resizing');
                        e.stopPropagation();
                        e.preventDefault();
                    }};
                    const mouseMoveHandler = function(e) {{
                        const dx = e.clientX - x;
                        const next = Math.max(myMin, w + dx);
                        cols[idx] = next + 'px';
                        table.style.setProperty('--cols', cols.join(' '));
                        recomputeMinWidth();
                    }};
                    const mouseUpHandler = function() {{
                        document.removeEventListener('mousemove', mouseMoveHandler);
                        document.removeEventListener('mouseup', mouseUpHandler);
                        resizer.classList.remove('resizing');
                        // Mark as user-resized so later recomputeNarrowColumns()
                        // calls don't overwrite the width this user just chose.
                        headCells[idx].dataset.userResized = 'true';
                    }};
                    resizer.addEventListener('mousedown', mouseDownHandler);
                    resizer.addEventListener('click', (e) => e.stopPropagation());
                }});
            }};
            createResizableTable(document.getElementById('reportTable'));

            let sortDirection = {initial_sort_dir};
            let lastSortedCol = {initial_sort_col};

            const NUMERIC_SK = new Set([2, 3, 4, 5, 6, 7]);

            function sortTable(n) {{
                const table = document.getElementById("reportTable");
                const body = table.querySelector('.grid-body');

                if (n !== lastSortedCol) {{ sortDirection = 1; lastSortedCol = n; }}
                else {{ sortDirection *= -1; }}

                const headers = table.querySelectorAll('.grid-headrow > .hcell');
                for (let i = 0; i < headers.length; i++) {{
                    headers[i].classList.remove('sorted-asc', 'sorted-desc');
                }}
                if (headers[n]) headers[n].classList.add(sortDirection > 0 ? 'sorted-asc' : 'sorted-desc');

                const isNum = NUMERIC_SK.has(n);
                const keyAttr = 'data-sk-' + n;
                const keyOf = (row) => parseSortKey(row ? row.getAttribute(keyAttr) : null, isNum);
                const cmp = (a, b) => compareSortKeys(a, b, isNum);

                const groups = Array.from(body.getElementsByClassName("group"));
                const outerKey = new Map();
                groups.forEach(g => outerKey.set(g, keyOf(g.querySelector(':scope > .grid-row'))));

                // Detach the body during the reorder so we get one reflow at the end.
                const bodyParent = body.parentNode;
                const bodyNextSib = body.nextSibling;
                bodyParent.removeChild(body);

                groups.sort((a, b) => cmp(outerKey.get(a), outerKey.get(b)) * sortDirection);

                groups.forEach(grp => {{
                    const allRows = groupRows(grp);
                    if (allRows.length <= 2) return;
                    const original = allRows[0];
                    const trailing = [];
                    let endIdx = allRows.length;
                    while (endIdx > 1 && allRows[endIdx - 1].getAttribute('data-sk-1') === 'ext') {{
                        trailing.unshift(allRows[endIdx - 1]);
                        endIdx--;
                    }}
                    const middle = allRows.slice(1, endIdx);
                    if (middle.length > 1) {{
                        const midKey = new Map();
                        middle.forEach(r => midKey.set(r, keyOf(r)));
                        middle.sort((a, b) => cmp(midKey.get(a), midKey.get(b)) * sortDirection);
                    }}
                    const frag = document.createDocumentFragment();
                    frag.appendChild(original);
                    middle.forEach(r => frag.appendChild(r));
                    trailing.forEach(r => frag.appendChild(r));
                    grp.appendChild(frag);
                }});

                const bodyFrag = document.createDocumentFragment();
                groups.forEach(g => bodyFrag.appendChild(g));
                body.appendChild(bodyFrag);

                bodyParent.insertBefore(body, bodyNextSib);
                // The newly-sorted header gained a ::after arrow → its column
                // may need to widen; recompute so the arrow never clips. Only
                // narrow (0-7) columns have the ellipsis-or-grow tradeoff.
                if (n < 8) recomputeNarrowColumns();
            }}

            // Wire header click → sortTable via addEventListener instead of an
            // inline onclick attribute. Inline event handlers depend on the
            // global lookup chain at click time and have failed under stricter
            // CSP and on file:// origins (e.g. kio-fuse SMB mounts).
            document.querySelectorAll('.grid-headrow > .hcell').forEach((h) => {{
                h.addEventListener('click', (e) => {{
                    if (e.target.classList.contains('resizer')) return;
                    const c = parseInt(h.getAttribute('data-col'), 10);
                    if (Number.isFinite(c)) sortTable(c);
                }});
            }});

            (function initReasonTooltip() {{
                const tip = document.createElement('div');
                tip.id = 'rsnTip';
                document.body.appendChild(tip);

                const tipTextFor = (el) => {{
                    if (!el || !el.closest) return '';
                    const icon = el.closest('.rejection-icon');
                    if (icon) return icon.getAttribute('data-tip') || '';
                    const cell = el.closest('.cell');
                    if (cell && cell.previousElementSibling && cell.scrollWidth > cell.clientWidth + 1) {{
                        return cell.textContent.trim();
                    }}
                    return '';
                }};

                let tipW = 0, tipH = 0;
                const onMove = (e) => {{
                    const pad = 12;
                    let x = e.clientX + pad, y = e.clientY + pad;
                    if (x + tipW > window.innerWidth - 4)  x = e.clientX - tipW - pad;
                    if (y + tipH > window.innerHeight - 4) y = e.clientY - tipH - pad;
                    tip.style.left = x + 'px';
                    tip.style.top  = y + 'px';
                }};
                const hideTip = () => {{
                    tip.classList.remove('visible');
                    document.removeEventListener('mousemove', onMove);
                }};
                document.addEventListener('mouseover', (e) => {{
                    const text = tipTextFor(e.target);
                    if (!text) return;
                    tip.textContent = text;
                    if (!tip.classList.contains('visible')) {{
                        tip.classList.add('visible');
                        document.addEventListener('mousemove', onMove);
                    }}
                    tipW = tip.offsetWidth; tipH = tip.offsetHeight;
                }});
                document.addEventListener('mouseout', (e) => {{
                    if (!tipTextFor(e.target)) return;
                    const next = e.relatedTarget;
                    if (!next || !tipTextFor(next)) hideTip();
                }});
                window.addEventListener('scroll', hideTip, true);
            }})();

            const ctxCount = document.getElementById('countChart').getContext('2d');
            const ctxSize = document.getElementById('sizeChart').getContext('2d');
            const ctxGroup = document.getElementById('groupChart').getContext('2d');

            const labels = {_js(chart_labels)};
            const groupLabels = {_js(group_chart_labels)};

            const UNIQUE_TRACKERS = {_js(sorted(unique_trackers))};
            const UNIQUE_CATEGORIES = {_js(sorted(unique_categories))};
            const TOTAL_GROUPS = {_js(group_idx)};
            const TOTAL_TORRENTS = {_js(total_torrents)};
            // Slider bounds: [min, max, step, displayConverter] per range filter.
            // Size/Uploaded show GiB but the data-sk attrs are bytes — input values
            // and slider thumb values are in GiB (whole-units), filter converts.
            const RANGE_BOUNDS = {{
                seeds:  [{_js(ds_seeds_min)}, {_js(ds_seeds_max)}, 1],
                ratio:  [{_js(round(ds_ratio_min, 2))}, {_js(round(ds_ratio_max, 2))}, 0.01],
                size:   [{_js(round(ds_size_min  / 1073741824, 1))}, {_js(round(ds_size_max  / 1073741824, 1))}, 0.1],
                up:     [{_js(round(ds_up_min    / 1073741824, 1))}, {_js(round(ds_up_max    / 1073741824, 1))}, 0.1],
                seeded: [{_js(ds_seeded_min // 86400)}, {_js(ds_seeded_max // 86400)}, 1]
            }};

            (function initFilters() {{
                const GIB = 1073741824;
                const DAY = 86400;
                let _groupsCache = null;

                const statusSet = new Set();
                const reasonsSet = new Set();
                const trackersSet = new Set();
                const categoriesSet = new Set();

                function populateDropdown(panel, values, targetSet) {{
                    panel.innerHTML = '';
                    for (const v of values) {{
                        const label = document.createElement('label');
                        const cb = document.createElement('input');
                        cb.type = 'checkbox';
                        cb.value = v;
                        cb.addEventListener('change', () => {{
                            if (cb.checked) targetSet.add(v); else targetSet.delete(v);
                            updateBtnLabel(panel, targetSet);
                            applyFilters();
                        }});
                        label.appendChild(cb);
                        label.appendChild(document.createTextNode(' ' + (v || '(none)')));
                        panel.appendChild(label);
                    }}
                }}

                function updateBtnLabel(panel, targetSet) {{
                    const btn = panel._anchorBtn;
                    if (!btn) return;
                    if (targetSet.size === 0) {{
                        btn.textContent = 'Any ▾';
                        btn.title = '';
                    }} else if (targetSet.size === 1) {{
                        const v = Array.from(targetSet)[0];
                        btn.textContent = v + ' ▾';
                        btn.title = v;            // hover shows full text when ellipsised
                    }} else {{
                        btn.textContent = targetSet.size + ' selected ▾';
                        btn.title = Array.from(targetSet).join(', ');
                    }}
                }}

                populateDropdown(document.querySelector('[data-filter-panel="tracker"]'), UNIQUE_TRACKERS, trackersSet);
                populateDropdown(document.querySelector('[data-filter-panel="category"]'), UNIQUE_CATEGORIES, categoriesSet);

                // Status/reasons panel: wire up the static checkboxes
                document.querySelectorAll('[data-filter-status]').forEach(cb => {{
                    cb.addEventListener('change', () => {{
                        if (cb.checked) statusSet.add(cb.value); else statusSet.delete(cb.value);
                        updateStatusBtnLabel();
                        applyFilters();
                    }});
                }});
                document.querySelectorAll('[data-filter-reason]').forEach(cb => {{
                    cb.addEventListener('change', () => {{
                        if (cb.checked) reasonsSet.add(cb.value); else reasonsSet.delete(cb.value);
                        updateStatusBtnLabel();
                        applyFilters();
                    }});
                }});

                // Seed from server-pre-checked inputs so the default landing view
                // matches the DELETE-only preset without a user interaction.
                document.querySelectorAll('[data-filter-status]:checked').forEach(cb => statusSet.add(cb.value));
                document.querySelectorAll('[data-filter-reason]:checked').forEach(cb => reasonsSet.add(cb.value));

                function updateStatusBtnLabel() {{
                    const btn = document.querySelector('[data-filter="status"]');
                    if (!btn) return;
                    const n = statusSet.size + reasonsSet.size;
                    btn.textContent = n === 0 ? 'Any ▾' : n + ' selected ▾';
                }}

                function positionPanel(panel) {{
                    const btn = panel._anchorBtn;
                    if (!btn) return;
                    const rect = btn.getBoundingClientRect();
                    const margin = 4;
                    const spaceBelow = window.innerHeight - rect.bottom - margin;
                    const spaceAbove = rect.top - margin;
                    // Clear caps before measuring natural height.
                    panel.style.maxHeight = '';
                    panel.style.top = '';
                    const desired = panel.offsetHeight;
                    const openUp  = (spaceBelow < desired) && (spaceAbove > spaceBelow);
                    const cap     = Math.max(120, openUp ? spaceAbove : spaceBelow);
                    panel.style.maxHeight = cap + 'px';
                    panel.style.left = rect.left + 'px';
                    panel.style.minWidth = rect.width + 'px';
                    if (openUp) {{
                        panel.style.top = (rect.top - Math.min(desired, cap) - 2) + 'px';
                    }} else {{
                        panel.style.top = rect.bottom + 'px';
                    }}
                }}

                // Map every trigger button to its panel ONCE — needed because we
                // portal panels to <body> on first open, after which
                // btn.nextElementSibling no longer points at the panel.
                const _btnPanel = new WeakMap();
                let _openPanel = null;
                document.querySelectorAll('.filter-multi-btn').forEach(btn => {{
                    const p = btn.nextElementSibling;
                    if (p && p.classList.contains('filter-multi-panel')) {{
                        _btnPanel.set(btn, p);
                        p._anchorBtn = btn;   // Reverse pointer; survives portaling to <body>.
                    }}
                }});

                // Multi-select dropdown open/close
                document.addEventListener('click', (e) => {{
                    // Clicks *inside* an open panel (e.g. checkboxes) must not close it.
                    if (e.target.closest('.filter-multi-panel')) return;

                    const btn = e.target.closest('.filter-multi-btn');
                    const targetPanel = btn ? _btnPanel.get(btn) : null;
                    if (_openPanel && _openPanel !== targetPanel) {{
                        _openPanel.classList.remove('open');
                        _openPanel = null;
                    }}
                    if (btn && targetPanel) {{
                        targetPanel.classList.toggle('open');
                        if (targetPanel.classList.contains('open')) {{
                            // Portal to <body> so ancestor overflow can't clip the popover.
                            if (targetPanel.parentNode !== document.body) {{
                                document.body.appendChild(targetPanel);
                            }}
                            positionPanel(targetPanel);
                            _openPanel = targetPanel;
                        }} else {{
                            _openPanel = null;
                        }}
                        e.stopPropagation();
                    }}
                }});

                // Text/number/date inputs
                let debounceTimer = null;
                const debounceApply = () => {{
                    clearTimeout(debounceTimer);
                    debounceTimer = setTimeout(applyFilters, 120);
                }};
                document.querySelectorAll('.filter-row input[type="number"], .filter-row input[type="text"], .filter-row input[type="date"]').forEach(inp => {{
                    inp.addEventListener('input', debounceApply);
                }});

                function updateRangeBtnLabel(name) {{
                    const panel = document.querySelector('[data-range-panel="' + name + '"]');
                    if (!panel) return;
                    const btn = panel._anchorBtn;
                    if (!btn) return;
                    // Only the row inputs — exclude slider thumbs (data-slider).
                    const inputs = panel.querySelectorAll('label.range-row input');
                    const minV = inputs[0] && inputs[0].value.trim();
                    const maxV = inputs[1] && inputs[1].value.trim();
                    const isDate = panel.getAttribute('data-range-unit') === 'date';
                    const sep = isDate ? ' … ' : '–';
                    let label;
                    if (!minV && !maxV)        label = 'Any';
                    else if (minV && !maxV)    label = (isDate ? 'from ' : '≥ ') + minV;
                    else if (!minV && maxV)    label = (isDate ? 'to '   : '≤ ') + maxV;
                    else                       label = minV + sep + maxV;
                    btn.textContent = label + ' ▾';
                    btn.title = (minV || maxV) ? (label + ' (' + name + ')') : '';
                }}

                document.querySelectorAll('.filter-range-panel').forEach(panel => {{
                    const name = panel.getAttribute('data-range-panel');
                    // Plain inputs (Min/Max <input type=number/date>): keep label in sync.
                    panel.querySelectorAll('label.range-row input').forEach(inp => {{
                        inp.addEventListener('input', () => updateRangeBtnLabel(name));
                    }});
                    const clr = panel.querySelector('.range-clear-btn');
                    if (clr) clr.addEventListener('click', () => {{
                        panel.querySelectorAll('input').forEach(i => {{ i.value = ''; }});
                        const slider = panel.querySelector('.range-slider');
                        if (slider && slider._reset) slider._reset();
                        updateRangeBtnLabel(name);
                        applyFilters();
                    }});
                }});

                // Dual-thumb range sliders for the four bounded numeric ranges.
                // The actual filter values stay in the existing Min/Max inputs;
                // the slider just provides a visual control that two-way-binds
                // to those inputs (and clamps to the dataset min/max).
                function wireRangeSlider(slot) {{
                    const name = slot.getAttribute('data-slider-for');
                    const bounds = RANGE_BOUNDS[name];
                    if (!bounds) return;
                    const [bmin, bmax, step] = bounds;
                    if (!(bmax > bmin)) {{
                        const note = document.createElement('div');
                        note.className = 'range-slider-note';
                        note.textContent = (bmax === bmin) ? ('Single value: ' + bmin) : '';
                        slot.appendChild(note);
                        return;
                    }}
                    const panel = slot.closest('.filter-range-panel');
                    const inputs = panel.querySelectorAll('label.range-row input');
                    const inMin = inputs[0], inMax = inputs[1];

                    const sMin = document.createElement('input');
                    const sMax = document.createElement('input');
                    [sMin, sMax].forEach(s => {{
                        s.type = 'range'; s.min = bmin; s.max = bmax; s.step = step;
                    }});
                    sMin.value = bmin; sMax.value = bmax;
                    slot.appendChild(sMin); slot.appendChild(sMax);

                    const span = bmax - bmin;
                    const pct = (v) => ((v - bmin) / span) * 100;
                    const refreshTrack = () => {{
                        const lo = Math.min(parseFloat(sMin.value), parseFloat(sMax.value));
                        const hi = Math.max(parseFloat(sMin.value), parseFloat(sMax.value));
                        slot.style.setProperty('--p1', pct(lo) + '%');
                        slot.style.setProperty('--p2', pct(hi) + '%');
                    }};
                    refreshTrack();

                    // Half-step tolerance for "is this slider at the bound?" — without it
                    // a step=0.1 value like 0.3 round-trips through parseFloat as
                    // 0.30000000000000004, breaks the equality check, and the filter
                    // activates with that slightly-bigger min, silently excluding rows
                    // whose value is exactly 0.3.
                    const eps = step / 2;
                    const slidersToInputs = () => {{
                        const lo = Math.min(parseFloat(sMin.value), parseFloat(sMax.value));
                        const hi = Math.max(parseFloat(sMin.value), parseFloat(sMax.value));
                        inMin.value = (Math.abs(lo - bmin) < eps) ? '' : lo;
                        inMax.value = (Math.abs(hi - bmax) < eps) ? '' : hi;
                        refreshTrack();
                        updateRangeBtnLabel(name);
                        debounceApply();
                    }};
                    sMin.addEventListener('input', slidersToInputs);
                    sMax.addEventListener('input', slidersToInputs);

                    const inputsToSliders = () => {{
                        const lo = inMin.value === '' ? bmin : parseFloat(inMin.value);
                        const hi = inMax.value === '' ? bmax : parseFloat(inMax.value);
                        if (Number.isFinite(lo)) sMin.value = Math.max(bmin, Math.min(bmax, lo));
                        if (Number.isFinite(hi)) sMax.value = Math.max(bmin, Math.min(bmax, hi));
                        refreshTrack();
                    }};
                    inMin.addEventListener('input', inputsToSliders);
                    inMax.addEventListener('input', inputsToSliders);

                    slot._reset = () => {{
                        sMin.value = bmin; sMax.value = bmax;
                        refreshTrack();
                    }};
                }}
                document.querySelectorAll('.range-slider').forEach(wireRangeSlider);

                // Clear all
                const clearBtn = document.getElementById('filterClearBtn');
                if (clearBtn) {{
                    clearBtn.addEventListener('click', () => {{
                        // Walk both contexts: inline filter-row inputs (Name, Path)
                        // and the multi-select / range / date inputs that have been
                        // portaled to <body> via document.body.appendChild(targetPanel).
                        document.querySelectorAll('.filter-row input, .filter-multi-panel input').forEach(i => {{
                            if (i.type === 'checkbox')   i.checked = false;
                            else if (i.type !== 'range') i.value = '';   // sliders reset via slot._reset()
                        }});
                        statusSet.clear(); reasonsSet.clear();
                        trackersSet.clear(); categoriesSet.clear();
                        document.querySelectorAll('.filter-multi-btn').forEach(b => {{ b.textContent = 'Any ▾'; b.title = ''; }});
                        document.querySelectorAll('.range-slider').forEach(slot => {{ if (slot._reset) slot._reset(); }});
                        applyFilters();
                    }});
                }}

                // cache filter input refs once
                const inputs = {{}};
                document.querySelectorAll('.filter-row [data-filter]').forEach(el => {{
                    inputs[el.getAttribute('data-filter')] = el;
                }});

                function readNum(name) {{
                    const el = inputs[name];
                    if (!el || el.value === '') return null;
                    const n = parseFloat(el.value);
                    return isFinite(n) ? n : null;
                }}
                function readText(name) {{
                    const el = inputs[name];
                    const v = el ? el.value : '';
                    return (v || '').toLowerCase().trim();
                }}

                // numericInRange is provided by the inlined report-logic.js.
                const getAttr = (el, n) => el.getAttribute('data-sk-' + n) || '';
                let _lastHidden = null;  // Uint8Array: 1 = hidden last pass, 0 = visible

                function readDateSec(name) {{
                    // <input type="date">.valueAsNumber gives ms since epoch at UTC midnight,
                    // or NaN when empty.
                    const el = inputs[name];
                    if (!el || !el.value) return null;
                    const ms = el.valueAsNumber;
                    return Number.isFinite(ms) ? Math.floor(ms / 1000) : null;
                }}

                function applyFilters() {{
                    const seedsMin = readNum('seedsMin'), seedsMax = readNum('seedsMax');
                    const ratioMin = readNum('ratioMin'), ratioMax = readNum('ratioMax');
                    const sizeMinB = readNum('sizeMin'), sizeMaxB = readNum('sizeMax');
                    const upMinB   = readNum('upMin'),   upMaxB   = readNum('upMax');
                    const seededMinD = readNum('seededMin'), seededMaxD = readNum('seededMax');
                    let addedFromSec = readDateSec('addedFrom');
                    let addedToSec   = readDateSec('addedTo');
                    // Inclusive end-of-day for "to" date so picking 2024-03-05 also includes events on that day.
                    if (addedToSec !== null) addedToSec += 86400 - 1;
                    const nameQ = readText('name');
                    const pathQ = readText('path');

                    const sizeMinBytes = sizeMinB !== null ? sizeMinB * GIB : null;
                    const sizeMaxBytes = sizeMaxB !== null ? sizeMaxB * GIB : null;
                    const upMinBytes   = upMinB   !== null ? upMinB   * GIB : null;
                    const upMaxBytes   = upMaxB   !== null ? upMaxB   * GIB : null;
                    const seededMinSec = seededMinD !== null ? seededMinD * DAY : null;
                    const seededMaxSec = seededMaxD !== null ? seededMaxD * DAY : null;

                    let groups = _groupsCache;
                    if (!groups) {{
                        groups = _groupsCache = Array.from(document.getElementsByClassName('group'));
                        // Cache parsed-once state on each group so the per-keystroke loop reads plain JS props.
                        for (const g of groups) {{
                            g._status = g.dataset.status;
                            g._reasonSet = new Set((g.dataset.reasons || '').split(/\\s+/).filter(Boolean));
                            g._seedsMin = g.dataset.seedsMin;
                            g._search = g.dataset.search || '';
                        }}
                    }}
                    if (!_lastHidden || _lastHidden.length !== groups.length) {{
                        _lastHidden = new Uint8Array(groups.length);
                    }}

                    const needsRowScan = trackersSet.size || categoriesSet.size;

                    for (let i = 0; i < groups.length; i++) {{
                        const g = groups[i];
                        let hide = false;

                        if (statusSet.size && !statusSet.has(g._status)) hide = true;
                        else if (reasonsSet.size) {{
                            let any = false;
                            for (const r of g._reasonSet) if (reasonsSet.has(r)) {{ any = true; break; }}
                            if (!any) hide = true;
                        }}

                        if (!hide && !numericInRange(g._seedsMin, seedsMin, seedsMax)) hide = true;

                        if (!hide && nameQ && !g._search.includes(nameQ)) hide = true;
                        if (!hide && pathQ && !g._search.includes(pathQ)) hide = true;

                        if (!hide && (ratioMin !== null || ratioMax !== null
                                      || sizeMinBytes !== null || sizeMaxBytes !== null
                                      || upMinBytes !== null || upMaxBytes !== null
                                      || seededMinSec !== null || seededMaxSec !== null
                                      || addedFromSec !== null || addedToSec !== null)) {{
                            const rows = groupRows(g);
                            const orig = rows[0];
                            if (!orig) hide = true;
                            else {{
                                if (!numericInRange(getAttr(orig, 3), ratioMin,    ratioMax))    hide = true;
                                else if (!numericInRange(getAttr(orig, 4), sizeMinBytes, sizeMaxBytes)) hide = true;
                                else if (!numericInRange(getAttr(orig, 5), upMinBytes,   upMaxBytes))   hide = true;
                                else if (!numericInRange(getAttr(orig, 6), seededMinSec, seededMaxSec)) hide = true;
                                else if (!numericInRange(getAttr(orig, 7), addedFromSec, addedToSec))   hide = true;
                            }}
                        }}

                        if (!hide && needsRowScan) {{
                            let ok = false;
                            for (const tr of groupRows(g)) {{
                                if (trackersSet.size && !trackersSet.has(getAttr(tr, 8))) continue;
                                if (categoriesSet.size && !categoriesSet.has(getAttr(tr, 9))) continue;
                                ok = true; break;
                            }}
                            if (!ok) hide = true;
                        }}

                        // Skip the DOM mutation when the visibility state is unchanged.
                        const prev = _lastHidden[i];
                        const next = hide ? 1 : 0;
                        if (prev !== next) {{
                            if (hide) g.classList.add('filtered-hidden');
                            else      g.classList.remove('filtered-hidden');
                            _lastHidden[i] = next;
                        }}
                    }}
                    updateFilterCounts();
                    // Visible set changed → DELETE-only / KEEP-only / mixed
                    // samples may carry different max content widths.
                    if (typeof recomputeNarrowColumns === 'function') recomputeNarrowColumns();
                }}

                const filterCountsEl = document.getElementById('filterCounts');
                const emptyStateEl = document.getElementById('emptyState');
                let _visGNode = null, _visTNode = null, _lastVisG = -1, _lastVisT = -1;
                if (filterCountsEl) {{
                    _visGNode = document.createElement('strong');
                    _visTNode = document.createElement('strong');
                    filterCountsEl.append('Showing ', _visGNode, ' / ' + TOTAL_GROUPS + ' groups · ',
                                          _visTNode, ' / ' + TOTAL_TORRENTS + ' torrents');
                }}
                function updateFilterCounts() {{
                    const groups = _groupsCache;
                    let visG, visT;
                    if (!groups || !_lastHidden) {{
                        visG = TOTAL_GROUPS; visT = TOTAL_TORRENTS;
                    }} else {{
                        visG = 0; visT = 0;
                        for (let i = 0; i < groups.length; i++) {{
                            if (_lastHidden[i]) continue;
                            visG++;
                            visT += parseInt(groups[i].dataset.rowCount, 10) || 0;
                        }}
                    }}
                    if (_visGNode && (visG !== _lastVisG || visT !== _lastVisT)) {{
                        _visGNode.textContent = visG;
                        _visTNode.textContent = visT;
                        _lastVisG = visG; _lastVisT = visT;
                    }}
                    if (emptyStateEl) {{
                        // Show the empty-state placeholder when filters narrow to zero
                        // groups. Also keeps the table-container tall enough that
                        // popovers anchored to the filter row have somewhere to render.
                        emptyStateEl.classList.toggle('shown', visG === 0 && TOTAL_GROUPS > 0);
                    }}
                }}
                updateStatusBtnLabel();
                applyFilters();

                // Re-anchor the open panel on scroll/resize — panels are
                // position:fixed and would otherwise drift away from their sticky button.
                const reanchorOpenPanel = () => {{
                    if (_openPanel) positionPanel(_openPanel);
                }};
                window.addEventListener('scroll', reanchorOpenPanel, true);
                window.addEventListener('resize', reanchorOpenPanel);
            }})();

            new Chart(ctxCount, {{
                type: 'bar',
                data: {{
                    labels: labels,
                    datasets: [
                        {{ label: 'Deleted Count', data: {_js(ds_count_del)}, backgroundColor: '#ff5252' }},
                        {{ label: 'Total Count', data: {_js(ds_count_total)}, backgroundColor: '#333' }}
                    ]
                }},
                options: {{
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {{ title: {{ display: true, text: 'Torrents per Tracker', color: '#fff' }} }},
                    scales: {{ x: {{ stacked: true }}, y: {{ stacked: false, beginAtZero: true }} }}
                }}
            }});

            new Chart(ctxSize, {{
                type: 'bar',
                data: {{
                    labels: labels,
                    datasets: [
                        {{ label: 'Deleted Size (GiB)', data: {_js(ds_size_del)}, backgroundColor: '#ff5252' }},
                        {{ label: 'Total Size (GiB)', data: {_js(ds_size_total)}, backgroundColor: '#333' }}
                    ]
                }},
                options: {{
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {{ title: {{ display: true, text: 'Size per Tracker (GiB)', color: '#fff' }} }},
                    scales: {{ x: {{ stacked: true }}, y: {{ stacked: false, beginAtZero: true }} }}
                }}
            }});

            new Chart(ctxGroup, {{
                type: 'bar',
                data: {{
                    labels: groupLabels,
                    datasets: [
                        {{ label: 'Deleted (Groups)', data: {_js(ds_group_del)}, backgroundColor: '#ff5252' }},
                        {{ label: 'Total (Groups)', data: {_js(ds_group_total)}, backgroundColor: '#333' }}
                    ]
                }},
                options: {{
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {{ title: {{ display: true, text: 'Groups by Torrent Count', color: '#fff' }} }},
                    scales: {{ x: {{ stacked: true }}, y: {{ stacked: false, beginAtZero: true }} }}
                }}
            }});
        </script>
    </body>
    </html>
    """


    if HTML_EXPORT:
        try:
            base, ext = os.path.splitext(HTML_EXPORT)
            final_html_path = f"{base}_{ts_str}{ext}"
            with open(final_html_path, "w", encoding="utf-8") as f:
                f.write(full_html)
            print(f"{Colors.GREEN}Successfully exported HTML report to: {final_html_path}{Colors.END}")
        except Exception as e:
            print(f"{Colors.RED}Error exporting HTML: {e}{Colors.END}")

    if CSV_EXPORT:
        base, ext = os.path.splitext(CSV_EXPORT)
        csv_filename = f"{base}_{ts_str}{ext or '.csv'}"

        print(f"{Colors.BOLD}[INFO]{Colors.END} Exporting CSV report to {csv_filename}...")
        try:
            with open(csv_filename, 'w', newline='', encoding='utf-8') as csvfile:
                fieldnames = ['Group ID', 'Status', 'Type', 'Name', 'Size', 'Tracker', 'Category', 'Added', 'Seeding Time', 'Ratio', 'Seeders', 'Path']
                writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                writer.writeheader()

                for idx, (h, d) in enumerate(sorted_items, 1):
                    is_del_group = idx in eligible_ids
                    status = "DELETE" if is_del_group else "KEEP"

                    group_torrents = [d['original']] + d['crossseeds']

                    external_path = None
                    for t in group_torrents:
                         if t.get('_external_path'):
                             external_path = t['_external_path']
                             break

                    for t in group_torrents:
                        add_date = format_timestamp(t.get('added_on', 0))
                        seed_time = format_duration(t.get('seeding_time', 0), "d:hh:mm")

                        writer.writerow({
                            'Group ID': idx,
                            'Status': status,
                            'Type': 'ORIGINAL' if t == d['original'] else 'CROSS-SEED',
                            'Name': t.get('name', ''),
                            'Size': format_size_smart(t.get('size', 0)),
                            'Tracker': t.get('_tracker_domain') or "Unknown",
                            'Category': t.get('category', ''),
                            'Added': add_date,
                            'Seeding Time': seed_time,
                            'Ratio': f"{t.get('ratio', 0):.2f}",
                            'Seeders': t.get('_seeder_count', 0),
                            'Path': t.get('content_path', '')
                        })

                    if external_path:
                        writer.writerow({
                            'Group ID': idx,
                            'Status': status,
                            'Type': 'MEDIA-LIBRARY',
                            'Name': d['original'].get('name', ''),
                            'Size': format_size_smart(d['original'].get('size', 0)),
                            'Tracker': '',
                            'Category': 'External Library',
                            'Added': '',
                            'Seeding Time': '',
                            'Ratio': '',
                            'Seeders': '',
                            'Path': external_path
                        })


            print(f"{Colors.GREEN}Successfully exported CSV report to: {csv_filename}{Colors.END}")

        except Exception as e:
            print(f"{Colors.RED}Error writing CSV: {e}{Colors.END}")


def manual_loop(client, emap):
    if not emap:
        return

    def _delete_and_log(gid):
        result = client.delete_torrents([t['hash'] for t in emap[gid]], delete_files=True)
        if result == "dry_run":
            print(f"{Colors.YELLOW}[DRY RUN] Group {gid} would be deleted (no action taken){Colors.END}")
        elif result is None:
            print(f"{Colors.RED}Group {gid} deletion FAILED (qBittorrent request error); group kept.{Colors.END}")
        else:
            print(f"{Colors.GREEN}Group {gid} deleted.{Colors.END}")
            emap.pop(gid, None)

    while True:
        choice = input(f"\n{Colors.BOLD}Manual:{Colors.END} Enter Group ID(s) (e.g. 5,7), 'all', or 'q': ").strip().lower()
        if choice in ['q', 'quit']:
            break

        if choice == 'all':
            print(f"{Colors.RED}WARNING: You are about to delete ALL {len(emap)} eligible groups.{Colors.END}")
            if input(f"{Colors.BOLD}Type 'YES' to confirm execution: {Colors.END}") == 'YES':
                for gid in list(emap):
                    _delete_and_log(gid)
                break
            else:
                print(f"{Colors.YELLOW}Deletion cancelled.{Colors.END}")
        else:
            ids_to_process = []
            for s in choice.split(','):
                try:
                    gid = int(s.strip())
                    if gid in emap:
                        ids_to_process.append(gid)
                    else:
                        print(f"{Colors.RED}Group {gid} not found or not eligible.{Colors.END}")
                except ValueError:
                    print(f"{Colors.YELLOW}Ignored non-numeric input: {s.strip()!r}{Colors.END}")

            if not ids_to_process:
                continue

            print(f"{Colors.RED}You selected {len(ids_to_process)} group(s) for deletion.{Colors.END}")
            if input(f"{Colors.BOLD}Type 'YES' to confirm execution: {Colors.END}") == 'YES':
                for gid in ids_to_process:
                    if gid in emap:
                        _delete_and_log(gid)
            else:
                print(f"{Colors.YELLOW}Deletion cancelled.{Colors.END}")


def _finalize_deletion(client, emap):
    """Dispatch to manual loop, live auto-delete with confirm, or dry-run notice."""
    if MANUAL_MODE:
        manual_loop(client, emap)
        return
    if not emap:
        print(f"{Colors.GREEN}Nothing to delete.{Colors.END}")
        return
    if DRY_RUN:
        print(f"{Colors.YELLOW}DRY RUN. Use --manual or --delete.{Colors.END}")
        return

    print(f"\n{Colors.BOLD}{Colors.RED}WARNING: LIVE DELETION MODE IS ACTIVE.{Colors.END}")
    print(f"{Colors.RED}You are about to PERMANENTLY DELETE {len(emap)} groups.{Colors.END}")
    confirm = input(f"{Colors.BOLD}Type 'YES' to confirm execution: {Colors.END}")

    if confirm == 'YES':
        print(f"{Colors.RED}AUTO-DELETING...{Colors.END}")
        for gid, ts in emap.items():
            result = client.delete_torrents([t['hash'] for t in ts], delete_files=True)
            if result is None:
                print(f"{Colors.RED}Group {gid} deletion FAILED (qBittorrent request error).{Colors.END}")
            else:
                print(f"{Colors.GREEN}Group {gid} deleted.{Colors.END}")
    else:
        print(f"{Colors.YELLOW}Deletion cancelled.{Colors.END}")


def get_group_sort_key(item):
    return _torrent_sort_key(item[1]['original'], SORT_BY)



def scan_external_libraries(paths):
    """
    Scans external directories for files with hardlinks (nlink > 1).
    """
    inodes = {}
    if not paths:
        return inodes
    debug_log(f"  > Raw Input Paths: {paths}")
    final_paths = []
    for p in paths:
        braced_paths = expand_braces(p)
        for bp in braced_paths:
            if any(c in bp for c in ['*', '?', '[']):
                matches = glob.glob(bp)
                if matches:
                    final_paths.extend(matches)
                else:
                    print(f"{Colors.YELLOW}  ! Wildcard warning: '{bp}' matched 0 paths{Colors.END}")
            else:
                final_paths.append(bp)

    final_paths = sorted(set(final_paths))

    if not final_paths:
        print(f"{Colors.RED}  ! No valid paths found after expansion.{Colors.END}")
        return inodes

    debug_log(f"[SCAN] > Scanning {len(final_paths)} locations:")
    for fp in final_paths:
        debug_log(f"[SCAN]   > Target: {fp}")

    total_files_scanned = 0
    start_time = datetime.now()

    def _on_walk_error(err):
        print(f"{Colors.YELLOW}  ! {err}{Colors.END}")

    for p in final_paths:
        debug_log(f"[SCAN] > Walking: {p}...")
        try:
            for root, dirs, files in os.walk(p, onerror=_on_walk_error):
                dirs[:] = [d for d in dirs if not d.startswith('.') and d.lower() not in ('@eaDir', '#recycle')]

                for f in files:
                    total_files_scanned += 1

                    if not DEBUG_MODE and total_files_scanned % 2000 == 0:
                        sys.stdout.write(f"\r{Colors.DIM}  ... Scanned {total_files_scanned} files...{Colors.END}")
                        sys.stdout.flush()

                    file_path = os.path.join(root, f)
                    try:
                        stat = os.stat(file_path)
                        if stat.st_nlink > 1:
                            inode_tuple = (stat.st_dev, stat.st_ino)
                            if inode_tuple not in inodes:
                                debug_log(f"[SCAN]   + Found Link: {f} (Inode: {stat.st_ino})")
                            inodes[inode_tuple] = file_path
                    except OSError:
                        continue
        except Exception as e:
            print(f"{Colors.RED}  ! Error walking {p}: {e}{Colors.END}")

    if not DEBUG_MODE:
        _clear_progress_line()

    duration = (datetime.now() - start_time).total_seconds()

    SCAN_STATS['files_scanned'] = total_files_scanned
    SCAN_STATS['unique_inodes'] = len(inodes)
    SCAN_STATS['scan_duration'] = duration

    print(f"{Colors.GREEN}  ✓ Scanning complete in {duration:.2f}s.{Colors.END}")
    print(f"{Colors.GREEN}  ✓ Scanned {total_files_scanned} files. Found {len(inodes)} unique hard-linked inodes.{Colors.END}\n")
    return inodes



def check_no_hard_links(client):
    if not NO_HARD_LINKS_CATEGORIES:
        print(f"{Colors.RED}ERROR: --no-hard-links-categories must be specified to use this mode.{Colors.END}")
        return

    torrents = _fetch_and_filter_torrents(client)
    external_inodes = _scan_external_libs_phase()

    def is_target_category(cat):
        if not cat: return False
        cat = cat.lower()
        for spec in _NO_HARD_LINKS_CATEGORY_SPECS:
            if matches_pattern(cat, spec):
                return True
        return False

    category_torrents = [t for t in torrents if is_target_category(t.get('category', ''))]

    _fetch_seeders_phase(client, category_torrents)

    t_start = datetime.now()
    print(f"{Colors.BOLD}[4/6]{Colors.END} Filtering torrents for orphans...")

    debug_log(f"[FILTER] Starting analysis of {len(torrents)} torrents against {len(external_inodes)} external inodes...")

    identity_map = defaultdict(list)
    for t in torrents:
        t['_identity'] = get_path_identity(t)
        identity_map[t['_identity']].append(t)

    orphans = []
    external_matches_count = 0

    total_to_process = len(category_torrents)

    for idx, t in enumerate(category_torrents, 1):
        if not DEBUG_MODE and idx % 100 == 0:
            sys.stdout.write(f"\r{Colors.DIM}  ... Analyzed {idx}/{total_to_process} candidates...{Colors.END}")
            sys.stdout.flush()

        ident = t['_identity']

        if len(identity_map[ident]) >= 2:
            continue

        is_external_match = False
        pair = _parse_inode_identity(ident) if external_inodes else None
        if pair and pair in external_inodes:
            is_external_match = True
            debug_log(f"Torrent '{t.get('name')}' saved: Matches external hardlink (Inode {pair[1]})")

        t['_external_hardlink'] = is_external_match
        if is_external_match:
             t['_external_path'] = external_inodes[pair]

        if is_external_match:
            external_matches_count += 1
            orphans.append(t)
            continue

        if "heuristic" in ident:
            t['_path_error'] = True
        orphans.append(t)

    if not DEBUG_MODE:
        _clear_progress_line()

    SCAN_STATS['group_duration'] = (datetime.now() - t_start).total_seconds()
    print(f"{Colors.GREEN}  ✓ Processing complete in {SCAN_STATS['group_duration']:.2f}s.{Colors.END}")

    if external_matches_count > 0:
        print(f"{Colors.GREEN}  ✓ Preserved {external_matches_count} torrents found in external libraries{Colors.END}")

    print(f"{Colors.GREEN}  ✓ Found {len(orphans)} torrents without internal hard links.{Colors.END}\n")

    all_groups = {t['hash']: {'original': t, 'crossseeds': [], 'name': t['name']} for t in orphans}

    _run_analyze_and_finalize(client, all_groups)


def _run_analyze_and_finalize(client, all_groups):
    sorted_items = sorted(all_groups.items(), key=get_group_sort_key, reverse=(SORT_ORDER == 'desc'))

    print(f"{Colors.BOLD}[5/6]{Colors.END} Analyze deletable torrents...")
    t_start = datetime.now()
    emap = {}
    for idx, (h, d) in enumerate(sorted_items, 1):
        elig, ts = print_group(client, d, idx, len(sorted_items))
        if elig:
            emap[idx] = ts
    SCAN_STATS['analyze_duration'] = (datetime.now() - t_start).total_seconds()
    print(f"{Colors.GREEN}\n  ✓ Analysis complete in {SCAN_STATS['analyze_duration']:.2f}s.{Colors.END}")

    print(f"\n{Colors.BOLD}[6/6]{Colors.END} Finalizing & exporting reports...")
    stats = calculate_stats(all_groups, emap)
    print_summary(stats)
    export_reports(sorted_items, emap.keys())
    _finalize_deletion(client, emap)


def main():
    print_header()
    print_config()
    client = QBittorrentClient(QBITTORRENT_HOST, QBITTORRENT_USER, QBITTORRENT_PASS, QBITTORRENT_API_KEY)
    if NO_HARD_LINKS_MODE:
        check_no_hard_links(client)
        return
    all_groups = load_and_group_torrents(client)
    _run_analyze_and_finalize(client, all_groups)


if __name__ == "__main__":
    main()
