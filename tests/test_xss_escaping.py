"""Regression test: export_reports() must HTML-escape attacker-controlled fields.

Run directly:    python3 tests/test_xss_escaping.py
Or via unittest: python3 -m unittest tests.test_xss_escaping
"""
import glob
import os
import re
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_module():
    """Import cross_seed_cleaner_clean with argv/env stubbed so argparse and
    the env-driven config block don't explode under a test runner."""
    saved_argv = sys.argv
    saved_env = {
        k: os.environ.get(k)
        for k in ("UNRELIABLE_TRACKERS", "CATEGORY_ALLOWLIST", "HTML_EXPORT", "CSV_EXPORT")
    }
    sys.argv = ["cross_seed_cleaner_clean.py"]
    os.environ["UNRELIABLE_TRACKERS"] = '<img src=x onerror=alert("unreliable-env")>'
    os.environ["CATEGORY_ALLOWLIST"] = '<script>alert("cat-allow")</script>'
    sys.path.insert(0, REPO_ROOT)
    try:
        if "cross_seed_cleaner_clean" in sys.modules:
            del sys.modules["cross_seed_cleaner_clean"]
        import cross_seed_cleaner_clean as csc  # noqa: E402
        return csc
    finally:
        sys.argv = saved_argv
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class XSSEscapingTest(unittest.TestCase):
    XSS_NAME = '</td><script>alert("xss-name")</script><td>'
    XSS_PATH = '" onmouseover="alert(\'xss-path\')" x="'
    XSS_CAT = '<img src=x onerror=alert("xss-cat")>'
    XSS_TRACKER = 'http://evil.example/</script><script>alert("xss-js")</script>/'
    XSS_EXT = '</td><script>alert("xss-ext")</script>'

    def setUp(self):
        self.csc = _load_module()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

        self.csc.HTML_EXPORT = os.path.join(self.tmpdir.name, "report.html")
        self.csc.CSV_EXPORT = os.path.join(self.tmpdir.name, "report.csv")
        self.csc.ELIGIBLE_ONLY = False
        self.csc.SCAN_STATS["total_scanned"] = 2
        self.csc.SCAN_STATS["eligible_count"] = 0
        self.csc.SCAN_STATS["errors"] = 0

    def _render(self):
        torrent = {
            "name": self.XSS_NAME,
            "content_path": self.XSS_PATH,
            "category": self.XSS_CAT,
            "tracker": self.XSS_TRACKER,
            "size": 5 * 1024 * 1024 * 1024,
            "ratio": 1.5,
            "uploaded": 1024 * 1024 * 1024,
            "seeding_time": 86400 * 30,
            "added_on": 1700000000,
            "num_complete": 10,
            "_seeder_count": 10,
            "_external_path": self.XSS_EXT,
            "hash": "deadbeef",
        }
        cross = dict(torrent)
        cross["name"] = self.XSS_NAME + "-cross"
        cross["hash"] = "cafebabe"
        all_groups = {"group1": {"original": torrent, "crossseeds": [cross]}}

        self.csc.export_reports(client=None, all_groups=all_groups, eligible_ids={1})

        base = os.path.join(self.tmpdir.name, "report")
        matches = sorted(glob.glob(base + "*.html"), key=os.path.getmtime)
        self.assertTrue(matches, "export_reports produced no HTML output")
        with open(matches[-1], "r", encoding="utf-8") as f:
            return f.read()

    def test_no_raw_xss_in_html_context(self):
        html = self._render()
        self.assertGreater(len(html), 1000, "HTML report suspiciously short")

        # Payload markers must appear only in escaped form.
        self.assertNotIn('<script>alert("xss-name")', html, "name payload survived unescaped")
        self.assertNotIn('<script>alert("xss-ext")',  html, "external path payload survived unescaped")
        self.assertNotIn('<script>alert("xss-cat")',  html, "category payload survived unescaped")
        self.assertNotIn('<script>alert("xss-js")',   html, "tracker payload survived unescaped")
        self.assertNotIn('onmouseover="alert',        html, "attribute-break payload survived unescaped")

        # Escaped forms must be present.
        self.assertIn('&lt;script&gt;alert(&quot;xss-name&quot;)', html)
        self.assertIn('&lt;script&gt;alert(&quot;xss-ext&quot;)',  html)

    def test_no_script_breakout_in_inline_js(self):
        html = self._render()
        # Extract the inline Chart.js setup block (regex anchors on newline after <script>,
        # which doesn't match the vendored Chart.js bundle's tag — that one is tested below).
        block = re.search(r'<script>\s*\n(.*?)</script>', html, re.DOTALL)
        self.assertIsNotNone(block, "inline <script> setup block missing from report")
        inline_js = block.group(1)
        self.assertNotIn("</script>", inline_js, "</script> breakout inside inline JS")

    def test_chartjs_is_vendored_not_cdn(self):
        html = self._render()
        self.assertNotIn("cdn.jsdelivr.net", html, "CDN reference leaked into generated report")
        self.assertIn("/*!\n * Chart.js v4.5.1", html, "vendored Chart.js banner missing")


if __name__ == "__main__":
    unittest.main()
