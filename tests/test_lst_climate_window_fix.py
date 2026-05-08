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


class MetarRminQueryRegression(unittest.TestCase):
    """End-to-end regression: `_get_metar_running_min` must NEVER return a
    value derived from observations timestamped outside the LST climate-day
    window. Catches reintroductions of the ±12h-pad bug (or any future widen).

    Setup: writes a temp obs.sqlite with two awc rows for KHOU 2026-05-08:
      - LEAK row at 5/7 09:53 CDT (= 5/7 14:53 UTC) with temp_f=55.0  ← BEFORE LST window start
      - REAL row at 5/8 06:30 UTC                       with temp_f=73.0  ← INSIDE window

    Pre-fix behavior (48h window): MIN would return 55.0 (the leak).
    Post-fix behavior (24h LST window): MIN returns 73.0.
    """

    @classmethod
    def setUpClass(cls):
        import sqlite3
        from datetime import datetime, timezone
        cls._tmp_db = Path(_TMPDIR) / "obs_regression.sqlite"
        if cls._tmp_db.exists():
            cls._tmp_db.unlink()
        conn = sqlite3.connect(str(cls._tmp_db))
        conn.execute("""
            CREATE TABLE observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                station TEXT NOT NULL,
                obs_time INTEGER NOT NULL,
                received_time INTEGER NOT NULL,
                source TEXT NOT NULL,
                temp_f REAL,
                temp_c_raw REAL,
                raw_message TEXT
            )
        """)
        # The exact leak timestamp from the original bug report
        leak_ts = int(datetime(2026, 5, 7, 14, 53, tzinfo=timezone.utc).timestamp())
        # An in-window observation
        real_ts = int(datetime(2026, 5, 8, 6, 30, tzinfo=timezone.utc).timestamp())
        # A NEXT-day leak (post-window) — also must be excluded
        next_day_ts = int(datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc).timestamp())
        conn.executemany(
            "INSERT INTO observations(station, obs_time, received_time, source, temp_f) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                ("KHOU", leak_ts,     leak_ts,     "awc", 55.0),  # leak (prior day morning)
                ("KHOU", real_ts,     real_ts,     "awc", 73.0),  # real in-window
                ("KHOU", next_day_ts, next_day_ts, "awc", 50.0),  # post-window leak
                # A different station shouldn't influence the query
                ("KDFW", real_ts,     real_ts,     "awc", 40.0),
                # An in-window non-METAR source must be excluded too (only awc/ldm)
                ("KHOU", real_ts + 60, real_ts + 60, "madis_fsl2", 40.0),
            ],
        )
        conn.commit()
        conn.close()

        cls._orig_db_path = pb.OBS_DB_PATH
        pb.OBS_DB_PATH = str(cls._tmp_db)

    @classmethod
    def tearDownClass(cls):
        pb.OBS_DB_PATH = cls._orig_db_path

    def test_returns_in_window_value_not_leak(self):
        rm = pb._get_metar_running_min("KHOU", "2026-05-08")
        self.assertIsNotNone(rm, "function must return a value when in-window data exists")
        self.assertEqual(rm, 73.0,
            "must return the in-window 73.0F, not the 55.0F prior-day leak "
            "or the 50.0F next-day leak")
        # Stronger: must NEVER equal the leak value
        self.assertNotEqual(rm, 55.0,
            "REGRESSION: prior-day leak temp returned — LST window is too wide")
        self.assertNotEqual(rm, 50.0,
            "REGRESSION: next-day leak temp returned — LST window extends too far")

    def test_madis_source_excluded(self):
        """The madis_fsl2 row at 40.0 is INSIDE the LST window but is not
        an awc/ldm source — must NOT be returned as the min."""
        rm = pb._get_metar_running_min("KHOU", "2026-05-08")
        self.assertNotEqual(rm, 40.0,
            "non-METAR source (madis_fsl2) must be excluded — only awc/ldm count")

    def test_returns_none_when_no_in_window_obs(self):
        """A different climate_date with no in-window obs must return None,
        NOT silently fall through to the leak data from neighboring days."""
        rm = pb._get_metar_running_min("KHOU", "2026-05-10")  # no obs for this date
        self.assertIsNone(rm,
            "function must return None when no awc/ldm obs exist in the "
            "target LST window — must NOT pull from prior/next day")

    def test_other_station_isolated(self):
        """Querying KHOU must not be polluted by KDFW data even if same ts."""
        rm = pb._get_metar_running_min("KHOU", "2026-05-08")
        self.assertNotEqual(rm, 40.0,
            "KDFW's 40.0F must not leak into KHOU's query")


if __name__ == "__main__":
    unittest.main()
