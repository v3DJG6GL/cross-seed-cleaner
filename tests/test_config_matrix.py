"""Configuration surface: str2bool, path splitting/brace expansion, config<env<CLI
precedence, validation exits, and CLI behavior (cross_seed_cleaner.py: get_config
/ _validate_config / str2bool / smart_split_paths / expand_braces)."""
import subprocess
import sys

import pytest

from conftest import load_module, REPO_ROOT


# ─── str2bool ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("val,expected", [
    ("yes", True), ("true", True), ("t", True), ("y", True), ("1", True),
    ("TRUE", True), ("Yes", True),
    ("no", False), ("false", False), ("0", False), ("on", False),
    ("2", False), ("", False), ("enabled", False), (" true ", False),
])
def test_str2bool(csc, val, expected):
    assert csc.str2bool(val) is expected


def test_str2bool_passthrough(csc):
    assert csc.str2bool(True) is True
    assert csc.str2bool(False) is False


# ─── str2bool_safe (DRY_RUN safety switch — fails SAFE) ───────────────────────

@pytest.mark.parametrize("val,expected", [
    # Explicit off tokens disable dry-run (arm live) — the documented triggers.
    ("no", False), ("false", False), ("f", False), ("n", False), ("0", False),
    ("off", False), ("disable", False), ("disabled", False),
    ("FALSE", False), (" off ", False),         # case + whitespace tolerant
    # Recognized true tokens stay simulated.
    ("yes", True), ("true", True), ("1", True), ("y", True),
    # The dangerous gap str2bool got wrong: unrecognized / empty / padded values
    # must NOT silently arm live deletion — they stay simulated (True), unlike
    # plain str2bool which returns False here.
    ("on", True), ("enabled", True), ("2", True), ("maybe", True),
    ("", True), (" true ", True), ("True ", True),
])
def test_str2bool_safe(csc, val, expected):
    assert csc.str2bool_safe(val) is expected


def test_str2bool_safe_passthrough(csc):
    assert csc.str2bool_safe(True) is True
    assert csc.str2bool_safe(False) is False


# ─── smart_split_paths ───────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("", []),
    ("/a,/b", ["/a", "/b"]),
    ("/a, /b", ["/a", "/b"]),                  # stripped
    ("/m/{a,b}", ["/m/{a,b}"]),                # comma inside braces not split
    ("/m/{a,b},/n", ["/m/{a,b}", "/n"]),
    ("/a,", ["/a"]),                           # trailing comma dropped
    ("   ", []),                               # whitespace-only dropped
    ("/m/{a,b", ["/m/{a,b"]),                  # unbalanced open: never returns to depth 0
    ("/a}b,/c", ["/a}b", "/c"]),               # stray close-brace must not swallow next comma
])
def test_smart_split_paths(csc, raw, expected):
    assert csc.smart_split_paths(raw) == expected


# ─── expand_braces ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("/data/{a,b}/m", ["/data/a/m", "/data/b/m"]),
    ("/m/{only}", ["/m/only"]),
    ("/x", ["/x"]),
])
def test_expand_braces(csc, text, expected):
    assert csc.expand_braces(text) == expected


def test_expand_braces_nested(csc):
    assert set(csc.expand_braces("/m/{a,{b,c}}")) == {"/m/a", "/m/b", "/m/c"}


# ─── config < env < CLI precedence ───────────────────────────────────────────

def test_precedence_config_default(csc):
    assert csc.MIN_SEEDERS == 4               # config.py default


def test_precedence_env_overrides_config():
    assert load_module(env={"MIN_SEEDERS": "7"}).MIN_SEEDERS == 7


def test_precedence_cli_overrides_env():
    m = load_module(env={"MIN_SEEDERS": "7"}, argv=["x", "--min-seeders", "9"])
    assert m.MIN_SEEDERS == 9


# ─── EXCLUDED_TRACKERS precedence (list option) ──────────────────────────────

def test_excluded_trackers_config_default_empty(csc):
    assert csc.EXCLUDED_TRACKERS == []        # config.py default "" -> []


def test_excluded_trackers_env_splits_to_list():
    m = load_module(env={"EXCLUDED_TRACKERS": "a.org, b.org"})
    assert m.EXCLUDED_TRACKERS == ["a.org", "b.org"]


def test_excluded_trackers_cli_overrides_env():
    m = load_module(env={"EXCLUDED_TRACKERS": "a.org"},
                    argv=["x", "--excluded-trackers", "c.org,d.org"])
    assert m.EXCLUDED_TRACKERS == ["c.org", "d.org"]


