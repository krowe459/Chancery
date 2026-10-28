"""Run with: python -m unittest discover -s tests"""
import importlib.util
import json
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))
EXAMPLE = json.loads((SRC / "prompt_example.json").read_text("utf-8"))


def check(value, schema, path="summary"):
    """Minimal JSON Schema check covering the keywords SUMMARY_SCHEMA uses."""
    kind = schema["type"]
    if kind == "object":
        assert isinstance(value, dict), path
        assert set(value) == set(schema["required"]), f"{path}: keys {sorted(value)}"
        assert set(schema["properties"]) == set(schema["required"]), path
        for key, sub in schema["properties"].items():
            check(value[key], sub, f"{path}.{key}")
    elif kind == "array":
        assert isinstance(value, list) and value, f"{path}: expected a non-empty list"
        for i, item in enumerate(value):
            check(item, schema["items"], f"{path}[{i}]")
    elif kind == "string":
        assert isinstance(value, str) and value.strip(), f"{path}: expected text"
        if "enum" in schema:
            assert value in schema["enum"], f"{path}: {value!r} not in {schema['enum']}"


@unittest.skipUnless(importlib.util.find_spec("anthropic"), "anthropic not installed")
class PromptExampleTest(unittest.TestCase):
    def test_example_matches_summary_schema(self):
        from summarize import SUMMARY_SCHEMA
        check(EXAMPLE["summary"], SUMMARY_SCHEMA)

    def test_example_is_in_the_final_prompt_only(self):
        import summarize
        issue = json.dumps(EXAMPLE["summary"]["core_legal_issue"], ensure_ascii=False)
        self.assertIn(issue, summarize.EXAMPLE_PROMPT)
        self.assertNotIn(EXAMPLE["decision"], summarize.SYSTEM_PROMPT)
        self.assertEqual(len(summarize._system(False)), 1)
        self.assertEqual(summarize._system(True)[-1]["cache_control"], {"type": "ephemeral"})

    def test_post_trial_dispositions_are_offered(self):
        import summarize
        enum = summarize.SUMMARY_SCHEMA["properties"]["disposition"]["enum"]
        for value in ("judgment for plaintiff", "judgment for defendant",
                      "judgment in part for each side"):
            self.assertIn(value, enum)
            self.assertIn(f'"{value}"', summarize.SYSTEM_PROMPT)

    def test_takeaway_rule_limits_case_descriptions_to_the_opinion(self):
        import summarize
        rule = " ".join(summarize.SYSTEM_PROMPT.split())
        self.assertIn("Everything it says about this case", rule)
        self.assertIn("Do not label the case with a category the opinion does not use", rule)
        self.assertIn("The advice must follow from the ground the Court actually decided on", rule)
        self.assertIn("do not generalize the holding beyond the conditions", rule)

    def test_a_claim_is_won_only_with_relief(self):
        # Davenport v. Toro (Oct. 27): breach proved, no damages, deposit
        # counterclaim lost; labeled "judgment in part for each side".
        import summarize
        rule = " ".join(summarize.SYSTEM_PROMPT.split())
        self.assertIn("A party wins a claim only if it gets relief on it", rule)

    def test_magistrate_status_is_described_in_the_reports_own_words(self):
        import summarize
        rule = " ".join(summarize.SYSTEM_PROMPT.split())
        self.assertIn("That is for the label only", rule)
        self.assertNotIn("which is not binding until adopted", rule)


if __name__ == "__main__":
    unittest.main()
