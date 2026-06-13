"""Tracker-error mode: eligibility rule + scan_dead_trackers pipeline
(cross_seed_cleaner.py:evaluate_dead_trackers / scan_dead_trackers)."""
import time

from conftest import FakeClient, reconfigure


NOW = 1_700_000_000          # arbitrary "now" used in unit tests
OLD = NOW - 60 * 24 * 3600   # 60 days ago — safely past both default windows (1d min-age, 30d min-inactivity)


def _tr(url='http://t/announce', status=5, updating=False, msg=''):
    """Build a tracker dict matching the qBittorrent WebAPI shape."""
    return {'url': url, 'status': status, 'updating': updating, 'msg': msg}


def _torrent(h='h1', trackers=None, category='', added=OLD, last_activity=0, **extra):
    """Build a torrent dict with `_trackers` stashed (as the production
    fetch path does). last_activity defaults to 0 (no activity recorded)
    so the recent-activity check defaults to "skip" — tests that need it
    pass last_activity explicitly."""
    d = {
        'hash': h, 'name': h, 'category': category, 'added_on': added,
        'last_activity': last_activity,
        'size': 100, 'num_complete': 0, 'num_incomplete': 0, 'tracker': '',
        '_trackers': trackers or [],
    }
    d.update(extra)
    return d


def _eval(csc, t, now=NOW):
    return csc.evaluate_dead_trackers({'original': t}, now)


# ─── evaluate_dead_trackers: the eligibility rule ────────────────────────────

def test_eligible_when_all_real_trackers_dead(csc):
    t = _torrent(trackers=[_tr(status=4), _tr(url='http://b/announce', status=5)])
    r = _eval(csc, t)
    assert r['eligible'] is True
    assert r['reasons'] == []
    assert r['all_torrents'] == [t]
    assert r['externally_linked'] is False


def test_excluded_when_one_tracker_working(csc):
    t = _torrent(trackers=[_tr(status=5), _tr(url='http://b/announce', status=2)])
    r = _eval(csc, t)
    assert r['eligible'] is False
    assert r['reasons'] == ['TRACKER_ALIVE']


def test_excluded_when_one_not_contacted_yet(csc):
    """Status 1 = "Not contacted yet" — torrent may still be alive."""
    t = _torrent(trackers=[_tr(status=5), _tr(url='http://b/announce', status=1)])
    r = _eval(csc, t)
    assert r['eligible'] is False
    assert r['reasons'] == ['TRACKER_ALIVE']


def test_excluded_when_a_tracker_is_updating(csc):
    t = _torrent(trackers=[_tr(status=5, updating=True)])
    r = _eval(csc, t)
    assert r['eligible'] is False
    assert r['reasons'] == ['TRACKER_UPDATING']


def test_excluded_when_only_sticky_trackers(csc):
    """DHT/PeX/LSD entries have URLs starting with "**" — never count as
    real trackers, so a torrent that has only those is excluded with
    NO_REAL_TRACKERS (no signal either way about real announce health)."""
    t = _torrent(trackers=[_tr(url='** [DHT] **', status=0),
                           _tr(url='** [PeX] **', status=0),
                           _tr(url='** [LSD] **', status=0)])
    r = _eval(csc, t)
    assert r['eligible'] is False
    assert r['reasons'] == ['NO_REAL_TRACKERS']


def test_excluded_when_added_recently(csc):
    """A torrent added 5 minutes ago is well within the default 1-day min-age."""
    t = _torrent(trackers=[_tr(status=5)], added=NOW - 5 * 60)
    r = _eval(csc, t)
    assert r['eligible'] is False
    assert 'RECENTLY_ADDED' in r['reasons']


def test_min_age_can_be_disabled(csc):
    """TRACKER_ERROR_MIN_AGE_DAYS = 0 disables the min-age check entirely."""
    reconfigure(csc, TRACKER_ERROR_MIN_AGE_DAYS=0)
    t = _torrent(trackers=[_tr(status=5)], added=NOW - 5)   # added 5 seconds ago
    r = _eval(csc, t)
    assert r['eligible'] is True


def test_excluded_when_added_on_zero(csc):
    """added_on == 0 means we don't know how old it is — conservative skip."""
    t = _torrent(trackers=[_tr(status=5)], added=0)
    r = _eval(csc, t)
    assert r['eligible'] is False
    assert r['reasons'] == ['NO_ADDED_TIME']


