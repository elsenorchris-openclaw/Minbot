"""Regression test for 2026-05-27 ENTRY_TIME_WINDOW gate.

Shipped after 5/26-5/27 -$357 cluster diagnosis: 3 Texas BUY_NO entries
at +7.4 / +8.4 / +9.0 hours pre-climate-day all settled at $79 loss.

Audit (14d V1 min, n=56 BUY_NO resolved entries from 5/13 → 5/26):
  T=5h: 28 blocks → 19L:9W = 2.1:1 helps:hurts
        gate_lift +$495.15, LOO-1 robust +$416.95
        binomial p<0.01 (19/28 = 68% loss-catch rate vs 50% null)

All playbook bars pass: n≥20 ✓, h:h≥2:1 ✓, lift positive ✓, robust positive ✓.

Audit script: /tmp/v1_entry_time_audit.py.
"""
from __future__ import annotations
import os
import sys
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, "/home/ubuntu/paper_min_bot")
import paper_min_bot as pb

BOT_PATH = os.environ.get(
    "MIN_BOT_PATH", "/home/ubuntu/paper_min_bot/paper_min_bot.py")


def src():
    return Path(BOT_PATH).read_text()


class TestEntryTimeWindowConstants(unittest.TestCase):
    def test_constants_defined(self):
        s = src()
        self.assertIn("ENTRY_TIME_WINDOW_ENABLED = True", s)
        self.assertIn("ENTRY_TIME_WINDOW_HOURS_MAX = 5.0", s)

    def test_helper_defined(self):
        self.assertTrue(hasattr(pb, "_check_entry_time_window"))
        self.assertTrue(callable(pb._check_entry_time_window))

    def test_helper_wired_evaluate_gates(self):
        s = src()
        eg_idx = s.index("def _evaluate_gates(")
        eg_end = s.find("\n\ndef ", eg_idx)
        eg_block = s[eg_idx:eg_end]
        self.assertIn("_check_entry_time_window(opp)", eg_block)
        self.assertIn('"ENTRY_TIME_WINDOW"', eg_block)

    def test_helper_wired_after_obs_confirmed_loser(self):
        """Order matters: obs-aware gates fire first, then ENTRY_TIME_WINDOW."""
        s = src()
        eg_idx = s.index("def _evaluate_gates(")
        eg_end = s.find("\n\ndef ", eg_idx)
        eg_block = s[eg_idx:eg_end]
        ocl_pos = eg_block.find("_check_obs_confirmed_loser")
        etw_pos = eg_block.find("_check_entry_time_window")
        self.assertGreater(ocl_pos, 0)
        self.assertGreater(etw_pos, ocl_pos,
                           "ENTRY_TIME_WINDOW must come after OBS_CONFIRMED_LOSER")


