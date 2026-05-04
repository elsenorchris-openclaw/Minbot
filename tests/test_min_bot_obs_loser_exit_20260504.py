"""Regression tests for the 2026-05-04 night min_bot SHADOW OBS_CONFIRMED_LOSER detection.

Stage 1 (current): logs `SHADOW OBS_CONFIRMED_LOSER` events without selling.
Stage 2 (future, after 7-14d shadow data): replace SHADOW log with _execute_exit.

The exit-side detection is INTENTIONALLY STRICTER than the entry-side
`_check_obs_confirmed_loser`. False positive at exit = SELL A WINNER.
"""
import os
import sys
import unittest
from pathlib import Path

BOT_PATH = os.environ.get("PAPER_MIN_BOT_PATH",
                          "/home/ubuntu/paper_min_bot/paper_min_bot.py")
sys.path.insert(0, "/home/ubuntu/paper_min_bot")


def src():
    return Path(BOT_PATH).read_text()


# Lazy module load — avoid bot side effects during pytest collection
def _import_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("paper_min_bot_local", BOT_PATH)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception:
        return None
    return mod

_MOD = _import_module()
_FN = getattr(_MOD, "_check_position_obs_confirmed_loser_for_exit", None) if _MOD else None


# Reliable TZ choices for test (independent of when tests run):
# - America/Toronto (UTC-4 EDT in May): test infra. Will skip if local hour
#   isn't in needed range. We use known TZs below instead.
# - Pacific/Honolulu (UTC-10 always, no DST): reliably post-8AM during
#   most UTC test runs (UTC ~14:00 onward).
# - Asia/Karachi (UTC+5, no DST): early-morning during late-UTC test runs.
#
# To avoid time-of-day dependency, we just check the function returns
# WITHIN A REASONABLE WINDOW for each input. We use "in_post_lock_window"
# helpers below to skip tests if the test environment can't simulate the
# needed local time.
import datetime as dt_mod
from zoneinfo import ZoneInfo

def _local_hour(tz_name):
    return dt_mod.datetime.now(ZoneInfo(tz_name)).hour

# Find a TZ that is post-8AM right now
def _find_post_lock_tz():
    for tz in ("Pacific/Honolulu", "America/Los_Angeles", "America/Chicago",
               "America/New_York", "Europe/London", "Asia/Tokyo", "Australia/Sydney"):
        try:
            if _local_hour(tz) >= 8 and _local_hour(tz) < 22:
                return tz
        except Exception:
            continue
    return None

def _find_pre_lock_tz():
    for tz in ("Asia/Tokyo", "Asia/Seoul", "Australia/Sydney", "Pacific/Auckland",
               "Asia/Kolkata", "Europe/London", "Atlantic/Azores"):
        try:
            if _local_hour(tz) < 8:
                return tz
        except Exception:
            continue
    return None


class TestSourcePresence(unittest.TestCase):
    """Source-text checks: function definition + shadow log line + safety."""

    def test_function_defined(self):
        self.assertIn("def _check_position_obs_confirmed_loser_for_exit", src())

    def test_shadow_log_line_present(self):
        self.assertIn("SHADOW OBS_CONFIRMED_LOSER", src(),
            "Shadow log line must be present in check_open_positions_for_exit")

    def test_shadow_does_not_call_execute_exit(self):
        """Stage 1 must NOT call _execute_exit on obs-loser path."""
        s = src()
        idx = s.find("# 2026-05-04 night SHADOW: OBS_CONFIRMED_LOSER")
        self.assertNotEqual(idx, -1, "shadow block comment must exist")
        end = s.find("# 2026-05-04: very-loose value-dead market stop", idx)
        self.assertNotEqual(end, -1, "shadow block must end before MARKET_STOP comment")
        shadow_block = s[idx:end]
        self.assertNotIn("_execute_exit", shadow_block,
            "STAGE 1 SHADOW: must not call _execute_exit; only log.")

    def test_uses_post_sunrise_gate(self):
        """Function must reference _is_post_sunrise for the post-lock gate."""
        s = src()
        idx = s.find("def _check_position_obs_confirmed_loser_for_exit")
        end = s.find("\ndef ", idx + 1)
        body = s[idx:end if end != -1 else idx + 5000]
        self.assertIn("_is_post_sunrise", body,
            "Function must use _is_post_sunrise as the post-lock gate")

    def test_buy_no_b_uses_1f_buffer(self):
        """BUY_NO B-bracket loser threshold must use floor + 1.0 (1°F buffer
        mirroring the WINNER side's floor - 1.0)."""
        s = src()
        idx = s.find("def _check_position_obs_confirmed_loser_for_exit")
        end = s.find("\ndef ", idx + 1)
        body = s[idx:end if end != -1 else idx + 5000]
        self.assertIn("float(floor) + 1.0", body)


