"""Tests for the 2026-05-02 BRACKET MATH FIX in calc_bracket_probability_min.

Port of the V1/V2 2026-04-22 fix (which was Brier-validated 0.250→0.228 on
n=398 max-temp brackets). For B-brackets the integration range widens by ±0.5
to match Kalshi's integer-CLI settlement. T-tails are unaffected here because
parse_market_bracket already pre-buffers them.

Why it matters: at boundary forecasts (μ within 1°F of bracket edge) the
old formula systematically understated mp by ~10pp, which let bypass-eligible
trades through the directional gate at MAX_BET. Five historical losses share
this signature — see Section "5 historical bypass losses" below.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

_TMPDIR = tempfile.mkdtemp(prefix="bracket_math_test_")
os.environ["HOME"] = _TMPDIR
sys.path.insert(0, "/home/ubuntu/paper_min_bot")

import paper_min_bot as pb
pb.DATA_DIR = Path(_TMPDIR) / "data"
pb.LOG_DIR = Path(_TMPDIR) / "logs"
pb.DATA_DIR.mkdir(parents=True, exist_ok=True)
pb.LOG_DIR.mkdir(parents=True, exist_ok=True)
pb.POSITIONS_FILE = pb.DATA_DIR / "positions.json"
pb.TRADES_FILE = pb.DATA_DIR / "trades.jsonl"
pb.SETTLEMENTS_FILE = pb.DATA_DIR / "settlements.jsonl"


def _read_source():
    return Path("/home/ubuntu/paper_min_bot/paper_min_bot.py").read_text()


class TestSourceWiring(unittest.TestCase):
    def test_fix_comment_present(self):
        src = _read_source()
        self.assertIn("2026-05-02 BRACKET MATH FIX", src)
        self.assertIn("Brier", src)  # cite the V1/V2 backtest evidence

    def test_buffer_applied_to_b_brackets_only(self):
        src = _read_source()
        # Helper variables introduced
        self.assertIn("floor_eff", src)
        self.assertIn("cap_eff", src)
        self.assertIn("is_b_bracket", src)
        # Original `floor` usage in CDF lines must be gone (replaced by floor_eff)
        # The function body should reference floor_eff/cap_eff in the CDF calls
        fn_start = src.index("def calc_bracket_probability_min")
        fn_end = src.index("\ndef ", fn_start + 1)
        body = src[fn_start:fn_end]
        # Each CDF call uses _eff variants, not raw floor/cap
        self.assertGreaterEqual(body.count("floor_eff"), 4)
        self.assertGreaterEqual(body.count("cap_eff"),   4)


class TestBBracketBuffer(unittest.TestCase):
    """B-bracket: P(continuous in [floor-0.5, cap+0.5]) — half-bracket integration."""

    def test_lax_pattern(self):
        # μ=57, σ=3, bracket [58,59]: pre-fix mp ≈ 12%, post-fix ≈ 23%
        p = pb.calc_bracket_probability_min(mu=57.0, sigma=3.0, floor=58.0, cap=59.0)
        self.assertAlmostEqual(p, 0.232, places=2)

    def test_sfo_pattern(self):
        # μ=53, σ=2.4, bracket [54,55]: pre-fix mp ≈ 14%, post-fix ≈ 27%
        p = pb.calc_bracket_probability_min(mu=53.0, sigma=2.4, floor=54.0, cap=55.0)
        self.assertAlmostEqual(p, 0.268, places=2)

    def test_atl_pattern(self):
        # μ=61.3, σ=2.5, bracket [63,64]: pre-fix ~11%, post-fix ~22%
        p = pb.calc_bracket_probability_min(mu=61.3, sigma=2.5, floor=63.0, cap=64.0)
        self.assertGreater(p, 0.20)
        self.assertLess(p, 0.25)

    def test_nyc_pattern(self):
        # μ=50, σ=2.5, bracket [51,52]: pre-fix ~13%, post-fix ~26%
        p = pb.calc_bracket_probability_min(mu=50.0, sigma=2.5, floor=51.0, cap=52.0)
        self.assertGreater(p, 0.24)
        self.assertLess(p, 0.28)

    def test_den_apr30_pattern(self):
        # μ=37.6, σ=2.5, bracket [38,39]: pre-fix ~10%, post-fix ~29%
        p = pb.calc_bracket_probability_min(mu=37.6, sigma=2.5, floor=38.0, cap=39.0)
        self.assertGreater(p, 0.27)
        self.assertLess(p, 0.31)

    def test_mu_at_floor_post_fix(self):
        # μ exactly at floor=58, σ=2, br [58,59]: pre-fix mp = 19% (CDF(59)-CDF(58)).
        # Post-fix: P(continuous in [57.5, 59.5]) = CDF(0.75)-CDF(-0.25) = 0.372.
        p = pb.calc_bracket_probability_min(mu=58.0, sigma=2.0, floor=58.0, cap=59.0)
        self.assertGreater(p, 0.36)
        self.assertLess(p, 0.39)

    def test_deep_outside_minimal_change(self):
        # μ way outside bracket — buffer matters less. mp stays tiny either way.
        p = pb.calc_bracket_probability_min(mu=50.0, sigma=2.0, floor=60.0, cap=61.0)
        self.assertLess(p, 0.005)

    def test_buffer_adds_mass_not_removes(self):
        """For ANY B-bracket, post-fix mp >= pre-fix mp (we're integrating over
        a wider range). Verify monotonicity across many parameter combinations."""
        from math import erf, sqrt
        def cdf(x, mu, s): return 0.5 * (1 + erf((x - mu) / (s * sqrt(2))))
        for mu in (50.0, 55.0, 60.0, 65.0, 70.0):
            for sigma in (1.5, 2.5, 3.5):
                for fl, cp in ((54.0, 55.0), (60.0, 61.0), (65.0, 66.0)):
                    legacy = max(0.0, min(1.0, cdf(cp, mu, sigma) - cdf(fl, mu, sigma)))
                    new = pb.calc_bracket_probability_min(mu=mu, sigma=sigma,
                                                           floor=fl, cap=cp)
                    self.assertGreaterEqual(new + 1e-9, legacy,
                        f"new mp must be >= legacy at μ={mu} σ={sigma} br=[{fl},{cp}]")


class TestTBracketsUnchanged(unittest.TestCase):
    """T-tails: parse_market_bracket already returns half-integer bounds.
    The B-only fix MUST NOT double-buffer them."""

    def test_t_high_unchanged(self):
        # T71 in min markets: parser returns floor=71.5, cap=None.
        # P(low >= 72) should be ~ P(continuous >= 71.5).
        # μ=70, σ=2: pre-fix and post-fix should match.
        p = pb.calc_bracket_probability_min(mu=70.0, sigma=2.0,
                                             floor=71.5, cap=None)
        # Sanity: this is 1 - cdf(71.5; 70, 2) ≈ 1 - 0.773 = 0.227
        self.assertAlmostEqual(p, 0.227, places=2)

    def test_t_low_unchanged(self):
        # T64 in min markets: parser returns floor=None, cap=63.5.
        # P(low <= 63) should be ~ P(continuous <= 63.5).
        # μ=65, σ=2: cdf(63.5; 65, 2) ≈ 0.227.
        p = pb.calc_bracket_probability_min(mu=65.0, sigma=2.0,
                                             floor=None, cap=63.5)
        self.assertAlmostEqual(p, 0.227, places=2)

    def test_t_high_buffer_not_doubled(self):
        """If we accidentally apply ±0.5 to T-tails, mp would shift by ~5-10pp.
        Verify it's stable on a known case."""
        # μ=70, σ=2, floor=71.5 (T71 from parser)
        # CORRECT: 1 - cdf(71.5) ≈ 0.227
        # BUG (double-buffer): 1 - cdf(71.5 + 0.5 = 72.0) ≈ 0.159 — off by ~7pp
        p = pb.calc_bracket_probability_min(mu=70.0, sigma=2.0,
                                             floor=71.5, cap=None)
        self.assertGreater(p, 0.20)  # would fail at 0.159 if double-buffered


