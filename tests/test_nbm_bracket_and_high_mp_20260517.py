"""Tests for the 2 BUY_NO gates shipped 2026-05-17 PM:
  - BUY_NO_NBM_IN_BRACKET — d-1+ BUY_NO B-bracket when NBM-OM in upper 1°F of
    YES bracket AND HRRR<cap
  - BUY_NO_HIGH_MP_TRAP — BUY_NO when model_prob > 0.24

Driver: 6-day bleed $-172, recent fully-stopped BUY_NO catastrophes uncaught
by existing stack (HRRR_DISSENT/COLD_OUTLIER/HCDT/LBT_A)."""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestNbmInBracket(unittest.TestCase):
    """BUY_NO_NBM_IN_BRACKET — d-1+ BUY_NO B-bracket only."""

    def setUp(self):
        import paper_min_bot as pb
        self.pb = pb
        # Save originals
        self._orig_enabled = pb.BUY_NO_NBM_IN_BRACKET_ENABLED
        self._orig_margin = pb.BUY_NO_NBM_IN_BRACKET_MARGIN_F
        pb.BUY_NO_NBM_IN_BRACKET_ENABLED = True
        pb.BUY_NO_NBM_IN_BRACKET_MARGIN_F = 1.0

    def tearDown(self):
        self.pb.BUY_NO_NBM_IN_BRACKET_ENABLED = self._orig_enabled
        self.pb.BUY_NO_NBM_IN_BRACKET_MARGIN_F = self._orig_margin

    def _opp(self, **overrides):
        # Trigger case: PHIL-MAY18-B65.5 today (NBM=65.6, HRRR=63, cap=66)
        base = {
            "action": "BUY_NO", "floor": 65.0, "cap": 66.0,
            "mu_nbm_om": 65.6, "mu_hrrr": 63.0, "mu_nbp": 67.0,
            "is_today": False,
        }
        base.update(overrides)
        return base

    def test_fires_on_trigger_case_phil(self):
        """PHIL-MAY18 pattern: NBM in [65, 66] AND HRRR<66 → fire."""
        result = self.pb._check_nbm_in_bracket(self._opp())
        self.assertIsNotNone(result)
        self.assertIn("BUY_NO_NBM_IN_BRACKET", result)

    def test_fires_on_mia_may12(self):
        """MIA-MAY12-B77.5 historical (-$43.04): NBM=77.9 cap=78 HRRR=74."""
        opp = self._opp(floor=77.0, cap=78.0, mu_nbm_om=77.9,
                        mu_hrrr=74.0, mu_nbp=77.0)
        self.assertIsNotNone(self.pb._check_nbm_in_bracket(opp))

    def test_fires_on_min_may16(self):
        """MIN-MAY16-B57.5 historical (-$15.36): NBM=57.8 cap=58 HRRR=54.6."""
        opp = self._opp(floor=57.0, cap=58.0, mu_nbm_om=57.8,
                        mu_hrrr=54.6, mu_nbp=59.0)
        self.assertIsNotNone(self.pb._check_nbm_in_bracket(opp))

    def test_no_fire_when_nbm_above_cap(self):
        """NBM > cap → outside the upper-edge band."""
        opp = self._opp(mu_nbm_om=67.0)  # above cap=66
        self.assertIsNone(self.pb._check_nbm_in_bracket(opp))

    def test_no_fire_when_nbm_below_lower_band(self):
        """NBM = cap - 1.5 (below the 1°F band) → no fire."""
        opp = self._opp(mu_nbm_om=64.5)  # cap-1.5
        self.assertIsNone(self.pb._check_nbm_in_bracket(opp))

    def test_no_fire_when_hrrr_above_cap(self):
        """HRRR >= cap = no cold-direction confirmation."""
        opp = self._opp(mu_hrrr=66.5)
        self.assertIsNone(self.pb._check_nbm_in_bracket(opp))

    def test_no_fire_when_hrrr_equals_cap(self):
        """HRRR == cap → strict < cap required."""
        opp = self._opp(mu_hrrr=66.0)
        self.assertIsNone(self.pb._check_nbm_in_bracket(opp))

    def test_no_fire_on_buy_yes(self):
        opp = self._opp(action="BUY_YES")
        self.assertIsNone(self.pb._check_nbm_in_bracket(opp))

    def test_no_fire_on_d0(self):
        """is_today=True → d-1+ gate skipped (running_min handles d-0)."""
        opp = self._opp(is_today=True)
        self.assertIsNone(self.pb._check_nbm_in_bracket(opp))

    def test_no_fire_on_t_bracket(self):
        """T-bracket has only floor or only cap → not B-bracket."""
        opp_thigh = self._opp(floor=65.0, cap=None)
        opp_tlow = self._opp(floor=None, cap=66.0)
        self.assertIsNone(self.pb._check_nbm_in_bracket(opp_thigh))
        self.assertIsNone(self.pb._check_nbm_in_bracket(opp_tlow))

    def test_no_fire_when_disabled(self):
        self.pb.BUY_NO_NBM_IN_BRACKET_ENABLED = False
        self.assertIsNone(self.pb._check_nbm_in_bracket(self._opp()))

    def test_no_fire_when_nbm_missing(self):
        opp = self._opp(mu_nbm_om=None)
        self.assertIsNone(self.pb._check_nbm_in_bracket(opp))

    def test_no_fire_when_hrrr_missing(self):
        opp = self._opp(mu_hrrr=None)
        self.assertIsNone(self.pb._check_nbm_in_bracket(opp))

    def test_nbm_exactly_at_cap_fires(self):
        """Boundary: NBM == cap (right at upper edge) → fires."""
        opp = self._opp(mu_nbm_om=66.0)
        self.assertIsNotNone(self.pb._check_nbm_in_bracket(opp))

    def test_nbm_exactly_at_lower_band_fires(self):
        """Boundary: NBM == cap - margin → fires (inclusive lower bound)."""
        opp = self._opp(mu_nbm_om=65.0)  # cap-1.0 with margin=1.0
        self.assertIsNotNone(self.pb._check_nbm_in_bracket(opp))


