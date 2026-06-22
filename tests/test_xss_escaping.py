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
    """Import cross_seed_cleaner with argv/env stubbed so argparse and
    the env-driven config block don't explode under a test runner."""
    saved_argv = sys.argv
    saved_env = {
        k: os.environ.get(k)
        for k in ("HTML_EXPORT", "CSV_EXPORT")
    }
    sys.argv = ["cross_seed_cleaner.py"]
    sys.path.insert(0, REPO_ROOT)
    try:
        if "cross_seed_cleaner" in sys.modules:
            del sys.modules["cross_seed_cleaner"]
        import cross_seed_cleaner as csc  # noqa: E402
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
    # _tracker_msg is the tracker error string ("Torrent not registered with
    # this tracker", etc.) surfaced in tracker-error mode reports — populated
    # by qBittorrent and therefore tracker-attacker-controllable. Rendered into
    # a data-tip attribute, so attribute-breakout is the main concern.
    XSS_TRACKER_MSG = '" onclick="alert(\'xss-tmsg\')" x="'

    # Config-panel fields rendered into the report's <ul>. UNRELIABLE_TRACKERS and
    # EXCLUDED_TRACKERS are env-overridable; CATEGORY_ALLOWLIST/BLOCKLIST are
    # config-file lists with no env path, so set them all directly (like
    # HTML_EXPORT below) to guarantee the payloads reach the rendered config panel.
    XSS_UNRELIABLE = '<img src=x onerror=alert("xss-unreliable")>'
    XSS_EXCLUDED = '<img src=x onerror=alert("xss-excluded")>'
    XSS_CAT_ALLOW = '<script>alert("xss-cat-allow")</script>'
    XSS_CAT_BLOCK = '<img src=x onerror=alert("xss-cat-block")>'
    XSS_MHL_CAT = '<img src=x onerror=alert("xss-mhl-cat")>'
    # External media paths and path mappings are operator config rendered into
    # the same config panel via _h(); without coverage, dropping that _h() wrapper
    # leaves every test green (same failure mode the lists above guard against).
    XSS_EXT_MEDIA = '<img src=x onerror=alert("xss-extmedia")>'
    XSS_MAP_KEY = '<img src=x onerror=alert("xss-mapkey")>'
    XSS_MAP_VAL = '<script>alert("xss-mapval")</script>'

    def setUp(self):
        self.csc = _load_module()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

        self.csc.HTML_EXPORT = os.path.join(self.tmpdir.name, "report.html")
        self.csc.CSV_EXPORT = os.path.join(self.tmpdir.name, "report.csv")
        self.csc.UNRELIABLE_TRACKERS = [self.XSS_UNRELIABLE]
        self.csc.EXCLUDED_TRACKERS = [self.XSS_EXCLUDED]
        self.csc.CATEGORY_ALLOWLIST = [self.XSS_CAT_ALLOW]
        self.csc.CATEGORY_BLOCKLIST = [self.XSS_CAT_BLOCK]
        self.csc.MISSING_HARD_LINKS_CATEGORIES = [self.XSS_MHL_CAT]
        self.csc.EXTERNAL_MEDIA_PATHS = [self.XSS_EXT_MEDIA]
        self.csc.PATH_MAPPINGS = {self.XSS_MAP_KEY: self.XSS_MAP_VAL}

    def _render(self):
        torrent = {
            "name": self.XSS_NAME,
            "content_path": self.XSS_PATH,
            "category": self.XSS_CAT,
            "tracker": self.XSS_TRACKER,
            "_tracker_domain": self.XSS_TRACKER,
            "_tracker_msg": self.XSS_TRACKER_MSG,
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
        sorted_items = [("group1", {"original": torrent, "crossseeds": [cross]})]

        self.csc.export_reports(sorted_items=sorted_items, eligible_ids={1})

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
        self.assertNotIn('onerror=alert("xss-cat")',  html, "category payload survived unescaped")
        self.assertNotIn('<script>alert("xss-js")',   html, "tracker payload survived unescaped")
        self.assertNotIn('onmouseover="alert',        html, "attribute-break payload survived unescaped")
        self.assertNotIn('onclick="alert(\'xss-tmsg\')"', html,
                         "tracker_msg attribute-break payload survived unescaped")

        # Escaped forms must be present.
        self.assertIn('&lt;script&gt;alert(&quot;xss-name&quot;)', html)
        self.assertIn('&lt;script&gt;alert(&quot;xss-ext&quot;)',  html)

        # Config-panel fields (unreliable trackers, category allow/block lists)
        # are attacker-influenceable via env/config and rendered into the report.
        # Without these assertions the payloads above were dead setup: dropping
        # the _h() wrapper on those config <li> items left every test green.
        self.assertNotIn('onerror=alert("xss-unreliable")', html, "unreliable-trackers payload survived unescaped")
        self.assertNotIn('onerror=alert("xss-excluded")', html, "excluded-trackers payload survived unescaped")
        self.assertNotIn('<script>alert("xss-cat-allow")', html, "category-allowlist payload survived unescaped")
        self.assertNotIn('onerror=alert("xss-cat-block")', html, "category-blocklist payload survived unescaped")
        self.assertNotIn('onerror=alert("xss-mhl-cat")', html, "missing-hard-links category payload survived unescaped")
        self.assertIn('alert(&quot;xss-unreliable&quot;)', html)
        self.assertIn('alert(&quot;xss-excluded&quot;)', html)
        self.assertIn('&lt;script&gt;alert(&quot;xss-cat-allow&quot;)', html)
        self.assertIn('alert(&quot;xss-cat-block&quot;)', html)
        self.assertIn('alert(&quot;xss-mhl-cat&quot;)', html)

        # External media paths and path mappings (operator config in the panel).
        self.assertNotIn('onerror=alert("xss-extmedia")', html, "external-media-path payload survived unescaped")
        self.assertNotIn('onerror=alert("xss-mapkey")', html, "path-mapping key payload survived unescaped")
        self.assertNotIn('<script>alert("xss-mapval")', html, "path-mapping value payload survived unescaped")
        self.assertIn('alert(&quot;xss-extmedia&quot;)', html)
        self.assertIn('alert(&quot;xss-mapkey&quot;)', html)
        self.assertIn('&lt;script&gt;alert(&quot;xss-mapval&quot;)', html)

    def test_no_script_breakout_in_inline_js(self):
        html = self._render()
        # Extract the inline Chart.js setup block (regex anchors on newline after <script>,
        # which doesn't match the vendored Chart.js bundle's tag — that one is tested below).
        block = re.search(r'<script>\s*\n(.*?)</script>', html, re.DOTALL)
        self.assertIsNotNone(block, "inline <script> setup block missing from report")
        inline_js = block.group(1)
        self.assertNotIn("</script>", inline_js, "</script> breakout inside inline JS")
        # The non-greedy capture above stops at the FIRST </script>, so an injected
        # </script> in the user-controlled chart data (tracker domains feed the chart
        # `labels` array) would truncate the captured block *before* the injected tag
        # rather than include it — leaving the assertNotIn above vacuously green. Pin
        # that the block still reaches the chart setup emitted after that data, so a
        # real breakout (which would cut the capture short) is actually detected.
        self.assertIn("new Chart(ctxGroup", inline_js,
                      "inline JS block truncated before chart setup — </script> breakout in chart data")

    def test_js_string_escapes_line_terminators(self):
        # U+2028 (line separator) and U+2029 (paragraph separator) are valid
        # JSON but terminate a statement inside an inline <script>, breaking the
        # script if emitted raw. js_string relies on json.dumps's default
        # ensure_ascii=True to escape them; a refactor to ensure_ascii=False
        # (e.g. to keep non-ASCII tracker names readable) would silently
        # reintroduce the break in the chart `labels`. Pin that they never
        # survive raw — the </script> case has an end-to-end anchor above, this
        # locks the sibling line-terminator escape at the source.
        ls, ps = "\u2028", "\u2029"  # U+2028 line sep, U+2029 paragraph sep
        out = self.csc.js_string(f"a{ls}b{ps}c")
        self.assertNotIn(ls, out, "raw U+2028 survived js_string")
        self.assertNotIn(ps, out, "raw U+2029 survived js_string")
        self.assertIn("\\u2028", out)
        self.assertIn("\\u2029", out)

    def test_chartjs_is_vendored_not_cdn(self):
        html = self._render()
        self.assertNotIn("cdn.jsdelivr.net", html, "CDN reference leaked into generated report")
        self.assertIn("/*!\n * Chart.js v4.5.1", html, "vendored Chart.js banner missing")


if __name__ == "__main__":
    unittest.main()