class TestLegacyBehaviorPreserved(unittest.TestCase):
    """Cases where the fix MUST NOT change the answer materially."""

    def test_running_min_below_bracket_returns_zero(self):
        """If rm forces low BELOW bracket entirely, prob is 0 regardless."""
        p = pb.calc_bracket_probability_min(mu=60.0, sigma=2.0, floor=70.0, cap=71.0,
                                             running_min=55.0)
        self.assertEqual(p, 0.0)

    def test_running_min_truncation_works(self):
        # rm=58 with bracket [60, 61]: floor_eff=59.5, cap_eff=61.5.
        # rm_buffered = 59.0. floor_eff (59.5) > rm_buffered (59.0) → 0.0
        p = pb.calc_bracket_probability_min(mu=60.0, sigma=2.0, floor=60.0, cap=61.0,
                                             running_min=58.0)
        self.assertEqual(p, 0.0)

    def test_high_sigma_sanity(self):
        # Wide sigma → smoother distribution. Buffer adds mass but bounded.
        # Without buffer: P(continuous in [55, 56]) ≈ 0.034
        # With buffer:    P(continuous in [54.5, 56.5]) ≈ 0.069
        p = pb.calc_bracket_probability_min(mu=50.0, sigma=10.0, floor=55.0, cap=56.0)
        self.assertGreater(p, 0.06)
        self.assertLess(p, 0.08)


class TestRegressionGuards(unittest.TestCase):
    """Catch silent breakage of the function on existing call sites."""

    def test_returns_float_in_unit_range(self):
        for mu in (40.0, 50.0, 60.0, 70.0, 80.0):
            for sigma in (1.0, 2.0, 3.0, 5.0):
                for fl, cp in ((50.0, 51.0), (60.0, 61.0), (None, 55.5),
                                (65.5, None)):
                    p = pb.calc_bracket_probability_min(mu=mu, sigma=sigma,
                                                         floor=fl, cap=cp)
                    self.assertIsInstance(p, float)
                    self.assertGreaterEqual(p, 0.0)
                    self.assertLessEqual(p, 1.0)

    def test_zero_sigma_handled(self):
        # sigma <= 0 falls through to sigma = 0.5 (existing behavior).
        p = pb.calc_bracket_probability_min(mu=58.0, sigma=0.0, floor=58.0, cap=59.0)
        self.assertGreater(p, 0.5)  # μ at floor with tiny σ → most mass in bracket


if __name__ == "__main__":
    unittest.main()