class TestHighMpTrap(unittest.TestCase):
    """BUY_NO_HIGH_MP_TRAP — BUY_NO any bracket, all days_out."""

    def setUp(self):
        import paper_min_bot as pb
        self.pb = pb
        self._orig_enabled = pb.BUY_NO_HIGH_MP_TRAP_ENABLED
        self._orig_mp_max = pb.BUY_NO_HIGH_MP_TRAP_MP_MAX
        pb.BUY_NO_HIGH_MP_TRAP_ENABLED = True
        pb.BUY_NO_HIGH_MP_TRAP_MP_MAX = 0.24

    def tearDown(self):
        self.pb.BUY_NO_HIGH_MP_TRAP_ENABLED = self._orig_enabled
        self.pb.BUY_NO_HIGH_MP_TRAP_MP_MAX = self._orig_mp_max

    def _opp(self, mp=0.30, action="BUY_NO"):
        return {"action": action, "model_prob": mp}

    def test_fires_above_threshold(self):
        """mp=0.30 (SEA-MAY11 / MIA-MAY12 pattern) → fire."""
        result = self.pb._check_high_mp_trap(self._opp(mp=0.30))
        self.assertIsNotNone(result)
        self.assertIn("BUY_NO_HIGH_MP_TRAP", result)

    def test_fires_at_dc_may17(self):
        """DC-MAY17 today's pattern: mp=0.24+ (borderline)."""
        # Test with explicit mp > 0.24
        self.assertIsNotNone(self.pb._check_high_mp_trap(self._opp(mp=0.241)))

    def test_no_fire_at_threshold_exactly(self):
        """mp == 0.24 → strict > required, no fire."""
        self.assertIsNone(self.pb._check_high_mp_trap(self._opp(mp=0.24)))

    def test_no_fire_below_threshold(self):
        """mp=0.20 (today's LV/MIA pattern — NOT caught by this gate)."""
        self.assertIsNone(self.pb._check_high_mp_trap(self._opp(mp=0.20)))

    def test_no_fire_in_sweet_spot(self):
        """mp 15-20% (calibration sweet spot, 88% wr) → no fire."""
        self.assertIsNone(self.pb._check_high_mp_trap(self._opp(mp=0.17)))
        self.assertIsNone(self.pb._check_high_mp_trap(self._opp(mp=0.19)))

    def test_no_fire_on_buy_yes(self):
        """BUY_YES not affected — gate is BUY_NO-only."""
        self.assertIsNone(self.pb._check_high_mp_trap(self._opp(mp=0.30, action="BUY_YES")))

    def test_no_fire_when_mp_missing(self):
        self.assertIsNone(self.pb._check_high_mp_trap({"action": "BUY_NO"}))

    def test_no_fire_when_disabled(self):
        self.pb.BUY_NO_HIGH_MP_TRAP_ENABLED = False
        self.assertIsNone(self.pb._check_high_mp_trap(self._opp(mp=0.30)))

    def test_extreme_mp_fires(self):
        """mp=0.50 (coin flip) → definitely fires."""
        self.assertIsNotNone(self.pb._check_high_mp_trap(self._opp(mp=0.50)))


class TestMaxBetRollback(unittest.TestCase):
    """BUY_NO sizing $10->$80 on 5/26 then $80->$20 on 5/27 per Chris (FLAT_BET_NO_USD + MAX_BET_USD) after the 5/27 -$357 correlated-bust night.
    History: $50 → $25 (5/17 PM bleed-defense) → $50 (5/20 after 5/19 +$49 settle)."""

    def test_max_bet_is_20(self):
        import paper_min_bot as pb
        self.assertEqual(pb.MAX_BET_USD, 5.00)
        self.assertEqual(pb.FLAT_BET_NO_USD, 5.00)   # 2026-06-02: FLAT stake $20->$5 per Chris


if __name__ == "__main__":
    unittest.main()