def test_status_zero_disqualifies(csc):
    """qBittorrent emits status 0 only for sticky entries when disabled, but
    a multi-tier real tracker that fell to status 0 (a defensive case) must
    not be treated as dead — status 0 is not in DEAD_TRACKER_STATUSES."""
    t = _torrent(trackers=[_tr(status=0)])
    r = _eval(csc, t)
    assert r['eligible'] is False
    assert r['reasons'] == ['TRACKER_ALIVE']


def test_configurable_dead_set_strict(csc):
    """With DEAD_TRACKER_STATUSES={5}, a tracker reporting status 4
    (Not working) disqualifies the torrent."""
    reconfigure(csc, DEAD_TRACKER_STATUSES=[5])
    t = _torrent(trackers=[_tr(status=4)])
    r = _eval(csc, t)
    assert r['eligible'] is False
    assert r['reasons'] == ['TRACKER_ALIVE']


def test_configurable_dead_set_inclusive(csc):
    """With DEAD_TRACKER_STATUSES={4,5,6}, every error code is fatal."""
    reconfigure(csc, DEAD_TRACKER_STATUSES=[4, 5, 6])
    for st in (4, 5, 6):
        t = _torrent(trackers=[_tr(status=st)])
        assert _eval(csc, t)['eligible'] is True, f"status {st} should be dead"


def test_reasons_combine_when_multiple_apply(csc):
    """Both min-age AND tracker-alive can fire on the same torrent."""
    t = _torrent(
        trackers=[_tr(status=5), _tr(url='http://b/announce', status=2)],
        added=NOW - 60,
    )
    r = _eval(csc, t)
    assert r['eligible'] is False
    assert set(r['reasons']) == {'RECENTLY_ADDED', 'TRACKER_ALIVE'}


# ─── recent-activity guardrail ───────────────────────────────────────────────

def test_excluded_when_recent_activity(csc):
    """Default min-inactivity is 30 days. A torrent with peers swapping
    data 5 days ago is protected — DHT/PeX may be keeping it alive even
    if the tracker is dead."""
    t = _torrent(trackers=[_tr(status=5)], last_activity=NOW - 5 * 24 * 3600)
    r = _eval(csc, t)
    assert r['eligible'] is False
    assert 'RECENT_ACTIVITY' in r['reasons']


def test_eligible_when_last_activity_is_old(csc):
    """Activity 60 days ago — well past the 30-day default. Eligible."""
    t = _torrent(trackers=[_tr(status=5)], last_activity=NOW - 60 * 24 * 3600)
    r = _eval(csc, t)
    assert r['eligible'] is True


def test_eligible_when_last_activity_zero(csc):
    """last_activity = 0 means "never seen any peers" — qBittorrent emits
    this for torrents that have never exchanged data. The inactivity
    check skips, and the added_on min-age covers freshness instead."""
    t = _torrent(trackers=[_tr(status=5)], last_activity=0)
    r = _eval(csc, t)
    assert r['eligible'] is True


def test_inactivity_check_disabled_when_zero_days(csc):
    """Setting TRACKER_ERROR_MIN_INACTIVITY_DAYS=0 turns the check off
    entirely — even a torrent with activity right now becomes eligible
    purely on tracker state."""
    reconfigure(csc, TRACKER_ERROR_MIN_INACTIVITY_DAYS=0)
    t = _torrent(trackers=[_tr(status=5)], last_activity=NOW - 60)
    r = _eval(csc, t)
    assert r['eligible'] is True


def test_inactivity_threshold_configurable(csc):
    """Lower the threshold to 1 day: a torrent inactive for 2 days is
    now eligible; one inactive for 12h is still protected."""
    reconfigure(csc, TRACKER_ERROR_MIN_INACTIVITY_DAYS=1)
    eligible_t = _torrent(trackers=[_tr(status=5)], last_activity=NOW - 2 * 24 * 3600)
    assert _eval(csc, eligible_t)['eligible'] is True
    protected_t = _torrent(trackers=[_tr(status=5)], last_activity=NOW - 12 * 3600)
    assert 'RECENT_ACTIVITY' in _eval(csc, protected_t)['reasons']


# ─── scan_dead_trackers: pipeline integration ────────────────────────────────

