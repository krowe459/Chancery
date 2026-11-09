"""Run with: python -m unittest discover -s tests"""
import importlib.util
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

HAVE_DEPS = all(importlib.util.find_spec(m) for m in ("requests", "bs4"))
BODY = " ".join(f"The petition fails to plead insolvency under section 291 ground {i}." for i in range(400))
NOTE = ("*An earlier version of this Final Report was issued on November 5, 2026, with an "
        "incorrect title and some formatting errors. This version corrects those errors but makes "
        "no substantive changes to the contents of the Final Report. The time to take exceptions "
        "under Court of Chancery Rule 144 runs from the date of this Final Report.")


@unittest.skipUnless(HAVE_DEPS, "scraper dependencies not installed")
class ReissueTest(unittest.TestCase):
    def setUp(self):
        import scrape
        self.scrape = scrape
        self.row = scrape.CaseRow("de-403200", "Melissa Brandao v. HerdDogg Inc",
                                  "C.A. No. 2026-0824-TJF", "2026-11-06", "Fox M.", "Civil", "u2")
        self.earlier = {"case_id": "de-403130", "name": "Melissa Brandao v. HerdDogg Inc",
                        "docket": "C.A. No. 2026-0824-TJF", "ny_date": "2026-11-05", "url": "u1",
                        "summary": {"holding": "h"}}
        self.saved = scrape.extract_text

    def tearDown(self):
        self.scrape.extract_text = self.saved

    def find(self, new_text, earlier_text, entries=None):
        self.scrape.extract_text = lambda session, url: earlier_text
        return self.scrape.find_reissue(None, self.row, new_text, entries or [self.earlier])

    def test_corrected_reissue_is_recognized_with_the_courts_note(self):
        # Nov. 6 Brandao: same report, new title and date, and a note saying so.
        found = self.find("MAGISTRATE'S FINAL REPORT " + NOTE + " " + BODY, "MEMORANDUM OPINION " + BODY)
        self.assertGreaterEqual(found["overlap"], self.scrape.REISSUE_SAME)
        self.assertEqual(found["of"]["case_id"], "de-403130")
        self.assertTrue(found["note"].startswith("An earlier version of this Final Report"))
        self.assertIn("runs from the date of this Final Report", found["note"])

    def test_another_decision_in_the_same_case_is_not_a_reissue(self):
        # US INFRA's Oct. 15 and Nov. 2 letters share a case number but 4% of text.
        other = " ".join(f"Spoliation motion deferred to post-trial decision point {i}." for i in range(80))
        self.assertIsNone(self.find(other, BODY))

    def test_different_case_numbers_and_unreadable_earlier_copies_are_skipped(self):
        stranger = {**self.earlier, "docket": "C.A. No. 2026-0801-NAC"}
        self.assertIsNone(self.find(BODY, BODY, [stranger]))
        def broken(session, url):
            raise RuntimeError("HTTP 404")
        self.scrape.extract_text = broken
        self.assertIsNone(self.scrape.find_reissue(None, self.row, BODY, [self.earlier]))

    def test_docket_key_matches_formats(self):
        key = self.scrape.docket_key
        self.assertEqual(key("C.A. No. 2026-0824-TJF"), key("2026-0824-TJF"))
        self.assertEqual(key("N25C-07-121 KMM CCLD"), "N25C-07-121")
        self.assertEqual(key("235, 2026"), "235, 2026")


class ReissueRenderingTest(unittest.TestCase):
    def test_reissue_entry_points_to_the_earlier_summary(self):
        import send_digest
        entry = {"name": "Melissa Brandao v. HerdDogg Inc", "url": "u2", "docket": "C.A. No. 2026-0824-TJF",
                 "reissue_of": {"of": {"case_id": "de-403130", "name": "Melissa Brandao v. HerdDogg Inc",
                                       "ny_date": "2026-11-05", "url": "u1"},
                                "overlap": 0.993, "note": NOTE.lstrip("*")}}
        html = send_digest.render_entry(entry, "2026-11-06")
        self.assertIn("Corrected reissue of", html)
        self.assertIn("summarized in the 2026-11-05 digest", html)
        self.assertIn("state/archive/2026-11-05.json", html)
        self.assertIn("runs from the date of this Final Report", html)
        self.assertNotIn("Core Legal Issue", html)


if __name__ == "__main__":
    unittest.main()
