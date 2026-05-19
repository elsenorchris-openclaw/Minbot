"""Tests for BUY_NO_HRRR_IN_BRACKET_WARM gate shipped 2026-05-19 PM.

Gate blocks BUY_NO B-bracket when:
  (a) mu > cap (warm-side bet — bot expects cli > cap)
  AND
  (b) floor <= mu_hrrr <= cap (HRRR predicts cli will land IN the YES bracket)

Direct two-source opposition on risk direction.

Backtest: stack-aware n=5 May 4-19 (settled+stopped with cli ground-truth from
obs.sqlite cli_reports), h:hu 5:0 PERFECT (∞), lift +$174.21, LOO-1 +$113.86.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestHrrrInBracketWarm(unittest.TestCase):
    """Core: mu > cap AND HRRR ∈ [floor, cap]."""

    def setUp(self):
        import paper_min_bot as pb
        self.pb = pb
        self._orig = pb.BUY_NO_HRRR_IN_BRACKET_WARM_ENABLED
        pb.BUY_NO_HRRR_IN_BRACKET_WARM_ENABLED = True

    def tearDown(self):
        self.pb.BUY_NO_HRRR_IN_BRACKET_WARM_ENABLED = self._orig

    def _opp(self, **overrides):
        # Base: DC-MAY12 pattern (mu=48 nbp, floor=45 cap=46, HRRR=45.4)
        base = {
            "action": "BUY_NO", "floor": 45.0, "cap": 46.0,
            "mu": 48.0, "mu_source": "nbp",
            "mu_hrrr": 45.4, "is_today": False,
        }
        base.update(overrides)
        return base

    def test_fires_dc_may12_pattern(self):
        """DC-MAY12 historical -$60.35: mu=48 nbp, floor=45, cap=46, HRRR=45.4 → fires."""
        r = self.pb._check_hrrr_in_bracket_warm(self._opp())
        self.assertIsNotNone(r)
        self.assertIn("BUY_NO_HRRR_IN_BRACKET_WARM", r)
        self.assertIn("warm-side bet", r)

    def test_fires_nyc_may15_pattern(self):
        """NYC-MAY15 -$59.84: mu=53 nbp, floor=49 cap=50, HRRR=50.0 (at cap exactly)."""
        opp = self._opp(mu=53.0, floor=49.0, cap=50.0, mu_hrrr=50.0)
        self.assertIsNotNone(self.pb._check_hrrr_in_bracket_warm(opp))

    def test_fires_dal_may14_pattern(self):
        """DAL-MAY14 -$31.64: mu=69 nbp, floor=67 cap=68, HRRR=67.8."""
        opp = self._opp(mu=69.0, floor=67.0, cap=68.0, mu_hrrr=67.8)
        self.assertIsNotNone(self.pb._check_hrrr_in_bracket_warm(opp))

    def test_fires_mia_may04_pattern(self):
        """MIA-MAY04 -$21.96: mu=74 nbp, floor=71 cap=72, HRRR=72.0 (at cap)."""
        opp = self._opp(mu=74.0, floor=71.0, cap=72.0, mu_hrrr=72.0)
        self.assertIsNotNone(self.pb._check_hrrr_in_bracket_warm(opp))

    def test_fires_dal_may11_pattern(self):
        """DAL-MAY11 -$0.42: mu=61.7 nbm_d0_override, floor=58 cap=59, HRRR=58.3.
        Verifies the filter is source-agnostic (not NBP-only)."""
        opp = self._opp(mu=61.7, floor=58.0, cap=59.0, mu_hrrr=58.3,
                        mu_source="nbm_d0_override", is_today=True)
        self.assertIsNotNone(self.pb._check_hrrr_in_bracket_warm(opp))

    def test_fires_hrrr_at_floor_exactly(self):
        """Boundary: HRRR == floor → fires (inclusive)."""
        opp = self._opp(mu=48.0, floor=45.0, cap=46.0, mu_hrrr=45.0)
        self.assertIsNotNone(self.pb._check_hrrr_in_bracket_warm(opp))

    def test_fires_hrrr_at_cap_exactly(self):
        """Boundary: HRRR == cap → fires (inclusive)."""
        opp = self._opp(mu=48.0, floor=45.0, cap=46.0, mu_hrrr=46.0)
        self.assertIsNotNone(self.pb._check_hrrr_in_bracket_warm(opp))

    def test_no_fire_mu_at_cap_not_warm(self):
        """mu == cap → not warm-side, no fire (strict mu > cap)."""
        opp = self._opp(mu=46.0, floor=45.0, cap=46.0, mu_hrrr=45.5)
        self.assertIsNone(self.pb._check_hrrr_in_bracket_warm(opp))

    def test_no_fire_mu_below_cap(self):
        """mu < cap → not warm-side."""
        opp = self._opp(mu=44.0, floor=45.0, cap=46.0, mu_hrrr=45.5)
        self.assertIsNone(self.pb._check_hrrr_in_bracket_warm(opp))

    def test_no_fire_hrrr_below_floor(self):
        """HRRR < floor (cold-direction warning, supports BUY_NO) → no fire."""
        opp = self._opp(mu=48.0, floor=45.0, cap=46.0, mu_hrrr=42.0)
        self.assertIsNone(self.pb._check_hrrr_in_bracket_warm(opp))

    def test_no_fire_hrrr_above_cap(self):
        """HRRR > cap (agrees with warm-side bet) → no fire."""
        opp = self._opp(mu=48.0, floor=45.0, cap=46.0, mu_hrrr=47.0)
        self.assertIsNone(self.pb._check_hrrr_in_bracket_warm(opp))

    def test_no_fire_clean_warm_setup(self):
        """Standard warm-side BUY_NO with HRRR also warm (above cap) → no fire."""
        opp = self._opp(mu=68.0, floor=64.0, cap=66.0, mu_hrrr=67.0)
        self.assertIsNone(self.pb._check_hrrr_in_bracket_warm(opp))


class TestHrrrInBracketWarmGuards(unittest.TestCase):
    """Top-level guards."""

    def setUp(self):
        import paper_min_bot as pb
        self.pb = pb
        self._orig = pb.BUY_NO_HRRR_IN_BRACKET_WARM_ENABLED
        pb.BUY_NO_HRRR_IN_BRACKET_WARM_ENABLED = True

    def tearDown(self):
        self.pb.BUY_NO_HRRR_IN_BRACKET_WARM_ENABLED = self._orig

    def _opp(self, **overrides):
        base = {
            "action": "BUY_NO", "floor": 45.0, "cap": 46.0,
            "mu": 48.0, "mu_source": "nbp", "mu_hrrr": 45.4,
        }
        base.update(overrides)
        return base

    def test_disabled_returns_none(self):
        self.pb.BUY_NO_HRRR_IN_BRACKET_WARM_ENABLED = False
        self.assertIsNone(self.pb._check_hrrr_in_bracket_warm(self._opp()))

    def test_buy_yes_bypassed(self):
        self.assertIsNone(self.pb._check_hrrr_in_bracket_warm(
            self._opp(action="BUY_YES")))

    def test_t_bracket_bypassed(self):
        """T-bracket (no floor) → bypassed (B-only)."""
        self.assertIsNone(self.pb._check_hrrr_in_bracket_warm(self._opp(floor=None)))

    def test_missing_cap_bypassed(self):
        self.assertIsNone(self.pb._check_hrrr_in_bracket_warm(self._opp(cap=None)))

    def test_missing_mu_bypassed(self):
        self.assertIsNone(self.pb._check_hrrr_in_bracket_warm(self._opp(mu=None)))

    def test_missing_hrrr_bypassed(self):
        """No HRRR data → bypass (we can't make the call without nowcast)."""
        self.assertIsNone(self.pb._check_hrrr_in_bracket_warm(self._opp(mu_hrrr=None)))


if __name__ == "__main__":
    unittest.main()
