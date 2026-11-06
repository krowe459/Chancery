"""Run with: python -m unittest discover -s tests"""
import importlib.util
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

HAVE_DEPS = all(importlib.util.find_spec(m) for m in ("requests", "bs4"))

WOW = ("COURT OF CHANCERY OF THE STATE OF DELAWARE NATHAN A. COOK VICE CHANCELLOR "
       "Re: In re WideOpenWest, Inc. Stockholders' Litigation, C.A. No. 2026-0801-NAC "
       "Dear Counsel: This letter decision resolves the leadership dispute.")


@unittest.skipUnless(HAVE_DEPS, "scraper dependencies not installed")
class CaptionDocketTest(unittest.TestCase):
    def setUp(self):
        from scrape import caption_docket
        self.fix = caption_docket

    def test_wrong_initials_in_the_listing_are_corrected(self):
        # Nov. 5: listed as 2026-0801-VCC; the caption says NAC.
        self.assertEqual(self.fix(WOW, "C.A. No. 2026-0801-VCC"), "C.A. No. 2026-0801-NAC")

    def test_missing_initials_are_added(self):
        # Oct. 28: R1 RCM was listed as "C.A. No. 2025-1067".
        text = "IN RE R1 RCM INC. MERGER LITIGATION CONSOLIDATED C.A. No. 2025-1067-JTL OPINION"
        self.assertEqual(self.fix(text, "C.A. No. 2025-1067"), "C.A. No. 2025-1067-JTL")

    def test_matching_and_parenthetical_dockets_are_kept(self):
        text = "C.A. No. 2023-0601-LM (KSJM) MEMORANDUM OPINION"
        self.assertEqual(self.fix(text, "C.A. No. 2023-0601-LM (KSJM)"),
                         "C.A. No. 2023-0601-LM (KSJM)")
        self.assertEqual(self.fix(WOW, "2026-0801-NAC"), "2026-0801-NAC")

    def test_only_the_listed_case_number_is_used(self):
        # A caption or early footnote can name a related case first.
        text = ("cross-filed in Travus Pope v. Hycroft, C.A. No. 2023-0563-LWW. "
                "Re: Jordan Darga v. Hycroft, C.A. No. 2023-0612-LWW")
        self.assertEqual(self.fix(text, "C.A. No. 2023-0612-XYZ"), "C.A. No. 2023-0612-LWW")
        self.assertEqual(self.fix(text, "C.A. No. 2024-0001-ABC"), "C.A. No. 2024-0001-ABC")

    def test_superior_court_numbers(self):
        text = "C.A. No.: N25C-07-121 KMM CCLD Dear Counsel:"
        self.assertEqual(self.fix(text, "N25C-07-121 KMS CCLD"), "N25C-07-121 KMM CCLD")
        self.assertEqual(self.fix(text, "N25C-07-121 KMM CCLD"), "N25C-07-121 KMM CCLD")

    def test_page_one_is_read_when_the_text_lost_the_case_number(self):
        # WideOpenWest repeats "C.A. No. 2026-0801-NAC" as its running header,
        # so the footnote-aware text drops it from the page-1 caption too.
        import scrape
        calls = []
        saved = scrape.caption_text
        scrape.caption_text = lambda session, url, chars: calls.append(chars) or WOW
        try:
            fixed = scrape.opinion_docket(None, "u", "Dear Counsel: This letter decision",
                                          "C.A. No. 2026-0801-VCC")
            kept = scrape.opinion_docket(None, "u", WOW, "C.A. No. 2026-0801-VCC")
            scrape.caption_text = lambda session, url, chars: (_ for _ in ()).throw(OSError())
            offline = scrape.opinion_docket(None, "u", "no number", "C.A. No. 2026-0801-VCC")
        finally:
            scrape.caption_text = saved
        self.assertEqual((fixed, kept, offline),
                         ("C.A. No. 2026-0801-NAC", "C.A. No. 2026-0801-NAC",
                          "C.A. No. 2026-0801-VCC"))
        self.assertEqual(len(calls), 1)          # only when the text lacked a number

    def test_supreme_court_and_unknown_dockets(self):
        text = "No. 251, 2026 Court Below—Court of Chancery C.A. No. 2026-0505"
        self.assertEqual(self.fix(text, "251, 2026"), "251, 2026")
        self.assertEqual(self.fix(WOW, ""), "C.A. No. 2026-0801-NAC")   # unlisted preview
        self.assertEqual(self.fix("no caption here", ""), "")


if __name__ == "__main__":
    unittest.main()
