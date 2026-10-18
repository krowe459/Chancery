"""Run with: python -m unittest discover -s tests"""
import importlib.util
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

HAVE_DEPS = all(importlib.util.find_spec(m) for m in ("requests", "bs4"))
FRI_OCT_16 = date(2026, 10, 16)


@unittest.skipUnless(HAVE_DEPS, "scraper dependencies not installed")
class LatePostingTest(unittest.TestCase):
    def setUp(self):
        from scrape import CaseRow, business_days_old, skip_late_postings
        self.age, self.skip = business_days_old, skip_late_postings
        self.row = lambda cid, d: CaseRow(cid, f"Case {cid}", f"C.A. No. {cid}", d, "J", "Civil", "")

    def test_business_day_count(self):
        self.assertEqual(self.age("2026-10-16", FRI_OCT_16), 0)
        self.assertEqual(self.age("2026-10-15", FRI_OCT_16), 1)
        self.assertEqual(self.age("2026-10-13", FRI_OCT_16), 3)   # Tue -> Fri
        self.assertEqual(self.age("2026-10-16", date(2026, 10, 19)), 1)  # Fri -> Mon
        self.assertEqual(self.age("2026-10-20", FRI_OCT_16), 0)   # future-dated
        self.assertIsNone(self.age("Oct. 16, 2026", FRI_OCT_16))  # unparsed listing date

    def test_oct_16_listing(self):
        # The real Oct. 16 rows: four decided that day, M.A.C. Job (Oct. 13)
        # and Mitchell Partners (Oct. 1) posted late.
        rows = [self.row("de-402200", "2026-10-16"), self.row("de-402210", "2026-10-13"),
                self.row("de-402220", "2026-10-01"), self.row("de-402270", "2026-10-16"),
                self.row("de-402300", "2026-10-14")]
        state = {"cases": {}}
        kept = self.skip(rows, state, "stamp", 2, FRI_OCT_16)
        self.assertEqual([r.case_id for r in kept], ["de-402200", "de-402270", "de-402300"])
        self.assertEqual({k: v["status"] for k, v in state["cases"].items()},
                         {"de-402210": "skipped_old", "de-402220": "skipped_old"})
        self.assertEqual(state["cases"]["de-402220"]["date"], "2026-10-01")

    def test_disabled_and_unparsed_dates_are_kept(self):
        rows = [self.row("a", "2026-10-01"), self.row("b", "sometime")]
        self.assertEqual(len(self.skip(rows, {"cases": {}}, "s", -1, FRI_OCT_16)), 2)
        kept = self.skip(rows, {"cases": {}}, "s", 2, FRI_OCT_16)
        self.assertEqual([r.case_id for r in kept], ["b"])


if __name__ == "__main__":
    unittest.main()
