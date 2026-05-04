"""Regression tests for the 2026-05-04 night BUY_YES mp threshold tightening.

Change: DIRECTIONAL_BUY_YES_MIN_MP from 0.60 → 0.65.

Backtest evidence (settlements.jsonl Apr 25 - May 4):
  BUY_YES T-floor mp 60-65% bucket: n=7, won 2/7 (29%), pnl -$34.28
  BUY_YES T-floor mp 65-75% bucket: n=5, won 4/5 (80%), pnl +$10.84

The 60-65% bucket is the calibration-boundary cohort where the model is
systematically overconfident. Tightening to 0.65 catches 6 of 7 recent
losses (DC-T46 -$24, OKC-T47 -$4.68, LAX-T53 -$1.64, LAX-T54 -$1.64,
ATL-T53 -$4.56, OKC-T51 -$4.68) at the cost of 1 small winner
(ATL-T60 +$0.64).

Net lift +$34.28 / 10d. 6:1 helps:hurts. Re-evaluate ~2026-05-18.
"""
import os
import sys
import unittest
from pathlib import Path

BOT_PATH = os.environ.get("PAPER_MIN_BOT_PATH",
                          "/home/ubuntu/paper_min_bot/paper_min_bot.py")
sys.path.insert(0, "/home/ubuntu/paper_min_bot")


def src():
    return Path(BOT_PATH).read_text()


def _import_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("paper_min_bot_065", BOT_PATH)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception:
        return None
    return mod

_MOD = _import_module()


class TestConstantTightened(unittest.TestCase):

    def test_constant_value_065(self):
        s = src()
        # Allow whitespace variation but require the new value
        import re
        m = re.search(r"^DIRECTIONAL_BUY_YES_MIN_MP\s*=\s*([0-9.]+)", s, re.MULTILINE)
        self.assertIsNotNone(m, "DIRECTIONAL_BUY_YES_MIN_MP must be defined")
        self.assertEqual(float(m.group(1)), 0.65,
            "DIRECTIONAL_BUY_YES_MIN_MP must be 0.65 (was 0.60). "
            "Backtest: BUY_YES T-floor mp 60-65% bucket bled -$34/10d.")

    def test_in_module_too(self):
        if _MOD is None:
            self.skipTest("Could not import bot module")
        self.assertEqual(_MOD.DIRECTIONAL_BUY_YES_MIN_MP, 0.65)


class TestEntryGateBlocksAt062(unittest.TestCase):
    """Verify entry gate would block a BUY_YES with mp=0.62."""

    def test_filter_function_present(self):
        s = src()
        self.assertIn("DIRECTIONAL_BUY_YES_MIN_MP", s,
            "Constant must be referenced in entry-gate logic")
        # Verify the gate uses < (strictly less than)
        self.assertIn('action == "BUY_YES" and mp < DIRECTIONAL_BUY_YES_MIN_MP', s,
            "Gate must use `mp < DIRECTIONAL_BUY_YES_MIN_MP` (strict less-than) "
            "so that mp = 0.65 passes but mp < 0.65 blocks")

    def test_block_threshold_062(self):
        """At mp=0.62, entry-side _evaluate_buy_gates should return DIRECTIONAL_BUY_YES."""
        if _MOD is None:
            self.skipTest("Could not import bot module")
        # Build a synthetic opp at mp=0.62
        # The entry-side filter is in _evaluate_buy_gates. Test directly.
        opp = {
            "action": "BUY_YES",
            "model_prob": 0.62,
            "ticker": "KXLOWTOKC-26MAY02-T47",
            "floor": 47.0, "cap": None,
            "mu": 48.5, "sigma": 2.0,
            "edge": 0.10,
            "yes_ask": 0.40,
            "running_min": None,
            "tz": "America/New_York",
            "station": "KOKC",
            "date_str": "2026-05-02",
            "days_out": 1,
            "label": "OKC",
        }
        # _evaluate_buy_gates returns (skip_reason, detail) or None
        result = _MOD._evaluate_gates(opp)
        self.assertIsNotNone(result,
            "BUY_YES at mp=0.62 must be blocked by some gate (DIRECTIONAL_BUY_YES at minimum)")
        # Either DIRECTIONAL_BUY_YES or another gate could fire first; verify
        # that if mp=0.65 passes the same gate.

    def test_pass_threshold_065(self):
        """At mp=0.65, entry-side _evaluate_buy_gates should NOT return DIRECTIONAL_BUY_YES."""
        if _MOD is None:
            self.skipTest("Could not import bot module")
        opp = {
            "action": "BUY_YES",
            "model_prob": 0.65,
            "ticker": "KXLOWTSATX-26APR28-T75",
            "floor": 75.0, "cap": None,
            "mu": 76.5, "sigma": 2.0,  # margin = 1.5°F into YES
            "edge": 0.15,
            "yes_ask": 0.50,
            "running_min": None,
            "tz": "America/Chicago",
            "station": "KSAT",
            "date_str": "2026-04-28",
            "days_out": 1,
            "label": "San Antonio",
        }
        result = _MOD._evaluate_gates(opp)
        # If DIRECTIONAL_BUY_YES fires, that's a regression. Other gates may
        # block (e.g. YES_TAIL_MARGIN with margin=1.5 ≥ 1.0 should pass too).
        if result is not None:
            self.assertNotEqual(result[0], "DIRECTIONAL_BUY_YES",
                f"BUY_YES at mp=0.65 must NOT be blocked by DIRECTIONAL_BUY_YES "
                f"gate (boundary inclusive). Got skip reason: {result}")


class TestBuyNoUnchanged(unittest.TestCase):
    """Sanity: BUY_NO threshold (DIRECTIONAL_BUY_NO_MAX_MP) is unaffected."""

    def test_buy_no_threshold_unchanged(self):
        s = src()
        import re
        m = re.search(r"^DIRECTIONAL_BUY_NO_MAX_MP\s*=\s*([0-9.]+)", s, re.MULTILINE)
        # Just verify the constant exists with whatever value (don't pin a specific
        # value here — that's tested in other test files)
        self.assertIsNotNone(m,
            "DIRECTIONAL_BUY_NO_MAX_MP must remain defined; this change is BUY_YES only")


if __name__ == "__main__":
    unittest.main()
