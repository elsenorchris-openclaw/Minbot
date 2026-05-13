"""Tests for NARROW_MARGIN_TP gate (shipped 2026-05-13).

Source-text checks (no import needed) + behavioral tests for
_check_narrow_margin_tp via direct call into the bot module.
"""
import os
import sys
import unittest
from pathlib import Path

# Allow tests to find the bot module
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import paper_min_bot as pb  # noqa: E402

SRC_PATH = Path(__file__).resolve().parents[1] / "paper_min_bot.py"


def src() -> str:
    return SRC_PATH.read_text()


class TestNMTPConstants(unittest.TestCase):
    """Constants must exist at expected values."""

    def test_enabled_flag_present(self):
        self.assertTrue(hasattr(pb, "NARROW_MARGIN_TP_ENABLED"))
        self.assertIsInstance(pb.NARROW_MARGIN_TP_ENABLED, bool)

    def test_thresholds_at_audited_values(self):
        self.assertEqual(pb.NARROW_MARGIN_TP_MARGIN_F, 1.5)
        self.assertEqual(pb.NARROW_MARGIN_TP_MIN_BID_C, 65)
        self.assertEqual(pb.NARROW_MARGIN_TP_MAX_BID_C, 80)
        self.assertEqual(pb.NARROW_MARGIN_TP_MIN_MTM, 0.20)

    def test_station_whitelist_exact(self):
        # The 4 whitelist stations are NOT arbitrary — they're the only
        # stations with net-positive NMTP swing in the 14d audit AND the
        # only stations with empirically losing BUY_NO B-bracket over 14d.
        # Changing this set without a re-audit defeats the design.
        self.assertEqual(
            set(pb.NARROW_MARGIN_TP_STATIONS),
            {"KLAX", "KMDW", "KMIA", "KSEA"},
        )

    def test_whitelist_is_frozenset(self):
        self.assertIsInstance(pb.NARROW_MARGIN_TP_STATIONS, frozenset)


class TestNMTPFunction(unittest.TestCase):
    """_check_narrow_margin_tp returns True only for the audited trigger."""

    def _miami_pos(self, **overrides):
        # Mirrors KXLOWTMIA-26MAY12-B77.5 (the case that prompted this gate)
        base = dict(
            action="BUY_NO",
            station="KMIA",
            floor=77.0,
            cap=78.0,
            entry_price=0.50,
            count=94,
        )
        base.update(overrides)
        return base

    def test_canonical_trigger_fires(self):
        pos = self._miami_pos()
        # rm=78.8 (margin=0.8°F ∈ (0, 1.5]), bid=79c, mtm = (0.79-0.50)/0.50 = +58%
        self.assertTrue(pb._check_narrow_margin_tp(pos, 78.8, 79))

    def test_disabled_flag_blocks(self):
        pos = self._miami_pos()
        original = pb.NARROW_MARGIN_TP_ENABLED
        try:
            pb.NARROW_MARGIN_TP_ENABLED = False
            self.assertFalse(pb._check_narrow_margin_tp(pos, 78.8, 79))
        finally:
            pb.NARROW_MARGIN_TP_ENABLED = original

    def test_buy_yes_blocked(self):
        pos = self._miami_pos(action="BUY_YES")
        self.assertFalse(pb._check_narrow_margin_tp(pos, 78.8, 79))

    def test_t_bracket_blocked(self):
        # T-cap (cap only)
        pos = self._miami_pos(floor=None)
        self.assertFalse(pb._check_narrow_margin_tp(pos, 78.8, 79))
        # T-floor (floor only)
        pos = self._miami_pos(cap=None)
        self.assertFalse(pb._check_narrow_margin_tp(pos, 78.8, 79))

    def test_station_not_in_whitelist_blocked(self):
        pos = self._miami_pos(station="KATL")  # not in whitelist
        self.assertFalse(pb._check_narrow_margin_tp(pos, 78.8, 79))

    def test_rm_in_bracket_blocked(self):
        # rm IN bracket — SHADOW handles, NMTP must not fire
        pos = self._miami_pos()
        self.assertFalse(pb._check_narrow_margin_tp(pos, 77.5, 79))  # margin=-0.5

    def test_rm_at_cap_exactly_blocked(self):
        # rm == cap means margin=0 — SHADOW territory, not NMTP
        pos = self._miami_pos()
        self.assertFalse(pb._check_narrow_margin_tp(pos, 78.0, 79))

    def test_rm_too_far_above_cap_blocked(self):
        pos = self._miami_pos()
        # margin = 80.0 - 78.0 = 2.0 > MARGIN_F=1.5 → safe-zone, no need to TP
        self.assertFalse(pb._check_narrow_margin_tp(pos, 80.0, 79))

    def test_rm_at_max_margin_fires(self):
        pos = self._miami_pos()
        # margin = 1.5 exactly is the boundary — should fire (≤ MARGIN_F)
        self.assertTrue(pb._check_narrow_margin_tp(pos, 79.5, 79))

    def test_bid_below_floor_blocked(self):
        pos = self._miami_pos()
        self.assertFalse(pb._check_narrow_margin_tp(pos, 78.8, 64))  # bid < 65

    def test_bid_above_ceiling_blocked(self):
        pos = self._miami_pos()
        # bid > 80c — extreme MTM zone where market is usually right
        self.assertFalse(pb._check_narrow_margin_tp(pos, 78.8, 81))

    def test_bid_at_min_fires_if_mtm_clears(self):
        pos = self._miami_pos(entry_price=0.50)
        # bid=65c, mtm = (0.65-0.50)/0.50 = +30% ≥ 20%
        self.assertTrue(pb._check_narrow_margin_tp(pos, 78.8, 65))

    def test_low_mtm_blocked(self):
        # entry 0.60, bid 65c → mtm = (0.65-0.60)/0.60 = 8.3% < 20%
        pos = self._miami_pos(entry_price=0.60)
        self.assertFalse(pb._check_narrow_margin_tp(pos, 78.8, 65))

    def test_none_rm_blocked(self):
        pos = self._miami_pos()
        self.assertFalse(pb._check_narrow_margin_tp(pos, None, 79))

    def test_none_bid_blocked(self):
        pos = self._miami_pos()
        self.assertFalse(pb._check_narrow_margin_tp(pos, 78.8, None))


