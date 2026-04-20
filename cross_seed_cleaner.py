#!/usr/bin/env python3
"""
Cross-Seed Cleaner v2025.12.24 - Simple Tracker-Based Seeder Counting
For unreliable trackers, count seeders as num_complete + num_incomplete.
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
from collections import defaultdict
from datetime import datetime

# ============================================================================
# 1. SETTINGS (edit these; environment variables and command-line flags override at startup)
# ============================================================================
# Connection details for the qBittorrent Web UI.
# The URL must start with http:// or https:// and include :port if qBittorrent runs on a non-default port.
# Don't add a trailing slash.
QBITTORRENT_HOST = "http://localhost:8080"
QBITTORRENT_USER = "admin"
QBITTORRENT_PASS = "password"

# Global safety net: a group is kept (not deleted) if any of its torrents has fewer than X seeders.
# In No-Hard-Links mode, only the orphan torrent itself is checked.
MIN_SEEDERS = 4

# Group safety: if a group is made up of X or more torrents in total (Original plus Cross-Seeds), it is kept.
# E.g. X=6 means groups of 6+ torrents are protected from deletion.
# This check is skipped in No-Hard-Links mode.
MAX_TORRENTS_IN_GROUP = 6

# The Original torrent must have been seeding for at least this many days before its cross-seeds can be deleted.
# Decimal values are supported (e.g. 7.5).
MIN_ORIGINAL_SEED_TIME_DAYS = 365

# The Original torrent must be at least this large (in GiB) before its cross-seeds can be deleted.
# Decimal values are supported.
# Set to 0 to turn off the size check.
MIN_SIZE_GIB = 15

# Helper flags
DEBUG_MODE = False                      # Enable verbose logging
# True = simulate only (nothing is deleted); False = actually delete torrents.
# The --dry-run / --delete command-line flags override this.
# In Manual Mode the cleaner always asks you to confirm before deleting, no matter what this is set to.
DRY_RUN = True
# True = hide non-eligible groups from the CLI output and the HTML report.
# This does NOT affect the CSV export — the CSV always contains every group.
ELIGIBLE_ONLY = True

# Output filenames.
# A timestamp is added before the extension (e.g. output.html -> output_2026.04.21_14.30.00.html).
# Leave empty (or pass an empty string via environment variable / command-line) to turn the export off.
HTML_EXPORT = "output.html"
CSV_EXPORT = "output.csv"

# No Hard Links Mode
# When enabled, the script finds torrents in selected qBittorrent categories that have NO hard-links.
# Category selection supports:
# - Exact match:  "cross-seed-links"
# - Regex match:  prefix with "r:", e.g. "r:autobrr-.*"
# - Combined:     "cross-seed-category,r:autobrr-.*"
# Category names and patterns are compared in lowercase — write yours in lowercase to make sure they match.
NO_HARD_LINKS_MODE = False
NO_HARD_LINKS_CATEGORIES = "cross-seed-category,r:autobrr-.*"

# Path(s) to external media libraries to scan for hardlinks.
# Supports:
# 1. Comma-separated paths:         "/mnt/movies, /mnt/tv"
# 2. Wildcards (* = anything, ? = one character, [abc] = one of the listed characters): "/mnt/users/*"
# 3. Brace groups {a,b,c} expand into multiple paths (not nested): "/mnt/media/{movies,tv,anime}"
# 4. Mixed — combine any of the above: "/mnt/local/{movies,tv}, /mnt/remote/user_*"
EXTERNAL_MEDIA_PATHS = "/mnt/hdd-pool/userdata/media/{user_1,user_2,user_3}"

# Category filter mode.
# Applies to the ORIGINAL torrent's category only.
# Modes:
#   "allow" = Only process groups where Original matches ALLOWLIST
#   "block" = Skip groups where Original matches BLOCKLIST
#   "both"  = Must match ALLOWLIST *and* NOT match BLOCKLIST
#   "none"  = Disable category filtering (process everything)
CATEGORY_FILTER_MODE = "block"

# Sorting for CLI output (CLI Table & Group processing order)
# Options:
#   "seeders" / "seeds" = Number of active seeders (aliases)
#   "ratio"             = Share ratio
#   "size"              = Torrent size
#   "uploaded"          = Total uploaded amount
#   "added"             = Date added (useful to see oldest first)
#   "time"              = Seeding time
#   "name"              = Name of torrent (case-insensitive)
SORT_BY = "name"

# Sorting Order
# Options:
#   "asc"  = Ascending (Smallest/Oldest first)
#   "desc" = Descending (Largest/Newest first)
SORT_ORDER = "asc"

# Some trackers misreport the seeder count.
# For trackers listed here, the cleaner estimates the seeder count from the total peer count (seeders + leechers) instead.
# Patterns are matched against the tracker's domain, with any leading "tracker." or "www." removed.
# Comma-separated.
# Each entry is either a plain domain or a regex prefixed with "r:".
# Regex patterns match from the start of the domain — add "$" at the end to require a full match.
UNRELIABLE_TRACKERS = "hdts-announce.ru,hd-space.pw,tfa.tf"

# Hardcoded — cannot be overridden by environment variable or command-line.
# Remap internal (container) paths to host paths so the cleaner can check files on disk.
# Format: {"Internal Container Path": "Host Path"}
# If a path starts with more than one listed prefix, the longest one wins.
# Paths that don't match any prefix are used as-is.
PATH_MAPPINGS = {
    "/media/downloads/torrents": "/mnt/hdd-pool/userdata/media/downloads/torrents",
    "/media/downloads/freeleech": "/mnt/hdd-pool/appdata/qbittorrent/freeleech",
}

# Filter Lists.
# Each entry is either an exact category name or a regex prefixed with "r:".
# Examples:
#   "Movies"       -> Exact match for category "Movies"
#   "r:.*-4k$"     -> Regex match (matches "Movies-4k", "TV-4k"); also "r:autobrr-.*"
# Regex patterns match from the start of the category name — add "$" at the end (like in "r:.*-4k$") to require the full name to match.
# Matching is case-sensitive.
CATEGORY_ALLOWLIST = ["sonarr-imported", "radarr-imported", "lidarr-imported", "r:.*-allowsuffix$"]
CATEGORY_BLOCKLIST = ["freeleech-orpheus", "r:.*-blocksuffix$"]

# ============================================================================
# 2. CONFIGURATION LOADER (CLI > ENV > DEFAULTS)
# ============================================================================
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
    env_host = os.environ.get("QBITTORRENT_HOST", QBITTORRENT_HOST)
    env_user = os.environ.get("QBITTORRENT_USER", QBITTORRENT_USER)
    env_pass = os.environ.get("QBITTORRENT_PASS", QBITTORRENT_PASS)
    env_min_seeders = int(os.environ.get("MIN_SEEDERS", MIN_SEEDERS))
    env_max_group = int(os.environ.get("MAX_TORRENTS_IN_GROUP", MAX_TORRENTS_IN_GROUP))
    env_min_days = float(os.environ.get("MIN_ORIGINAL_SEED_TIME_DAYS", MIN_ORIGINAL_SEED_TIME_DAYS))
    env_min_size_gib = float(os.environ.get("MIN_SIZE_GIB", MIN_SIZE_GIB))
    env_debug = str2bool(os.environ.get("DEBUG_MODE", str(DEBUG_MODE)))
    env_dry_run = str2bool(os.environ.get("DRY_RUN", str(DRY_RUN)))
    env_eligible_only = str2bool(os.environ.get("ELIGIBLE_ONLY", str(ELIGIBLE_ONLY)))
    env_html_export = os.environ.get("HTML_EXPORT", HTML_EXPORT)
    env_csv_export = os.environ.get("CSV_EXPORT", CSV_EXPORT)
    env_no_hard_links_mode = str2bool(os.environ.get("NO_HARD_LINKS_MODE", str(NO_HARD_LINKS_MODE)))
    env_no_hard_links_cats = os.environ.get("NO_HARD_LINKS_CATEGORIES", NO_HARD_LINKS_CATEGORIES)
    env_ext_media_paths = os.environ.get("EXTERNAL_MEDIA_PATHS", EXTERNAL_MEDIA_PATHS)

    parser = argparse.ArgumentParser(description='Cross-Seed Cleaner: Deduplicate and cleanup torrents.')
    parser.add_argument('--host', default=env_host, help='qBittorrent Host')
    parser.add_argument('--user', default=env_user, help='qBittorrent User')
    parser.add_argument('--password', default=env_pass, help='qBittorrent Password')
    parser.add_argument('--min-seeders', type=int, default=env_min_seeders, help='Minimum seeders required')
    parser.add_argument('--max-group-size', type=int, default=env_max_group, help='Max torrents in group')
    parser.add_argument('--min-days', type=float, default=env_min_days, help='Min seed time in DAYS')
    parser.add_argument('--min-size-gib', type=float, default=env_min_size_gib, help='Min torrent size in GiB (0=no limit)')
    parser.add_argument('--debug', action=argparse.BooleanOptionalAction, default=env_debug, help='Enable debug logging')
    parser.add_argument('--manual', action='store_true', help='Enable Interactive Manual Deletion Mode')
    parser.add_argument('--eligible-only', action=argparse.BooleanOptionalAction, default=env_eligible_only, help='Hide non-eligible groups from CLI and HTML (CSV always exports all groups)')
    parser.add_argument('--html', type=str, default=env_html_export, help='Path to save HTML report')
    parser.add_argument('--csv', type=str, default=env_csv_export, help='Path to save CSV report')

    parser.add_argument('--no-hard-links-mode', action=argparse.BooleanOptionalAction, default=env_no_hard_links_mode, help='Enable mode to check for torrents without hard links')
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

# ============================================================================
# 3. GLOBAL CONFIGURATION
# ============================================================================
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
MIN_SEEDERS = ARGS.min_seeders
MAX_TORRENTS_IN_GROUP = ARGS.max_group_size
MIN_ORIGINAL_SEED_TIME_DAYS = ARGS.min_days
MIN_ORIGINAL_SEED_TIME_HOURS = MIN_ORIGINAL_SEED_TIME_DAYS * 24
MIN_SIZE_GIB = ARGS.min_size_gib
MIN_SIZE_BYTES = MIN_SIZE_GIB * 1024 * 1024 * 1024
_SORTED_PATH_MAPPING_PREFIXES = sorted(PATH_MAPPINGS.keys(), key=len, reverse=True)
DEBUG_MODE = ARGS.debug
MANUAL_MODE = ARGS.manual
ELIGIBLE_ONLY = ARGS.eligible_only
HTML_EXPORT = ARGS.html
CSV_EXPORT = ARGS.csv

CHARTJS_SOURCE = None
if HTML_EXPORT:
    _VENDOR_CHARTJS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'vendor', 'chart.js', 'chart.umd.min.js')
    try:
        with open(_VENDOR_CHARTJS_PATH, 'r', encoding='utf-8') as _f:
            CHARTJS_SOURCE = _f.read()
    except FileNotFoundError:
        sys.stderr.write(
            f"ERROR: vendored Chart.js not found at {_VENDOR_CHARTJS_PATH}.\n"
            f"Run from a full checkout of the repository (the vendor/chart.js/ directory must be present).\n"
        )
        sys.exit(1)


NO_HARD_LINKS_MODE = ARGS.no_hard_links_mode
NO_HARD_LINKS_CATEGORIES = [c.strip().lower() for c in ARGS.no_hard_links_categories.split(',') if c.strip()] if ARGS.no_hard_links_categories else []
EXTERNAL_MEDIA_PATHS = smart_split_paths(ARGS.external_media_paths) if ARGS.external_media_paths else []

CATEGORY_FILTER_MODE = os.environ.get("CATEGORY_FILTER_MODE", CATEGORY_FILTER_MODE)
SORT_BY = os.environ.get("SORT_BY", SORT_BY)
SORT_ORDER = os.environ.get("SORT_ORDER", SORT_ORDER)
_UNRELIABLE_RAW = os.environ.get("UNRELIABLE_TRACKERS", UNRELIABLE_TRACKERS)
UNRELIABLE_TRACKERS = [t.strip() for t in _UNRELIABLE_RAW.split(",") if t.strip()] if _UNRELIABLE_RAW else []

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
    def __init__(self, host, username, password):
        self.host = host.rstrip('/')
        self.cookie = None
        self.login(username, password)

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
        if self.cookie:
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
        if remote_path.startswith(remote_prefix):
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


def _compile_specs(patterns, label):
    """Build a list of (compiled_regex_or_None, literal_or_None) from raw r:…/exact patterns."""
    specs = []
    for p in patterns:
        if p.startswith("r:"):
            try:
                specs.append((re.compile(p[2:]), None))
            except re.error as e:
                sys.stderr.write(f"ERROR: invalid regex in {label}: {p!r} ({e})\n")
                sys.exit(1)
        else:
            specs.append((None, p))
    return specs


def matches_pattern(text, spec):
    regex, literal = spec
    if regex is not None:
        return bool(regex.match(text))
    return text == literal


_CATEGORY_ALLOWLIST_SPECS = _compile_specs(CATEGORY_ALLOWLIST, "CATEGORY_ALLOWLIST")
_CATEGORY_BLOCKLIST_SPECS = _compile_specs(CATEGORY_BLOCKLIST, "CATEGORY_BLOCKLIST")
_UNRELIABLE_TRACKERS_SPECS = _compile_specs(UNRELIABLE_TRACKERS, "UNRELIABLE_TRACKERS")
_NO_HARD_LINKS_CATEGORY_SPECS = _compile_specs(NO_HARD_LINKS_CATEGORIES, "NO_HARD_LINKS_CATEGORIES")

def _domain_from_tracker_url(url):
    """Normalize a tracker URL to a display domain, or return None."""
    if not url or '://' not in url or url.startswith('**'):
        return None
    host = urllib.parse.urlparse(url).hostname
    if not host:
        return None
    return host.replace('tracker.', '').replace('www.', '')


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
    """[1/7] fetch + [2/7] category-filter. Returns list of torrents that passed."""
    t_start = datetime.now()
    print(f"{Colors.BOLD}[1/7]{Colors.END} Fetching torrents...")
    torrents = client.get_torrents()
    SCAN_STATS['fetch_duration'] = (datetime.now() - t_start).total_seconds()
    print(f"{Colors.GREEN}  ✓ Fetching complete in {SCAN_STATS['fetch_duration']:.2f}s.{Colors.END}")
    print(f"{Colors.GREEN}  ✓ Found {len(torrents)} torrents.{Colors.END}\n")

    t_start = datetime.now()
    print(f"{Colors.BOLD}[2/7]{Colors.END} Filtering torrents by category...")
    debug_log(f"[FILTER] Applying filters to {len(torrents)} torrents...")

    filtered = []
    skipped = 0
    for t in torrents:
        cat = t.get('category', '')
        name = t.get('name', 'Unknown')
        if category_allowed(cat):
            filtered.append(t)
            debug_log(f"[FILTER] + Allowed '{name}' (Category: '{cat}')")
        else:
            skipped += 1
            debug_log(f"[FILTER] - Blocked '{name}' (Category: '{cat}')")

    SCAN_STATS['filter_duration'] = (datetime.now() - t_start).total_seconds()
    print(f"{Colors.GREEN}  ✓ Filtering complete in {SCAN_STATS['filter_duration']:.2f}s.{Colors.END}")
    print(f"{Colors.GREEN}  ✓ Kept {len(filtered)} torrents ({skipped} skipped).{Colors.END}\n")
    return filtered


def _scan_external_libs_phase():
    """[3/7] scan configured external paths; returns {(dev,ino): path} dict."""
    t_start = datetime.now()
    external_inodes = {}
    if EXTERNAL_MEDIA_PATHS:
        print(f"{Colors.BOLD}[3/7]{Colors.END} Scanning external libraries...")
        external_inodes = scan_external_libraries(EXTERNAL_MEDIA_PATHS)
    else:
        print(f"{Colors.BOLD}[3/7]{Colors.END} Skipping external libraries scan (Not Configured)...")
    SCAN_STATS['scan_duration'] = (datetime.now() - t_start).total_seconds()
    return external_inodes


def _fetch_seeders_phase(client, torrents):
    """[4/7] populate _seeder_count on each torrent."""
    t_start = datetime.now()
    print(f"{Colors.BOLD}[4/7]{Colors.END} Fetching seeders...")

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
    print(f"{Colors.BOLD}[5/7]{Colors.END} Grouping torrents by matching inodes...")

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

    mode = CATEGORY_FILTER_MODE.lower()

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
    time_ok = orig.get('seeding_time', 0) >= MIN_ORIGINAL_SEED_TIME_HOURS * 3600
    cat_ok = category_allowed(orig.get('category', ''))

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
    if code == "EXTERNAL_LINK": return "External Hardlink Found"
    if code == "PATH_ERROR": return "Path Error"
    if code == "LOW_SEEDS": return f"Low Seeds < {MIN_SEEDERS}"
    if code == "SMALL_SIZE": return f"Small Size < {MIN_SIZE_GIB}GiB"
    if code == "LOW_TIME": return f"Low Time < {MIN_ORIGINAL_SEED_TIME_DAYS}d"
    if code == "TOO_MANY": return f"> {MAX_TORRENTS_IN_GROUP} items"
    if code == "CATEGORY_FILTER": return "Category Filter"
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

def _torrent_sort_key(torrent, by):
    field = _SORT_KEY_MAP[by]
    if field == 'name':
        return torrent.get('name', '').lower()
    return torrent.get(field, 0)

def sort_torrents(original, crossseeds, by, order):
    rev = (order == "desc")
    return [original] + sorted(crossseeds, key=lambda t: _torrent_sort_key(t, by), reverse=rev)

def print_header():
    w = 262
    print(f"\n{Colors.BOLD}{Colors.CYAN}{'═' * w}{Colors.END}")
    print(f"{Colors.BOLD}{Colors.CYAN}║{' ' * ((w-50)//2)}CROSS-SEED CLEANER v33 - Simplified Seeder Count{' ' * ((w-50)//2)}║{Colors.END}")
    print(f"{Colors.BOLD}{Colors.CYAN}{'═' * w}{Colors.END}\n")


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
        """Colorize boolean-like values"""
        s = str(val)
        if s == "True": return f"{Colors.GREEN}True{Colors.END}"
        if s == "False": return f"{Colors.RED}False{Colors.END}"
        if s == "Disabled": return f"{Colors.RED}Disabled{Colors.END}"
        if s == "None": return f"{Colors.DIM}None{Colors.END}"
        return s

    rows = [
        [bold("Execution Mode"), f"{mode_color}{mode_text}{Colors.END}"],
        [bold("Min Seeders"), str(MIN_SEEDERS)],
        [bold("Min Seed Time"), f"{MIN_ORIGINAL_SEED_TIME_DAYS} days"],
        [bold("Min Size"), f"{MIN_SIZE_GIB} GiB" + (" (no limit)" if MIN_SIZE_GIB == 0 else "")],
        [bold("Max Group Size"), str(MAX_TORRENTS_IN_GROUP)],
        [bold("Category Mode"), CATEGORY_FILTER_MODE],
        [bold("Cat Allowlist"), cat_allow_str],
        [bold("Cat Blocklist"), cat_block_str],
        [bold("Unreliable Trackers"), unreliable_str],
        [bold("Eligible Only"), c(ELIGIBLE_ONLY)],
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

    if ELIGIBLE_ONLY and not eligible:
        return eligible, all_t

    status = f"{Colors.GREEN}✓ ELIGIBLE{Colors.END}" if eligible else f"{Colors.YELLOW}✗ KEPT{Colors.END} |"
    reasons = [f"{_REASON_CLI_COLOR[code]}{_reason_text(code)}{Colors.END}" for code in result['reasons']]

    if reasons:
        reason_str = " " + " | ".join(reasons)
    elif NO_HARD_LINKS_MODE:
        reason_str = " Orphan (No Hard Link)"
    else:
        reason_str = ""

    print(f"\n{Colors.BOLD}{Colors.BLUE}{'─' * 262}{Colors.END}")
    print(f"{Colors.BOLD}Group {num}/{total}: "
          f"{Colors.CYAN}{orig.get('name')[:140]}{Colors.END} "
          f"({status}{reason_str})")

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
        c_size = Colors.END; c_time = Colors.END; c_cat = Colors.END

        if is_orig:
            c_size = Colors.GREEN if size >= MIN_SIZE_BYTES else Colors.RED
            c_time = Colors.GREEN if seed_time >= (MIN_ORIGINAL_SEED_TIME_HOURS * 3600) else Colors.RED
            if NO_HARD_LINKS_MODE:
                cat_bad = t.get('_path_error') or not category_allowed(t.get('category', ''))
            else:
                cat_bad = not category_allowed(orig.get('category', ''))
            c_cat = Colors.RED if cat_bad else Colors.GREEN

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
    w = 262
    print(f"\n{Colors.BOLD}{Colors.CYAN}{'═' * w}{Colors.END}")
    print(f"{Colors.BOLD}{Colors.CYAN}║{' ' * ((w-21)//2)}SUMMARY & STATISTICS{' ' * ((w-21)//2)}║{Colors.END}")
    print(f"{Colors.BOLD}{Colors.CYAN}{'═' * w}{Colors.END}\n")

    Table.render([f"{Colors.BOLD}Metric{Colors.END}", f"{Colors.BOLD}Value{Colors.END}"], rows, [40, 105])
    print()



def export_reports(sorted_items, eligible_ids):
    def _mono_block(lines):
        return f"<div style='margin-top:2px; font-family:monospace; font-size:10px; color:#aaa; line-height:1.2; word-break:break-all;'>{'<br>'.join(lines)}</div>"

    _h = html_escape
    _js = js_string

    total_groups = len(sorted_items)
    del_groups_count = len(eligible_ids)
    keep_groups_count = total_groups - del_groups_count

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
                rejection_reasons.append({'icon': _REASON_HTML_ICON[code], 'text': _reason_text(code)})

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

    orig_count = total_groups
    cross_count = total_torrents - total_groups

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
        f"<b>Eligible Only:</b> {ELIGIBLE_ONLY}",
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

        .charts-row { display: flex; gap: 20px; margin-bottom: 20px; }
        .chart-col { flex: 1; background: #1e1e1e; padding: 15px; border-radius: 6px; border: 1px solid #333; }
        .chart-container { position: relative; height: 350px; width: 100%; }

        .metrics-row { display: flex; gap: 20px; margin-bottom: 20px; }
        .metric-col { flex: 1; background: #1e1e1e; padding: 20px; border-radius: 6px; border: 1px solid #333; }

        .metric-title { font-size: 16px; font-weight: bold; margin-bottom: 15px; border-bottom: 1px solid #333; padding-bottom: 10px; }
        .metric-item { display: flex; justify-content: space-between; margin-bottom: 8px; font-size: 14px; }
        .metric-val { font-weight: bold; color: #fff; }

        .table-container { overflow-x: auto; }
        table { width: 100%; border-collapse: separate; border-spacing: 0; font-size: 13px; table-layout: fixed; }
        th { text-align: left; padding: 12px 8px; background: #252525; color: #aaa; font-weight: 600; border-bottom: 2px solid #333; position: sticky; top: 0; white-space: nowrap; cursor: pointer; user-select: none; }
        th:hover { color: #fff; background: #333; }
        td { padding: 8px; border-bottom: 1px solid #2a2a2a; vertical-align: middle; color: #ddd; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        tr:hover td { background: #2a2a2a; }
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
        .rejection-icon { cursor: help; margin-left: 2px; font-size: 14px; opacity: 0.8; }
        .rejection-icon:hover { opacity: 1; }
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
                        <div>• Filtering categories: <span style="color:#fff; float:right">{SCAN_STATS.get('filter_duration', 0):.2f}s</span></div>
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
                <div class="metric-title" style="color: #ff5252;">Eligible for Deletion ({stats_eligible['count']})</div>
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
            <table id="reportTable">
                <thead>
                    <tr>
                        <th onclick="sortTable(0)" style="width:140px">Status<div class="resizer"></div></th>
                        <th onclick="sortTable(1)" style="width:70px">Type<div class="resizer"></div></th>
                        <th onclick="sortTable(2)" style="width:40px">Seeds<div class="resizer"></div></th>
                        <th onclick="sortTable(3)" style="width:35px">Ratio<div class="resizer"></div></th>
                        <th onclick="sortTable(4)" style="width:70px">Size<div class="resizer"></div></th>
                        <th onclick="sortTable(5)" style="width:70px">Uploaded<div class="resizer"></div></th>
                        <th onclick="sortTable(6)" style="width:80px">Seeded (D:H)<div class="resizer"></div></th>
                        <th onclick="sortTable(7)" style="width:100px">Added<div class="resizer"></div></th>
                        <th onclick="sortTable(8)" style="width:160px">Tracker<div class="resizer"></div></th>
                        <th onclick="sortTable(9)" style="width:130px">Category<div class="resizer"></div></th>
                        <th onclick="sortTable(10)">Name<div class="resizer"></div></th>
                        <th onclick="sortTable(11)">Path<div class="resizer"></div></th>
                    </tr>
                </thead>
    """]

    for row in report_rows:
        if ELIGIBLE_ONLY and not row['is_del']:
            continue

        d = row['data']
        is_del_group = row['is_del']

        status_class = "status-delete" if is_del_group else "status-keep"
        status_text = "DELETE" if is_del_group else "KEEP"
        badge_html = f'<span class="status-badge {status_class}">{status_text}</span>'

        reasons_html = ""
        if not is_del_group:
            reasons = row.get('reasons', [])
            if reasons:
                reasons_html = "".join(
                    f'<span class="rejection-icon" title="{_h(r["text"])}">{_h(r["icon"])}</span>'
                    for r in reasons
                )

        status_cell_content = f'<div class="status-container">{badge_html}{reasons_html}</div>'
        torrents_to_list = [d['original']] + d['crossseeds']

        html_parts.append('<tbody class="group-body">')

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

            if is_orig:
                c_size = "text-success" if t_size >= MIN_SIZE_BYTES else "text-danger"
                c_time = "text-success" if t_time >= MIN_ORIGINAL_SEED_TIME_HOURS * 3600 else "text-danger"
                c_cat = "text-success" if category_allowed(t_cat) else "text-danger"

                if t.get('_path_error'):
                    c_cat = "text-danger"

            name_h = _h(t.get('name', ''))
            path_h = _h(t.get('content_path', ''))
            html_parts.append(f"""
            <tr>
                <td>{status_cell_content}</td>
                <td>{type_badge}</td>
                <td><span class="{c_seeds}">{cur_seeds}</span></td>
                <td>{t.get('ratio', 0):.2f}</td>
                <td><span class="{c_size}">{format_size_smart(t_size)}</span></td>
                <td>{format_size_smart(t.get('uploaded', 0))}</td>
                <td><span class="{c_time}">{format_duration(t_time)}</span></td>
                <td style="font-size:11px; color:#888;">{added_ts}</td>
                <td>{_h(tracker_clean)}</td>
                <td><span class="{c_cat}">{_h(t_cat)}</span></td>
                <td class="name-cell" title="{name_h}">{name_h}</td>
                <td class="path-cell" title="{path_h}">{path_h}</td>
            </tr>
            """)

        external_path = d['original'].get('_external_path')
        if external_path:
            ext_status_cell = f'<div class="status-container"><span class="status-badge status-keep">KEEP</span>{reasons_html}</div>'

            orig_size = d['original'].get('size', 0)

            html_parts.append(f"""
            <tr>
                <td>{ext_status_cell}</td>
                <td><span class="type-badge" style="color:#aaa; border:1px solid #555;">EXT</span></td>
                <td style="text-align:center; color:#555;">-</td>
                <td style="text-align:center; color:#555;">-</td>
                <td><span class="text-success">{format_size_smart(orig_size)}</span></td>
                <td style="text-align:center; color:#555;">-</td>
                <td style="text-align:center; color:#555;">-</td>
                <td style="text-align:center; color:#555;">-</td>
                <td style="text-align:center; color:#555;">-</td>
                <td><span style="color:#2196f3;">External Library</span></td>
                <td class="name-cell" style="color:#2196f3; font-style:italic;">{_h(d['original'].get('name', ''))}</td>
                <td class="path-cell">{_h(external_path)}</td>
            </tr>
            """)


        html_parts.append("</tbody>")

    html_parts.append("""
            </table>
            </div>
        </div>
    </div>
    """)
    html_body = "".join(html_parts)

    full_html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'">
        <title>Cross-Seed Cleaner Report</title>
        <meta charset="UTF-8">
        <script>{CHARTJS_SOURCE}</script>
        <style>{css_block}</style>
    </head>
    <body>
        {html_body}
        <script>
            const createResizableTable = function(table) {{
                const cols = table.querySelectorAll('th');
                [].forEach.call(cols, function(col) {{
                    const resizer = col.querySelector('.resizer');
                    if (!resizer) return;
                    let x = 0; let w = 0;
                    const mouseDownHandler = function(e) {{
                        x = e.clientX;
                        const styles = window.getComputedStyle(col);
                        w = parseInt(styles.width, 10);
                        document.addEventListener('mousemove', mouseMoveHandler);
                        document.addEventListener('mouseup', mouseUpHandler);
                        resizer.classList.add('resizing');
                    }};
                    const mouseMoveHandler = function(e) {{
                        const dx = e.clientX - x;
                        col.style.width = (w + dx) + 'px';
                    }};
                    const mouseUpHandler = function() {{
                        document.removeEventListener('mousemove', mouseMoveHandler);
                        document.removeEventListener('mouseup', mouseUpHandler);
                        resizer.classList.remove('resizing');
                    }};
                    resizer.addEventListener('mousedown', mouseDownHandler);
                }});
            }};
            createResizableTable(document.getElementById('reportTable'));

            let sortDirection = 1;
            let lastSortedCol = -1;

            function sortTable(n) {{
                const table = document.getElementById("reportTable");
                const groups = Array.from(table.getElementsByClassName("group-body"));

                // Reset direction if clicking a new column
                if (n !== lastSortedCol) {{
                    sortDirection = 1;
                    lastSortedCol = n;
                }} else {{
                    sortDirection *= -1;
                }}

                const getVal = (grp, idx) => {{
                    const firstRow = grp.rows[0];
                    if (!firstRow) return "";
                    return firstRow.cells[idx].innerText.trim();
                }};

                const parseSize = (s) => {{
                    const match = s.match(/^([\\d\\.]+)\\s*(B|KiB|MiB|GiB|TiB|PiB)$/i);
                    if (!match) return 0;
                    const v = parseFloat(match[1]);
                    const u = match[2].toLowerCase();
                    const mul = {{ 'b': 1, 'kib': 1024, 'mib': 1048576, 'gib': 1.073741824e+09, 'tib': 1.099511627776e+12, 'pib': 1.125899906842624e+15 }};
                    return v * (mul[u] || 1);
                }};

                const parseTime = (s) => {{
                    if (!s.includes(':')) return 0;
                    const parts = s.split(':').map(Number);
                    if (parts.length === 2) return (parts[0] * 24) + parts[1];
                    return 0;
                }};

                groups.sort((a, b) => {{
                    const valA = getVal(a, n);
                    const valB = getVal(b, n);

                    // 1. Size Columns (4: Size, 5: Uploaded)
                    if (n === 4 || n === 5) {{
                        return (parseSize(valA) - parseSize(valB)) * sortDirection;
                    }}

                    // 2. Duration Column (6: Seeded)
                    if (n === 6) {{
                        return (parseTime(valA) - parseTime(valB)) * sortDirection;
                    }}

                    // 3. Pure Numeric Columns (2: Seeds, 3: Ratio)
                    if (n === 2 || n === 3) {{
                         return (parseFloat(valA) - parseFloat(valB)) * sortDirection;
                    }}

                    // 4. Default: String Sort (Name, Category, Tracker, etc.)
                    // Use localeCompare for correct alphabetical sorting
                    return valA.localeCompare(valB, undefined, {{numeric: true, sensitivity: 'base'}}) * sortDirection;
                }});

                groups.forEach(g => table.appendChild(g));
            }}

            const ctxCount = document.getElementById('countChart').getContext('2d');
            const ctxSize = document.getElementById('sizeChart').getContext('2d');
            const ctxGroup = document.getElementById('groupChart').getContext('2d');

            const labels = {_js(chart_labels)};
            const groupLabels = {_js(group_chart_labels)};

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
                        {{ label: 'Eligible (Groups)', data: {_js(ds_group_del)}, backgroundColor: '#ff5252' }},
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
            client.delete_torrents([t['hash'] for t in ts])
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
    print(f"{Colors.BOLD}[5/7]{Colors.END} Filtering torrents for orphans...")

    debug_log(f"[FILTER] Starting analysis of {len(category_torrents)} category torrents against {len(external_inodes)} external inodes...")

    identity_map = defaultdict(list)
    for t in category_torrents:
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

    print(f"{Colors.BOLD}[6/7]{Colors.END} Analyze deletable torrents...")
    t_start = datetime.now()
    emap = {}
    for idx, (h, d) in enumerate(sorted_items, 1):
        elig, ts = print_group(client, d, idx, len(sorted_items))
        if elig:
            emap[idx] = ts
    SCAN_STATS['analyze_duration'] = (datetime.now() - t_start).total_seconds()
    print(f"{Colors.GREEN}\n  ✓ Analysis complete in {SCAN_STATS['analyze_duration']:.2f}s.{Colors.END}")

    print(f"\n{Colors.BOLD}[7/7]{Colors.END} Finalizing & exporting reports...")
    stats = calculate_stats(all_groups, emap)
    print_summary(stats)
    export_reports(sorted_items, emap.keys())
    _finalize_deletion(client, emap)


def main():
    print_header()
    print_config()
    client = QBittorrentClient(QBITTORRENT_HOST, QBITTORRENT_USER, QBITTORRENT_PASS)
    if NO_HARD_LINKS_MODE:
        check_no_hard_links(client)
        return
    all_groups = load_and_group_torrents(client)
    _run_analyze_and_finalize(client, all_groups)


if __name__ == "__main__":
    main()