def _setup_scan(csc, monkeypatch, torrents, trackers_by_hash, **client_kwargs):
    """Wire scan_dead_trackers so _run_analyze_and_finalize captures groups."""
    csc.TRACKER_ERROR_MODE = True
    fake = FakeClient(torrents=torrents, trackers_by_hash=trackers_by_hash, **client_kwargs)
    captured = {}
    monkeypatch.setattr(csc, '_run_analyze_and_finalize',
                        lambda client, all_groups, **kw: captured.update(all_groups))
    return fake, captured


def test_scan_passes_dead_to_finalize(csc, monkeypatch):
    """End-to-end: a torrent with all dead trackers reaches finalize as eligible."""
    # added_on must be old enough to clear the min-age window — use a real recent past
    old = int(time.time()) - 2 * 24 * 3600   # 2 days ago — past the 1-day default min-age
    tor = {'hash': 'h1', 'name': 'dead-one', 'category': '', 'added_on': old,
           'size': 100, 'num_complete': 0, 'num_incomplete': 0, 'tracker': ''}
    fake, cap = _setup_scan(csc, monkeypatch, [tor],
                            {'h1': [_tr(status=5, msg='unregistered torrent')]})
    csc.scan_dead_trackers(fake)
    assert set(cap) == {'h1'}
    assert cap['h1']['_evaluation']['eligible'] is True
    # tracker error msg should be surfaced on the torrent
    assert cap['h1']['original']['_tracker_msg'] == 'unregistered torrent'


def test_scan_keeps_alive_torrent_with_reason(csc, monkeypatch):
    old = int(time.time()) - 2 * 24 * 3600   # 2 days ago — past the 1-day default min-age
    tor = {'hash': 'h1', 'name': 'alive', 'category': '', 'added_on': old,
           'size': 100, 'num_complete': 0, 'num_incomplete': 0, 'tracker': ''}
    fake, cap = _setup_scan(csc, monkeypatch, [tor],
                            {'h1': [_tr(status=5), _tr(url='http://b/announce', status=2)]})
    csc.scan_dead_trackers(fake)
    assert cap['h1']['_evaluation']['eligible'] is False
    assert cap['h1']['_evaluation']['reasons'] == ['TRACKER_ALIVE']


def test_category_blocklist_protects_dead_torrent(csc, monkeypatch):
    """A torrent in a blocked category appears in the report tagged
    CATEGORY_FILTER (so the user can see their blocklist is working)
    but is NOT eligible for deletion — matching how evaluate_group
    surfaces blocked rows in standard mode."""
    reconfigure(csc,
                CATEGORY_FILTER_MODE='block',
                CATEGORY_BLOCKLIST=['protected'])
    old = int(time.time()) - 2 * 24 * 3600   # 2 days ago — past the 1-day default min-age
    tor = {'hash': 'h1', 'name': 'dead-but-protected', 'category': 'protected',
           'added_on': old, 'size': 100, 'num_complete': 0, 'num_incomplete': 0,
           'tracker': ''}
    fake, cap = _setup_scan(csc, monkeypatch, [tor],
                            {'h1': [_tr(status=5)]})
    csc.scan_dead_trackers(fake)
    # Visible in the report (visibility fix)…
    assert set(cap) == {'h1'}
    # …but kept, with CATEGORY_FILTER as the reason.
    assert cap['h1']['_evaluation']['eligible'] is False
    assert 'CATEGORY_FILTER' in cap['h1']['_evaluation']['reasons']


def test_ignore_category_filter_flag_makes_blocked_torrent_eligible(csc, monkeypatch):
    """--tracker-error-mode-ignore-category-filter bypasses the blocklist
    (and allowlist) in tracker-error mode, so a dead torrent in a
    blocked category becomes eligible. Other modes are unaffected."""
    reconfigure(csc,
                CATEGORY_FILTER_MODE='block',
                CATEGORY_BLOCKLIST=['protected'],
                TRACKER_ERROR_MODE_IGNORE_CATEGORY_FILTER=True)
    old = int(time.time()) - 2 * 24 * 3600
    tor = {'hash': 'h1', 'name': 'dead-in-blocked-cat', 'category': 'protected',
           'added_on': old, 'size': 100, 'num_complete': 0, 'num_incomplete': 0,
           'tracker': ''}
    fake, cap = _setup_scan(csc, monkeypatch, [tor],
                            {'h1': [_tr(status=5)]})
    csc.scan_dead_trackers(fake)
    assert cap['h1']['_evaluation']['eligible'] is True
    assert 'CATEGORY_FILTER' not in cap['h1']['_evaluation']['reasons']


