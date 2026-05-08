"""Tests for the 2026-05-08 _get_metar_running_min LST-window fix.

Pre-fix: ±12h pad around UTC midnight → 48h window leaked yesterday's morning
cooling cycle into today's query. KHOU on 5/8: pulled in 5/7 14:53 UTC
(09:53 CDT) low of 64.94°F → cli_aligned_rmin returned 65 → false
OBS_CONFIRMED_ALIVE on B70.5 (real today rm was 73.4).

Fix: per-station LST climate-day window via Jan-15 (standard-time-only)
offset. CDT/CST stations: UTC-6 LST. PDT/PST: UTC-8. EST: UTC-5. MST: UTC-7.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

_TMPDIR = tempfile.mkdtemp(prefix="lst_window_test_")
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
        self.assertTrue(hasattr(pb, "_lst_climate_window_utc"))
        self.assertTrue(callable(pb._lst_climate_window_utc))

    def test_old_pad_removed_from_metar_rmin_function(self):
        s = _src()
        idx = s.index("def _get_metar_running_min(")
        end = s.find("\n\ndef ", idx)
        block = s[idx:end]
        self.assertNotIn("- 43200", block,
            "12h pad before midnight must be removed (was the leak)")
        self.assertNotIn("+ 43200", block,
            "12h pad after midnight must be removed")

    def test_metar_rmin_uses_lst_helper(self):
        s = _src()
        idx = s.index("def _get_metar_running_min(")
        end = s.find("\n\ndef ", idx)
        block = s[idx:end]
        self.assertIn("_lst_climate_window_utc(", block,
            "_get_metar_running_min must use the per-station LST helper")


class LSTWindowCorrectness(unittest.TestCase):
    """Verify the Jan-15 trick produces the right LST UTC bounds per tz."""

    def _expect_window(self, station, climate_date, expected_lst_utc_offset_h):
        """Window should span 24h, starting at climate_date 00:00 LST → UTC.

        For Eastern station on 2026-05-08:
          LST = EST = UTC-5, so 5/8 00:00 LST = 5/8 05:00 UTC
        For Central: 5/8 00:00 LST = 5/8 06:00 UTC
        etc.
        """
        from datetime import datetime, timezone, timedelta
        win = pb._lst_climate_window_utc(station, climate_date)
        self.assertIsNotNone(win, f"helper must return a window for {station}")
        start_ts, end_ts = win
        self.assertEqual(end_ts - start_ts, 24 * 3600,
            "window must be exactly 24h")
        expected_start = datetime.strptime(climate_date, "%Y-%m-%d") \
            .replace(tzinfo=timezone.utc) \
            + timedelta(hours=-expected_lst_utc_offset_h)
        self.assertEqual(start_ts, int(expected_start.timestamp()),
            f"{station} LST start should be UTC{expected_lst_utc_offset_h:+}h")

    def test_eastern_station(self):
        # KNYC = America/New_York → EST (UTC-5) Jan15
        self._expect_window("KNYC", "2026-05-08", -5)

    def test_central_station_houston(self):
        # KHOU = America/Chicago → CST (UTC-6) Jan15
        self._expect_window("KHOU", "2026-05-08", -6)

    def test_central_station_dallas(self):
        self._expect_window("KDFW", "2026-05-08", -6)

    def test_mountain_station(self):
        # KDEN = America/Denver → MST (UTC-7) Jan15
        self._expect_window("KDEN", "2026-05-08", -7)

    def test_arizona_no_dst(self):
        # KPHX = America/Phoenix → MST (UTC-7) year-round (no DST)
        self._expect_window("KPHX", "2026-05-08", -7)

    def test_pacific_station(self):
        # KSEA = America/Los_Angeles → PST (UTC-8) Jan15
        self._expect_window("KSEA", "2026-05-08", -8)

    def test_unknown_station_returns_none(self):
        self.assertIsNone(pb._lst_climate_window_utc("KZZZ", "2026-05-08"))

    def test_bad_date_returns_none(self):
        self.assertIsNone(pb._lst_climate_window_utc("KHOU", "not-a-date"))


class RegressionForHOUBug(unittest.TestCase):
    """Concrete: HOU on 2026-05-08, the original bug scenario.

    Pre-fix window (±12h pad): [2026-05-07T12:00 UTC, 2026-05-09T12:00 UTC] = 48h
    Post-fix window (CST=UTC-6): [2026-05-08T06:00 UTC, 2026-05-09T06:00 UTC] = 24h

    The 5/7 14:53 UTC observation (09:53 CDT 5/7 morning low) is OUTSIDE the
    post-fix window — it should not leak in.
    """

    def test_hou_window_excludes_yesterday_morning(self):
        from datetime import datetime, timezone
        win = pb._lst_climate_window_utc("KHOU", "2026-05-08")
        start_ts, end_ts = win
        # 5/7 14:53 UTC obs = the 64.94°F leak
        leak_ts = int(datetime(2026, 5, 7, 14, 53, tzinfo=timezone.utc).timestamp())
        self.assertLess(leak_ts, start_ts,
            "yesterday's 5/7 morning obs must be BEFORE today's LST window starts")

    def test_hou_window_includes_proper_today_data(self):
        from datetime import datetime, timezone
        win = pb._lst_climate_window_utc("KHOU", "2026-05-08")
        start_ts, end_ts = win
        # 5/8 06:00 UTC obs (today's 73.4°F madis reading)
        today_ts = int(datetime(2026, 5, 8, 6, 0, tzinfo=timezone.utc).timestamp())
        self.assertGreaterEqual(today_ts, start_ts)
        self.assertLess(today_ts, end_ts)


if __name__ == "__main__":
    unittest.main()