def test_excluded_trackers_bad_regex_exits():
    # A malformed r: pattern must fail at import (via _compile_specs), exactly
    # like every other r:-backed list option — not silently ignore the entry.
    with pytest.raises(SystemExit):
        load_module(env={"EXCLUDED_TRACKERS": "r:["})


# ─── validation exits (import-time) ──────────────────────────────────────────

@pytest.mark.parametrize("env", [
    {"SORT_BY": "bogus"},
    {"SORT_BY": "NAME"},        # case-sensitive
    {"SORT_ORDER": "DESC"},     # case-sensitive
    {"CATEGORY_FILTER_MODE": "deny"},
])
def test_validate_config_exits(env):
    with pytest.raises(SystemExit):
        load_module(env=env)


def test_filter_mode_is_case_insensitive():
    m = load_module(env={"CATEGORY_FILTER_MODE": "BOTH"})
    assert m.CATEGORY_FILTER_MODE == "BOTH"
    assert m._CATEGORY_FILTER_MODE_LC == "both"


# ─── numeric env coercion ────────────────────────────────────────────────────

def test_bad_int_env_exits_friendly(capsys):
    # A non-numeric numeric env override is a config typo; it must fail like
    # every other bad setting — a friendly ERROR + clean exit, not a raw
    # ValueError traceback.
    with pytest.raises(SystemExit):
        load_module(env={"MIN_SEEDERS": "abc"})
    err = capsys.readouterr().err
    assert "ERROR" in err and "MIN_SEEDERS" in err


def test_bad_float_env_exits_friendly(capsys):
    with pytest.raises(SystemExit):
        load_module(env={"MIN_SIZE_GIB": "1.2.3"})
    err = capsys.readouterr().err
    assert "ERROR" in err and "MIN_SIZE_GIB" in err


def test_float_inf_accepted():
    m = load_module(env={"MIN_SIZE_GIB": "inf"})
    assert m.MIN_SIZE_GIB == float("inf")


# ─── tracker-error threshold validation ──────────────────────────────────────

@pytest.mark.parametrize("name", [
    "TRACKER_ERROR_MIN_AGE_DAYS",
    "TRACKER_ERROR_MIN_INACTIVITY_DAYS",
])
def test_tracker_error_threshold_rejects_nan(name, capsys):
    # NaN parses via float() but slips past the old `< 0` check, then the
    # eligibility gate `THRESHOLD > 0` is also False — silently DISABLING the
    # recently-added / recent-activity protection (fail-OPEN: a freshly-added
    # dead-tracker torrent would become deletion-eligible). Validation must
    # reject it like a negative.
    with pytest.raises(SystemExit):
        load_module(env={name: "nan"})
    err = capsys.readouterr().err
    assert "ERROR" in err and name in err


@pytest.mark.parametrize("name", [
    "TRACKER_ERROR_MIN_AGE_DAYS",
    "TRACKER_ERROR_MIN_INACTIVITY_DAYS",
])
def test_tracker_error_threshold_rejects_negative(name):
    with pytest.raises(SystemExit):
        load_module(env={name: "-1"})


@pytest.mark.parametrize("name", [
    "TRACKER_ERROR_MIN_AGE_DAYS",
    "TRACKER_ERROR_MIN_INACTIVITY_DAYS",
])
def test_tracker_error_threshold_inf_accepted(name):
    # inf is over-protective (every torrent looks recently-added/active), so it
    # fails SAFE and stays accepted — only NaN and negatives are rejected.
    m = load_module(env={name: "inf"})
    assert getattr(m, name) == float("inf")


# ─── DEAD_TRACKER_STATUSES validation ────────────────────────────────────────

def test_dead_statuses_non_integer_exits(capsys):
    # A non-integer token is a config typo; _parse_dead_statuses must exit with
    # a friendly ERROR rather than silently dropping the entry or tracebacking.
    with pytest.raises(SystemExit):
        load_module(env={"DEAD_TRACKER_STATUSES": "4,x,6"})
    err = capsys.readouterr().err
    assert "non-integer" in err and "DEAD_TRACKER_STATUSES" in err


def test_dead_statuses_out_of_range_exits(capsys):
    # 3 is not a real qBittorrent tracker status; the set must be a subset of
    # {0,1,2,4,5,6} or the mode would key off a code qBittorrent never emits.
    with pytest.raises(SystemExit):
        load_module(env={"DEAD_TRACKER_STATUSES": "3"})
    err = capsys.readouterr().err
    assert "DEAD_TRACKER_STATUSES" in err and "invalid" in err


