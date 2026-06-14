"""Regression tests: the QBittorrentClient list-returning helpers must not
choke on a non-list response body.

_request returns the raw decoded string when a 200 response body isn't JSON
(e.g. a reverse proxy or captive portal returning an HTML/plain-text error
page). Callers that blindly iterate the result would crash with AttributeError
('str' has no attribute 'get') instead of falling back safely.
"""
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_module():
    saved_argv = sys.argv
    sys.argv = ["cross_seed_cleaner.py"]
    sys.path.insert(0, REPO_ROOT)
    try:
        if "cross_seed_cleaner" in sys.modules:
            del sys.modules["cross_seed_cleaner"]
        import cross_seed_cleaner as csc  # noqa: E402
        return csc
    finally:
        sys.argv = saved_argv


class ClientNonListBodyTest(unittest.TestCase):
    def setUp(self):
        self.csc = _load_module()
        # Build a client without __init__ (which logs in over HTTP).
        self.client = self.csc.QBittorrentClient.__new__(self.csc.QBittorrentClient)
        self.client.host = "http://localhost"
        self.client.cookie = None
        self.client.api_key = None
        self.client._webapi_version = "2.11.0"

    def _stub_request(self, value):
        self.client._request = lambda *a, **k: value

    def test_get_torrents_coerces_string_body_to_empty(self):
        self._stub_request("Forbidden.")
        self.assertEqual(self.client.get_torrents(), [])

    def test_get_torrents_passes_through_real_list(self):
        self._stub_request([{"hash": "a"}])
        self.assertEqual(self.client.get_torrents(), [{"hash": "a"}])

    def test_get_torrent_trackers_coerces_string_body_to_empty(self):
        self._stub_request("Forbidden.")
        self.assertEqual(self.client.get_torrent_trackers("abc"), [])

    def test_bulk_trackers_string_body_falls_through_no_crash(self):
        # Both the bulk call and the get_torrents() fallback see the string body;
        # neither must crash on iterating it. Requires BOTH the bulk isinstance
        # guard and the get_torrents() list coercion.
        self._stub_request("<html>Bad Gateway</html>")
        self.assertEqual(self.client.get_torrents_with_trackers(), [])

    def test_bulk_trackers_real_list_populates_trackers(self):
        self._stub_request([{"hash": "a", "trackers": [{"url": "http://t.example/announce"}]}])
        out = self.client.get_torrents_with_trackers()
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["_trackers"], [{"url": "http://t.example/announce"}])


class SeederCountNullGuardTest(unittest.TestCase):
    """get_seeder_count must not crash when num_complete/num_incomplete arrive
    as null (present-but-None), and must preserve the -1 'unscraped' sentinel."""

    def setUp(self):
        self.csc = _load_module()

    def _count(self, **fields):
        # '_trackers': [] -> get_tracker_domain returns None -> reliable branch,
        # so no network and num_complete is returned verbatim.
        torrent = {"_trackers": [], "name": "t", **fields}
        return self.csc.get_seeder_count(client=None, torrent=torrent)

    def test_none_count_treated_as_zero(self):
        self.assertEqual(self._count(num_complete=None), 0)

    def test_missing_count_treated_as_zero(self):
        self.assertEqual(self._count(), 0)

    def test_unscraped_minus_one_preserved(self):
        self.assertEqual(self._count(num_complete=-1), -1)

    def test_real_count_passthrough(self):
        self.assertEqual(self._count(num_complete=7), 7)


class VersionAtLeastTest(unittest.TestCase):
    """_version_at_least gates the bulk-trackers endpoint. It must compare clean
    dotted versions correctly and read only the leading digits of a suffixed
    token rather than merging digits across non-digit characters."""

    def setUp(self):
        self.csc = _load_module()

    def test_clean_versions(self):
        f = self.csc._version_at_least
        self.assertTrue(f("2.11.0", (2, 11, 0)))
        self.assertFalse(f("2.10.5", (2, 11, 0)))
        self.assertTrue(f("2.11", (2, 11, 0)))      # missing patch pads to 0
        self.assertTrue(f("3.0.0", (2, 11, 0)))

    def test_missing_or_malformed_is_false(self):
        f = self.csc._version_at_least
        self.assertFalse(f("", (2, 11, 0)))
        self.assertFalse(f(None, (2, 11, 0)))
        self.assertFalse(f("2.x.0", (2, 0, 0)))     # non-numeric token -> safe False

    def test_suffixed_token_reads_leading_digits_only(self):
        # "9z2" must read as 9, not 92 (digit-merge). 1.9 < 1.10, so this is False;
        # the old join-all-digits behavior read 1.92 and wrongly returned True.
        self.assertFalse(self.csc._version_at_least("1.9z2", (1, 10)))


class VersionRawReadTest(unittest.TestCase):
    """app/webapiVersion is plain text, not JSON. _request must read it raw so a
    two-component version like '2.20' isn't coerced to the float 2.2 (dropping
    the trailing zero), which would mis-route the bulk-trackers version check."""

    def setUp(self):
        self.csc = _load_module()
        self.client = self.csc.QBittorrentClient.__new__(self.csc.QBittorrentClient)
        self.client.host = "http://localhost"
        self.client.cookie = None
        self.client.api_key = None
        self.client._webapi_version = None

    def _stub_urlopen(self, body):
        import urllib.request

        class _Resp:
            def read(self_inner):
                return body.encode("utf-8")

        saved = urllib.request.urlopen
        urllib.request.urlopen = lambda *a, **k: _Resp()
        self.addCleanup(lambda: setattr(urllib.request, "urlopen", saved))

    def test_two_component_version_not_float_coerced(self):
        # Without raw=True, json.loads("2.20") -> 2.2 -> "2.2".
        self._stub_urlopen("2.20")
        self.assertEqual(self.client.webapi_version(), "2.20")

    def test_three_component_version_verbatim(self):
        self._stub_urlopen("2.11.4")
        self.assertEqual(self.client.webapi_version(), "2.11.4")


if __name__ == "__main__":
    unittest.main()
