"""Run with: python -m unittest discover -s tests"""
import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

HAVE_DEPS = all(importlib.util.find_spec(m) for m in ("requests", "bs4", "anthropic"))


@unittest.skipUnless(HAVE_DEPS, "scraper dependencies not installed")
class RunTimeBudgetTest(unittest.TestCase):
    def test_stops_starting_opinions_after_the_budget(self):
        import common
        import scrape
        tmp = Path(tempfile.mkdtemp())
        clock = [0.0]
        summarized = []

        def summarize(text, row):
            summarized.append(row.case_id)
            clock[0] += 25 * 60                      # each opinion takes 25 minutes
            return {"holding": "h", "_input_tokens": 10}

        rows = [scrape.CaseRow(f"de-{i}", f"Case {i}", f"2026-{i:04d}-X", "2026-10-20", "J",
                               "Civil", f"u{i}") for i in (1, 2, 3)]
        saved = {k: getattr(scrape, k) for k in ("PROCESSED_PATH", "ACCUMULATION_PATH",
                                                 "fetch_listing", "extract_text", "now_ny")}
        saved_monotonic, saved_summarize = scrape.time.monotonic, sys.modules.get("summarize")
        try:
            scrape.PROCESSED_PATH, scrape.ACCUMULATION_PATH = tmp / "p.json", tmp / "a.json"
            old = {"processed_at": "2026-10-19T12:00:00-04:00", "status": "ok"}
            common.save_json(scrape.PROCESSED_PATH, {"page_hash": "x", "cases": {
                "de-0": old, "de-8": {**old, "court": "supreme"},
                "de-9": {**old, "court": "superior"}}})
            others = {k: [scrape.CaseRow(f"de-{n}", "Old", "", "2026-10-19", "J", "", "u", k)]
                      for k, n in (("supreme", 8), ("superior", 9))}
            scrape.fetch_listing = lambda session, retries, wait, court: others.get(court.key, rows)
            scrape.extract_text = lambda session, url: "text"
            scrape.now_ny = lambda: datetime(2026, 10, 20, 11, 50, tzinfo=common.NY)
            scrape.time.monotonic = lambda: clock[0]
            sys.modules["summarize"] = types.SimpleNamespace(summarize_opinion=summarize)
            os.environ.update(ANTHROPIC_API_KEY="fake", RUN_TIME_BUDGET_MINUTES="40")
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(scrape.main(), 0)
            state = common.load_json(scrape.PROCESSED_PATH, {})
        finally:
            for k, v in saved.items():
                setattr(scrape, k, v)
            scrape.time.monotonic = saved_monotonic
            if saved_summarize is None:
                sys.modules.pop("summarize", None)
            else:
                sys.modules["summarize"] = saved_summarize
            os.environ.pop("RUN_TIME_BUDGET_MINUTES", None)

        # 0 min -> case 1; 25 min -> case 2; 50 min >= 40 -> stop.
        self.assertEqual(summarized, ["de-1", "de-2"])
        self.assertNotIn("de-3", state["cases"])        # left for the next run
        self.assertEqual(state["page_hashes"]["chancery"], "x")  # backlog keeps the fast path off
        self.assertIn("supreme", state["page_hashes"])            # no backlog there
        self.assertIn("Run time budget reached (50.0 min)", out.getvalue())


if __name__ == "__main__":
    unittest.main()
