"""Regression test for 2026-05-12 COLD_SOURCE_OUTLIER gate.

Shipped after KXLOWTNYC-26MAY12-B46.5 (-$72.74 MTM) entered at:
  picked μ = 41.1 (HRRR)
  NBP=48.0, NBM-OM=47.0, HRRR=41.1 → median=47.0
  gap = 41.1 - 47.0 = -5.9°F < -4.0°F → BLOCK

Backtest (V1 min lifecycle pool n=105 incl 10 open-MTM since 2026-04-28):
  T=4°F: n=1 block (today's NYC), 1:0 helps:hurts, +$72.74 lift
  T=3°F: n=4 blocks, 1:3 h:h (kills winners — too aggressive)
  Symmetric |μ-med|>4 also kills DEN-26MAY12 warm-outlier winner (+$6.17).
  Asymmetric cold-only chosen.

Audit script: /tmp/v1_min_h20_outlier_backtest.py.
"""
import os, re, unittest, sys
from pathlib import Path

BOT_PATH = os.environ.get(
    "MIN_BOT_PATH", "/home/ubuntu/paper_min_bot/paper_min_bot.py")


def src():
    return Path(BOT_PATH).read_text()


class TestColdSourceOutlierConstants(unittest.TestCase):
    def test_constants_defined(self):
        s = src()
        self.assertIn("COLD_SOURCE_OUTLIER_ENABLED = True", s)
        self.assertIn("COLD_SOURCE_OUTLIER_F = 4.0", s)

    def test_helper_defined(self):
        sys.path.insert(0, "/home/ubuntu/paper_min_bot")
        if "paper_min_bot" in sys.modules:
            del sys.modules["paper_min_bot"]
        import paper_min_bot as m
        self.assertTrue(hasattr(m, "_check_cold_source_outlier"))
        self.assertTrue(callable(m._check_cold_source_outlier))

    def test_helper_wired_evaluate_gates(self):
        s = src()
        eg_idx = s.index("def _evaluate_gates(")
        eg_end = s.find("\n\ndef ", eg_idx)
        eg_block = s[eg_idx:eg_end]
        self.assertIn("_check_cold_source_outlier(opp)", eg_block)
        self.assertIn('"COLD_SOURCE_OUTLIER"', eg_block)

    def test_helper_wired_execute_opportunity(self):
        s = src()
        ex_idx = s.index("def execute_opportunity(")
        ex_end = s.find("\n\ndef ", ex_idx)
        ex_block = s[ex_idx:ex_end]
        self.assertIn("_check_cold_source_outlier(opp)", ex_block)
        self.assertIn('"COLD_SOURCE_OUTLIER"', ex_block)


class TestColdSourceOutlierHelper(unittest.TestCase):
    """Functional: helper returns block string for cold-outlier, None otherwise."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, "/home/ubuntu/paper_min_bot")
        if "paper_min_bot" in sys.modules:
            del sys.modules["paper_min_bot"]
        import paper_min_bot as m
        cls.m = m

    def test_blocks_nyc_pattern(self):
        # NYC-26MAY12-B46.5: HRRR=41.1, NBP=48, NBM-OM=47, picked=HRRR
        # gap = 41.1 - median(48,47,41.1) = 41.1 - 47.0 = -5.9 < -4.0 → BLOCK
        opp = dict(action="BUY_NO", mu=41.1,
                   mu_nbp=48.0, mu_nbm_om=47.0, mu_hrrr=41.1)
        r = self.m._check_cold_source_outlier(opp)
        self.assertIsNotNone(r)
        self.assertIn("COLD_SOURCE_OUTLIER", r)

    def test_passes_picked_equals_median(self):
        # Picked source is the median → gap=0 → no block
        opp = dict(action="BUY_NO", mu=47.0,
                   mu_nbp=48.0, mu_nbm_om=47.0, mu_hrrr=41.1)
        self.assertIsNone(self.m._check_cold_source_outlier(opp))

    def test_passes_warm_outlier(self):
        # Picked is WARMER than median by 5°F — asymmetric gate, should pass
        opp = dict(action="BUY_NO", mu=53.0,
                   mu_nbp=48.0, mu_nbm_om=47.0, mu_hrrr=41.1)
        self.assertIsNone(self.m._check_cold_source_outlier(opp))

    def test_boundary_at_minus_4_passes(self):
        # gap = -4.0 exactly: strict < check, so equal does NOT block
        opp = dict(action="BUY_NO", mu=44.0,
                   mu_nbp=48.0, mu_nbm_om=48.0, mu_hrrr=44.0)
        # median = median(48,48,44) = 48; gap = 44 - 48 = -4.0
        self.assertIsNone(self.m._check_cold_source_outlier(opp))

    def test_boundary_just_below_minus_4_blocks(self):
        opp = dict(action="BUY_NO", mu=43.9,
                   mu_nbp=48.0, mu_nbm_om=48.0, mu_hrrr=43.9)
        # gap = 43.9 - 48 = -4.1 < -4.0 → BLOCK
        self.assertIsNotNone(self.m._check_cold_source_outlier(opp))

    def test_passes_buy_yes(self):
        # Gate is BUY_NO only
        opp = dict(action="BUY_YES", mu=41.1,
                   mu_nbp=48.0, mu_nbm_om=47.0, mu_hrrr=41.1)
        self.assertIsNone(self.m._check_cold_source_outlier(opp))

    def test_passes_insufficient_sources(self):
        # Only 1 source available → cannot compute median → pass
        opp = dict(action="BUY_NO", mu=41.1, mu_hrrr=41.1)
        self.assertIsNone(self.m._check_cold_source_outlier(opp))

    def test_passes_no_mu(self):
        opp = dict(action="BUY_NO", mu=None,
                   mu_nbp=48.0, mu_nbm_om=47.0, mu_hrrr=41.1)
        self.assertIsNone(self.m._check_cold_source_outlier(opp))

    def test_disabled_flag_skips(self):
        # Save + flip flag, ensure helper returns None when disabled
        original = self.m.COLD_SOURCE_OUTLIER_ENABLED
        try:
            self.m.COLD_SOURCE_OUTLIER_ENABLED = False
            opp = dict(action="BUY_NO", mu=41.1,
                       mu_nbp=48.0, mu_nbm_om=47.0, mu_hrrr=41.1)
            self.assertIsNone(self.m._check_cold_source_outlier(opp))
        finally:
            self.m.COLD_SOURCE_OUTLIER_ENABLED = original


if __name__ == "__main__":
    unittest.main()
