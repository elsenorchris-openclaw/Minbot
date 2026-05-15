"""Tests for V1 min SKIP_BUY_NO_HRRR_DISSENT gate (2026-05-15 ship).

Filter: BUY_NO B-bracket gate. Block when HRRR <= cap AND
min(NBP, NBM-OM) >= cap + HRRR_DISSENT_NBP_NBM_BUFFER.

Trigger: KXLOWTNYC-26MAY15-B49.5 entered 2026-05-14 14:10 UTC, BUY_NO 88c
@ $0.68, MTM −$59.84. Entry: NBP=53, NBM-OM=52.3, HRRR=50.0 (HRRR exactly
at cap 50). Bot picked NBP as primary; existing filters didn't fire.

Backtest pool (n=140 since 2026-04-15): 1 historical fire, h:hu 1:0,
+$30 lift (caught KXLOWTDC-26MAY12-B45.5 −$60.35 with same pattern).
"""
import os
import sys
import unittest
from pathlib import Path

# Make paper_min_bot importable
ROOT = Path("/home/ubuntu/paper_min_bot")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Force ENABLED before import (constant is read at module load)
os.environ.setdefault("HRRR_DISSENT_ENABLED", "1")


class TestHrrrDissentConstants(unittest.TestCase):
    """Verify constants are present at expected defaults."""

    def test_constants_present(self):
        import paper_min_bot as bot
        self.assertTrue(hasattr(bot, "HRRR_DISSENT_ENABLED"),
                        "HRRR_DISSENT_ENABLED constant must exist")
        self.assertTrue(bot.HRRR_DISSENT_ENABLED,
                        "HRRR_DISSENT_ENABLED must be True (default-on)")
        self.assertTrue(hasattr(bot, "HRRR_DISSENT_NBP_NBM_BUFFER"),
                        "HRRR_DISSENT_NBP_NBM_BUFFER constant must exist")
        self.assertAlmostEqual(bot.HRRR_DISSENT_NBP_NBM_BUFFER, 1.5, places=4,
                               msg="HRRR_DISSENT_NBP_NBM_BUFFER must default to 1.5°F")


class TestHrrrDissentFunction(unittest.TestCase):
    """Verify _check_hrrr_dissent fires only on the correct pattern."""

    def setUp(self):
        import paper_min_bot as bot
        self.bot = bot
        self.fn = bot._check_hrrr_dissent

    def _opp(self, **kw):
        base = {
            "action": "BUY_NO",
            "floor": 49.0, "cap": 50.0,
            "mu_nbp": 53.0, "mu_nbm_om": 52.3, "mu_hrrr": 50.0,
        }
        base.update(kw)
        return base

    def test_today_nyc_trigger_blocks(self):
        """The actual 2026-05-15 NY case (mirror values) must trigger."""
        result = self.fn(self._opp())
        self.assertIsNotNone(result, "NYC-MAY15 pattern should block")
        self.assertIn("HRRR_DISSENT", result)
        self.assertIn("50.0", result)  # HRRR value

    def test_historical_dc_pattern_blocks(self):
        """KXLOWTDC-26MAY12-B45.5 pattern: HRRR=45.4 <= cap=45.5, NBP=48, NBM=49.1."""
        result = self.fn(self._opp(
            floor=44.0, cap=45.5,
            mu_nbp=48.0, mu_nbm_om=49.1, mu_hrrr=45.4,
        ))
        self.assertIsNotNone(result, "DC-MAY12 pattern should block")

    def test_buy_yes_does_not_trigger(self):
        result = self.fn(self._opp(action="BUY_YES"))
        self.assertIsNone(result, "Filter must not fire on BUY_YES")

    def test_t_bracket_does_not_trigger(self):
        """T-low / T-high brackets (one of floor/cap None) must not trigger."""
        result = self.fn(self._opp(floor=None, cap=50.0))
        self.assertIsNone(result, "T-bracket (no floor) must not trigger B-only filter")
        result2 = self.fn(self._opp(floor=49.0, cap=None))
        self.assertIsNone(result2, "T-bracket (no cap) must not trigger")

    def test_hrrr_above_cap_does_not_trigger(self):
        """If HRRR > cap, no dissent — filter must not fire."""
        result = self.fn(self._opp(mu_hrrr=51.0))  # above cap 50
        self.assertIsNone(result, "HRRR above cap should not trigger")

    def test_slow_models_too_close_does_not_trigger(self):
        """If NBP or NBM-OM is within buffer of cap, no confident dissent."""
        # KXLOWTMIN-26MAY14-B49.5 pattern: NBP=50.0 (at cap+0.5, not cap+1.5)
        result = self.fn(self._opp(
            cap=49.5, mu_nbp=50.0, mu_nbm_om=50.4, mu_hrrr=46.1,
        ))
        self.assertIsNone(result,
                          "When min(NBP, NBM-OM) within buffer of cap, no trigger")

    def test_one_slow_model_low_does_not_trigger(self):
        """KXLOWTDEN-26MAY09-B47.5 pattern: NBP=45 (below cap), HRRR=50.7 (above)."""
        result = self.fn(self._opp(
            cap=47.5, mu_nbp=45.0, mu_nbm_om=51.6, mu_hrrr=50.7,
        ))
        self.assertIsNone(result, "HRRR above cap → no trigger")

    def test_missing_source_does_not_trigger(self):
        """Filter requires all 3 sources; if any is None, abstain."""
        result = self.fn(self._opp(mu_hrrr=None))
        self.assertIsNone(result, "Missing HRRR → no trigger")
        result2 = self.fn(self._opp(mu_nbp=None))
        self.assertIsNone(result2, "Missing NBP → no trigger")
        result3 = self.fn(self._opp(mu_nbm_om=None))
        self.assertIsNone(result3, "Missing NBM-OM → no trigger")

    def test_hrrr_at_exact_cap_triggers(self):
        """HRRR exactly at cap (today's NY case) must trigger (<=, not <)."""
        result = self.fn(self._opp(mu_hrrr=50.0, cap=50.0))
        self.assertIsNotNone(result, "HRRR == cap must trigger (boundary inclusive)")

    def test_disable_via_constant(self):
        """Setting HRRR_DISSENT_ENABLED=False must disable filter."""
        original = self.bot.HRRR_DISSENT_ENABLED
        try:
            self.bot.HRRR_DISSENT_ENABLED = False
            result = self.fn(self._opp())
            self.assertIsNone(result, "Disabled filter must not fire")
        finally:
            self.bot.HRRR_DISSENT_ENABLED = original


class TestHrrrDissentWired(unittest.TestCase):
    """Verify filter is wired into both decision-evaluation paths."""

    def test_wired_in_filter_chains(self):
        """_check_hrrr_dissent must be called in the entry decision paths."""
        bot_path = Path("/home/ubuntu/paper_min_bot/paper_min_bot.py")
        src = bot_path.read_text()
        # At least 2 call sites (one in pure-decision return-tuple path,
        # one in audit_skip path)
        n_calls = src.count("_check_hrrr_dissent(opp)")
        self.assertGreaterEqual(n_calls, 2,
                                f"Expected >= 2 wire sites for _check_hrrr_dissent, found {n_calls}")
        # Audit skip tag must use HRRR_DISSENT
        self.assertIn('"HRRR_DISSENT"', src,
                      "Skip log must use exact tag 'HRRR_DISSENT' for audit grep")


if __name__ == "__main__":
    unittest.main()