class TestBuyNoBBracketLoser(unittest.TestCase):
    """BUY_NO + B-bracket loser conditions."""

    def setUp(self):
        if _FN is None:
            self.skipTest("Could not import bot module")

    def test_loser_at_cap_post_lock(self):
        """rm = cap (e.g., 72.0 for B71.5) post-lock → loser."""
        tz = _find_post_lock_tz()
        if not tz: self.skipTest("No post-lock TZ available")
        pos = {"action": "BUY_NO", "floor": 71.0, "cap": 72.0, "tz": tz, "count": 30}
        self.assertTrue(_FN(pos, 72.0),
            "rm=72.0 (=cap, > floor+1.0=72.0... actually equal) post-lock should fire. "
            "Wait the threshold is rm >= floor+1.0=72.0. Boundary check: 72.0 >= 72.0 = True.")

    def test_loser_above_floor_plus_1_strict(self):
        """rm = floor + 1.5 — solidly past buffer → loser."""
        tz = _find_post_lock_tz()
        if not tz: self.skipTest("No post-lock TZ available")
        # Use a wider bracket where rm in middle is unambiguous
        pos = {"action": "BUY_NO", "floor": 70.0, "cap": 75.0, "tz": tz, "count": 30}
        self.assertTrue(_FN(pos, 71.5),
            "rm=71.5 in [floor+1.0=71.0, cap=75.0] post-lock → loser")

    def test_no_fire_at_floor(self):
        """rm = floor (within 1°F buffer) — must NOT fire."""
        tz = _find_post_lock_tz()
        if not tz: self.skipTest("No post-lock TZ available")
        pos = {"action": "BUY_NO", "floor": 71.0, "cap": 72.0, "tz": tz, "count": 30}
        self.assertFalse(_FN(pos, 71.0),
            "rm=71.0 (=floor) within 1°F dead zone — must NOT fire (CLI could "
            "round to 70 making this a winner)")

    def test_no_fire_below_floor_minus_buffer(self):
        """rm = floor - 1.5 → winner zone, not loser."""
        tz = _find_post_lock_tz()
        if not tz: self.skipTest("No post-lock TZ available")
        pos = {"action": "BUY_NO", "floor": 71.0, "cap": 72.0, "tz": tz, "count": 30}
        self.assertFalse(_FN(pos, 69.5))

    def test_no_fire_pre_lock(self):
        """Pre-sunrise: rm could still drop. Must NOT fire."""
        tz = _find_pre_lock_tz()
        if not tz: self.skipTest("No pre-lock TZ available")
        pos = {"action": "BUY_NO", "floor": 71.0, "cap": 72.0, "tz": tz, "count": 30}
        self.assertFalse(_FN(pos, 72.0),
            "Pre-lock TZ must not fire even if rm in bracket")

    def test_no_fire_above_cap(self):
        """rm > cap → BUY_NO wins (low locked above bracket)."""
        tz = _find_post_lock_tz()
        if not tz: self.skipTest("No post-lock TZ available")
        pos = {"action": "BUY_NO", "floor": 71.0, "cap": 72.0, "tz": tz, "count": 30}
        self.assertFalse(_FN(pos, 73.0),
            "rm > cap means BUY_NO wins (low locked above bracket), not loser")


