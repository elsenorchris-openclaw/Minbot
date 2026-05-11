"""Tests for SKIP_MODEL_MARKET_DISAGREE gate (2026-05-04 V2 port, both sides).

Backtest evidence (min_bot LIVE pool 2026-04-29 → 2026-05-02, n=24):
  - V2 defaults (mp_floor=0.22, gap_min=0.25): blocks 1, h:hu=1:0, lift=+$24
  - Catches DC-T46 BUY_YES -$24 — biggest live loss in current-era pool
  - V2 origin: BUY_NO blocker, +$223 lift / 15L:5W on n=253 (memory:
    project_v2_skip_model_market_disagree_20260502)

Bundled with COASTAL_TIGHT_FLOOR fix (commit 93b1570 wired the gate into
_evaluate_gates only; execute_opportunity was missing it — gate shadow-
logged but never blocked trades).

Tests:
  Source-level wiring (constants, helper, both call sites)
  Helper behavior (BUY_NO triggers, BUY_YES triggers, edge cases)
  Integration: DC-T46 BUY_YES candidate is blocked
  COASTAL_TIGHT_FLOOR: gate present in BOTH _evaluate_gates AND
    execute_opportunity (regression for the silent shadow-only bug)
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

_TMPDIR = tempfile.mkdtemp(prefix="mmd_test_")
os.environ["HOME"] = _TMPDIR
# Per memory feedback_min_bot_test_import_path: full test suite imports
# paper_min_bot from /tmp/paper_min_bot (not /home/ubuntu/paper_min_bot).
# Stay consistent so this test runs alongside the suite.
sys.path.insert(0, "/tmp/paper_min_bot")

import paper_min_bot as pb  # noqa: E402

pb.DATA_DIR = Path(_TMPDIR) / "data"
pb.LOG_DIR = Path(_TMPDIR) / "logs"
pb.DATA_DIR.mkdir(parents=True, exist_ok=True)
pb.LOG_DIR.mkdir(parents=True, exist_ok=True)
pb.POSITIONS_FILE = pb.DATA_DIR / "positions.json"
pb.TRADES_FILE = pb.DATA_DIR / "trades.jsonl"
pb.SETTLEMENTS_FILE = pb.DATA_DIR / "settlements.jsonl"

SRC_PATH = Path(os.environ.get(
    "PAPER_MIN_BOT_PATH", "/home/ubuntu/paper_min_bot/paper_min_bot.py"))


def _src() -> str:
    return SRC_PATH.read_text()


# ─────────────────────────────────────────────────────────────────────────────
# 1. Source-level wiring
# ─────────────────────────────────────────────────────────────────────────────
class TestSourceWiringMMD(unittest.TestCase):
    def test_constants_defined(self):
        s = _src()
        self.assertIn("SKIP_MODEL_MARKET_DISAGREE_MP_FLOOR = 0.22", s)
        self.assertIn("SKIP_MODEL_MARKET_DISAGREE_GAP_MIN = 0.20", s)

    def test_helper_defined(self):
        self.assertTrue(hasattr(pb, "_check_model_market_disagree"))
        self.assertTrue(callable(pb._check_model_market_disagree))

    def test_helper_called_from_evaluate_gates(self):
        s = _src()
        eg_idx = s.index("def _evaluate_gates(")
        eg_end = s.find("\n\ndef ", eg_idx)
        eg_block = s[eg_idx:eg_end]
        self.assertIn("_check_model_market_disagree(opp)", eg_block)
        self.assertIn('"MODEL_MARKET_DISAGREE"', eg_block)

    def test_helper_called_from_execute_opportunity(self):
        s = _src()
        ex_idx = s.index("def execute_opportunity(")
        ex_end = s.find("\n\ndef ", ex_idx)
        ex_block = s[ex_idx:ex_end]
        self.assertIn("_check_model_market_disagree(opp)", ex_block)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Helper behavior
# ─────────────────────────────────────────────────────────────────────────────
class TestMMDHelperBuyNo(unittest.TestCase):
    """BUY_NO: block when model says YES is non-trivial AND market overpays NO."""

    def test_blocks_classic_disagree(self):
        # mp=0.30, yes_bid=60c → gap=0.30 ≥ 0.25, mp ≥ 0.22 → block
        opp = dict(action="BUY_NO", model_prob=0.30, yes_bid=60, no_bid=40)
        self.assertIsNotNone(pb._check_model_market_disagree(opp))

    def test_passes_below_mp_floor(self):
        # mp=0.10 < 0.22 floor; bot's model strongly NO → trust model
        opp = dict(action="BUY_NO", model_prob=0.10, yes_bid=60, no_bid=40)
        self.assertIsNone(pb._check_model_market_disagree(opp))

    def test_passes_small_gap(self):
        # mp=0.30, yes_bid=49c → gap=0.19 < 0.20 (was 0.25 pre-2026-05-11)
        opp = dict(action="BUY_NO", model_prob=0.30, yes_bid=49, no_bid=51)
        self.assertIsNone(pb._check_model_market_disagree(opp))

    def test_blocks_at_new_0_20_boundary(self):
        # 2026-05-11: GAP_MIN tightened 0.25 → 0.20. KSEA-26MAY11-B50.5
        # entry pattern (mp=0.267, yes_bid=47, gap=0.203) must now block.
        opp = dict(action="BUY_NO", model_prob=0.267, yes_bid=47, no_bid=53)
        self.assertIsNotNone(pb._check_model_market_disagree(opp))

    def test_boundary_exact_thresholds_blocks(self):
        # mp=0.22 EXACTLY, yes_bid 48c gives gap 0.26 (≥ 0.25 with margin
        # to dodge float-precision rounding).
        opp = dict(action="BUY_NO", model_prob=0.22, yes_bid=48, no_bid=52)
        self.assertIsNotNone(pb._check_model_market_disagree(opp))

    def test_passes_when_yes_bid_missing(self):
        opp = dict(action="BUY_NO", model_prob=0.50, yes_bid=None, no_bid=50)
        self.assertIsNone(pb._check_model_market_disagree(opp))

    def test_passes_when_mp_missing(self):
        opp = dict(action="BUY_NO", model_prob=None, yes_bid=80, no_bid=20)
        self.assertIsNone(pb._check_model_market_disagree(opp))


class TestMMDHelperBuyYes(unittest.TestCase):
    """BUY_YES: block when model says NO is non-trivial AND market overpays NO.

    Mirror logic for daily-LOWS: if mp_no = 1−mp ≥ MP_FLOOR AND
    no_bid_frac − mp_no ≥ GAP_MIN → market knows something model doesn't.
    """

    def test_blocks_dc_t46_pattern(self):
        # DC-T46 actual: mp_yes=0.62, no_bid=76c
        # mp_no = 0.38 ≥ 0.22, gap = 0.76 − 0.38 = 0.38 ≥ 0.25 → BLOCK
        opp = dict(action="BUY_YES", model_prob=0.62, yes_bid=24, no_bid=76)
        self.assertIsNotNone(pb._check_model_market_disagree(opp))

    def test_passes_when_market_agrees_with_model(self):
        # mp_yes=0.70 → mp_no=0.30; no_bid=35c → gap 0.05 < 0.25
        opp = dict(action="BUY_YES", model_prob=0.70, yes_bid=65, no_bid=35)
        self.assertIsNone(pb._check_model_market_disagree(opp))

    def test_passes_below_mp_no_floor(self):
        # mp_yes=0.85 → mp_no=0.15 < 0.22 floor — trust strong model YES
        opp = dict(action="BUY_YES", model_prob=0.85, yes_bid=20, no_bid=80)
        self.assertIsNone(pb._check_model_market_disagree(opp))

    def test_passes_when_no_bid_missing(self):
        opp = dict(action="BUY_YES", model_prob=0.40, yes_bid=30, no_bid=None)
        self.assertIsNone(pb._check_model_market_disagree(opp))


class TestMMDHelperEdgeCases(unittest.TestCase):
    def test_passes_other_action(self):
        opp = dict(action=None, model_prob=0.40, yes_bid=70, no_bid=30)
        self.assertIsNone(pb._check_model_market_disagree(opp))

    def test_returns_string_when_blocking(self):
        opp = dict(action="BUY_NO", model_prob=0.50, yes_bid=80, no_bid=20)
        r = pb._check_model_market_disagree(opp)
        self.assertIsInstance(r, str)
        self.assertIn("MODEL_MARKET_DISAGREE", r)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Integration: DC-T46 candidate replay
# ─────────────────────────────────────────────────────────────────────────────
class TestDCT46Integration(unittest.TestCase):
    """Replay the DC-T46 BUY_YES candidate via _evaluate_gates and confirm
    MODEL_MARKET_DISAGREE blocks the trade."""

    def test_dc_t46_blocked(self):
        # Reconstructed from joined settlement+candidate record (5/01 entry)
        opp = {
            "action": "BUY_YES",
            "entry_price": 0.24,
            "edge": 0.30,            # plenty of edge — gate must still fire
            "model_prob": 0.62,
            "mu": 47.0,
            "sigma": 2.0,
            "mu_source": "nbp",
            "yes_bid": 24,
            "yes_ask": 30,
            "no_bid": 76,
            "no_ask": 82,
            "floor": 46.5,
            "cap": None,
            "station": "KDCA",
            "series": "KXLOWTDC",
            "date_str": "2026-05-01",
            "running_min": None,
            "is_today": True,
            "post_sunrise_lock": False,
            "disagreement": 0.5,
        }
        blocked_by, reason = pb._evaluate_gates(opp)
        # Multiple gates correctly block DC-T46. Production gate order:
        # DIRECTIONAL_BUY_YES (mp=0.62 < 0.65 since 2026-05-04 commit 3aed75c) →
        # YES_TAIL_MARGIN (μ-floor=+0.5°F < 1.0) → MODEL_MARKET_DISAGREE.
        # Test only requires the trade to be blocked by SOME defensible gate.
        self.assertIsNotNone(blocked_by, f"DC-T46 must be blocked, got: {blocked_by}/{reason}")
        self.assertIn(blocked_by, ("DIRECTIONAL_BUY_YES", "YES_TAIL_MARGIN", "MODEL_MARKET_DISAGREE"))

    def test_mmd_blocks_buy_yes_b_bracket_clean_setup(self):
        """B-bracket BUY_YES setup that bypasses YES_TAIL_MARGIN (only fires on
        tails) and other gates, leaving MMD as the blocker. Catches future
        regressions where reordering moves MMD after another always-firing
        gate that masks it."""
        opp = {
            "action": "BUY_YES",
            "entry_price": 0.24,
            "edge": 0.30,
            "model_prob": 0.65,         # mp_no = 0.35 ≥ 0.22 ✓
            "mu": 50.0,                  # comfortably inside [49, 51] B-bracket
            "sigma": 2.0,
            "mu_source": "nbp",
            "yes_bid": 24,
            "yes_ask": 30,
            "no_bid": 76,                # gap = 0.76 - 0.35 = 0.41 ≥ 0.25 ✓
            "no_ask": 82,
            "floor": 49.0,
            "cap": 51.0,                 # B-bracket
            "station": "KATL",
            "series": "KXLOWTATL",
            "date_str": "2026-05-04",
            "running_min": None,
            "is_today": True,
            "post_sunrise_lock": False,
            "disagreement": 0.3,
            "mu_nbp": 50.0,
            "mu_hrrr": 50.0,
            "mu_nbm_om": 50.0,
        }
        blocked_by, reason = pb._evaluate_gates(opp)
        self.assertEqual(blocked_by, "MODEL_MARKET_DISAGREE",
                         f"expected MODEL_MARKET_DISAGREE, got {blocked_by}/{reason}")


# ─────────────────────────────────────────────────────────────────────────────
# 4. COASTAL_TIGHT_FLOOR — both call sites
# ─────────────────────────────────────────────────────────────────────────────
class TestCoastalTightFloorBothCallSites(unittest.TestCase):
    """Regression for the silent shadow-only bug: commit 93b1570 wired
    COASTAL_TIGHT_FLOOR into _evaluate_gates ONLY (the candidate-log path),
    NOT execute_opportunity (the actual decision path). Same class as the
    May 1 addon-path bug. This test ensures both paths block the same trade.

    2026-05-06: gate disabled by default (_COASTAL_TIGHT_FLOOR_ENABLED=False).
    Tests force-enable in setUp to keep validating the wiring for re-enable.
    """

    def setUp(self):
        self._orig_ctf_enabled = pb._COASTAL_TIGHT_FLOOR_ENABLED
        pb._COASTAL_TIGHT_FLOOR_ENABLED = True

    def tearDown(self):
        pb._COASTAL_TIGHT_FLOOR_ENABLED = self._orig_ctf_enabled

    def test_constant_present(self):
        self.assertTrue(hasattr(pb, "COASTAL_TIGHT_FLOOR_STATIONS"))
        self.assertIn("KLAX", pb.COASTAL_TIGHT_FLOOR_STATIONS)
        self.assertIn("KSFO", pb.COASTAL_TIGHT_FLOOR_STATIONS)
        self.assertIn("KMIA", pb.COASTAL_TIGHT_FLOOR_STATIONS)
        self.assertIn("KHOU", pb.COASTAL_TIGHT_FLOOR_STATIONS)

    def test_in_evaluate_gates(self):
        s = _src()
        eg_idx = s.index("def _evaluate_gates(")
        eg_end = s.find("\n\ndef ", eg_idx)
        self.assertIn("COASTAL_TIGHT_FLOOR_STATIONS", s[eg_idx:eg_end])
        self.assertIn('"COASTAL_TIGHT_FLOOR"', s[eg_idx:eg_end])

    def test_in_execute_opportunity(self):
        """The bug we are fixing — COASTAL_TIGHT_FLOOR must appear inside
        execute_opportunity, not just _evaluate_gates."""
        s = _src()
        ex_idx = s.index("def execute_opportunity(")
        ex_end = s.find("\n\ndef ", ex_idx)
        ex_block = s[ex_idx:ex_end]
        self.assertIn("COASTAL_TIGHT_FLOOR_STATIONS", ex_block,
            "COASTAL_TIGHT_FLOOR not wired into execute_opportunity — silent shadow-only bug")
        self.assertIn("COASTAL_TIGHT_FLOOR_MIN_GAP_F", ex_block)

    def test_evaluate_gates_blocks_lax_tight_gap(self):
        """LAX BUY_NO B-bracket with floor-mu gap < 2.1°F → block."""
        opp = {
            "action": "BUY_NO",
            "entry_price": 0.50,
            "edge": 0.25,
            "model_prob": 0.18,     # inside [MIN_MODEL_PROB, MAX_MODEL_PROB]
            "mu": 56.0,             # gap = 56.5 - 56.0 = 0.5°F < 2.1°F → block
            "sigma": 2.0,
            "mu_source": "nbp",
            "yes_bid": 50,
            "yes_ask": 55,
            "no_bid": 45,
            "no_ask": 50,
            "floor": 56.5,
            "cap": 57.5,
            "station": "KLAX",
            "series": "KXLOWTLAX",
            "date_str": "2026-05-04",
            "running_min": None,
            "is_today": True,
            "post_sunrise_lock": False,
            "disagreement": 0.3,
            "mu_nbp": 56.0,
            "mu_hrrr": 56.0,
            "mu_nbm_om": 56.0,
        }
        blocked_by, _ = pb._evaluate_gates(opp)
        self.assertEqual(blocked_by, "COASTAL_TIGHT_FLOOR")

    def test_evaluate_gates_passes_lax_wide_gap(self):
        """LAX with mu well below floor → COASTAL_TIGHT_FLOOR doesn't fire."""
        opp = {
            "action": "BUY_NO",
            "entry_price": 0.50,
            "edge": 0.25,
            "model_prob": 0.10,
            "mu": 53.0,             # gap = 56.5 - 53.0 = 3.5°F ≥ 2.1°F → pass
            "sigma": 2.0,
            "mu_source": "nbp",
            "yes_bid": 50,
            "yes_ask": 55,
            "no_bid": 45,
            "no_ask": 50,
            "floor": 56.5,
            "cap": 57.5,
            "station": "KLAX",
            "series": "KXLOWTLAX",
            "date_str": "2026-05-04",
            "running_min": None,
            "is_today": True,
            "post_sunrise_lock": False,
            "disagreement": 0.3,
            "mu_nbp": 53.0,
            "mu_hrrr": 53.0,
            "mu_nbm_om": 53.0,
        }
        blocked_by, _ = pb._evaluate_gates(opp)
        # Should not be blocked by COASTAL_TIGHT_FLOOR (may pass entirely or
        # be blocked by another gate downstream — what we are asserting is
        # that the COASTAL gate is NOT the blocker here).
        self.assertNotEqual(blocked_by, "COASTAL_TIGHT_FLOOR")


# ─────────────────────────────────────────────────────────────────────────────
# 5. Gate ordering — MMD fires AFTER F2A / COASTAL but BEFORE MSG
# ─────────────────────────────────────────────────────────────────────────────
class TestGateOrdering(unittest.TestCase):
    """Ordering matters in _evaluate_gates: MMD added between
    COASTAL_TIGHT_FLOOR and MSG. If a future refactor reorders gates,
    surface the change."""

    def test_evaluate_gates_ordering_block(self):
        s = _src()
        eg_idx = s.index("def _evaluate_gates(")
        eg_end = s.find("\n\ndef ", eg_idx)
        eg = s[eg_idx:eg_end]
        ctf_pos = eg.index("COASTAL_TIGHT_FLOOR_STATIONS")
        mmd_pos = eg.index("_check_model_market_disagree")
        msg_pos = eg.index("_check_msg_gate")
        self.assertLess(ctf_pos, mmd_pos, "MMD must be after COASTAL_TIGHT_FLOOR")
        self.assertLess(mmd_pos, msg_pos, "MMD must be before MSG")


if __name__ == "__main__":
    unittest.main(verbosity=2)
