"""Run with: python -m unittest discover -s tests"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from citecheck import check_citations  # noqa: E402

# Shaped like pypdf output: citations broken across lines, footnotes interleaved.
OPINION = """Summary judgment may be appropriate. "There is no 'right' to a summary
judgment."12 Telxon Corp. v. Meyerson, 802 A.2d 257, 262 (Del. 2002).
13 In re El Paso Pipeline P’rs, L.P. Deriv. Litig., 2014 WL
2768782, at *9 (Del. Ch. June 12, 2014). See Ct. Ch. R. 56(a).
Advancement under Section 145 is permissive. Cal. Corp. Code §
17704.08(b). 500 N. King Street, Suite 350. D.R.E. 403."""


def run(*auths):
    summary = {"authorities_cited": [{"authority": a, "kind": k} for a, k in auths]}
    counts = check_citations(summary, OPINION)
    return counts, {a["authority"]: a["check"] for a in summary["authorities_cited"]}


class CiteCheckTest(unittest.TestCase):
    def test_real_citations_verify_despite_line_breaks(self):
        counts, checks = run(
            ("Telxon Corp. v. Meyerson, 802 A.2d 257 (Del. 2002)", "case"),
            ("In re El Paso Pipeline P'rs, L.P. Deriv. Litig., 2014 WL 2768782", "case"),
            ("8 Del. C. § 145", "statute"),
            ("Cal. Corp. Code § 17704.08(a)-(b)", "statute"),
            ("Ct. Ch. R. 56(a)", "rule"),
            ("D.R.E. 403", "rule"),
        )
        self.assertEqual(counts["not_found"], 0, checks)

    def test_party_names_ignore_punctuation(self):
        # Kulak v. On (Oct. 16): "In re ML/EQ" was reported missing because the
        # opinion keeps the slash and the extracted party name did not.
        from citecheck import check_citations as cc
        opinion = "[^29]In re ML/EQ Real Est. P\u2019ship Litig., Consol., 2000 WL 364188, at *1."
        for name in ("In re ML/EQ Real Est. P'ship Litig., 2000 WL 364188",
                     "In re ML EQ Real Estate Partnership Litigation, 2000 WL 364188"):
            summary = {"authorities_cited": [{"authority": name, "kind": "case"}]}
            self.assertEqual(cc(summary, opinion)["not_found"], 0, name)

    def test_loose_party_match_still_catches_invented_names(self):
        _, checks = run(("In re XY/ZQ Litig., 2000 WL 1234567", "case"))
        self.assertEqual(list(checks.values()), ["not_found"])

    def test_invented_reporter_cite_is_flagged(self):
        _, checks = run(("Telxon Corp. v. Meyerson, 802 A.2d 999 (Del. 2002)", "case"))
        self.assertEqual(list(checks.values()), ["not_found"])

    def test_invented_case_name_is_flagged(self):
        _, checks = run(("Smith v. Jones, 2020 WL 1234567 (Del. Ch. 2020)", "case"))
        self.assertEqual(list(checks.values()), ["not_found"])

    def test_bare_number_does_not_verify_a_statute(self):
        # "350" appears only in a street address.
        _, checks = run(("10 Del. C. § 350", "statute"))
        self.assertEqual(list(checks.values()), ["not_found"])

    def test_doctrine_without_cite_is_not_checked(self):
        _, checks = run(("Doctrine of fraudulent concealment", "doctrine"))
        self.assertEqual(list(checks.values()), ["not_checked"])

    def test_rerun_clears_stale_detail(self):
        summary = {"authorities_cited": [{"authority": "D.R.E. 403", "kind": "rule",
                                          "check": "not_found", "check_detail": "x"}]}
        check_citations(summary, OPINION)
        self.assertNotIn("check_detail", summary["authorities_cited"][0])


if __name__ == "__main__":
    unittest.main()
