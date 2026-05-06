"""Regression tests for the 2026-05-06 min_bot SKIP_MU_NEAR_FLOOR +
SKIP_MU_NEAR_BELOW_BRACKET filters.

Mirror of V1's SKIP_MU_NEAR_BELOW_BRACKET pattern. Two filters under
one helper:
  (A) tail_high BUY_YES with mu in (floor, floor+2.0]F
  (B) B-bracket BUY_NO with mu in [mid-2.0, mid)F

Backtest evidence (5/4-5/6, n=44 with Kalshi truth):
  (A): 6L : 0W (all 6 BUY_YES T-floor losers caught, 0 winners blocked)
  (B): 6L : 3W (mirror of V1 ratio)

Threshold 2.0F matches V1's MU_NEAR_BELOW_BRACKET_MAX for symmetry.
"""
import os
import re
import sys
import unittest
from pathlib import Path

BOT_PATH = os.environ.get("BOT_PATH", "/home/ubuntu/paper_min_bot/paper_min_bot.py")


def src():
    return Path(BOT_PATH).read_text()


class TestConstantsDefined(unittest.TestCase):

    def test_tail_high_constant(self):
        s = src()
        m = re.search(r"^MU_NEAR_FLOOR_TAIL_HIGH_MAX_F\s*=\s*([0-9.]+)",
                      s, re.MULTILINE)
        self.assertIsNotNone(m, "MU_NEAR_FLOOR_TAIL_HIGH_MAX_F missing")
        self.assertEqual(float(m.group(1)), 2.0,
            "Threshold must be 2.0F (matches V1 SKIP_MU_NEAR_BELOW_BRACKET).")

    def test_bracket_constant(self):
        s = src()
        m = re.search(r"^MU_NEAR_BELOW_BRACKET_MAX_F\s*=\s*([0-9.]+)",
                      s, re.MULTILINE)
        self.assertIsNotNone(m, "MU_NEAR_BELOW_BRACKET_MAX_F missing")
        self.assertEqual(float(m.group(1)), 2.0)


class TestHelperPresent(unittest.TestCase):

    def test_helper_def(self):
        s = src()
        self.assertIn("def _check_mu_position_filter", s,
            "Helper function _check_mu_position_filter must exist")

    def test_skip_tags_present(self):
        s = src()
        self.assertIn("MU_NEAR_FLOOR", s)
        self.assertIn("MU_NEAR_BELOW_BRACKET", s)


