"""Tests for BUY_NO_TAIL_RISK gate shipped 2026-05-18 PM.

Gate blocks BUY_NO B-bracket when 0 < mu - cap < BUY_NO_TAIL_RISK_MU_GAP_F
(default 2.0): chosen mu sits barely above cap = coin-flip bracket geometry.

Backtest: stack-aware n=12 May 4-18, h:hu 7:5 (1.40x positive), lift +$87.76,
LOO-1 +$35.86. Catches today's PHIL/LV/PHX/MIN MTM losers.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestBuyNoTailRiskMuNearCap(unittest.TestCase):
    """0 < mu - cap < BUY_NO_TAIL_RISK_MU_GAP_F."""

    def setUp(self):
        import paper_min_bot as pb
        self.pb = pb
        self._orig_enabled = pb.BUY_NO_TAIL_RISK_ENABLED
        self._orig_mu_gap = pb.BUY_NO_TAIL_RISK_MU_GAP_F
        pb.BUY_NO_TAIL_RISK_ENABLED = True
        pb.BUY_NO_TAIL_RISK_MU_GAP_F = 2.0

    def tearDown(self):
        self.pb.BUY_NO_TAIL_RISK_ENABLED = self._orig_enabled
        self.pb.BUY_NO_TAIL_RISK_MU_GAP_F = self._orig_mu_gap

    def _opp(self, **overrides):
        # Base case: PHIL-MAY18 (mu=67, cap=66, gap=1.0)
        base = {
            "action": "BUY_NO", "floor": 65.0, "cap": 66.0,
            "mu": 67.0, "mu_source": "nbp",
            "mu_hrrr": 63.0, "mu_nbp": 67.0, "mu_nbm_om": 65.6,
            "is_today": False,
        }
        base.update(overrides)
        return base

    def test_fires_phil_pattern(self):
        """PHIL-MAY18: mu=67, cap=66, gap=1.0 → fires."""
        result = self.pb._check_buy_no_tail_risk(self._opp())
        self.assertIsNotNone(result)
        self.assertIn("BUY_NO_TAIL_RISK", result)
        self.assertIn("barely above cap", result)

    def test_fires_lv_pattern(self):
        """LV-MAY18: mu=59.9, cap=59, gap=0.9 → fires."""
        opp = self._opp(mu=59.9, cap=59.0, floor=58.0,
                        mu_source="nbm_d1_override", mu_hrrr=56.0)
        self.assertIsNotNone(self.pb._check_buy_no_tail_risk(opp))

    def test_fires_phx_pattern(self):
        """PHX-MAY18: mu=68.4, cap=68, gap=0.4 → fires."""
        opp = self._opp(mu=68.4, cap=68.0, floor=67.0,
                        mu_source="nbm_d1_override", mu_hrrr=64.4)
        self.assertIsNotNone(self.pb._check_buy_no_tail_risk(opp))

    def test_fires_min_pattern(self):
        """MIN-MAY18: mu=56.5, cap=55, gap=1.5 → fires."""
        opp = self._opp(mu=56.5, cap=55.0, floor=54.0,
                        mu_source="hrrr", mu_hrrr=56.5)
        self.assertIsNotNone(self.pb._check_buy_no_tail_risk(opp))

    def test_fires_sea_may11_historical(self):
        """SEA-MAY11-B50.5 -$51.90: mu=52, cap=51, gap=1.0 → fires."""
        opp = self._opp(mu=52.0, cap=51.0, floor=50.0,
                        mu_source="nbp", mu_hrrr=48.6)
        self.assertIsNotNone(self.pb._check_buy_no_tail_risk(opp))

    def test_fires_dal_may14_historical(self):
        """DAL-MAY14-B67.5 -$31.64: mu=69, cap=68, gap=1.0 → fires."""
        opp = self._opp(mu=69.0, cap=68.0, floor=67.0,
                        mu_source="nbp", mu_hrrr=67.8)
        self.assertIsNotNone(self.pb._check_buy_no_tail_risk(opp))

    def test_no_fire_chi_pattern_gap_too_big(self):
        """CHI-MAY18: mu=71.9, cap=69, gap=2.9 → does NOT fire (>= 2.0)."""
        opp = self._opp(mu=71.9, cap=69.0, floor=68.0,
                        mu_source="nbm_d1_override", mu_hrrr=67.7)
        self.assertIsNone(self.pb._check_buy_no_tail_risk(opp))

    def test_no_fire_gap_exactly_2(self):
        """Boundary: gap=2.0 → NOT fire (strict <)."""
        opp = self._opp(mu=68.0, cap=66.0)  # gap=2.0
        self.assertIsNone(self.pb._check_buy_no_tail_risk(opp))

    def test_no_fire_gap_negative_mu_below_cap(self):
        """mu below cap (gap<0) → does NOT fire (need 0 < gap).

        Legit cold BUY_NO setup: mu well below floor, bot expects cli below
        the bracket. This is not bracket-boundary risk."""
        opp = self._opp(mu=41.0, cap=46.0, floor=45.0)
        self.assertIsNone(self.pb._check_buy_no_tail_risk(opp))

    def test_no_fire_mu_well_below_floor(self):
        """mu=41, floor=45, cap=46 (gap=-5 cold) → passes.

        Matches the TestEvaluateGates clean-pass fixture."""
        opp = self._opp(mu=41.0, cap=46.0, floor=45.0, mu_hrrr=41.0,
                        mu_nbp=41.0, mu_nbm_om=41.0)
        self.assertIsNone(self.pb._check_buy_no_tail_risk(opp))

    def test_no_fire_gap_zero(self):
        """mu == cap (gap=0) → does NOT fire (strict <)."""
        opp = self._opp(mu=66.0, cap=66.0)
        self.assertIsNone(self.pb._check_buy_no_tail_risk(opp))

    def test_custom_threshold(self):
        """Custom MU_GAP_F=1.0: gap=1.5 → does NOT fire (>=1.0)."""
        self.pb.BUY_NO_TAIL_RISK_MU_GAP_F = 1.0
        opp = self._opp(mu=67.5, cap=66.0)
        self.assertIsNone(self.pb._check_buy_no_tail_risk(opp))
        # gap=0.5 → fires
        opp2 = self._opp(mu=66.5, cap=66.0)
        self.assertIsNotNone(self.pb._check_buy_no_tail_risk(opp2))


class TestBuyNoTailRiskGuards(unittest.TestCase):
    """Top-level guards: enabled, action, T-bracket bypass."""

    def setUp(self):
        import paper_min_bot as pb
        self.pb = pb
        self._orig_enabled = pb.BUY_NO_TAIL_RISK_ENABLED
        pb.BUY_NO_TAIL_RISK_ENABLED = True

    def tearDown(self):
        self.pb.BUY_NO_TAIL_RISK_ENABLED = self._orig_enabled

    def _opp(self, **overrides):
        base = {
            "action": "BUY_NO", "floor": 65.0, "cap": 66.0,
            "mu": 67.0, "mu_source": "nbp",
            "mu_hrrr": 63.0, "is_today": False,
        }
        base.update(overrides)
        return base

    def test_disabled_returns_none(self):
        self.pb.BUY_NO_TAIL_RISK_ENABLED = False
        self.assertIsNone(self.pb._check_buy_no_tail_risk(self._opp()))

    def test_buy_yes_bypassed(self):
        """BUY_YES not gated."""
        self.assertIsNone(self.pb._check_buy_no_tail_risk(
            self._opp(action="BUY_YES")))

    def test_t_bracket_bypassed_no_floor(self):
        """T-bracket (no floor) bypassed — B-only."""
        self.assertIsNone(self.pb._check_buy_no_tail_risk(self._opp(floor=None)))

    def test_missing_cap_bypassed(self):
        self.assertIsNone(self.pb._check_buy_no_tail_risk(self._opp(cap=None)))

    def test_missing_mu_bypassed(self):
        self.assertIsNone(self.pb._check_buy_no_tail_risk(self._opp(mu=None)))

    def test_d0_still_gated(self):
        """No is_today guard — d-0 still subject to this gate."""
        self.assertIsNotNone(self.pb._check_buy_no_tail_risk(
            self._opp(is_today=True)))


if __name__ == "__main__":
    unittest.main()
