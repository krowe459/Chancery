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

SUPREME_FROM_CHANCERY = ("IN THE SUPREME COURT OF THE STATE OF DELAWARE ACME CORP., Defendant Below, "
                         "Appellant, v. JOHN DOE, Plaintiff Below, Appellee. No. 112, 2026 Court Below: "
                         "Court of Chancery of the State of Delaware C.A. No. 2025-0456-KSJM")
SUPREME_FROM_SUPERIOR = ("IN THE SUPREME COURT OF THE STATE OF DELAWARE STATE OF DELAWARE v. JANE ROE "
                         "No. 98, 2026 Court Below—Superior Court of the State of Delaware "
                         "Cr. ID No. 2301001234 (N)")


@unittest.skipUnless(HAVE_DEPS, "scraper dependencies not installed")
class ScopeTest(unittest.TestCase):
    def setUp(self):
        import scrape
        self.scrape = scrape
        self.row = lambda docket, court="superior", cid="de-1": scrape.CaseRow(
            cid, "A v. B", docket, "2026-10-21", "J", "Civil", "u", court)

    def test_superior_ccld_from_the_listing_needs_no_download(self):
        def caption():
            raise AssertionError("caption read")
        self.assertTrue(self.scrape.in_scope_superior(self.row("N24C-03-123 PRW CCLD"), caption))

    def test_superior_falls_back_to_the_caption(self):
        row = self.row("N24C-03-123 PRW")
        self.assertTrue(self.scrape.in_scope_superior(row, lambda: "C.A. No. N24C-03-123 PRW CCLD"))
        self.assertFalse(self.scrape.in_scope_superior(row, lambda: "C.A. No. N24C-03-123 PRW"))

    def test_supreme_needs_chancery_as_the_court_below(self):
        row = self.row("No. 112, 2026", "supreme")
        self.assertTrue(self.scrape.in_scope_supreme(row, lambda: SUPREME_FROM_CHANCERY))
        self.assertTrue(self.scrape.in_scope_supreme(
            row, lambda: "Upon appeal from the Court of Chancery. AFFIRMED."))
        self.assertFalse(self.scrape.in_scope_supreme(row, lambda: SUPREME_FROM_SUPERIOR))

    def test_check_scope_records_out_of_scope_and_retries_failures(self):
        court = self.scrape.COURTS[1]
        rows = [self.row("No. 1, 2026", "supreme", "de-1"), self.row("No. 2, 2026", "supreme", "de-2"),
                self.row("No. 3, 2026", "supreme", "de-3")]
        captions = {"u1": SUPREME_FROM_CHANCERY, "u2": SUPREME_FROM_SUPERIOR}
        rows = [r.__class__(**{**r.__dict__, "url": f"u{i}"}) for i, r in enumerate(rows, 1)]

        def caption_text(session, url):
            if url not in captions:
                raise RuntimeError("HTTP 503")
            return captions[url]
        saved = self.scrape.caption_text
        self.scrape.caption_text = caption_text
        state = {"cases": {}, "failures": {}}
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                kept = self.scrape.check_scope(rows, court, state, "s", None)
                for _ in range(2):
                    self.scrape.check_scope(rows[2:], court, state, "s", None)
        finally:
            self.scrape.caption_text = saved
        self.assertEqual([r.case_id for r in kept], ["de-1"])
        self.assertEqual(state["cases"]["de-2"]["status"], "out_of_scope")
        self.assertEqual(state["cases"]["de-3"]["status"], "scope_check_failed")  # third failure
        self.assertNotIn("de-3", state["failures"])

    def test_chancery_takes_every_row(self):
        rows = [self.row("C.A. No. 2026-0001-X", "chancery")]
        self.assertEqual(self.scrape.check_scope(rows, self.scrape.COURTS[0], {}, "s", None), rows)


