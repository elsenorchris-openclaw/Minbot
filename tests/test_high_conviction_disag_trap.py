"""Regression test for 2026-05-12 eve HIGH_CONVICTION_DISAG_TRAP gate.

Shipped after audit of 14d V1 min BUY_NO pool (n=83 settled+live) found that
mp<0.075 AND disag>=2.0°F catches 3 entries, ALL LOSERS, W:L 0:3:
  NYC-MAY12-B46.5  mp=0.055  disag=6.9  pnl=-$74.57
  AUS-MAY12-T59    mp=0.074  disag=2.1  pnl=-$59.63
  ATL-MAY07-B59.5  mp=0.062  disag=4.0  pnl=-$29.93

Lift +$164.13, robust(-1) +$89.56, robust(-2) +$29.93. Stack-aware vs
COLD_SOURCE_OUTLIER: AUS + ATL are unique catches (+$89.56 unique value).
Natural data gaps at thresholds (Miami winner disag=1.6 vs AUS loser
disag=2.1; AUS loser mp=0.074 vs OKC winner mp=0.078).

Sub-bar by playbook n>=20 (n=3) but 4/5 bars pass with strong margins.
COLD_SOURCE_OUTLIER precedent (n=1 at ship time).
"""
import os, unittest, sys
from pathlib import Path

BOT_PATH = os.environ.get(
    "MIN_BOT_PATH", "/home/ubuntu/paper_min_bot/paper_min_bot.py")


def src():
    return Path(BOT_PATH).read_text()


class TestHighConvictionDisagTrapConstants(unittest.TestCase):
    def test_constants_defined(self):
        s = src()
        self.assertIn("HIGH_CONVICTION_DISAG_TRAP_ENABLED = True", s)
        self.assertIn("HIGH_CONVICTION_DISAG_TRAP_MP_MAX  = 0.075", s)
        self.assertIn("HIGH_CONVICTION_DISAG_TRAP_DISAG_F = 2.0", s)

    def test_helper_defined(self):
        sys.path.insert(0, "/home/ubuntu/paper_min_bot")
        if "paper_min_bot" in sys.modules:
            del sys.modules["paper_min_bot"]
        import paper_min_bot as m
        self.assertTrue(hasattr(m, "_check_high_conviction_disag_trap"))
        self.assertTrue(callable(m._check_high_conviction_disag_trap))

    def test_helper_wired_evaluate_gates(self):
        s = src()
        eg_idx = s.index("def _evaluate_gates(")
        eg_end = s.find("\n\ndef ", eg_idx)
        eg_block = s[eg_idx:eg_end]
        self.assertIn("_check_high_conviction_disag_trap(opp)", eg_block)
        self.assertIn('"HIGH_CONVICTION_DISAG_TRAP"', eg_block)

    def test_helper_wired_execute_opportunity(self):
        s = src()
        ex_idx = s.index("def execute_opportunity(")
        ex_end = s.find("\n\ndef ", ex_idx)
        ex_block = s[ex_idx:ex_end]
        self.assertIn("_check_high_conviction_disag_trap(opp)", ex_block)
        self.assertIn('"HIGH_CONVICTION_DISAG_TRAP"', ex_block)


class TestHighConvictionDisagTrapHelper(unittest.TestCase):
    """Functional: helper returns block string for trap, None otherwise."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, "/home/ubuntu/paper_min_bot")
        if "paper_min_bot" in sys.modules:
            del sys.modules["paper_min_bot"]
        import paper_min_bot as m
        cls.m = m

    def test_blocks_nyc_pattern(self):
        # NYC-26MAY12-B46.5: mp=0.055, disag=6.9 → BLOCK
        opp = dict(action="BUY_NO", model_prob=0.055, disagreement=6.9)
        r = self.m._check_high_conviction_disag_trap(opp)
        self.assertIsNotNone(r)
        self.assertIn("HIGH_CONVICTION_DISAG_TRAP", r)

    def test_blocks_aus_pattern(self):
        # AUS-26MAY12-T59: mp=0.074, disag=2.1 → BLOCK
        opp = dict(action="BUY_NO", model_prob=0.074, disagreement=2.1)
        r = self.m._check_high_conviction_disag_trap(opp)
        self.assertIsNotNone(r)

    def test_blocks_atl_pattern(self):
        # ATL-26MAY07-B59.5: mp=0.062, disag=4.0 → BLOCK
        opp = dict(action="BUY_NO", model_prob=0.062, disagreement=4.0)
        self.assertIsNotNone(
            self.m._check_high_conviction_disag_trap(opp))

    def test_passes_okc_winner_pattern(self):
        # OKC-26MAY08-B47.5: mp=0.078, disag=2.6 → mp ABOVE 0.075 → PASS
        opp = dict(action="BUY_NO", model_prob=0.078, disagreement=2.6)
        self.assertIsNone(
            self.m._check_high_conviction_disag_trap(opp))

    def test_passes_miami_winner_pattern(self):
        # Miami-MAY07: mp=0.055, disag=1.6 → disag BELOW 2.0 → PASS
        opp = dict(action="BUY_NO", model_prob=0.055, disagreement=1.6)
        self.assertIsNone(
            self.m._check_high_conviction_disag_trap(opp))

    def test_boundary_mp_at_max_passes(self):
        # mp = 0.075 exactly: strict < check, so equal does NOT block
        opp = dict(action="BUY_NO", model_prob=0.075, disagreement=5.0)
        self.assertIsNone(
            self.m._check_high_conviction_disag_trap(opp))

    def test_boundary_disag_at_min_blocks(self):
        # disag = 2.0 exactly: >= check, so equal DOES block
        opp = dict(action="BUY_NO", model_prob=0.05, disagreement=2.0)
        self.assertIsNotNone(
            self.m._check_high_conviction_disag_trap(opp))

    def test_passes_buy_yes(self):
        # Gate is BUY_NO only
        opp = dict(action="BUY_YES", model_prob=0.055, disagreement=6.9)
        self.assertIsNone(
            self.m._check_high_conviction_disag_trap(opp))

    def test_passes_no_mp(self):
        opp = dict(action="BUY_NO", model_prob=None, disagreement=6.9)
        self.assertIsNone(
            self.m._check_high_conviction_disag_trap(opp))

    def test_passes_no_disag(self):
        # Missing/None disag → treated as 0 → below threshold → PASS
        opp = dict(action="BUY_NO", model_prob=0.05, disagreement=None)
        self.assertIsNone(
            self.m._check_high_conviction_disag_trap(opp))

    def test_passes_high_mp(self):
        # DC-26MAY12 pattern: mp=0.193, disag=3.7 → mp too high → PASS
        # (DC is intentionally NOT caught by this gate per audit)
        opp = dict(action="BUY_NO", model_prob=0.193, disagreement=3.7)
        self.assertIsNone(
            self.m._check_high_conviction_disag_trap(opp))

    def test_disabled_flag_skips(self):
        original = self.m.HIGH_CONVICTION_DISAG_TRAP_ENABLED
        try:
            self.m.HIGH_CONVICTION_DISAG_TRAP_ENABLED = False
            opp = dict(action="BUY_NO", model_prob=0.055, disagreement=6.9)
            self.assertIsNone(
                self.m._check_high_conviction_disag_trap(opp))
        finally:
            self.m.HIGH_CONVICTION_DISAG_TRAP_ENABLED = original


if __name__ == "__main__":
    unittest.main()
