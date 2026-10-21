"""Run with: python -m unittest discover -s tests"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from verify import RESULT_SCHEMA, VERIFY_MODEL, collect_claims, fact_check  # noqa: E402

SUMMARY = {
    "key_facts_and_concessions": ["PRADCO sent a letter of intent on Oct. 29.",
                                  "Thompson signed the letter of intent."],
    "arguments_rejected_or_waived": ["Waiver: rejected for lack of knowledge."],
    "holding": "Judgment for Techno-X.",
    "brief_summary": "Techno-X sued Spartan Forge.",
    "practical_takeaway": "Not checked: inference by design.",
}


CLAIM_TEXT = {1: SUMMARY["key_facts_and_concessions"][0], 2: SUMMARY["key_facts_and_concessions"][1],
              3: SUMMARY["arguments_rejected_or_waived"][0], 4: SUMMARY["holding"],
              5: SUMMARY["brief_summary"]}


def fake_call(verdicts):
    """Stand-in for summarize._call that returns canned verdicts by claim id,
    one dict per call (cycled), and records what it was sent."""
    sent = []

    def call(messages, **kw):
        sent.append({"prompt": messages[0]["content"], **kw})
        table = verdicts[(len(sent) - 1) % len(verdicts)]
        results = []
        for cid, v in table.items():
            bad = v not in ("supported", "not_in_excerpt")
            claim = CLAIM_TEXT.get(cid, "")
            results.append({"id": cid, "verdict": v, "problem": f"problem {cid}" if bad else "",
                            "assertion": claim.split()[0] + " " + claim.split()[1] if bad and claim else "",
                            "opinion_says": "something else" if bad else "", "evidence": "",
                            "confirmed": bad})
        return json.dumps({"results": results}), 1000
    return call, sent


class CollectClaimsTest(unittest.TestCase):
    def test_lists_and_text_fields_but_not_takeaway(self):
        claims = collect_claims(SUMMARY)
        self.assertEqual([c["field"] for c in claims],
                         ["key_facts_and_concessions", "key_facts_and_concessions",
                          "arguments_rejected_or_waived", "holding", "brief_summary"])
        self.assertEqual([c["id"] for c in claims], [1, 2, 3, 4, 5])
        self.assertEqual(claims[1]["index"], 1)
        self.assertIsNone(claims[3]["index"])
        self.assertNotIn("inference by design", json.dumps(claims))


class FactCheckTest(unittest.TestCase):
    def test_single_pass_flags_unsupported_and_contradicted(self):
        call, sent = fake_call([{1: "supported", 2: "unsupported", 3: "supported",
                                 4: "contradicted", 5: "supported"}])
        usage = []
        fc = fact_check(SUMMARY, text="opinion text", chunks=None, call=call, usage=usage)
        self.assertEqual(fc["coverage"], "full")
        self.assertEqual(fc["checked"], 5)
        self.assertEqual([(f["field"], f["index"], f["verdict"]) for f in fc["flags"]],
                         [("key_facts_and_concessions", 1, "unsupported"),
                          ("holding", None, "contradicted")])
        self.assertEqual(usage, [1000])
        self.assertEqual(sent[0]["model"], VERIFY_MODEL)
        self.assertIs(sent[0]["schema"], RESULT_SCHEMA)
        self.assertIn("2. Thompson signed the letter of intent.", sent[0]["prompt"])
        self.assertIn("<opinion>\nopinion text\n</opinion>", sent[0]["prompt"])

    def test_chunked_flags_only_contradictions(self):
        # Chunk 1 supports claim 1 and contradicts claim 2; chunk 2 cannot see
        # claim 3 at all. "Unsupported" in an excerpt is not flagged.
        call, sent = fake_call([
            {1: "supported", 2: "contradicted", 3: "not_in_excerpt", 4: "unsupported", 5: "supported"},
            {1: "not_in_excerpt", 2: "supported", 3: "not_in_excerpt", 4: "unsupported", 5: "supported"},
        ])
        fc = fact_check(SUMMARY, text=None, chunks=["part one", "part two"], call=call, usage=[])
        self.assertEqual(fc["coverage"], "contradictions_only")
        self.assertEqual([(f["field"], f["index"]) for f in fc["flags"]],
                         [("key_facts_and_concessions", 1)])
        self.assertEqual(len(sent), 2)
        self.assertIn("part 1 of 2", sent[0]["prompt"])

    def test_missing_verdicts_are_counted_as_unchecked(self):
        call, _ = fake_call([{1: "supported", 2: "supported"}])
        fc = fact_check(SUMMARY, text="t", chunks=None, call=call, usage=[])
        self.assertEqual(fc["unchecked"], 3)
        self.assertEqual(fc["flags"], [])

    def test_empty_summary_makes_no_call(self):
        call, sent = fake_call([{}])
        fc = fact_check({}, text="t", chunks=None, call=call, usage=[])
        self.assertEqual((fc["checked"], sent), (0, []))


class VagueFlagTest(unittest.TestCase):
    def test_flags_need_a_quoted_assertion_and_a_counter_statement(self):
        from verify import specific
        claim = "Thompson signed the letter of intent on Oct. 29."
        good = {"assertion": "Thompson signed", "opinion_says": "PRADCO sent it"}
        self.assertTrue(specific(good, claim))
        self.assertFalse(specific({**good, "assertion": ""}, claim))
        self.assertFalse(specific({**good, "assertion": "Thompson agreed"}, claim))  # not in claim
        self.assertFalse(specific({**good, "opinion_says": " "}, claim))
        self.assertTrue(specific({**good, "assertion": "thompson  SIGNED"}, claim))  # case/space

    def test_vague_flag_is_dropped_and_counted(self):
        def call(messages, **kw):
            return json.dumps({"results": [
                {"id": 1, "verdict": "unsupported", "assertion": "", "opinion_says": "",
                 "problem": "...is fine... the problem is none substantive...", "evidence": ""},
                {"id": 2, "verdict": "unsupported", "assertion": "Thompson signed",
                 "opinion_says": "PRADCO sent the LOI", "problem": "Wrong act.", "evidence": "",
                 "confirmed": True},
            ]}), 10
        fc = fact_check(SUMMARY, text="t", chunks=None, call=call, usage=[])
        self.assertEqual(fc["dropped_vague"], 1)
        self.assertEqual([(f["index"], f["assertion"]) for f in fc["flags"]], [(1, "Thompson signed")])


class UnconfirmedFlagTest(unittest.TestCase):
    def test_flag_whose_own_check_finds_support_is_dropped(self):
        # The Oct. 16 preview's bond flag: verdict first, then "Actually ...
        # the assertion is supported". With confirmed=False it is dropped.
        claim = SUMMARY["brief_summary"]
        def call(messages, **kw):
            return json.dumps({"results": [
                {"id": 5, "evidence": "Techno-X sued", "opinion_says": "Techno-X sued Spartan Forge",
                 "assertion": claim, "problem": "Actually the claim is supported.",
                 "verdict": "unsupported", "confirmed": False}]}), 10
        fc = fact_check(SUMMARY, text="t", chunks=None, call=call, usage=[])
        self.assertEqual((fc["flags"], fc["dropped_unconfirmed"], fc["dropped_vague"]), ([], 1, 0))

    def test_schema_puts_evidence_before_verdict_and_check_last(self):
        order = list(RESULT_SCHEMA["properties"]["results"]["items"]["properties"])
        self.assertLess(order.index("evidence"), order.index("verdict"))
        self.assertEqual(order[-1], "confirmed")


class DigestRenderingTest(unittest.TestCase):
    def setUp(self):
        import send_digest
        self.render = send_digest.render_entry
        self.caps = (send_digest.MAX_KEY_FACTS, send_digest.MAX_ARGUMENTS)

    def entry(self, n_facts, flags=()):
        return {"name": "A v. B", "summary": {
            "core_legal_issue": "x", "holding": "h", "brief_summary": "b",
            "key_facts_and_concessions": [f"fact {i}" for i in range(n_facts)],
            "fact_check": {"coverage": "full", "flags": list(flags)}}}

    def test_long_list_is_capped_with_more_line(self):
        html = self.render(self.entry(self.caps[0] + 3), "2026-10-15")
        self.assertIn(f"fact {self.caps[0] - 1}<", html)
        self.assertNotIn(f"fact {self.caps[0]}<", html)
        self.assertIn("+3 more in the full summary", html)
        self.assertIn("state/archive/2026-10-15.json", html)

    def test_flagged_item_beyond_cap_stays_visible(self):
        late = self.caps[0] + 1
        flag = {"field": "key_facts_and_concessions", "index": late, "verdict": "unsupported",
                "assertion": "signed", "opinion_says": "PRADCO sent it",
                "problem": "Says signed; opinion says sent."}
        html = self.render(self.entry(self.caps[0] + 3, [flag]), "2026-10-15")
        self.assertIn(f"fact {late}<", html)
        self.assertIn("+2 more in the full summary", html)
        self.assertIn("Fact check (unsupported): &ldquo;signed&rdquo; &mdash; opinion: "
                      "PRADCO sent it. Says signed; opinion says sent.", html)
        self.assertIn("1 statement in this summary was flagged", html)

    def test_distinguished_authority_beyond_cap_stays_visible(self):
        import send_digest
        cap = send_digest.MAX_AUTHORITIES
        auths = [{"authority": f"Case {i} v. X", "role": "relied_on", "proposition": "p"}
                 for i in range(cap)]
        auths += [{"authority": "Khanna v. Covad", "role": "distinguished", "proposition": "p"},
                  {"authority": "Extra v. Y", "role": "relied_on", "proposition": "p"}]
        entry = self.entry(1)
        entry["summary"]["authorities_cited"] = auths
        html = self.render(entry, "2026-10-20")
        self.assertIn("Khanna v. Covad</strong> <em>(distinguished)</em>", html)
        self.assertNotIn("Extra v. Y", html)
        self.assertIn("+1 more in the full summary", html)

    def test_flag_on_text_field_and_old_entries_without_fact_check(self):
        flag = {"field": "holding", "index": None, "verdict": "contradicted", "problem": "Wrong party."}
        self.assertIn("Fact check (contradicted): Wrong party.",
                      self.render(self.entry(1, [flag]), "d"))
        old = {"name": "A v. B", "summary": {"core_legal_issue": "x", "holding": "h",
                                             "brief_summary": "b"}}
        self.assertNotIn("Fact check", self.render(old, "d"))


if __name__ == "__main__":
    unittest.main()