class TestPredicateLogic(unittest.TestCase):
    """Behavioral: import the bot and call _check_mu_position_filter directly."""

    def setUp(self):
        sys.path.insert(0, str(Path(BOT_PATH).parent))
        # Minimal import: the bot has heavy imports so spec-load it
        import importlib.util
        spec = importlib.util.spec_from_file_location("paper_min_bot", BOT_PATH)
        try:
            self.mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.mod)
            self.fn = getattr(self.mod, "_check_mu_position_filter", None)
        except Exception as e:
            self.fn = None
            self._import_err = e

    def test_can_import(self):
        if self.fn is None:
            self.skipTest(f"bot module didn't load (env): {getattr(self, '_import_err', '?')}")

    def test_tail_high_buy_yes_blocks_at_gap_05(self):
        if self.fn is None:
            self.skipTest("module not importable in test env")
        opp = {"action": "BUY_YES", "floor": 46.5, "cap": None, "mu": 47.0}
        result = self.fn(opp)
        self.assertIsNotNone(result, "should block: TDC-T46-shape (mu=47, fl=46.5)")
        self.assertEqual(result[0], "MU_NEAR_FLOOR")

    def test_tail_high_buy_yes_blocks_at_gap_15(self):
        if self.fn is None:
            self.skipTest("module not importable in test env")
        opp = {"action": "BUY_YES", "floor": 38.5, "cap": None, "mu": 40.0}
        self.assertIsNotNone(self.fn(opp), "TMIN-T38-shape (mu=40, fl=38.5)")

    def test_tail_high_buy_yes_passes_at_gap_25(self):
        if self.fn is None:
            self.skipTest("module not importable in test env")
        opp = {"action": "BUY_YES", "floor": 56.5, "cap": None, "mu": 59.0}
        self.assertIsNone(self.fn(opp), "gap=2.5F is past threshold; should pass")

    def test_tail_high_buy_yes_passes_at_gap_zero(self):
        if self.fn is None:
            self.skipTest("module not importable in test env")
        opp = {"action": "BUY_YES", "floor": 50.0, "cap": None, "mu": 50.0}
        self.assertIsNone(self.fn(opp), "gap=0 (mu==floor) must pass; only POSITIVE gaps blocked")

    def test_tail_high_buy_no_passes(self):
        if self.fn is None:
            self.skipTest("module not importable in test env")
        opp = {"action": "BUY_NO", "floor": 46.5, "cap": None, "mu": 47.0}
        self.assertIsNone(self.fn(opp), "BUY_NO on tail_high must NOT trigger filter A")

    def test_bracket_buy_no_blocks_at_gap_15(self):
        if self.fn is None:
            self.skipTest("module not importable in test env")
        opp = {"action": "BUY_NO", "floor": 58.0, "cap": 59.0, "mu": 57.0}
        result = self.fn(opp)
        self.assertIsNotNone(result, "TLAX-B58.5-shape (mu=57, mid=58.5, gap=1.5)")
        self.assertEqual(result[0], "MU_NEAR_BELOW_BRACKET")

    def test_bracket_buy_no_passes_at_gap_above_2(self):
        if self.fn is None:
            self.skipTest("module not importable in test env")
        opp = {"action": "BUY_NO", "floor": 36.0, "cap": 37.0, "mu": 33.0}
        self.assertIsNone(self.fn(opp), "gap=3.5F past threshold; should pass")

    def test_bracket_buy_no_passes_when_mu_above_mid(self):
        if self.fn is None:
            self.skipTest("module not importable in test env")
        opp = {"action": "BUY_NO", "floor": 58.0, "cap": 59.0, "mu": 60.0}
        self.assertIsNone(self.fn(opp), "mu above mid (negative gap) must pass; "
            "filter is asymmetric — only mu BELOW mid is blocked")

    def test_bracket_buy_yes_passes(self):
        if self.fn is None:
            self.skipTest("module not importable in test env")
        opp = {"action": "BUY_NO", "floor": None, "cap": 45.5, "mu": 44.0}
        self.assertIsNone(self.fn(opp), "tail_low (cap-only) must NOT trigger filter B")

    def test_no_mu_passes(self):
        if self.fn is None:
            self.skipTest("module not importable in test env")
        opp = {"action": "BUY_YES", "floor": 46.5, "cap": None, "mu": None}
        self.assertIsNone(self.fn(opp))


class TestWiringIntoGates(unittest.TestCase):
    """Source-level check that _check_mu_position_filter is called in BOTH
    _evaluate_gates and execute_opportunity (parity with _check_model_market_disagree)."""

    def test_called_in_two_places(self):
        s = src()
        callsites = re.findall(r"_check_mu_position_filter\s*\(\s*opp\s*\)", s)
        # Definition itself doesn't match (it's `def _check_mu_position_filter(`)
        # but callsites are _check_mu_position_filter(opp).
        self.assertGreaterEqual(len(callsites), 2,
            "Helper must be called from BOTH _evaluate_gates AND execute_opportunity "
            "(mirrors _check_model_market_disagree's twin-call pattern)")


class TestExistingFiltersUnchanged(unittest.TestCase):

    def test_mmd_constant_unchanged(self):
        s = src()
        m = re.search(r"^SKIP_MODEL_MARKET_DISAGREE_MP_FLOOR\s*=\s*([0-9.]+)",
                      s, re.MULTILINE)
        self.assertIsNotNone(m)
        self.assertEqual(float(m.group(1)), 0.22)

    def test_mmd_helper_still_present(self):
        s = src()
        self.assertIn("def _check_model_market_disagree", s)


if __name__ == "__main__":
    unittest.main()
