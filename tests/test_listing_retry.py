"""Run with: python -m unittest discover -s tests"""
import contextlib
import importlib.util
import io
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

HAVE_DEPS = all(importlib.util.find_spec(m) for m in ("requests", "bs4"))

LISTING = """<html><head><title>Opinions</title></head><body><table>
<tr><th>Case Name</th><th>Docket Number</th><th>Date</th><th>Judge</th><th>Type</th></tr>
<tr><td><a href="download.aspx?id=402310">Max Glass v. RWA Company LLC</a></td>
<td>C.A. No. 2026-0100-ABC</td><td>10/19/2026</td><td>Will V.C.</td><td>Civil</td></tr>
</table></body></html>"""
MAINTENANCE = "<html><head><title>Site Maintenance</title></head><body>Back soon.</body></html>"


@unittest.skipUnless(HAVE_DEPS, "scraper dependencies not installed")
class ListingRetryTest(unittest.TestCase):
    def setUp(self):
        import scrape
        self.scrape = scrape
        self.sleeps = []
        self._orig = (scrape.get_with_retry, scrape.time.sleep)
        scrape.time.sleep = self.sleeps.append

    def tearDown(self):
        self.scrape.get_with_retry, self.scrape.time.sleep = self._orig

    def serve(self, *pages):
        queue = list(pages)
        self.scrape.get_with_retry = lambda session, url, stream=False: SimpleNamespace(
            text=queue.pop(0), status_code=200)

    def fetch(self, retries=2, wait=60):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rows = self.scrape.fetch_listing(None, retries, wait)
        return rows, err.getvalue()

    def test_parser_reads_the_test_listing(self):
        self.assertEqual(len(self.scrape.parse_listing(LISTING)), 1)

    def test_blip_then_success(self):
        self.serve(MAINTENANCE, LISTING)
        rows, log = self.fetch()
        self.assertEqual([r.case_id for r in rows], ["de-402310"])
        self.assertEqual(self.sleeps, [60])
        self.assertIn("attempt 1 of 3", log)
        self.assertIn("'Site Maintenance'", log)

    def test_persistent_empty_page_fails_after_all_attempts(self):
        self.serve(MAINTENANCE, MAINTENANCE, "<html><body></body></html>")
        rows, log = self.fetch()
        self.assertEqual(rows, [])
        self.assertEqual(self.sleeps, [60, 60])        # no wait after the last try
        self.assertIn("attempt 3 of 3", log)
        self.assertIn("no <title>", log)

    def test_zero_retries_keeps_old_behaviour(self):
        self.serve(MAINTENANCE)
        rows, _ = self.fetch(retries=0)
        self.assertEqual((rows, self.sleeps), ([], []))


if __name__ == "__main__":
    unittest.main()
