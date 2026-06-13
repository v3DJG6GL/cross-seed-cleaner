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


# ─── smart_split_paths ───────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("", []),
    ("/a,/b", ["/a", "/b"]),
    ("/a, /b", ["/a", "/b"]),                  # stripped
    ("/m/{a,b}", ["/m/{a,b}"]),                # comma inside braces not split
    ("/m/{a,b},/n", ["/m/{a,b}", "/n"]),
    ("/a,", ["/a"]),                           # trailing comma dropped
    ("   ", []),                               # whitespace-only dropped
    ("/m/{a,b", ["/m/{a,b"]),                  # unbalanced: never returns to depth 0
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


# ─── dry-run resolution matrix ───────────────────────────────────────────────

def test_dry_run_default_true(csc):
    assert csc.DRY_RUN is True


def test_dry_run_env_false():
    assert load_module(env={"DRY_RUN": "false"}).DRY_RUN is False


def test_delete_flag_overrides_env_true():
    assert load_module(env={"DRY_RUN": "true"}, argv=["x", "--delete"]).DRY_RUN is False


def test_dry_run_flag_overrides_env_false():
    assert load_module(env={"DRY_RUN": "false"}, argv=["x", "--dry-run"]).DRY_RUN is True


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
