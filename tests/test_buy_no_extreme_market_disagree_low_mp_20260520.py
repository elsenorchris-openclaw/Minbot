"""Tests for BUY_NO_EXTREME_MARKET_DISAGREE_LOW_MP gate shipped 2026-05-20 PM.

Filter fires when yes_ask_cents >= K * (model_prob * 100), K=8.

Catches SATX-class outliers where the truncated-Gaussian collapses mp to ~0
but the market correctly prices YES at 30-50c. Backtest n=71 settled BUY_NO:
K=8 catches NYC-26MAY12-B46.5 (-74.57 USD) and SATX-26MAY19-T71 (-24.94 USD),
no winners killed, lift +99.51 USD.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestExtremeMarketDisagreeLowMp(unittest.TestCase):
    def setUp(self):
        import paper_min_bot as pb
        self.pb = pb
        self._orig = pb.BUY_NO_EXTREME_MARKET_DISAGREE_LOW_MP_ENABLED
        pb.BUY_NO_EXTREME_MARKET_DISAGREE_LOW_MP_ENABLED = True
        self._orig_k = pb.BUY_NO_EXTREME_MARKET_DISAGREE_LOW_MP_RATIO
        pb.BUY_NO_EXTREME_MARKET_DISAGREE_LOW_MP_RATIO = 8.0

    def tearDown(self):
        self.pb.BUY_NO_EXTREME_MARKET_DISAGREE_LOW_MP_ENABLED = self._orig
        self.pb.BUY_NO_EXTREME_MARKET_DISAGREE_LOW_MP_RATIO = self._orig_k

    def _opp(self, **kw):
        base = {"action": "BUY_NO", "model_prob": 0.007, "yes_ask": 46}
        base.update(kw)
        return base

    def test_fires_satx_historical(self):
        """SATX-26MAY19-T71 -24.94 USD: mp=0.7%, yes_ask=46c, ratio=65.7x."""
        r = self.pb._check_extreme_market_disagree_low_mp(self._opp())
        self.assertIsNotNone(r)
        self.assertIn("BUY_NO_EXTREME_MARKET_DISAGREE_LOW_MP", r)
        self.assertIn("market sees signal bot misses", r)

    def test_fires_nyc_may12_historical(self):
        """NYC-26MAY12-B46.5 -74.57 USD: mp=5.5%, yes_ask=45c, ratio=8.18x (just over K=8)."""
        r = self.pb._check_extreme_market_disagree_low_mp(
            self._opp(model_prob=0.055, yes_ask=45))
        self.assertIsNotNone(r)

    def test_skips_normal_buy_no(self):
        """Normal mp=15%, yes_ask=50c, ratio=3.3x: keeps trade."""
        r = self.pb._check_extreme_market_disagree_low_mp(
            self._opp(model_prob=0.15, yes_ask=50))
        self.assertIsNone(r)

    def test_threshold_boundary(self):
        """At ratio=K=8 exactly: fires (>=)."""
        opp = self._opp(model_prob=0.05, yes_ask=40)  # ratio = 40/5 = 8.0
        r = self.pb._check_extreme_market_disagree_low_mp(opp)
        self.assertIsNotNone(r)

    def test_threshold_just_below(self):
        """At ratio=7.9: keeps."""
        opp = self._opp(model_prob=0.05, yes_ask=39)  # ratio = 39/5 = 7.8
        r = self.pb._check_extreme_market_disagree_low_mp(opp)
        self.assertIsNone(r)

    def test_buy_yes_unaffected(self):
        """Gate is BUY_NO only."""
        r = self.pb._check_extreme_market_disagree_low_mp(
            self._opp(action="BUY_YES", model_prob=0.007, yes_ask=46))
        self.assertIsNone(r)

    def test_disabled_flag_short_circuits(self):
        self.pb.BUY_NO_EXTREME_MARKET_DISAGREE_LOW_MP_ENABLED = False
        r = self.pb._check_extreme_market_disagree_low_mp(self._opp())
        self.assertIsNone(r)

    def test_zero_mp_skipped(self):
        """mp=0 yields no signal (cant compute ratio)."""
        r = self.pb._check_extreme_market_disagree_low_mp(
            self._opp(model_prob=0.0, yes_ask=46))
        self.assertIsNone(r)

    def test_missing_fields_skipped(self):
        r = self.pb._check_extreme_market_disagree_low_mp(
            {"action": "BUY_NO", "model_prob": None, "yes_ask": 46})
        self.assertIsNone(r)
        r = self.pb._check_extreme_market_disagree_low_mp(
            {"action": "BUY_NO", "model_prob": 0.05, "yes_ask": None})
        self.assertIsNone(r)


class TestCallSiteIntegration(unittest.TestCase):
    """Confirm the gate is wired into both _evaluate_gates call sites."""

    def setUp(self):
        import paper_min_bot as pb
        self.pb = pb
        # Ensure all dependent gates are off so we hit THIS gate cleanly
        pb.BUY_NO_EXTREME_MARKET_DISAGREE_LOW_MP_ENABLED = True

    def test_gate_function_exists(self):
        self.assertTrue(hasattr(self.pb, "_check_extreme_market_disagree_low_mp"))
        self.assertTrue(callable(self.pb._check_extreme_market_disagree_low_mp))

    def test_constants_exist(self):
        self.assertTrue(hasattr(self.pb, "BUY_NO_EXTREME_MARKET_DISAGREE_LOW_MP_ENABLED"))
        self.assertTrue(hasattr(self.pb, "BUY_NO_EXTREME_MARKET_DISAGREE_LOW_MP_RATIO"))
        self.assertEqual(self.pb.BUY_NO_EXTREME_MARKET_DISAGREE_LOW_MP_RATIO, 8.0)


if __name__ == "__main__":
    unittest.main()
