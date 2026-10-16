"""Run with: python -m unittest discover -s tests"""
import importlib.util
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

HAVE_DEPS = all(importlib.util.find_spec(m) for m in ("requests", "bs4"))


@unittest.skipUnless(HAVE_DEPS, "scraper dependencies not installed")
class NormalizeTest(unittest.TestCase):
    def setUp(self):
        from preview import normalize
        self.normalize = normalize

    def test_ids_and_court_urls(self):
        url = "https://courts.delaware.gov/opinions/download.aspx?id=402160"
        self.assertEqual(self.normalize("402160"), url)
        self.assertEqual(self.normalize(" https://courts.delaware.gov/Opinions/Download.aspx?id=402160&x=1 "), url)

    def test_rejects_other_hosts_and_plain_http(self):
        for bad in ("http://courts.delaware.gov/opinions/download.aspx?id=1",
                    "https://courts.delaware.gov.evil.example/a.pdf",
                    "https://example.com/opinion.pdf", "12", "file:///etc/passwd"):
            with self.assertRaises(ValueError, msg=bad):
                self.normalize(bad)


class PreviewRenderingTest(unittest.TestCase):
    def test_title_and_full_summary_reference(self):
        from send_digest import render_html
        entry = {"name": "A v. B", "summary": {
            "core_legal_issue": "x", "holding": "h", "brief_summary": "b",
            "key_facts_and_concessions": [f"fact {i}" for i in range(12)]}}
        html = render_html([entry], "2026-10-16", full_summary_ref="summaries.json in this preview",
                           title="Chancery Digest &mdash; Preview (not sent)")
        self.assertIn("Preview (not sent)", html)
        self.assertIn("summaries.json in this preview", html)
        self.assertNotIn("state/archive", html)
        self.assertIn("Daily Digest", render_html([entry], "2026-10-16"))


if __name__ == "__main__":
    unittest.main()
