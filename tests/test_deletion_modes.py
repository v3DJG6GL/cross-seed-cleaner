"""Interactive manual loop and auto-delete finalize paths
(cross_seed_cleaner.py:2908-3003). Deletion is destructive, so the gating and
the success/failure reporting are safety-critical."""
import builtins

import pytest

from conftest import FakeClient


def feed(monkeypatch, responses):
    """Make builtins.input return each item of `responses` in turn."""
    it = iter(responses)
    monkeypatch.setattr(builtins, "input", lambda *a, **k: next(it))


def emap(*gids):
    return {g: [{"hash": f"h{g}"}] for g in gids}


# ─── manual_loop ─────────────────────────────────────────────────────────────

def test_manual_empty_emap_no_prompt(csc, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("input should not be called for empty emap")
    monkeypatch.setattr(builtins, "input", boom)
    csc.manual_loop(FakeClient(), {})   # returns immediately


def test_manual_quit(csc, monkeypatch):
    feed(monkeypatch, ["q"])
    client = FakeClient()
    csc.manual_loop(client, emap(1, 2))
    assert client.deleted == []


def test_manual_all_confirmed(csc, monkeypatch):
    feed(monkeypatch, ["all", "YES"])
    client = FakeClient()
    m = emap(1, 2)
    csc.manual_loop(client, m)
    assert len(client.deleted) == 2
    assert m == {}                       # all popped


def test_manual_all_cancelled(csc, monkeypatch):
    feed(monkeypatch, ["all", "no", "q"])
    client = FakeClient()
    m = emap(1, 2)
    csc.manual_loop(client, m)
    assert client.deleted == []
    assert set(m) == {1, 2}


def test_manual_select_id(csc, monkeypatch):
    feed(monkeypatch, ["1", "YES", "q"])
    client = FakeClient()
    m = emap(1, 2)
    csc.manual_loop(client, m)
    assert client.deleted == [(["h1"], True)]
    assert set(m) == {2}


def test_manual_non_numeric_ignored(csc, monkeypatch):
    feed(monkeypatch, ["abc", "q"])      # ignored -> no valid ids -> reprompt -> quit
    client = FakeClient()
    csc.manual_loop(client, emap(1))
    assert client.deleted == []


def test_manual_yes_is_case_sensitive(csc, monkeypatch):
    feed(monkeypatch, ["1", "yes", "q"])  # lowercase 'yes' != 'YES' -> cancelled
    client = FakeClient()
    m = emap(1)
    csc.manual_loop(client, m)
    assert client.deleted == []
    assert set(m) == {1}


def test_manual_dry_run_result_keeps_group(csc, monkeypatch):
    feed(monkeypatch, ["1", "YES", "q"])
    client = FakeClient(delete_results=["dry_run"])
    m = emap(1)
    csc.manual_loop(client, m)
    assert set(m) == {1}                 # not popped on dry_run


def test_manual_failure_keeps_group(csc, monkeypatch):
    feed(monkeypatch, ["1", "YES", "q"])
    client = FakeClient(delete_results=[None])   # request failure
    m = emap(1)
    csc.manual_loop(client, m)
    assert len(client.deleted) == 1              # attempted
    assert set(m) == {1}                         # but kept, not falsely reported deleted


def test_manual_duplicate_id(csc, monkeypatch):
    feed(monkeypatch, ["1,1", "YES", "q"])
    client = FakeClient()
    m = emap(1)
    csc.manual_loop(client, m)
    assert len(client.deleted) == 1              # second occurrence guarded by `gid in emap`


# ─── _finalize_deletion ──────────────────────────────────────────────────────

def test_finalize_manual_precedence(csc, monkeypatch):
    csc.MANUAL_MODE = True
    csc.DRY_RUN = True                   # would block auto, but manual takes precedence
    feed(monkeypatch, ["q"])
    client = FakeClient()
    csc._finalize_deletion(client, emap(1))
    assert client.deleted == []          # manual loop entered, user quit


def test_finalize_empty_nothing_to_delete(csc, monkeypatch):
    csc.MANUAL_MODE = False
    csc.DRY_RUN = False
    monkeypatch.setattr(builtins, "input", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no prompt")))
    client = FakeClient()
    csc._finalize_deletion(client, {})
    assert client.deleted == []


def test_finalize_dry_run_no_delete(csc, monkeypatch):
    csc.MANUAL_MODE = False
    csc.DRY_RUN = True
    monkeypatch.setattr(builtins, "input", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no prompt")))
    client = FakeClient()
    csc._finalize_deletion(client, emap(1))
    assert client.deleted == []


def test_finalize_live_confirmed(csc, monkeypatch):
    csc.MANUAL_MODE = False
    csc.DRY_RUN = False
    feed(monkeypatch, ["YES"])
    client = FakeClient()
    csc._finalize_deletion(client, emap(1, 2))
    assert len(client.deleted) == 2
    assert all(delete_files is True for _h, delete_files in client.deleted)


def test_finalize_live_cancelled(csc, monkeypatch):
    csc.MANUAL_MODE = False
    csc.DRY_RUN = False
    feed(monkeypatch, ["no"])
    client = FakeClient()
    csc._finalize_deletion(client, emap(1))
    assert client.deleted == []


def test_finalize_live_failure_continues(csc, monkeypatch):
    csc.MANUAL_MODE = False
    csc.DRY_RUN = False
    feed(monkeypatch, ["YES"])
    client = FakeClient(delete_results=[None, ""])   # first fails, loop continues
    csc._finalize_deletion(client, emap(1, 2))
    assert len(client.deleted) == 2
