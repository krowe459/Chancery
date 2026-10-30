"""Run with: python -m unittest discover -s tests"""
import base64
import contextlib
import importlib.util
import io
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

HAVE_DEPS = all(importlib.util.find_spec(m) for m in ("requests", "bs4", "pypdf", "anthropic"))
TRANSCRIPT = ("Re: Jones v. SBC Medical Group Hldgs., Inc., C.A. No. 2026-0193-PAF. Dear Counsel: "
              "The court has reviewed the parties' stipulation and proposed order dismissing this "
              "putative class action as moot. IT IS SO ORDERED.")


def blank_pdf(pages=3):
    """A PDF with pages but no text layer, like the Oct. 28 Jones v. SBC scan."""
    from pypdf import PdfWriter
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


@unittest.skipUnless(HAVE_DEPS, "dependencies not installed")
class ScannedPdfTest(unittest.TestCase):
    def test_pdf_without_text_raises_scanned_pdf_with_its_bytes(self):
        import scrape
        data = blank_pdf()
        saved = scrape._download
        scrape._download = lambda session, url: (data, "application/pdf")
        try:
            with self.assertRaises(scrape.ScannedPDF) as caught:
                scrape.extract_text(None, "u")
        finally:
            scrape._download = saved
        self.assertEqual(caught.exception.data, data)

    def test_opinion_text_transcribes_a_scan_and_notes_it(self):
        import scrape
        calls = []

        def extract(session, url):
            raise scrape.ScannedPDF(b"%PDF-scan")
        saved = scrape.extract_text, sys.modules.get("summarize")
        scrape.extract_text = extract
        sys.modules["summarize"] = types.SimpleNamespace(
            transcribe_pdf=lambda data: calls.append(data) or (TRANSCRIPT, 4200))
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                text, used, note = scrape.opinion_text(None, "u")
        finally:
            scrape.extract_text = saved[0]
            if saved[1] is None:
                sys.modules.pop("summarize", None)
            else:
                sys.modules["summarize"] = saved[1]
        self.assertEqual((text, used, calls), (TRANSCRIPT, 4200, [b"%PDF-scan"]))
        result = {"coverage_note": "Very long opinion."}
        scrape.add_note(result, note)
        self.assertTrue(result["coverage_note"].startswith("Scanned PDF with no text layer"))
        self.assertTrue(result["coverage_note"].endswith("Very long opinion."))

    def test_text_pdf_is_not_sent_for_transcription(self):
        import scrape
        saved = scrape.extract_text
        scrape.extract_text = lambda session, url: "opinion text"
        try:
            self.assertEqual(scrape.opinion_text(None, "u"), ("opinion text", 0, ""))
        finally:
            scrape.extract_text = saved


@unittest.skipUnless(HAVE_DEPS, "dependencies not installed")
class TranscribeTest(unittest.TestCase):
    def fake_client(self, stop_reason="end_turn", text=TRANSCRIPT):
        sent = []

        class Stream:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def get_final_message(self):
                return SimpleNamespace(
                    stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=text)],
                    usage=SimpleNamespace(input_tokens=3000, cache_creation_input_tokens=0,
                                          cache_read_input_tokens=0))

        def stream(**kw):
            sent.append(kw)
            return Stream()
        return SimpleNamespace(messages=SimpleNamespace(stream=stream)), sent

    def run_transcribe(self, data, **fake):
        import summarize
        client, sent = self.fake_client(**fake)
        saved = summarize.client
        summarize.client = lambda: client
        try:
            return summarize.transcribe_pdf(data), sent
        finally:
            summarize.client = saved

    def test_sends_the_pdf_as_a_document_block(self):
        import summarize
        data = blank_pdf()
        (text, used), sent = self.run_transcribe(data)
        self.assertEqual((text, used), (TRANSCRIPT, 3000))
        content = sent[0]["messages"][0]["content"]
        self.assertEqual(content[0]["type"], "document")
        self.assertEqual(content[0]["source"]["media_type"], "application/pdf")
        self.assertEqual(base64.standard_b64decode(content[0]["source"]["data"]), data)
        self.assertIn("[^3]", content[1]["text"])
        self.assertEqual(sent[0]["model"], summarize.MODEL)
        self.assertEqual(sent[0]["extra_body"], summarize.FALLBACK_BODY)

    def test_refusal_truncation_and_empty_output_raise(self):
        for fake in ({"stop_reason": "refusal"}, {"stop_reason": "max_tokens"}, {"text": "  "}):
            with self.assertRaises(RuntimeError):
                self.run_transcribe(blank_pdf(), **fake)

    def test_oversized_pdf_is_refused_before_any_request(self):
        import summarize
        with self.assertRaises(RuntimeError):
            self.run_transcribe(b"%PDF-" + b"0" * (summarize.TRANSCRIBE_MAX_BYTES + 1))


class VerifyQuoteRuleTest(unittest.TestCase):
    def test_flags_must_quote_the_wrong_words(self):
        # Oct. 29 Robertson v. Cerebral: the flag quoted an accurate clause
        # instead of the "retain and identify counsel early" advice.
        from verify import VERIFY_SYSTEM
        rule = " ".join(VERIFY_SYSTEM.split())
        self.assertIn("never quote a clause that the opinion supports", rule)
        self.assertIn("copy the words of that advice", rule)


if __name__ == "__main__":
    unittest.main()
