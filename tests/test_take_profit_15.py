"""Tests for TAKE_PROFIT_15 gate (shipped 2026-05-20).

Constants + behavioral checks for _check_take_profit_15 via direct call
into the bot module."""
import sys
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import paper_min_bot as pb  # noqa: E402


class TestTakeProfit15Constants(unittest.TestCase):
    def test_constants_exist(self):
        self.assertTrue(hasattr(pb, "TAKE_PROFIT_15_ENABLED"))
        self.assertIsInstance(pb.TAKE_PROFIT_15_ENABLED, bool)
        self.assertTrue(hasattr(pb, "TAKE_PROFIT_15_MIN_MTM_PCT"))
        self.assertTrue(hasattr(pb, "TAKE_PROFIT_15_MIN_LOCAL_HOUR"))
        self.assertTrue(hasattr(pb, "TAKE_PROFIT_15_MIN_LOCAL_MIN"))

    def test_threshold_values(self):
        self.assertEqual(pb.TAKE_PROFIT_15_MIN_MTM_PCT, 0.15)
        self.assertEqual(pb.TAKE_PROFIT_15_MIN_LOCAL_HOUR, 10)
        self.assertEqual(pb.TAKE_PROFIT_15_MIN_LOCAL_MIN, 30)


class TestTakeProfit15Behavior(unittest.TestCase):
    """Fixed `now_local` injected via the function arg so tests are
    deterministic regardless of when they run."""

    def _pos(self, **overrides):
        p = dict(
            action="BUY_NO",
            count=58,
            cost=24.94,
            entry_price=0.43,
            tz="America/Chicago",
        )
        p.update(overrides)
        return p

    def _t(self, h, m, tz="America/Chicago"):
        # 2026-05-19 a Tuesday — used for tests
        return datetime(2026, 5, 19, h, m, 0, tzinfo=ZoneInfo(tz))

    def test_satx_archetype_fires(self):
        """SATX-26MAY19-T71 archetype: NB=52c (qty 58 cost $24.94 → MTM
        +$5.22 = 20.9% of cost), past 10:30 CDT → FIRES."""
        self.assertTrue(pb._check_take_profit_15(self._pos(), 52, self._t(16, 59)))

    def test_below_15_pct_doesnt_fire(self):
        """NB=48c → MTM = 58*0.48 - 24.94 = +2.90 = 11.6% < 15% → no fire."""
        self.assertFalse(pb._check_take_profit_15(self._pos(), 48, self._t(16, 59)))

    def test_at_threshold_fires(self):
        """At exactly 15% MTM: cost $24.94, need MTM ≥ 3.74. NB s.t.
        58*nb/100 - 24.94 = 3.74 → nb = 49.45c → 50c gives MTM = 4.06 = 16.3% → fires.
        49c gives MTM = 3.48 = 14.0% → no fire."""
        self.assertTrue(pb._check_take_profit_15(self._pos(), 50, self._t(11, 0)))
        self.assertFalse(pb._check_take_profit_15(self._pos(), 49, self._t(11, 0)))

    def test_before_1030_doesnt_fire(self):
        """High MTM but before 10:30 LST → no fire."""
        self.assertFalse(pb._check_take_profit_15(self._pos(), 99, self._t(10, 29)))
        self.assertFalse(pb._check_take_profit_15(self._pos(), 99, self._t(9, 59)))
        self.assertFalse(pb._check_take_profit_15(self._pos(), 99, self._t(0, 0)))

    def test_exactly_1030_fires(self):
        """10:30:00 exactly — boundary should fire."""
        self.assertTrue(pb._check_take_profit_15(self._pos(), 99, self._t(10, 30)))

    def test_buy_yes_never_fires(self):
        """Rule is BUY_NO only (BUY_YES has different exit semantics)."""
        self.assertFalse(pb._check_take_profit_15(self._pos(action="BUY_YES"), 99, self._t(15, 0)))

    def test_invalid_inputs_dont_fire(self):
        # Rule uses cost/qty (not entry_price) for the MTM calc, so missing
        # entry_price alone shouldn't block. But cost/qty being 0/missing
        # should always block.
        self.assertFalse(pb._check_take_profit_15(self._pos(), None, self._t(15, 0)))
        self.assertFalse(pb._check_take_profit_15(self._pos(), 0, self._t(15, 0)))
        self.assertFalse(pb._check_take_profit_15(self._pos(cost=0), 99, self._t(15, 0)))
        self.assertFalse(pb._check_take_profit_15(self._pos(count=0), 99, self._t(15, 0)))

    def test_disabled_flag_kill(self):
        orig = pb.TAKE_PROFIT_15_ENABLED
        try:
            pb.TAKE_PROFIT_15_ENABLED = False
            self.assertFalse(pb._check_take_profit_15(self._pos(), 99, self._t(15, 0)))
        finally:
            pb.TAKE_PROFIT_15_ENABLED = orig

    def test_different_tz_uses_local_time(self):
        """Position in NY tz (EDT, UTC-4): the threshold is 10:30 NY time,
        not 10:30 UTC. So 10:30 EDT counts; 9:30 EDT doesn't even though
        UTC is 13:30 (well past UTC 10:30)."""
        pos = self._pos(tz="America/New_York")
        # 10:30 EDT → fires
        self.assertTrue(pb._check_take_profit_15(pos, 99, self._t(10, 30, "America/New_York")))
        # 09:30 EDT → no fire
        self.assertFalse(pb._check_take_profit_15(pos, 99, self._t(9, 30, "America/New_York")))


if __name__ == "__main__":
    unittest.main()
