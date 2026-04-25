"""Unit tests for paper_min_bot core functions.

Covers:
  - Bracket parsing (B/T-low/T-high detection, correct floor/cap)
  - Min-temp probability math (Gaussian CDF, running_min truncation, post-sunrise lock)
  - Hours-to-sunrise heuristic
  - Climate date helper

Does NOT exercise Kalshi, obs-pipeline DB, or NBP fetch — those are integration
tested via live runs.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

# Point DATA_DIR at a temp location before import
_TMPDIR = tempfile.mkdtemp(prefix="paper_min_test_")
os.environ["HOME"] = _TMPDIR
sys.path.insert(0, "/tmp/paper_min_bot")

# Override paths BEFORE import
import paper_min_bot as pb
pb.DATA_DIR = Path(_TMPDIR) / "data"
pb.LOG_DIR = Path(_TMPDIR) / "logs"
pb.DATA_DIR.mkdir(parents=True, exist_ok=True)
pb.LOG_DIR.mkdir(parents=True, exist_ok=True)


class TestBracketParse(unittest.TestCase):
    def test_bracket_b_parsed_correctly(self):
        b = pb.parse_market_bracket("KXLOWTAUS-26APR25-B64.5")
        self.assertEqual(b["kind"], "bracket")
        self.assertEqual(b["floor"], 64.0)
        self.assertEqual(b["cap"], 65.0)

    def test_bracket_b_integer_val(self):
        b = pb.parse_market_bracket("KXLOWTCHI-26APR25-B50.5")
        self.assertAlmostEqual(b["floor"], 50.0)
        self.assertAlmostEqual(b["cap"], 51.0)

    def test_tail_unclassified_needs_event_context(self):
        t = pb.parse_market_bracket("KXLOWTAUS-26APR25-T64")
        self.assertEqual(t["kind"], "tail")
        self.assertEqual(t["value"], 64)

    def test_tail_low_resolved_from_event(self):
        t_market = {"ticker": "KXLOWTAUS-26APR25-T64", "subtitle": "63° or below"}
        event = [
            {"ticker": "KXLOWTAUS-26APR25-T64"},
            {"ticker": "KXLOWTAUS-26APR25-B64.5"},
            {"ticker": "KXLOWTAUS-26APR25-B66.5"},
            {"ticker": "KXLOWTAUS-26APR25-B68.5"},
            {"ticker": "KXLOWTAUS-26APR25-T71"},
        ]
        r = pb.resolve_tail_bracket(t_market, event)
        self.assertEqual(r["kind"], "tail_low")
        self.assertIsNone(r["floor"])
        self.assertEqual(r["cap"], 63.5)

    def test_tail_high_resolved_from_event(self):
        t_market = {"ticker": "KXLOWTAUS-26APR25-T71"}
        event = [
            {"ticker": "KXLOWTAUS-26APR25-T64"},
            {"ticker": "KXLOWTAUS-26APR25-B64.5"},
            {"ticker": "KXLOWTAUS-26APR25-B68.5"},
            {"ticker": "KXLOWTAUS-26APR25-T71"},
        ]
        r = pb.resolve_tail_bracket(t_market, event)
        self.assertEqual(r["kind"], "tail_high")
        self.assertEqual(r["floor"], 71.5)
        self.assertIsNone(r["cap"])


class TestBracketProbability(unittest.TestCase):
    def test_gaussian_without_truncation(self):
        # N(65, 2) → P(64 < X < 66) should be ~38% (about ±0.5σ)
        p = pb.calc_bracket_probability_min(mu=65.0, sigma=2.0,
                                              floor=64.0, cap=66.0)
        self.assertTrue(0.30 < p < 0.45)

    def test_running_min_truncates_above(self):
        # Running min = 62 means the daily min can't exceed 62. The bracket
        # 64-66 requires low ≥ 64, which is impossible if low ≤ 62.
        p = pb.calc_bracket_probability_min(mu=65.0, sigma=2.0,
                                              floor=64.0, cap=66.0,
                                              running_min=62.0)
        self.assertAlmostEqual(p, 0.0, places=5)

    def test_running_min_conditions_probability_correctly(self):
        # Running min 65.5 imposes "low ≤ 65.5". The posterior P(bracket | X ≤ 65.5)
        # renormalizes — since the bracket 64-65.5 occupies most of the conditional
        # support, posterior prob should be higher than unconditional.
        p_no_rm = pb.calc_bracket_probability_min(mu=65.0, sigma=2.0,
                                                    floor=64.0, cap=66.0)
        p_rm = pb.calc_bracket_probability_min(mu=65.0, sigma=2.0,
                                                 floor=64.0, cap=66.0,
                                                 running_min=65.5)
        # Conditional posterior should be higher (we've ruled out most of upper tail)
        self.assertGreater(p_rm, p_no_rm)
        # But still valid probability
        self.assertLessEqual(p_rm, 1.0)
        self.assertGreaterEqual(p_rm, 0.0)

    def test_running_min_below_bracket_gives_zero(self):
        # Running min 62 — low definitely ≤62, so bracket 64-66 is impossible.
        p = pb.calc_bracket_probability_min(mu=65.0, sigma=2.0,
                                              floor=64.0, cap=66.0,
                                              running_min=62.0)
        self.assertEqual(p, 0.0)

    def test_running_min_far_above_bracket_negligible_impact(self):
        # Running min way above bracket — the rm constraint barely affects
        # the lower tail where the bracket lives.
        p_no_rm = pb.calc_bracket_probability_min(mu=50.0, sigma=3.0,
                                                    floor=48.0, cap=50.0)
        p_rm = pb.calc_bracket_probability_min(mu=50.0, sigma=3.0,
                                                 floor=48.0, cap=50.0,
                                                 running_min=80.0)  # impossible ceiling
        # Both should be nearly equal (rm=80 barely constrains)
        self.assertAlmostEqual(p_rm, p_no_rm, places=3)

    def test_post_sunrise_lock_collapses_to_rm(self):
        # Post-sunrise + rm=62 — sigma collapsed to 1.0°F to reflect ASOS-vs-CLI
        # ±1°F uncertainty (was 0.5°F before 2026-04-25 buffer fix). A bracket
        # straddling 62 should still take the bulk of probability; one far away
        # should be ~0%.
        p_hit = pb.calc_bracket_probability_min(mu=68.0, sigma=3.0,
                                                 floor=61.5, cap=62.5,
                                                 running_min=62.0,
                                                 post_sunrise_lock=True)
        # sigma=1.0, bracket ±0.5 from rm: P ≈ Φ(0.5)-Φ(-0.5) ≈ 0.38
        self.assertGreater(p_hit, 0.30)
        self.assertLess(p_hit, 0.50)
        p_miss = pb.calc_bracket_probability_min(mu=68.0, sigma=3.0,
                                                  floor=68.0, cap=69.0,
                                                  running_min=62.0,
                                                  post_sunrise_lock=True)
        self.assertLess(p_miss, 0.05)

    def test_low_tail_no_floor(self):
        # "T64" = low ≤ 63°F. Under N(65, 2), should be small.
        p = pb.calc_bracket_probability_min(mu=65.0, sigma=2.0,
                                              floor=None, cap=63.5)
        self.assertTrue(0.15 < p < 0.30)

    def test_high_tail_no_cap(self):
        # "T71" = low ≥ 72°F. Under N(65, 2), should be tiny.
        p = pb.calc_bracket_probability_min(mu=65.0, sigma=2.0,
                                              floor=71.5, cap=None)
        self.assertLess(p, 0.01)

    def test_sigma_zero_does_not_crash(self):
        # Edge case: sigma == 0 (post-sunrise with perfect obs)
        p = pb.calc_bracket_probability_min(mu=62.0, sigma=0.0,
                                              floor=61.5, cap=62.5,
                                              running_min=62.0)
        self.assertGreaterEqual(p, 0.0)
        self.assertLessEqual(p, 1.0)


class TestClimateDate(unittest.TestCase):
    def test_eastern_lst_year_round(self):
        # Climate date uses LST (no DST) — for America/New_York at UTC 15:00
        # on 2026-04-15 (during DST), LST date is 2026-04-15 (since 15 UTC
        # is 10 AM EST = 10 AM LST, same day).
        from datetime import datetime, timezone
        dt = datetime(2026, 4, 15, 15, 0, tzinfo=timezone.utc)
        self.assertEqual(pb._climate_date_nws("America/New_York", dt), "2026-04-15")

    def test_utc_midnight_previous_day_in_lst(self):
        # At 04:00 UTC, EST = 23:00 previous day. So climate date = yesterday.
        from datetime import datetime, timezone
        dt = datetime(2026, 4, 15, 4, 0, tzinfo=timezone.utc)
        self.assertEqual(pb._climate_date_nws("America/New_York", dt), "2026-04-14")


class TestBracketDetection(unittest.TestCase):
    def test_all_20_cities_have_nbp_mapping(self):
        # Every series in CITIES should have a corresponding NBP station code
        for series in pb.CITIES:
            self.assertIn(series, pb.NBP_STATION_MAP,
                           f"missing NBP mapping for {series}")

    def test_nbp_mapping_is_4_char_icao(self):
        for series, nbp in pb.NBP_STATION_MAP.items():
            self.assertEqual(len(nbp), 4, f"{series} → {nbp} is not 4 chars")
            self.assertTrue(nbp.startswith("K") or nbp.startswith("P"),
                            f"{series} → {nbp} should start with K (CONUS) or P (Pacific)")


class TestLiveBudget(unittest.TestCase):
    """Caps that gate execute_opportunity in LIVE mode."""

    def setUp(self):
        # Force a fully clean budget state per test (including daily totals,
        # which _reset_cycle_budget only zeros at the date rollover).
        with pb._cycle_budget_lock:
            pb._cycle_new_count = 0
            pb._today_exposure_usd = 0.0
            pb._today_date_utc = ""
        pb._reset_cycle_budget()

    def test_reset_clears_cycle_count(self):
        pb._budget_record(0.50)
        pb._budget_record(0.50)
        self.assertEqual(pb._cycle_new_count, 2)
        pb._reset_cycle_budget()
        self.assertEqual(pb._cycle_new_count, 0)

    def test_per_cycle_cap(self):
        for _ in range(pb.MAX_NEW_POSITIONS_PER_CYCLE):
            ok, _ = pb._budget_can_take(0.10)
            self.assertTrue(ok)
            pb._budget_record(0.10)
        ok, reason = pb._budget_can_take(0.10)
        self.assertFalse(ok)
        self.assertIn("cycle_cap", reason)

    def test_daily_exposure_cap(self):
        # Reset cycle count so per-cycle cap doesn't trigger first
        pb._reset_cycle_budget()
        # Bypass the per-cycle cap by manually loading exposure
        with pb._cycle_budget_lock:
            pb._today_exposure_usd = pb.DAILY_EXPOSURE_CAP_USD - 0.05
        ok, reason = pb._budget_can_take(0.10)  # would push over
        self.assertFalse(ok)
        self.assertIn("daily_cap", reason)
        ok, _ = pb._budget_can_take(0.04)  # fits
        self.assertTrue(ok)

    def test_utc_midnight_rolls_daily_exposure(self):
        with pb._cycle_budget_lock:
            pb._today_exposure_usd = 15.00
            pb._today_date_utc = "2026-01-01"  # stale
        pb._reset_cycle_budget()  # should detect rollover
        self.assertEqual(pb._today_exposure_usd, 0.0)
        self.assertNotEqual(pb._today_date_utc, "2026-01-01")


class TestWalletConfig(unittest.TestCase):
    def test_v2_constant_is_set(self):
        self.assertEqual(pb._KALSHI_KEY_ID_V2,
                         "7224fdb1-f5c9-4dc5-a1ce-b85013ad34d1")

    def test_wallet_default_is_v1(self):
        self.assertEqual(pb.WALLET, "v1")

    def test_live_config_caps_are_conservative(self):
        # Sanity: don't accidentally ship with $1000 caps.
        self.assertLessEqual(pb.MAX_BET_USD, 1.00)
        self.assertLessEqual(pb.DAILY_EXPOSURE_CAP_USD, 25.00)
        self.assertLessEqual(pb.MAX_NEW_POSITIONS_PER_CYCLE, 5)
        self.assertGreaterEqual(pb.MIN_EDGE, 0.20)


class TestObsCliBuffer(unittest.TestCase):
    """+1°F obs-vs-CLI buffer in calc_bracket_probability_min."""

    def test_running_min_just_above_cap_does_not_zero_probability(self):
        # Bracket [68,69], running_min=69.8 (0.8°F above cap).
        # Without the buffer, "bracket entirely above rm" zeros it out.
        # With +1°F buffer the bracket is treated as still possible.
        p = pb.calc_bracket_probability_min(
            mu=67.1, sigma=2.5, floor=68.0, cap=69.0, running_min=69.8,
        )
        self.assertGreater(p, 0.0,
            "running_min=69.8 vs cap=69.0 should leave nonzero P (CLI may round to 69)")

    def test_running_min_well_above_cap_still_zero(self):
        # 3°F above cap is well outside the +1°F buffer — still impossible.
        p = pb.calc_bracket_probability_min(
            mu=67.1, sigma=2.5, floor=70.0, cap=71.0, running_min=68.0,
        )
        self.assertEqual(p, 0.0,
            "floor=70 vs running_min=68 (and +1 buffer=69) should be impossible")

    def test_post_sunrise_sigma_widened_to_1F(self):
        # With running_min=70, bracket [68,69], post_sunrise_lock=True.
        # Sigma=1.0 (vs old 0.5) gives more probability mass within ±1°F.
        p = pb.calc_bracket_probability_min(
            mu=70.0, sigma=2.5,
            floor=68.0, cap=69.0,
            running_min=70.0, post_sunrise_lock=True,
        )
        # P(X in [68,69] | X ~ N(70, 1.0)) ≈ Φ(-1) - Φ(-2) ≈ 0.1587 - 0.0228 ≈ 0.136
        self.assertGreater(p, 0.10)
        self.assertLess(p, 0.25)


class TestPerEventCascade(unittest.TestCase):
    """Per-event cycle cap — don't pile correlated bracket bets in same city."""

    def setUp(self):
        with pb._cycle_budget_lock:
            pb._cycle_new_count = 0
            pb._today_exposure_usd = 0.0
            pb._today_date_utc = ""
            pb._cycle_event_counts = {}
        pb._reset_cycle_budget()

    def test_per_event_cap(self):
        ok, _ = pb._budget_can_take(0.10, "KXLOWTNYC-26APR25")
        self.assertTrue(ok)
        pb._budget_record(0.10, "KXLOWTNYC-26APR25")
        # Second entry on same event should be blocked.
        ok, reason = pb._budget_can_take(0.10, "KXLOWTNYC-26APR25")
        self.assertFalse(ok)
        self.assertIn("event_cap", reason)
        # But a different event should still be allowed.
        ok, _ = pb._budget_can_take(0.10, "KXLOWTCHI-26APR25")
        self.assertTrue(ok)


