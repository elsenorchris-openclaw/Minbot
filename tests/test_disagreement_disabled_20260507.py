"""Regression test for 2026-05-07 DISAGREEMENT filter disable.

Audit found 1:6 helps:hurts on 7 resolved blocked tickers. Filter overrides
high-confidence model signal; lift est −$22.98 over 7 trades.
Pattern preserved via flag for fresh re-enable.
"""
import os, re, unittest, sys
from pathlib import Path

BOT_PATH = "/home/ubuntu/paper_min_bot/paper_min_bot.py"


def src(): return Path(BOT_PATH).read_text()


class TestDisagreementDisabled(unittest.TestCase):
    def test_flag_set_false(self):
        s = src()
        m = re.search(r"^_DISAGREEMENT_ENABLED\s*=\s*False\b", s, re.MULTILINE)
        self.assertIsNotNone(m, "_DISAGREEMENT_ENABLED must be False (disabled).")

    def test_max_disagreement_constant_preserved(self):
        s = src()
        # Predicate constant kept so re-enable doesn't need a re-discover.
        m = re.search(r"^MAX_DISAGREEMENT_F\s*=\s*5\.0\b", s, re.MULTILINE)
        self.assertIsNotNone(m, "MAX_DISAGREEMENT_F constant should remain 5.0.")

    def test_evaluate_gates_call_site_gated(self):
        s = src()
        # _evaluate_gates check must be wrapped with `if _DISAGREEMENT_ENABLED:`
        self.assertRegex(
            s,
            r"if _DISAGREEMENT_ENABLED:\s*\n\s+disag = float\(opp\.get\(\"disagreement\"\) or 0\)\s*\n\s+if disag > MAX_DISAGREEMENT_F:")

    def test_production_call_site_gated(self):
        s = src()
        # Production filter site (with _audit_skip) must also be gated
        self.assertRegex(
            s,
            r"if _DISAGREEMENT_ENABLED:\s*\n\s+disagreement = float\(opp\.get\(\"disagreement\", 0\.0\)\)\s*\n\s+if disagreement > MAX_DISAGREEMENT_F:")

    def test_module_imports_with_flag_false(self):
        sys.path.insert(0, "/home/ubuntu/paper_min_bot")
        if "paper_min_bot" in sys.modules:
            del sys.modules["paper_min_bot"]
        import paper_min_bot as m
        self.assertFalse(m._DISAGREEMENT_ENABLED)
        self.assertEqual(m.MAX_DISAGREEMENT_F, 5.0)


if __name__ == "__main__":
    unittest.main()
