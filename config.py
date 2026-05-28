"""
Cross-Seed Cleaner — user-editable settings.

Edit this file to configure cross-seed-cleaner. Environment variables and
CLI flags override these values at runtime (see get_config() in
cross_seed_cleaner.py for the full precedence chain).

Two sections are NOT overridable and live here as the sole source of truth:
CATEGORY_ALLOWLIST / CATEGORY_BLOCKLIST, and PATH_MAPPINGS.
"""

# ─── CONNECTION ────────────────────────────────────────────────────────────
# Connection details for the qBittorrent Web UI.
# The URL must start with http:// or https:// and include :port if qBittorrent runs on a non-default port.
# Don't add a trailing slash.
QBITTORRENT_HOST = "http://localhost:8080"
QBITTORRENT_USER = "admin"
QBITTORRENT_PASS = "password"

# API key for qBittorrent v5.2.0+ (WebAPI v2.14.1+). Format: "qbt_" + 28 chars.
# When set (non-empty), it is used instead of username/password — generate it in
# qBittorrent: Preferences -> WebUI -> API Key. Leave empty to use user/password.
QBITTORRENT_API_KEY = ""


# ─── SAFETY LIMITS ─────────────────────────────────────────────────────────
# A group is kept (not deleted) if any of its torrents has fewer than X seeders.
# In No-Hard-Links mode, only the orphan torrent itself is checked.
MIN_SEEDERS = 4

# If a group is made up of X or more torrents in total (Original plus Cross-Seeds), it is kept.
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


# ─── REPORT EXPORTS ────────────────────────────────────────────────────────
# A timestamp is added to each export filename before the extension (e.g. output.html -> output_2026.04.21_14.30.00.html).
# Leave empty (or pass an empty string via environment variable / command-line) to turn the export off.
HTML_EXPORT = "output.html"
CSV_EXPORT = "output.csv"


# ─── RUN MODES ─────────────────────────────────────────────────────────────
# Enable verbose logging
DEBUG_MODE = False

# True = simulate only (nothing is deleted); False = actually delete torrents.
# The --dry-run / --delete command-line flags override this.
# In Manual Mode the script always asks you to confirm before deleting, no matter what this is set to.
DRY_RUN = True


# ─── NO-HARD-LINKS MODE ────────────────────────────────────────────────────
# When enabled, the script finds torrents in selected qBittorrent categories that have NO hard-links.
# Category selection supports:
# - Exact match:  "cross-seed-links"
# - Regex match:  prefix with "r:", e.g. "r:autobrr-.*"
# - Combined:     "cross-seed-category,r:autobrr-.*"
# Category names and patterns are compared in lowercase — write yours in lowercase to make sure they match.
NO_HARD_LINKS_MODE = False
NO_HARD_LINKS_CATEGORIES = "cross-seed-category,r:autobrr-.*"


# ─── EXTERNAL MEDIA PATHS ──────────────────────────────────────────────────
# Path(s) to external media libraries to scan for hardlinks (used in No-Hard-Links mode).
# Supports:
# 1. Comma-separated paths:         "/mnt/movies, /mnt/tv"
# 2. Wildcards (* = anything, ? = one character, [abc] = one of the listed characters): "/mnt/users/*"
# 3. Brace groups {a,b,c} expand into multiple paths (not nested): "/mnt/media/{movies,tv,anime}"
# 4. Mixed — combine any of the above: "/mnt/local/{movies,tv}, /mnt/remote/user_*"
EXTERNAL_MEDIA_PATHS = "/mnt/hdd-pool/userdata/media/{user_1,user_2,user_3}"


# ─── CATEGORY FILTERING ────────────────────────────────────────────────────
# CATEGORY_FILTER_MODE applies to *any* torrent in a hardlink group. If any
# member of the group is blocked (or not in the allowlist), the whole group
# is kept — avoids deleting allowed partners of a protected torrent.
# Choose one:
#   "allow" = Only process groups where every member matches ALLOWLIST
#   "block" = Skip groups where any member matches BLOCKLIST
#   "both"  = Every member must match ALLOWLIST and none may match BLOCKLIST
#   "none"  = Disable category filtering (process everything)
CATEGORY_FILTER_MODE = "block"

# Each ALLOWLIST / BLOCKLIST entry is either an exact category name or a regex prefixed with "r:".
# Examples:
#   "Movies"       -> Exact match for category "Movies"
#   "r:.*-4k$"     -> Regex match (matches "Movies-4k", "TV-4k"); also "r:autobrr-.*"
# Regex patterns match from the start of the category name — add "$" at the end (like in "r:.*-4k$") to require the full name to match.
# Matching is case-sensitive.
# These two lists cannot be overridden by environment variable or command-line.
CATEGORY_ALLOWLIST = ["sonarr-imported", "radarr-imported", "lidarr-imported", "r:.*-allowsuffix$"]
CATEGORY_BLOCKLIST = ["freeleech-orpheus", "r:.*-blocksuffix$"]


# ─── SORTING ───────────────────────────────────────────────────────────────
# SORT_BY — sort field for the CLI table and for group-processing order. Choose one:
#   "seeders" / "seeds" = Number of active seeders (aliases)
#   "ratio"             = Share ratio
#   "size"              = Torrent size
#   "uploaded"          = Total uploaded amount
#   "added"             = Date added (useful to see oldest first)
#   "time"              = Seeding time
#   "name"              = Name of torrent (case-insensitive)
SORT_BY = "name"

# SORT_ORDER — direction. Choose one:
#   "asc"  = Ascending (Smallest/Oldest first)
#   "desc" = Descending (Largest/Newest first)
SORT_ORDER = "asc"


# ─── UNRELIABLE TRACKERS ───────────────────────────────────────────────────
# Some trackers misreport the seeder count.
# For trackers listed here, the script estimates the seeder count from the total peer count (seeders + leechers) instead.
# Patterns are matched against the tracker's domain, with any leading "tracker." or "www." removed.
# Comma-separated.
# Each entry is either a plain domain or a regex prefixed with "r:".
# Regex patterns match from the start of the domain — add "$" at the end to require a full match.
UNRELIABLE_TRACKERS = "hdts-announce.ru,hd-space.pw,tfa.tf"


# ─── PATH MAPPINGS (hardcoded) ─────────────────────────────────────────────
# Remap internal qBittorrent (container) paths to host paths so the script can check files on disk.
# Format: {"Internal Container Path": "Host Path"}
# If two mappings could both apply (e.g. "/media" and "/media/downloads" both match "/media/downloads/file.mkv"), the more-specific (longer) one is used.
# Paths that don't match any prefix are used as-is.
# Cannot be overridden by environment variable or command-line.
PATH_MAPPINGS = {
    "/media/downloads/torrents": "/mnt/hdd-pool/userdata/media/downloads/torrents",
    "/media/downloads/freeleech": "/mnt/hdd-pool/appdata/qbittorrent/freeleech",
}