class TestCooldowns(unittest.TestCase):
    """Cooldown helpers ported from V2 H-2 fix."""

    def setUp(self):
        with pb._cooldown_lock:
            pb._paused_tickers.clear()
        pb._insufficient_balance_until = 0.0

    def test_paused_cooldown(self):
        self.assertFalse(pb._in_paused_cooldown("KXLOWTNYC-26APR25-T45"))
        pb._set_paused_cooldown("KXLOWTNYC-26APR25-T45")
        self.assertTrue(pb._in_paused_cooldown("KXLOWTNYC-26APR25-T45"))
        # Other tickers unaffected
        self.assertFalse(pb._in_paused_cooldown("KXLOWTCHI-26APR25-T45"))

    def test_insufficient_balance_cooldown(self):
        self.assertFalse(pb._in_insufficient_balance_cooldown())
        pb._set_insufficient_balance_cooldown()
        self.assertTrue(pb._in_insufficient_balance_cooldown())


class TestLiveSafetyConstants(unittest.TestCase):
    def test_max_edge_is_set(self):
        self.assertGreater(pb.MAX_EDGE, pb.MIN_EDGE)
        self.assertLess(pb.MAX_EDGE, 1.0)

    def test_disagreement_threshold_reasonable(self):
        self.assertGreater(pb.MAX_DISAGREEMENT_F, 2.0)
        self.assertLess(pb.MAX_DISAGREEMENT_F, 10.0)

    def test_per_event_per_cycle_is_one(self):
        self.assertEqual(pb.MAX_NEW_PER_EVENT_PER_CYCLE, 1)


