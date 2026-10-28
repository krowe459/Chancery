"""Run with: python -m unittest discover -s tests"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from types import SimpleNamespace  # noqa: E402

from pdftext import _separator_top, assemble, pdf_to_text, visible  # noqa: E402

W, H = 612.0, 792.0
LEFT, RIGHT = 72.0, 540.0


def line(text, top, size=14.0, x0=LEFT, x1=RIGHT):
    """Words spread evenly across [x0, x1]. "^n" makes a raised footnote marker."""
    tokens = text.split()
    step = (x1 - x0) / max(len(tokens), 1)
    words = []
    for i, tok in enumerate(tokens):
        marker = tok.startswith("^")
        words.append({
            "text": tok[1:] if marker else tok,
            "x0": x0 + i * step, "x1": x0 + (i + 1) * step - 3,
            "top": top - 2 if marker else top,
            "size": 9.0 if marker else size,
        })
    return words


def header(n):
    return line(f"C.A. No. 2025-1455-DG Page {n} of 3", 40, x1=300)


PAGE_1 = header(1) + (
    line("The Court holds that the plaintiff was not an officer ^1 of the", 120, x0=108)
    + line("corporation, because the board never appointed her to any role", 152)
    + line("that the bylaws recognize as an office of the corporation and", 184)
    + line("^1 Plaintiff conceded she was appointed by a division head and", 640, size=13)
    + line("not by the board.", 655, size=13, x1=200)
    + line("^2 This footnote continues", 680, size=13, x1=300)
)
PAGE_2 = header(2) + (
    line("so the motion is denied. ^2", 120, x1=300)
    + line("onto the next page and ends here.", 640, size=13, x1=300)
)
PAGE_3 = header(3) + line("IT IS SO ORDERED.", 120, x0=108, x1=300)


def build(*pages):
    return assemble([(W, H, p) for p in pages])


class AssembleTest(unittest.TestCase):
    def setUp(self):
        self.text = build(PAGE_1, PAGE_2, PAGE_3)

    def test_markers_inline_and_footnotes_after_paragraph(self):
        para, notes, *_ = self.text.split("\n\n")
        self.assertIn("not an officer[^1] of the corporation", para)
        self.assertTrue(para.endswith("so the motion is denied.[^2]"))
        self.assertIn("[^1]: Plaintiff conceded she was appointed by a division head and "
                      "not by the board.", notes)

    def test_footnote_continued_on_next_page_is_joined(self):
        self.assertIn("[^2]: This footnote continues onto the next page and ends here.", self.text)

    def test_paragraph_continues_across_page_break(self):
        self.assertIn("office of the corporation and so the motion is denied.", self.text)

    def test_running_headers_removed(self):
        self.assertNotIn("Page 2 of 3", self.text)

    def test_new_paragraph_after_indent(self):
        self.assertTrue(self.text.split("\n\n")[-1].startswith("IT IS SO ORDERED."))

    def test_block_quote_kept_together_and_ended(self):
        page = (line("The bylaw provides:", 120, x0=108, x1=300)
                + line("the Corporation shall pay the expenses incurred", 152, x0=144, x1=468)
                + line("in defending any such proceeding in advance.", 184, x0=144, x1=468)
                + line("Plaintiff relies on that language for her claim and", 216)
                + line("for the relief she seeks.", 248, x1=300))
        paras = build(page).split("\n\n")
        self.assertEqual(paras[1], "the Corporation shall pay the expenses incurred "
                                   "in defending any such proceeding in advance.")
        self.assertTrue(paras[2].startswith("Plaintiff relies"))

    def test_heading_is_its_own_paragraph(self):
        # Heading and the paragraph's first line share an indent; neither is a block quote.
        page = (line("1. Plaintiff was not an officer of the corporation.", 120, x0=108, x1=470)
                + line("Plaintiff's first theory is that she is entitled to relief", 152, x0=108)
                + line("because she was an officer of the corporation itself.", 184, x1=400))
        paras = build(page).split("\n\n")
        self.assertEqual(paras[0], "1. Plaintiff was not an officer of the corporation.")
        self.assertEqual(paras[1], "Plaintiff's first theory is that she is entitled to relief "
                                   "because she was an officer of the corporation itself.")

    def test_unreferenced_footnote_is_kept(self):
        page = line("Body text with no marker at all.", 120, x1=300) + \
               line("^7 A footnote nothing points to.", 640, size=13, x1=300)
        text = build(page)
        self.assertIn("Footnotes not matched", text)
        self.assertIn("[^7]: A footnote nothing points to.", text)

    def test_separator_rule_marks_body_size_footnotes(self):
        # Kulak v. On (Oct. 16): footnote text is the same 12pt as the body;
        # only the separator rule above it marks the footnote area.
        page = (line("The motions are denied for the reasons below ^1 and the", 120, size=12)
                + line("case will proceed to a final order shortly.", 152, size=12, x1=330)
                + line("^1 Dkt. 162.", 640, size=12, x1=200))
        without = build(page)
        self.assertNotIn("[^1]: Dkt. 162.", without)
        with_rule = assemble([(W, H, page, 630.0)])
        self.assertIn("reasons below[^1] and the case", with_rule)
        self.assertIn("[^1]: Dkt. 162.", with_rule)

    def test_table_borders_are_not_a_separator(self):
        # US WorldMeds v. Supernus (Oct. 26), page 16: a two-column fee table
        # whose right-column borders (365-513pt) pass the length and margin
        # tests, above the real separator at 658pt.
        words = (line("Gibson Dunn Fee Motion Request $1,898,019.09", 76)
                 + [w for top in range(92, 440, 17) for w in line("$80,732.29", top, x0=365, x1=420)]
                 + line("^51 US WorldMeds, 2026 WL 2098302, at 4.", 666, size=12, x1=400))
        def rules(top):   # one table row: the segments Word draws for its borders
            return [{"top": top, "x0": a, "x1": b, "height": 0.5}
                    for a, b in ((72, 72.5), (72, 364), (364, 365), (365, 513), (513, 513.5))]
        rects = [r for top in range(72, 440, 17) for r in rules(top)]
        rects.append({"top": 658.2, "x0": 72, "x1": 216, "height": 0.5})
        page = SimpleNamespace(height=H, rects=rects, lines=[])
        self.assertEqual(_separator_top(page, words), 658.2)
        page.rects = rects[:-1]
        self.assertIsNone(_separator_top(page, words))

    def test_hidden_anchor_text_is_dropped(self):
        words = [{"text": "paid", "size": 14.0}, {"text": "50F", "size": 1.0},
                 {"text": "51", "size": 9.0}]
        self.assertEqual([w["text"] for w in visible(words)], ["paid", "51"])

    def test_empty_document(self):
        self.assertIsNone(assemble([(W, H, [])]))


class FallbackTest(unittest.TestCase):
    def test_unreadable_pdf_returns_none(self):
        self.assertIsNone(pdf_to_text(b"%PDF-1.4 not really a pdf", "plain text " * 50))


if __name__ == "__main__":
    unittest.main()