def test_ignore_category_filter_flag_also_bypasses_allowlist(csc, monkeypatch):
    """Symmetric: an allowlist that would normally exclude the torrent's
    category is also bypassed when the ignore flag is on."""
    reconfigure(csc,
                CATEGORY_FILTER_MODE='allow',
                CATEGORY_ALLOWLIST=['movies'],
                TRACKER_ERROR_MODE_IGNORE_CATEGORY_FILTER=True)
    old = int(time.time()) - 2 * 24 * 3600
    tor = {'hash': 'h1', 'name': 'dead-not-on-allowlist', 'category': 'random',
           'added_on': old, 'size': 100, 'num_complete': 0, 'num_incomplete': 0,
           'tracker': ''}
    fake, cap = _setup_scan(csc, monkeypatch, [tor],
                            {'h1': [_tr(status=5)]})
    csc.scan_dead_trackers(fake)
    assert cap['h1']['_evaluation']['eligible'] is True
    assert 'CATEGORY_FILTER' not in cap['h1']['_evaluation']['reasons']


def test_ignore_flag_makes_category_allowed_true_for_blocked_cat(csc):
    """category_allowed() drives the CLI/HTML category-cell coloring. When
    tracker-error mode + ignore-flag are on, an eligible torrent in a
    blocked category must not be rendered as red. Anchor the helper so
    a future refactor can't silently restore the visual inconsistency."""
    reconfigure(csc,
                CATEGORY_FILTER_MODE='block',
                CATEGORY_BLOCKLIST=['protected'],
                TRACKER_ERROR_MODE=True,
                TRACKER_ERROR_MODE_IGNORE_CATEGORY_FILTER=True)
    assert csc.category_allowed('protected') is True
    # Sanity: without the ignore flag, the blocklist still applies.
    reconfigure(csc,
                CATEGORY_FILTER_MODE='block',
                CATEGORY_BLOCKLIST=['protected'],
                TRACKER_ERROR_MODE=True,
                TRACKER_ERROR_MODE_IGNORE_CATEGORY_FILTER=False)
    assert csc.category_allowed('protected') is False


def test_bulk_path_used_when_supported(csc, monkeypatch):
    old = int(time.time()) - 2 * 24 * 3600   # 2 days ago — past the 1-day default min-age
    tors = [{'hash': f'h{i}', 'name': f'h{i}', 'category': '', 'added_on': old,
             'size': 100, 'num_complete': 0, 'num_incomplete': 0, 'tracker': ''}
            for i in range(3)]
    tbh = {t['hash']: [_tr(status=5)] for t in tors}
    fake, _cap = _setup_scan(csc, monkeypatch, tors, tbh, webapi_version='2.11.0')
    csc.scan_dead_trackers(fake)
    assert fake.bulk_calls == 1
    assert fake.per_hash_calls == 0     # bulk path didn't fall back


def test_fallback_path_used_when_too_old(csc, monkeypatch):
    old = int(time.time()) - 2 * 24 * 3600   # 2 days ago — past the 1-day default min-age
    tors = [{'hash': f'h{i}', 'name': f'h{i}', 'category': '', 'added_on': old,
             'size': 100, 'num_complete': 0, 'num_incomplete': 0, 'tracker': ''}
            for i in range(3)]
    tbh = {t['hash']: [_tr(status=5)] for t in tors}
    fake, _cap = _setup_scan(csc, monkeypatch, tors, tbh, webapi_version='2.10.0')
    csc.scan_dead_trackers(fake)
    assert fake.bulk_calls == 0
    assert fake.per_hash_calls == 3     # one per-torrent call each


def test_main_dispatches_to_tracker_error_mode(csc, monkeypatch):
    """main() routes to scan_dead_trackers when TRACKER_ERROR_MODE is on."""
    csc.TRACKER_ERROR_MODE = True
    csc.MISSING_HARD_LINKS_MODE = False
    monkeypatch.setattr(csc, 'print_header', lambda: None)
    monkeypatch.setattr(csc, 'print_config', lambda: None)
    monkeypatch.setattr(csc, 'QBittorrentClient', lambda *a, **kw: FakeClient())
    called = []
    monkeypatch.setattr(csc, 'scan_dead_trackers', lambda c: called.append('tracker'))
    monkeypatch.setattr(csc, 'check_missing_hard_links', lambda c: called.append('mhl'))
    monkeypatch.setattr(csc, 'load_and_group_torrents', lambda c: called.append('std'))
    csc.main()
    assert called == ['tracker']