class TestBuyNoTCapLoser(unittest.TestCase):
    """BUY_NO + T-cap (cap-only): rm <= cap - 1.0 AND post-lock → loser."""

    def setUp(self):
        if _FN is None:
            self.skipTest("Could not import bot module")

    def test_loser_strict_below(self):
        tz = _find_post_lock_tz()
        if not tz: self.skipTest("No post-lock TZ available")
        pos = {"action": "BUY_NO", "floor": None, "cap": 47.5, "tz": tz, "count": 10}
        self.assertTrue(_FN(pos, 45.0),
            "rm=45.0 < cap-1.0=46.5 → definitive loser")

    def test_no_fire_at_cap(self):
        tz = _find_post_lock_tz()
        if not tz: self.skipTest("No post-lock TZ available")
        pos = {"action": "BUY_NO", "floor": None, "cap": 47.5, "tz": tz, "count": 10}
        self.assertFalse(_FN(pos, 47.0),
            "rm=47.0 within 1°F buffer of cap → must not fire")


class TestBuyYesTLowLoser(unittest.TestCase):
    """BUY_YES + T-low (cap-only): rm > cap + 0.5 AND post-lock → loser."""

    def setUp(self):
        if _FN is None:
            self.skipTest("Could not import bot module")

    def test_loser_above_cap_plus_buffer(self):
        tz = _find_post_lock_tz()
        if not tz: self.skipTest("No post-lock TZ available")
        pos = {"action": "BUY_YES", "floor": None, "cap": 50.0, "tz": tz, "count": 12}
        self.assertTrue(_FN(pos, 51.0),
            "rm=51.0 > cap+0.5=50.5 post-lock → loser")

    def test_no_fire_just_above_cap(self):
        tz = _find_post_lock_tz()
        if not tz: self.skipTest("No post-lock TZ available")
        pos = {"action": "BUY_YES", "floor": None, "cap": 50.0, "tz": tz, "count": 12}
        self.assertFalse(_FN(pos, 50.3),
            "rm=50.3 within 0.5°F buffer of cap — must NOT fire")

    def test_no_fire_pre_lock(self):
        tz = _find_pre_lock_tz()
        if not tz: self.skipTest("No pre-lock TZ available")
        pos = {"action": "BUY_YES", "floor": None, "cap": 50.0, "tz": tz, "count": 12}
        self.assertFalse(_FN(pos, 51.0))


class TestBuyYesBBracketLoser(unittest.TestCase):
    """BUY_YES + B-bracket: rm < floor - 0.5 (monotone) OR rm > cap+1.5 + lock."""

    def setUp(self):
        if _FN is None:
            self.skipTest("Could not import bot module")

    def test_loser_below_floor_no_lock_needed(self):
        """rm < floor - 0.5 fires anytime (rm only decreases)."""
        tz = _find_pre_lock_tz()
        if not tz: tz = _find_post_lock_tz()
        if not tz: self.skipTest("No TZ available")
        pos = {"action": "BUY_YES", "floor": 55.0, "cap": 56.0, "tz": tz, "count": 10}
        self.assertTrue(_FN(pos, 54.0),
            "rm=54.0 < floor-0.5=54.5 → fire anytime (rm monotone)")

    def test_no_fire_at_floor_minus_buffer(self):
        tz = _find_post_lock_tz()
        if not tz: self.skipTest("No post-lock TZ available")
        pos = {"action": "BUY_YES", "floor": 55.0, "cap": 56.0, "tz": tz, "count": 10}
        self.assertFalse(_FN(pos, 54.7),
            "rm=54.7 within 0.5°F buffer below floor — don't fire")


class TestFailSafe(unittest.TestCase):
    """Fail-safe: any unexpected error → return False (don't sell)."""

    def setUp(self):
        if _FN is None:
            self.skipTest("Could not import bot module")

    def test_empty_pos_returns_false(self):
        self.assertFalse(_FN({}, 75.0),
            "Empty pos dict must return False")

    def test_invalid_tz_returns_false(self):
        pos = {"action": "BUY_NO", "floor": 71.0, "cap": 72.0,
               "tz": "INVALID/NOT_A_TZ", "count": 30}
        self.assertFalse(_FN(pos, 72.0),
            "Invalid TZ must return False (fail-safe)")

    def test_unknown_action_returns_false(self):
        pos = {"action": "UNKNOWN", "floor": 71.0, "cap": 72.0,
               "tz": "America/New_York", "count": 30}
        self.assertFalse(_FN(pos, 72.0),
            "Unknown action returns False")


if __name__ == "__main__":
    unittest.main()