class TestNMTPSourceIntegration(unittest.TestCase):
    """Source-text checks: function is called from check_open_positions_for_exit
    and routes through _execute_exit with the right reason."""

    def test_function_defined(self):
        self.assertIn("def _check_narrow_margin_tp", src())

    def test_called_from_exit_check(self):
        s = src()
        idx = s.find("def check_open_positions_for_exit")
        self.assertNotEqual(idx, -1)
        body = s[idx:idx + 20000]
        self.assertIn("_check_narrow_margin_tp", body,
            "NMTP gate must be invoked in check_open_positions_for_exit")

    def test_uses_distinct_exit_reason(self):
        s = src()
        self.assertIn('"narrow_margin_tp"', s,
            "Exit reason must be 'narrow_margin_tp' for telemetry separation")

    def test_placement_before_shadow(self):
        # NMTP must be invoked BEFORE SHADOW so they cover non-overlapping
        # rm ranges (NMTP: rm just-above-cap; SHADOW: rm in bracket).
        s = src()
        nmtp_pos = s.find("_check_narrow_margin_tp(pos, rm, current_bid_c)")
        shadow_pos = s.find("_check_position_obs_confirmed_loser_for_exit(pos, float(rm))")
        self.assertGreater(nmtp_pos, 0)
        self.assertGreater(shadow_pos, 0)
        self.assertLess(nmtp_pos, shadow_pos,
            "NMTP must fire BEFORE SHADOW in the exit sequence")

    def test_placement_after_winner_override(self):
        # NMTP must be invoked AFTER the winner override so we don't
        # TP positions that obs already confirms as winners.
        s = src()
        winner_pos = s.find("_check_position_obs_winning(pos, float(rm))")
        nmtp_pos = s.find("_check_narrow_margin_tp(pos, rm, current_bid_c)")
        self.assertGreater(winner_pos, 0)
        self.assertGreater(nmtp_pos, 0)
        self.assertLess(winner_pos, nmtp_pos,
            "NMTP must fire AFTER winner override to avoid selling confirmed winners")


if __name__ == "__main__":
    unittest.main()