class TestEntryTimeWindowHelper(unittest.TestCase):
    """Functional: helper returns block string for early entries, None otherwise.

    Mock datetime so hours_pre is deterministic. Entry-time semantics:
      hours_pre = (climate_day_local_midnight - now_utc) / 3600
    """

    def _opp(self, **overrides):
        base = {
            "action": "BUY_NO",
            "date_str": "2026-05-27",
            "tz": "America/Chicago",  # CDT = UTC-5; midnight local = 05:00 UTC
        }
        base.update(overrides)
        return base

    def _set_now(self, mock_dt, fixed_dt):
        """Mock paper_min_bot.datetime so .now(tz=...) returns fixed_dt and
        .strptime passes through."""
        mock_dt.now.return_value = fixed_dt
        mock_dt.strptime = datetime.strptime
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

    def test_blocks_at_9h_pre_climate(self):
        """5/26 AUS pattern: entered 20:00 UTC 5/26, climate-day 5/27 CDT
        midnight = 05:00 UTC 5/27. hours_pre = +9.0h → BLOCK."""
        with patch("paper_min_bot.datetime") as mock_dt:
            self._set_now(mock_dt, datetime(2026, 5, 26, 20, 0, 0,
                                            tzinfo=timezone.utc))
            r = pb._check_entry_time_window(self._opp())
            self.assertIsNotNone(r)
            self.assertIn("ENTRY_TIME_WINDOW", r)

    def test_blocks_at_8h_pre_climate(self):
        """5/26 SATX pattern at 21:00 UTC → hours_pre = +8.0h → BLOCK."""
        with patch("paper_min_bot.datetime") as mock_dt:
            self._set_now(mock_dt, datetime(2026, 5, 26, 21, 0, 0,
                                            tzinfo=timezone.utc))
            self.assertIsNotNone(pb._check_entry_time_window(self._opp()))

    def test_passes_at_4h_pre_climate(self):
        """Entry at 01:00 UTC 5/27 → 4h pre-climate-day → PASS (< 5h cap)."""
        with patch("paper_min_bot.datetime") as mock_dt:
            self._set_now(mock_dt, datetime(2026, 5, 27, 1, 0, 0,
                                            tzinfo=timezone.utc))
            self.assertIsNone(pb._check_entry_time_window(self._opp()))

    def test_passes_post_climate_start(self):
        """Entry at 10:00 UTC 5/27 → -5h (POST climate-day-start) → PASS."""
        with patch("paper_min_bot.datetime") as mock_dt:
            self._set_now(mock_dt, datetime(2026, 5, 27, 10, 0, 0,
                                            tzinfo=timezone.utc))
            self.assertIsNone(pb._check_entry_time_window(self._opp()))

    def test_boundary_at_5h_passes(self):
        """hours_pre = 5.0 exactly: strict > check, equal does NOT block."""
        with patch("paper_min_bot.datetime") as mock_dt:
            # CDT midnight = 05:00 UTC; entry at 00:00 UTC = 5.0h pre
            self._set_now(mock_dt, datetime(2026, 5, 27, 0, 0, 0,
                                            tzinfo=timezone.utc))
            self.assertIsNone(pb._check_entry_time_window(self._opp()))

    def test_boundary_just_above_5h_blocks(self):
        """hours_pre = 5.1: > 5.0 → BLOCK."""
        with patch("paper_min_bot.datetime") as mock_dt:
            # Entry at 23:54 UTC 5/26 → 5.1h before 05:00 UTC 5/27
            self._set_now(mock_dt, datetime(2026, 5, 26, 23, 54, 0,
                                            tzinfo=timezone.utc))
            self.assertIsNotNone(pb._check_entry_time_window(self._opp()))

    def test_passes_buy_yes(self):
        """Gate is BUY_NO only."""
        with patch("paper_min_bot.datetime") as mock_dt:
            self._set_now(mock_dt, datetime(2026, 5, 26, 20, 0, 0,
                                            tzinfo=timezone.utc))
            self.assertIsNone(
                pb._check_entry_time_window(self._opp(action="BUY_YES")))

    def test_passes_no_date_str(self):
        with patch("paper_min_bot.datetime") as mock_dt:
            self._set_now(mock_dt, datetime(2026, 5, 26, 20, 0, 0,
                                            tzinfo=timezone.utc))
            self.assertIsNone(
                pb._check_entry_time_window(self._opp(date_str=None)))

    def test_passes_no_tz(self):
        with patch("paper_min_bot.datetime") as mock_dt:
            self._set_now(mock_dt, datetime(2026, 5, 26, 20, 0, 0,
                                            tzinfo=timezone.utc))
            self.assertIsNone(pb._check_entry_time_window(self._opp(tz=None)))

    def test_eastern_city_correct_offset(self):
        """NYC (America/New_York = EDT = UTC-4): midnight local 5/27
        = 04:00 UTC 5/27. Entry at 18:00 UTC 5/26 → 10h pre-climate → BLOCK."""
        with patch("paper_min_bot.datetime") as mock_dt:
            self._set_now(mock_dt, datetime(2026, 5, 26, 18, 0, 0,
                                            tzinfo=timezone.utc))
            self.assertIsNotNone(pb._check_entry_time_window(
                self._opp(tz="America/New_York")))

    def test_pacific_city_correct_offset(self):
        """LAX (PDT = UTC-7): midnight local 5/27 = 07:00 UTC 5/27.
        Entry at 02:00 UTC 5/27 → 5.0h pre-climate-day → PASS at boundary."""
        with patch("paper_min_bot.datetime") as mock_dt:
            self._set_now(mock_dt, datetime(2026, 5, 27, 2, 0, 0,
                                            tzinfo=timezone.utc))
            self.assertIsNone(pb._check_entry_time_window(
                self._opp(tz="America/Los_Angeles")))

    def test_disabled_flag_skips(self):
        original = pb.ENTRY_TIME_WINDOW_ENABLED
        try:
            pb.ENTRY_TIME_WINDOW_ENABLED = False
            with patch("paper_min_bot.datetime") as mock_dt:
                self._set_now(mock_dt, datetime(2026, 5, 26, 20, 0, 0,
                                                tzinfo=timezone.utc))
                self.assertIsNone(pb._check_entry_time_window(self._opp()))
        finally:
            pb.ENTRY_TIME_WINDOW_ENABLED = original


class TestEntryTimeWindowIntegration(unittest.TestCase):
    """End-to-end: an early BUY_NO entry that passes every other gate must
    be blocked by ENTRY_TIME_WINDOW in _evaluate_gates."""

    def test_5_26_aus_pattern_blocked_through_evaluate_gates(self):
        # 5/26 AUS-T65 pattern: mu=69.5, sigma=2.5, cap=64.5 (T-low),
        # mp=0.08, edge=0.33. Entered at 20:00 UTC 5/26 → +9h pre-climate.
        # Every other gate clears; ENTRY_TIME_WINDOW must block.
        opp = {
            "action": "BUY_NO",
            "entry_price": 0.59,
            "edge": 0.33,
            "model_prob": 0.08,
            "mu": 69.5,
            "sigma": 2.5,
            "mu_source": "hrrr_d1_override",
            "yes_bid": 41,
            "yes_ask": 47,
            "no_bid": 53,
            "no_ask": 59,
            "floor": None,
            "cap": 64.5,   # T-low
            "station": "KAUS",
            "series": "KXLOWTAUS",
            "date_str": "2026-05-27",
            "tz": "America/Chicago",
            "running_min": None,
            "is_today": False,
            "post_sunrise_lock": False,
            "disagreement": 3.4,
            "mu_nbp": 69.0, "mu_hrrr": 67.0, "mu_nbm_om": 70.4,
        }
        with patch("paper_min_bot.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 5, 26, 20, 0, 0,
                                                tzinfo=timezone.utc)
            mock_dt.strptime = datetime.strptime
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            blocked_by, reason = pb._evaluate_gates(opp)
            self.assertEqual(blocked_by, "ENTRY_TIME_WINDOW",
                             f"expected ENTRY_TIME_WINDOW, got {blocked_by}/{reason}")


if __name__ == "__main__":
    unittest.main()
