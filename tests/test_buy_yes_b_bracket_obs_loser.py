"""Tests for the BUY_YES + B-bracket "low locked above bracket" obs-loser
branch added 2026-05-02.

Triggered by PHIL-26MAY02-B49.5 BUY_YES this morning: bracket-math fix made
B-bracket BUY_YES viable (mp=62%, edge=+42%); rm=51.8 was 1.8°F above cap=50
with low likely already locked, but bot didn't block — `_check_obs_confirmed_loser`
for BUY_YES + B-bracket only checked `rm < floor`, missing the symmetric
`rm > cap` case.

The new branch fires when:
  rm > cap + 1.0  (CLI integer rounding + obs noise buffer)
  AND past local low-lock (heuristic: hour >= 6 local)
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

_TMPDIR = tempfile.mkdtemp(prefix="buy_yes_b_loser_test_")
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
    def test_branch_present(self):
        src = _read_source()
        # Must have the new "rm > cap" branch in the BUY_YES B-bracket section.
        # Find _check_obs_confirmed_loser and look for rm > cap pattern with past_low_lock
        fn_start = src.index("def _check_obs_confirmed_loser")
        fn_end = src.index("\ndef ", fn_start + 1)
        body = src[fn_start:fn_end]
        self.assertIn("_past_low_lock", body)
        self.assertIn("rm_f > float(cap) + 1.0", body)
        self.assertIn("hour >= 6", body)


class TestObsLoserBuyYesBBracket(unittest.TestCase):
    """Direct unit tests of _check_obs_confirmed_loser for BUY_YES B-bracket."""

    def _opp(self, **overrides):
        base = {
            "action": "BUY_YES",
            "floor": 49.0, "cap": 50.0,        # B-bracket [49, 50] (B49.5)
            "running_min": 51.8,                # PHIL pattern
            "tz": "America/New_York",
            "station": "KPHL",
        }
        base.update(overrides)
        return base

    def _at_hour(self, hour, tz="America/New_York"):
        """Return a datetime fixture for the given local hour today."""
        # Use a real today's date so ZoneInfo arithmetic is straightforward
        now_real = datetime.now(ZoneInfo(tz))
        return now_real.replace(hour=hour, minute=0, second=0, microsecond=0)

    def test_phil_pattern_blocks_at_645am(self):
        """PHIL exact pattern: rm=51.8, cap=50, 6:45 AM EDT → BLOCK."""
        with patch("paper_min_bot.datetime") as mock_dt:
            mock_dt.now.return_value = self._at_hour(6).replace(minute=45)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            self.assertTrue(pb._check_obs_confirmed_loser(self._opp()))

    def test_phil_pattern_blocks_at_717am(self):
        """PHIL second entry pattern."""
        with patch("paper_min_bot.datetime") as mock_dt:
            mock_dt.now.return_value = self._at_hour(7).replace(minute=17)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            self.assertTrue(pb._check_obs_confirmed_loser(self._opp()))

    def test_pre_low_lock_does_not_block(self):
        """rm > cap but pre-low-lock (3 AM) → don't block (low can still drop)."""
        with patch("paper_min_bot.datetime") as mock_dt:
            mock_dt.now.return_value = self._at_hour(3)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            self.assertFalse(pb._check_obs_confirmed_loser(self._opp()))

    def test_at_5am_does_not_block(self):
        """5 AM is right at typical low-formation window — don't block."""
        with patch("paper_min_bot.datetime") as mock_dt:
            mock_dt.now.return_value = self._at_hour(5)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            self.assertFalse(pb._check_obs_confirmed_loser(self._opp()))

    def test_at_6am_blocks(self):
        """6 AM is the cutoff — should block (low typically locked by then)."""
        with patch("paper_min_bot.datetime") as mock_dt:
            mock_dt.now.return_value = self._at_hour(6)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            self.assertTrue(pb._check_obs_confirmed_loser(self._opp()))

    def test_rm_just_above_cap_does_not_block(self):
        """rm = cap + 0.4 (< buffer): CLI could round to cap → in bracket → don't block."""
        with patch("paper_min_bot.datetime") as mock_dt:
            mock_dt.now.return_value = self._at_hour(7)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            opp = self._opp(running_min=50.4)  # cap=50, rm 0.4 above
            self.assertFalse(pb._check_obs_confirmed_loser(opp))

    def test_rm_at_buffer_boundary_does_not_block(self):
        """rm = cap + 1.0 exactly: NOT > cap+1 (strict greater-than). Don't block."""
        with patch("paper_min_bot.datetime") as mock_dt:
            mock_dt.now.return_value = self._at_hour(7)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            opp = self._opp(running_min=51.0)  # exactly cap+1
            self.assertFalse(pb._check_obs_confirmed_loser(opp))

    def test_rm_just_above_buffer_blocks(self):
        """rm = cap + 1.1: > cap+1 (strictly) and post-lock → block."""
        with patch("paper_min_bot.datetime") as mock_dt:
            mock_dt.now.return_value = self._at_hour(7)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            opp = self._opp(running_min=51.1)
            self.assertTrue(pb._check_obs_confirmed_loser(opp))

    def test_rm_below_floor_still_blocks_via_old_branch(self):
        """rm < floor was the original branch — must still fire regardless of hour."""
        with patch("paper_min_bot.datetime") as mock_dt:
            mock_dt.now.return_value = self._at_hour(3)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            opp = self._opp(running_min=47.0)  # below floor=49
            self.assertTrue(pb._check_obs_confirmed_loser(opp))

    def test_buy_no_b_bracket_unaffected_by_new_branch(self):
        """BUY_NO B-bracket logic is separate — rm above cap doesn't trigger here."""
        # BUY_NO B-bracket fires when rm IN bracket, not rm above cap.
        with patch("paper_min_bot.datetime") as mock_dt:
            mock_dt.now.return_value = self._at_hour(7)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            opp = self._opp(action="BUY_NO", running_min=51.8)
            # rm=51.8 > cap=50 (above bracket) → BUY_NO wins (NOT a loser).
            # The BUY_NO branch only fires if rm is IN bracket.
            self.assertFalse(pb._check_obs_confirmed_loser(opp))

    def test_buy_no_in_bracket_still_blocks(self):
        """BUY_NO B-bracket: rm IN bracket → blocks regardless."""
        with patch("paper_min_bot.datetime") as mock_dt:
            mock_dt.now.return_value = self._at_hour(7)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            opp = self._opp(action="BUY_NO", running_min=49.5)  # in [49, 50]
            self.assertTrue(pb._check_obs_confirmed_loser(opp))

    def test_t_low_buy_yes_unchanged(self):
        """BUY_YES T-low (existing branch) still fires the old way."""
        with patch("paper_min_bot.datetime") as mock_dt:
            mock_dt.now.return_value = self._at_hour(9)  # post _is_post_sunrise
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            opp = self._opp(floor=None, cap=63.5, running_min=66.0)
            self.assertTrue(pb._check_obs_confirmed_loser(opp))

    def test_t_high_buy_yes_unchanged(self):
        """BUY_YES T-high (existing branch) still fires the old way."""
        with patch("paper_min_bot.datetime") as mock_dt:
            mock_dt.now.return_value = self._at_hour(7)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            opp = self._opp(floor=71.5, cap=None, running_min=68.0)
            self.assertTrue(pb._check_obs_confirmed_loser(opp))

    def test_no_running_min_does_not_block(self):
        """rm=None (pre-obs) → can't decide, don't block."""
        with patch("paper_min_bot.datetime") as mock_dt:
            mock_dt.now.return_value = self._at_hour(7)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            opp = self._opp(running_min=None)
            self.assertFalse(pb._check_obs_confirmed_loser(opp))


if __name__ == "__main__":
    unittest.main()