@unittest.skipUnless(HAVE_DEPS, "scraper dependencies not installed")
class NewCourtSeedingTest(unittest.TestCase):
    def test_new_courts_are_seeded_and_old_hash_moves_to_chancery(self):
        import common
        import scrape
        tmp = Path(tempfile.mkdtemp())
        summarized = []
        C = scrape.CaseRow
        listings = {
            "chancery": [C("de-10", "Old Ch", "C.A. No. 2026-0010-X", "2026-10-21", "J", "Civil", "u10"),
                         C("de-11", "New Ch", "C.A. No. 2026-0011-X", "2026-10-22", "J", "Civil", "u11")],
            "supreme": [C("de-20", "Old Sup", "No. 1, 2026", "2026-10-21", "J", "", "u20", "supreme")],
            "superior": [C("de-30", "Old Sup Ct", "N24C-01-001 X CCLD", "2026-10-21", "J", "", "u30",
                           "superior")],
        }
        saved = {k: getattr(scrape, k) for k in ("PROCESSED_PATH", "ACCUMULATION_PATH", "fetch_listing",
                                                 "extract_text", "now_ny", "caption_text")}
        saved_summarize = sys.modules.get("summarize")
        try:
            scrape.PROCESSED_PATH, scrape.ACCUMULATION_PATH = tmp / "p.json", tmp / "a.json"
            common.save_json(scrape.PROCESSED_PATH, {"page_hash": "old", "cases": {"de-10": {
                "processed_at": "2026-10-21T12:00:00-04:00", "status": "ok"}}})
            scrape.fetch_listing = lambda session, retries, wait, court: listings[court.key]
            scrape.extract_text = lambda session, url: "text"
            scrape.caption_text = lambda session, url: (_ for _ in ()).throw(AssertionError("read"))
            scrape.now_ny = lambda: datetime(2026, 10, 22, 11, 50, tzinfo=common.NY)
            sys.modules["summarize"] = types.SimpleNamespace(
                summarize_opinion=lambda text, row: summarized.append(row.case_id) or {"_input_tokens": 1})
            os.environ["ANTHROPIC_API_KEY"] = "fake"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(scrape.main(), 0)
            state = common.load_json(scrape.PROCESSED_PATH, {})
            accum = common.load_json(scrape.ACCUMULATION_PATH, {})
        finally:
            for k, v in saved.items():
                setattr(scrape, k, v)
            if saved_summarize is None:
                sys.modules.pop("summarize", None)
            else:
                sys.modules["summarize"] = saved_summarize

        self.assertEqual(summarized, ["de-11"])             # only Chancery's new row
        self.assertEqual(state["cases"]["de-20"]["status"], "seeded")
        self.assertEqual(state["cases"]["de-30"]["court"], "superior")
        self.assertNotIn("page_hash", state)
        self.assertEqual(set(state["page_hashes"]), {"chancery", "supreme", "superior"})
        self.assertEqual(accum["entries"][0]["court"], "chancery")

    def test_failed_listing_still_runs_the_others_and_goes_red(self):
        import common
        import scrape
        tmp = Path(tempfile.mkdtemp())
        row = scrape.CaseRow("de-10", "Ch", "C.A. No. 2026-0010-X", "2026-10-21", "J", "Civil", "u10")
        saved = {k: getattr(scrape, k) for k in ("PROCESSED_PATH", "ACCUMULATION_PATH", "fetch_listing")}
        try:
            scrape.PROCESSED_PATH, scrape.ACCUMULATION_PATH = tmp / "p.json", tmp / "a.json"
            common.save_json(scrape.PROCESSED_PATH, {"page_hashes": {"chancery": scrape.fingerprint([row])},
                                                     "cases": {"de-10": {"processed_at": "x"}}})
            scrape.fetch_listing = lambda s, r, w, court: [row] if court.key == "chancery" else []
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(scrape.main(), 2)
        finally:
            for k, v in saved.items():
                setattr(scrape, k, v)
        self.assertIn("Supreme Court listing", err.getvalue())


class DigestCourtTest(unittest.TestCase):
    def test_order_and_labels(self):
        import send_digest
        entries = [{"case_id": "a", "name": "Ch", "date": "2026-10-22"},
                   {"case_id": "b", "name": "CCLD", "date": "2026-10-22", "court": "superior"},
                   {"case_id": "c", "name": "Appeal", "date": "2026-10-21", "court": "supreme"}]
        self.assertEqual([e["case_id"] for e in send_digest.order_entries(entries)], ["c", "a", "b"])
        html = send_digest.render_entry({**entries[2], "summary": {"holding": "h"}}, "d")
        self.assertIn("Supreme Court (appeal from Chancery)</div>", html)
        self.assertNotIn("Court of Chancery</div>", send_digest.render_entry(
            {**entries[0], "summary": {"holding": "h"}}, "d"))


@unittest.skipUnless(importlib.util.find_spec("anthropic"), "anthropic not installed")
class PromptCourtTest(unittest.TestCase):
    def test_meta_names_the_court(self):
        import summarize
        row = types.SimpleNamespace(name="A v. B", docket="No. 1, 2026", date="2026-10-22",
                                    judge="Valihura J.", court="supreme")
        self.assertTrue(summarize._meta(row).startswith(
            "Supreme Court of Delaware, on appeal from the Court of Chancery: A v. B"))
        old = types.SimpleNamespace(name="A", docket="d", date="x", judge="j")
        self.assertTrue(summarize._meta(old).startswith("Court of Chancery: "))
        enum = summarize.SUMMARY_SCHEMA["properties"]["disposition"]["enum"]
        for value in ("affirmed", "reversed", "reversed and remanded",
                      "affirmed in part and reversed in part"):
            self.assertIn(value, enum)
            self.assertIn(f'"{value}"', summarize.SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
