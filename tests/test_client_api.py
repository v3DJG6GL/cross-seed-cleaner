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


if __name__ == "__main__":
    unittest.main()
