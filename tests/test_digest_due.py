"""Run with: python -m unittest discover -s tests"""
import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

HAVE_DEPS = importlib.util.find_spec("requests") is not None


@unittest.skipUnless(HAVE_DEPS, "requests not installed")
class DigestDueTest(unittest.TestCase):
    def setUp(self):
        import common
        import send_digest
        self.sd, self.NY = send_digest, common.NY
        self.sent = {"sent": [{"ny_date": "2026-10-19"}]}

    def due(self, when, force=False, sent=None):
        now = datetime(*when, tzinfo=self.NY)
        return self.sd.digest_due(now, now.date().isoformat(), sent or self.sent, force)[0]

    def test_gate(self):
        self.assertTrue(self.due((2026, 10, 20, 18, 7)))           # Tue 6:07pm EDT slot
        self.assertFalse(self.due((2026, 10, 20, 17, 7)))          # off-DST slot: too early
        self.assertFalse(self.due((2026, 10, 19, 19, 7)))          # Mon: already sent
        self.assertFalse(self.due((2026, 10, 24, 18, 7)))          # Saturday
        self.assertTrue(self.due((2026, 10, 24, 18, 7), force=True))
        self.assertFalse(self.due((2026, 10, 19, 19, 7), force=True))  # force never re-sends

    def test_check_mode_reports_due_and_sends_nothing(self):
        out_file = Path(tempfile.mkdtemp()) / "out"
        calls = []
        saved = (self.sd.now_ny, self.sd.today_ny, self.sd.load_json, self.sd.send_via_resend)
        try:
            self.sd.now_ny = lambda: datetime(2026, 10, 20, 18, 7, tzinfo=self.NY)
            self.sd.today_ny = lambda: "2026-10-20"
            self.sd.load_json = lambda path, default: self.sent if "sent" in str(path) else default
            self.sd.send_via_resend = lambda *a: calls.append(a)
            os.environ["GITHUB_OUTPUT"] = str(out_file)
            sys.argv = ["send_digest.py", "--check"]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(self.sd.main(), 0)
        finally:
            self.sd.now_ny, self.sd.today_ny, self.sd.load_json, self.sd.send_via_resend = saved
            os.environ.pop("GITHUB_OUTPUT", None)
        self.assertIn("due=true", out_file.read_text())
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