def test_dead_statuses_empty_exits(capsys):
    # An empty set makes NO tracker count as dead — every torrent kept, the
    # whole mode a silent no-op; reject it as invalid.
    with pytest.raises(SystemExit):
        load_module(env={"DEAD_TRACKER_STATUSES": ""})
    err = capsys.readouterr().err
    assert "DEAD_TRACKER_STATUSES" in err


def test_dead_statuses_valid_parses_to_frozenset():
    m = load_module(env={"DEAD_TRACKER_STATUSES": "4, 5 ,6"})
    assert m.DEAD_TRACKER_STATUSES == frozenset({4, 5, 6})


# ─── dry-run resolution matrix ───────────────────────────────────────────────

def test_dry_run_default_true(csc):
    assert csc.DRY_RUN is True


def test_dry_run_env_false():
    assert load_module(env={"DRY_RUN": "false"}).DRY_RUN is False


def test_delete_flag_overrides_env_true():
    assert load_module(env={"DRY_RUN": "true"}, argv=["x", "--delete"]).DRY_RUN is False


def test_dry_run_flag_overrides_env_false():
    assert load_module(env={"DRY_RUN": "false"}, argv=["x", "--dry-run"]).DRY_RUN is True


@pytest.mark.parametrize("val", ["on", "enabled", "2", "maybe", "", "true ", "True "])
def test_dry_run_unrecognized_env_stays_simulated(val):
    # A non-canonical / typo'd DRY_RUN value must fail SAFE: the run stays in
    # dry-run (simulated) mode rather than silently arming live deletion. Plain
    # str2bool would read all of these as False (live) — str2bool_safe keeps them
    # simulated.
    assert load_module(env={"DRY_RUN": val}).DRY_RUN is True


@pytest.mark.parametrize("val", ["off", "disabled", "no", "0"])
def test_dry_run_explicit_off_still_goes_live(val):
    # The documented ways to request live mode via DRY_RUN must keep working.
    assert load_module(env={"DRY_RUN": val}).DRY_RUN is False


def test_dry_run_and_delete_mutually_exclusive():
    with pytest.raises(SystemExit):
        load_module(argv=["x", "--dry-run", "--delete"])


# ─── tracker-error-mode auto-imply ───────────────────────────────────────────

def test_ignore_category_filter_implies_tracker_error_mode():
    m = load_module(argv=["x", "--tracker-error-mode-ignore-category-filter"])
    assert m.TRACKER_ERROR_MODE is True
    assert m.TRACKER_ERROR_MODE_IGNORE_CATEGORY_FILTER is True


def test_ignore_category_filter_env_implies_tracker_error_mode():
    m = load_module(env={"TRACKER_ERROR_MODE_IGNORE_CATEGORY_FILTER": "true"})
    assert m.TRACKER_ERROR_MODE is True


def test_no_implication_when_modifier_absent(csc):
    assert csc.TRACKER_ERROR_MODE is False
    assert csc.TRACKER_ERROR_MODE_IGNORE_CATEGORY_FILTER is False


def test_tracker_error_mode_cli_can_override_env_true():
    """If TRACKER_ERROR_MODE=true is set in env (or config.py), the CLI must
    still be able to disable the mode for an ad-hoc run via the BooleanOptionalAction
    negation `--no-tracker-error-mode`. Without this, env-true permanently
    locks the flag on."""
    m = load_module(env={"TRACKER_ERROR_MODE": "true"},
                    argv=["x", "--no-tracker-error-mode"])
    assert m.TRACKER_ERROR_MODE is False


def test_missing_hard_links_mode_cli_can_override_env_true():
    """Symmetric to the tracker-error case: with the rename from
    --no-hard-links-mode to --missing-hard-links-mode, the cancel form
    --no-missing-hard-links-mode now exists and must beat env-true."""
    m = load_module(env={"MISSING_HARD_LINKS_MODE": "true"},
                    argv=["x", "--no-missing-hard-links-mode"])
    assert m.MISSING_HARD_LINKS_MODE is False


# ─── true CLI behavior (subprocess) ──────────────────────────────────────────

def _run(*args):
    return subprocess.run([sys.executable, "cross_seed_cleaner.py", *args],
                          cwd=REPO_ROOT, capture_output=True, text=True)


def test_cli_help():
    r = _run("--help")
    assert r.returncode == 0
    assert "usage" in r.stdout.lower()


def test_cli_bad_flag():
    assert _run("--definitely-not-a-flag").returncode == 2


def test_cli_mutually_exclusive():
    assert _run("--dry-run", "--delete").returncode == 2
