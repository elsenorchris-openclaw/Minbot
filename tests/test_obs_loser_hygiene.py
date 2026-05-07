"""Tests for the 2026-05-07 OBS_CONFIRMED_LOSER cobs-vs-rm hygiene check.

When the bot's running_min is ABOVE the latest observed temp, rm is stale or
sourced differently from live obs (cli-aligned RM can lag raw METAR briefly).
The LOSER signal is unreliable in that state. Backtest n=20 BUY_NO/B first-
fires over 14 days: 1 false-positive caught (KLAX-26MAY06-B56.5, cobs=55.4
< rm=57.0), 0 correct-blocks lost.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

_TMPDIR = tempfile.mkdtemp(prefix="obs_loser_hygiene_")
os.environ["HOME"] = _TMPDIR
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


class WiringTests(unittest.TestCase):
    def test_helper_defined(self):
        self.assertTrue(hasattr(pb, "_get_current_temp_f"))
        self.assertTrue(callable(pb._get_current_temp_f))

    def test_helper_signature(self):
        import inspect
        sig = inspect.signature(pb._get_current_temp_f)
        self.assertIn("station", sig.parameters)
        # window_sec optional
        self.assertEqual(sig.parameters["window_sec"].default, 1800)

    def test_loser_calls_helper(self):
        s = _src()
        idx = s.index("def _check_obs_confirmed_loser(")
        end = s.find("\n\ndef ", idx + 1)
        block = s[idx:end]
        self.assertIn("_get_current_temp_f(", block,
            "_check_obs_confirmed_loser must call _get_current_temp_f")
        # Must be inside the BUY_NO branch
        buy_no_idx = block.index('action == "BUY_NO"')
        helper_idx = block.index("_get_current_temp_f(")
        self.assertGreater(helper_idx, buy_no_idx,
            "helper must be called inside the BUY_NO branch")

    def test_helper_has_no_op_short_circuit(self):
        # When station is None, helper returns None and the gate proceeds normally.
        self.assertIsNone(pb._get_current_temp_f(None))


class HygieneCheckBehavior(unittest.TestCase):
    """Mock the helper to simulate cobs vs rm relationships."""

    def _opp(self, **kw):
        d = {
            "running_min": 57.0,
            "floor": 56.0,
            "cap": 57.0,
            "action": "BUY_NO",
            "station": "KLAX",
            "date_str": "2026-05-06",
        }
        d.update(kw)
        return d

    def test_cobs_well_below_rm_skips_loser(self):
        """KLAX 5/6 archetype: cobs=55.4 < rm=57.0 → skip LOSER signal."""
        orig = pb._get_current_temp_f
        # Also stub _cli_aligned_rmin to a no-op (return raw rm)
        orig_align = pb._cli_aligned_rmin
        pb._get_current_temp_f = lambda *a, **kw: 55.4
        pb._cli_aligned_rmin = lambda rm, *a, **kw: rm
        try:
            self.assertFalse(pb._check_obs_confirmed_loser(self._opp()))
        finally:
            pb._get_current_temp_f = orig
            pb._cli_aligned_rmin = orig_align

    def test_cobs_at_rm_still_fires(self):
        """cobs ≈ rm: gate fires normally (rm matches obs, in bracket)."""
        orig = pb._get_current_temp_f
        orig_align = pb._cli_aligned_rmin
        pb._get_current_temp_f = lambda *a, **kw: 57.0
        pb._cli_aligned_rmin = lambda rm, *a, **kw: rm
        try:
            self.assertTrue(pb._check_obs_confirmed_loser(self._opp()))
        finally:
            pb._get_current_temp_f = orig
            pb._cli_aligned_rmin = orig_align

    def test_cobs_above_rm_still_fires(self):
        """cobs > rm: warming, cooling done, normal LOSER firing."""
        orig = pb._get_current_temp_f
        orig_align = pb._cli_aligned_rmin
        pb._get_current_temp_f = lambda *a, **kw: 60.0
        pb._cli_aligned_rmin = lambda rm, *a, **kw: rm
        try:
            self.assertTrue(pb._check_obs_confirmed_loser(self._opp()))
        finally:
            pb._get_current_temp_f = orig
            pb._cli_aligned_rmin = orig_align

    def test_cobs_none_falls_through(self):
        """No fresh obs → don't skip on missing data; gate fires normally."""
        orig = pb._get_current_temp_f
        orig_align = pb._cli_aligned_rmin
        pb._get_current_temp_f = lambda *a, **kw: None
        pb._cli_aligned_rmin = lambda rm, *a, **kw: rm
        try:
            self.assertTrue(pb._check_obs_confirmed_loser(self._opp()))
        finally:
            pb._get_current_temp_f = orig
            pb._cli_aligned_rmin = orig_align

    def test_buy_yes_unaffected(self):
        """Hygiene check is BUY_NO-only; BUY_YES path uses different logic."""
        orig = pb._get_current_temp_f
        orig_align = pb._cli_aligned_rmin
        pb._get_current_temp_f = lambda *a, **kw: 50.0  # would trigger if applied to BUY_YES
        pb._cli_aligned_rmin = lambda rm, *a, **kw: rm
        try:
            # BUY_YES: rm < floor → loses (existing logic)
            o = self._opp(action="BUY_YES", running_min=55.0, floor=57.0, cap=58.0)
            self.assertTrue(pb._check_obs_confirmed_loser(o))
        finally:
            pb._get_current_temp_f = orig
            pb._cli_aligned_rmin = orig_align

    def test_threshold_is_half_degree(self):
        """Hygiene fires only when cobs < rm - 0.5°F (margin)."""
        orig = pb._get_current_temp_f
        orig_align = pb._cli_aligned_rmin
        pb._cli_aligned_rmin = lambda rm, *a, **kw: rm
        try:
            # cobs = rm - 0.4 → still fires (within margin)
            pb._get_current_temp_f = lambda *a, **kw: 56.6
            self.assertTrue(pb._check_obs_confirmed_loser(self._opp()))
            # cobs = rm - 0.6 → skipped
            pb._get_current_temp_f = lambda *a, **kw: 56.4
            self.assertFalse(pb._check_obs_confirmed_loser(self._opp()))
        finally:
            pb._get_current_temp_f = orig
            pb._cli_aligned_rmin = orig_align


if __name__ == "__main__":
    unittest.main()