class TestSettleKeepsDedupe(unittest.TestCase):
    """check_settlements must keep settled tickers in _open_positions so the
    per-ticker dedupe still blocks re-entry. Otherwise the cascade bug
    (50 orders, $21 lost on V2 wallet 04:09–04:26 UTC) recurs."""

    def setUp(self):
        # Fresh state per test
        with pb._positions_lock:
            pb._open_positions.clear()

    def test_settled_ticker_stays_in_open_positions(self):
        ticker = "KXLOWTNYC-26APR24-T48"
        pb._open_positions[ticker] = {
            "market_ticker": ticker, "action": "BUY_NO",
            "entry_price": 0.07, "count": 10,
            "station": "KNYC", "date_str": "2026-04-24",
            "floor": None, "cap": 47.5,
            "model_prob": 0.05, "mu": 50.0, "sigma": 2.5, "mu_source": "nbp",
            "running_min": 51.0, "label": "NYC",
        }

        # Stub get_cli_low + dirs/files so check_settlements has somewhere to write
        original_get_cli = pb.get_cli_low
        original_settlements_file = pb.SETTLEMENTS_FILE
        try:
            pb.get_cli_low = lambda station, date_str: 51 if (station, date_str) == ("KNYC", "2026-04-24") else None
            pb.SETTLEMENTS_FILE = Path(_TMPDIR) / "test_settlements.jsonl"
            n = pb.check_settlements()
        finally:
            pb.get_cli_low = original_get_cli
            pb.SETTLEMENTS_FILE = original_settlements_file

        self.assertEqual(n, 1)
        self.assertIn(ticker, pb._open_positions, "settled ticker must still be tracked for dedupe")
        self.assertTrue(pb._open_positions[ticker].get("settled"))
        self.assertEqual(pb._open_positions[ticker].get("cli_low"), 51)
        # NYC-T48 cap=47.5, cli=51 → not in bracket → BUY_NO won
        self.assertTrue(pb._open_positions[ticker].get("won"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
