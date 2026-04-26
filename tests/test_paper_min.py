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

import json
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
# CRITICAL: per-file Path constants are bound at module import time, before
# we can override DATA_DIR. Re-bind them now so tests that call
# _save_positions / _append_jsonl never write to production data files.
pb.POSITIONS_FILE = pb.DATA_DIR / "positions.json"
pb.TRADES_FILE = pb.DATA_DIR / "trades.jsonl"
pb.SETTLEMENTS_FILE = pb.DATA_DIR / "settlements.jsonl"
pb.STATS_FILE = pb.DATA_DIR / "stats.json"
pb.NBP_CACHE_FILE = pb.DATA_DIR / "nbp_cache.json"


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
        self.assertLessEqual(pb.MAX_BET_USD, 5.00)
        self.assertLessEqual(pb.DAILY_EXPOSURE_CAP_USD, 50.00)
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
    """Per-event lifetime cap — don't pile correlated bracket bets in same
    event. The cap counts against `_open_positions`, not a per-cycle dict,
    so once a bracket on an event is open, all other brackets on the same
    event are blocked until that one settles. CHI-26APR25 accumulated
    4 brackets across cycles 2026-04-25 under the prior per-cycle rule."""

    def setUp(self):
        with pb._cycle_budget_lock:
            pb._cycle_new_count = 0
            pb._today_exposure_usd = 0.0
            pb._today_date_utc = ""
        with pb._positions_lock:
            pb._open_positions.clear()
        pb._reset_cycle_budget()

    def _add_open(self, ticker, settled=False):
        with pb._positions_lock:
            pb._open_positions[ticker] = {
                "market_ticker": ticker, "kind": "entry",
                "settled": settled,
            }

    def test_per_event_cap_blocks_second_bracket(self):
        ok, _ = pb._budget_can_take(0.10, "KXLOWTNYC-26APR25")
        self.assertTrue(ok)
        self._add_open("KXLOWTNYC-26APR25-T48")
        # Different bracket on same event should be blocked.
        ok, reason = pb._budget_can_take(0.10, "KXLOWTNYC-26APR25")
        self.assertFalse(ok)
        self.assertIn("event_cap", reason)
        # But a different event should still be allowed.
        ok, _ = pb._budget_can_take(0.10, "KXLOWTCHI-26APR25")
        self.assertTrue(ok)

    def test_event_cap_holds_across_cycles(self):
        """The pre-fix bug: per-cycle counter wiped every scan. CHI-26APR25
        accumulated B47.5 → T48 → B43.5 → T41 across 4 cycles."""
        self._add_open("KXLOWTCHI-26APR25-B47.5")
        # Simulate scan_cycle()'s reset — must NOT clear the event guard.
        pb._reset_cycle_budget()
        ok, reason = pb._budget_can_take(0.10, "KXLOWTCHI-26APR25")
        self.assertFalse(ok)
        self.assertIn("event_cap", reason)

    def test_settled_position_does_not_block(self):
        """Once a position settles, the event slot frees up for the next day."""
        self._add_open("KXLOWTNYC-26APR25-T48", settled=True)
        ok, _ = pb._budget_can_take(0.10, "KXLOWTNYC-26APR25")
        self.assertTrue(ok)

    def test_open_count_helper_counts_only_unsettled(self):
        self._add_open("KXLOWTNYC-26APR25-T48", settled=False)
        self._add_open("KXLOWTNYC-26APR25-B45.5", settled=True)
        self._add_open("KXLOWTCHI-26APR25-T48", settled=False)
        self.assertEqual(pb._open_count_for_event("KXLOWTNYC-26APR25"), 1)
        self.assertEqual(pb._open_count_for_event("KXLOWTCHI-26APR25"), 1)
        self.assertEqual(pb._open_count_for_event("KXLOWTSEA-26APR25"), 0)
        self.assertEqual(pb._open_count_for_event(""), 0)


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

    def test_max_open_per_event_is_one(self):
        self.assertEqual(pb.MAX_OPEN_PER_EVENT, 1)


class TestSettleKeepsDedupe(unittest.TestCase):
    """check_settlements must keep settled tickers in _open_positions so the
    per-ticker dedupe still blocks re-entry. Otherwise the cascade bug
    (50 orders, $21 lost on V2 wallet 04:09–04:26 UTC) recurs."""

    def setUp(self):
        # Fresh state per test
        with pb._positions_lock:
            pb._open_positions.clear()

    def test_already_settled_position_is_not_reprocessed(self):
        """check_settlements must skip positions already marked settled.
        Without this, every cycle re-writes settlement records + spams Discord."""
        ticker = "KXLOWTNYC-26APR24-T48"
        pb._open_positions[ticker] = {
            "market_ticker": ticker, "action": "BUY_NO",
            "entry_price": 0.07, "count": 10,
            "station": "KNYC", "date_str": "2026-04-24",
            "floor": None, "cap": 47.5,
            "running_min": 51.0, "label": "NYC",
            "settled": True, "cli_low": 51, "won": True, "pnl": 9.30,
        }
        original_get_cli = pb.get_cli_low
        original_settlements_file = pb.SETTLEMENTS_FILE
        try:
            pb.get_cli_low = lambda station, date_str: 51
            pb.SETTLEMENTS_FILE = Path(_TMPDIR) / "test_settlements_2.jsonl"
            n = pb.check_settlements()
        finally:
            pb.get_cli_low = original_get_cli
            pb.SETTLEMENTS_FILE = original_settlements_file
        self.assertEqual(n, 0, "already-settled positions must not be reprocessed")

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


class TestDailyExposurePersistence(unittest.TestCase):
    """DAILY_EXPOSURE_CAP_USD must survive bot restarts: compute today's
    spend from trades.jsonl on startup."""

    def setUp(self):
        self._tmp_trades = Path(_TMPDIR) / "test_trades.jsonl"
        if self._tmp_trades.exists():
            self._tmp_trades.unlink()
        self._orig_trades_file = pb.TRADES_FILE
        pb.TRADES_FILE = self._tmp_trades

    def tearDown(self):
        pb.TRADES_FILE = self._orig_trades_file

    def _write(self, records):
        with open(self._tmp_trades, "w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

    def test_returns_zero_when_no_file(self):
        if self._tmp_trades.exists():
            self._tmp_trades.unlink()
        self.assertEqual(pb._compute_today_exposure(), 0.0)

    def test_sums_only_today_entries(self):
        from datetime import datetime, timezone, timedelta
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        self._write([
            {"ts": f"{today}T00:00:00+00:00", "kind": "entry", "cost": 0.50, "date_str": today},
            {"ts": f"{today}T01:00:00+00:00", "kind": "entry", "cost": 0.75, "date_str": today},
            {"ts": f"{yesterday}T23:00:00+00:00", "kind": "entry", "cost": 1.00, "date_str": yesterday},  # excluded: not today
            {"ts": f"{today}T02:00:00+00:00", "kind": "candidate", "cost": 0.25, "date_str": today},  # excluded: not entry
            {"ts": f"{today}T03:00:00+00:00", "kind": "entry", "cost": 99.99, "mode": "PAPER", "date_str": today},  # excluded: paper
            # The wallet-scope filter: an entry placed today on a market whose
            # date_str is yesterday (V2 cascade pattern) is excluded.
            {"ts": f"{today}T04:00:00+00:00", "kind": "entry", "cost": 21.00, "date_str": yesterday},
        ])
        self.assertAlmostEqual(pb._compute_today_exposure(), 1.25, places=2)


class TestKalshiReconcile(unittest.TestCase):
    """Reconcile Kalshi positions into _open_positions on startup so ghost
    positions (Kalshi has them, bot doesn't) get tracked."""

    def setUp(self):
        with pb._positions_lock:
            pb._open_positions.clear()
        self._orig_get = pb.kalshi_get
        self._orig_save = pb._save_positions
        pb._save_positions = lambda: None  # don't write disk in test

    def tearDown(self):
        pb.kalshi_get = self._orig_get
        pb._save_positions = self._orig_save

    def test_recovers_ghost_position(self):
        # Kalshi shows 1 NO position on a ticker bot doesn't track
        pb.kalshi_get = lambda path, params=None: {
            "market_positions": [{
                "ticker": "KXLOWTNYC-26APR25-T48",
                "position_fp": -3.0,
                "market_exposure_dollars": 0.21,
                "last_updated_ts": "2026-04-25T05:00:00Z",
            }]
        }
        added = pb._reconcile_kalshi_positions()
        self.assertEqual(added, 1)
        self.assertIn("KXLOWTNYC-26APR25-T48", pb._open_positions)
        rec = pb._open_positions["KXLOWTNYC-26APR25-T48"]
        self.assertEqual(rec["action"], "BUY_NO")
        self.assertEqual(rec["count"], 3)
        self.assertEqual(rec["station"], "KNYC")
        self.assertEqual(rec["date_str"], "2026-04-25")
        self.assertTrue(rec["_recovered_from_kalshi"])

    def test_skips_already_tracked(self):
        pb._open_positions["KXLOWTNYC-26APR25-T48"] = {"market_ticker": "KXLOWTNYC-26APR25-T48"}
        pb.kalshi_get = lambda path, params=None: {
            "market_positions": [{
                "ticker": "KXLOWTNYC-26APR25-T48",
                "position_fp": -3.0, "market_exposure_dollars": 0.21,
            }]
        }
        added = pb._reconcile_kalshi_positions()
        self.assertEqual(added, 0)

    def test_skips_zero_positions(self):
        pb.kalshi_get = lambda path, params=None: {
            "market_positions": [{
                "ticker": "KXLOWTNYC-26APR25-T48",
                "position_fp": 0.0, "market_exposure_dollars": 0.0,
            }]
        }
        added = pb._reconcile_kalshi_positions()
        self.assertEqual(added, 0)


class TestTradesLogReconcile(unittest.TestCase):
    """`_reconcile_from_trades_log` closes the Kalshi /portfolio/positions
    API-lag window after deploy churn. KXLOWTCHI-26APR25-T48 was bought
    twice on 2026-04-25 (16 min apart, between back-to-back deploy restarts)
    because positions.json was clobbered AND Kalshi hadn't propagated the
    fill before the per-ticker dedupe ran."""

    def setUp(self):
        with pb._positions_lock:
            pb._open_positions.clear()
        self._tmp_trades = Path(_TMPDIR) / "tlog_trades.jsonl"
        self._tmp_settles = Path(_TMPDIR) / "tlog_settles.jsonl"
        for p in (self._tmp_trades, self._tmp_settles):
            if p.exists():
                p.unlink()
        self._orig_trades = pb.TRADES_FILE
        self._orig_settles = pb.SETTLEMENTS_FILE
        self._orig_save = pb._save_positions
        pb.TRADES_FILE = self._tmp_trades
        pb.SETTLEMENTS_FILE = self._tmp_settles
        pb._save_positions = lambda: None

    def tearDown(self):
        pb.TRADES_FILE = self._orig_trades
        pb.SETTLEMENTS_FILE = self._orig_settles
        pb._save_positions = self._orig_save

    def _write_trades(self, records):
        with open(self._tmp_trades, "w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

    def _write_settles(self, records):
        with open(self._tmp_settles, "w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

    def _today(self):
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _yday(self):
        from datetime import datetime, timezone, timedelta
        return (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

    def test_no_trades_file_returns_zero(self):
        if self._tmp_trades.exists():
            self._tmp_trades.unlink()
        self.assertEqual(pb._reconcile_from_trades_log(), 0)

    def test_recovers_today_entry_missing_from_open_positions(self):
        today = self._today()
        self._write_trades([{
            "ts": f"{today}T05:27:57.721+00:00", "kind": "entry",
            "market_ticker": "KXLOWTCHI-26APR25-T48", "action": "BUY_YES",
            "entry_price": 0.08, "count": 6, "cost": 0.48,
            "station": "KMDW", "date_str": "2026-04-25", "label": "Chicago",
            "floor": 48.5, "cap": None,
        }])
        added = pb._reconcile_from_trades_log()
        self.assertEqual(added, 1)
        self.assertIn("KXLOWTCHI-26APR25-T48", pb._open_positions)
        rec = pb._open_positions["KXLOWTCHI-26APR25-T48"]
        self.assertTrue(rec["_recovered_from_trades_log"])
        self.assertEqual(rec["action"], "BUY_YES")
        self.assertEqual(rec["count"], 6)

    def test_skips_already_in_open_positions(self):
        today = self._today()
        with pb._positions_lock:
            pb._open_positions["KXLOWTCHI-26APR25-T48"] = {"market_ticker": "KXLOWTCHI-26APR25-T48"}
        self._write_trades([{
            "ts": f"{today}T05:27:57+00:00", "kind": "entry",
            "market_ticker": "KXLOWTCHI-26APR25-T48", "action": "BUY_YES",
            "entry_price": 0.08, "count": 6, "cost": 0.48,
        }])
        added = pb._reconcile_from_trades_log()
        self.assertEqual(added, 0)

    def test_skips_settled_tickers(self):
        today = self._today()
        self._write_trades([{
            "ts": f"{today}T05:27:57+00:00", "kind": "entry",
            "market_ticker": "KXLOWTNYC-26APR25-T48", "action": "BUY_NO",
            "entry_price": 0.07, "count": 10, "cost": 0.70,
        }])
        self._write_settles([{
            "ts": f"{today}T11:00:00+00:00", "kind": "settlement",
            "market_ticker": "KXLOWTNYC-26APR25-T48",
        }])
        added = pb._reconcile_from_trades_log()
        self.assertEqual(added, 0)
        self.assertNotIn("KXLOWTNYC-26APR25-T48", pb._open_positions)

    def test_skips_yesterday_entries(self):
        """Yesterday's open positions should already be picked up by
        `_reconcile_kalshi_positions` if still on Kalshi; if not, they
        settled. Either way, the trade-log fallback is for *today's*
        propagation lag only."""
        yday = self._yday()
        self._write_trades([{
            "ts": f"{yday}T05:27:57+00:00", "kind": "entry",
            "market_ticker": "KXLOWTCHI-26APR24-T48", "action": "BUY_YES",
            "entry_price": 0.08, "count": 6, "cost": 0.48,
        }])
        added = pb._reconcile_from_trades_log()
        self.assertEqual(added, 0)

    def test_skips_paper_mode(self):
        today = self._today()
        self._write_trades([{
            "ts": f"{today}T05:27:57+00:00", "kind": "entry",
            "mode": "PAPER",
            "market_ticker": "KXLOWTCHI-26APR25-T48", "action": "BUY_YES",
            "entry_price": 0.08, "count": 6, "cost": 0.48,
        }])
        added = pb._reconcile_from_trades_log()
        self.assertEqual(added, 0)

    def test_skips_non_entry_kinds(self):
        today = self._today()
        self._write_trades([
            {"ts": f"{today}T05:00:00+00:00", "kind": "candidate",
             "market_ticker": "KXLOWTCHI-26APR25-T48"},
            {"ts": f"{today}T05:30:00+00:00", "kind": "settlement",
             "market_ticker": "KXLOWTCHI-26APR25-T48"},
        ])
        added = pb._reconcile_from_trades_log()
        self.assertEqual(added, 0)

    def test_dedupes_multiple_entries_to_latest(self):
        """If trades.jsonl has two entry records for the same ticker (the
        exact bug we're guarding against), recovery adds it once."""
        today = self._today()
        self._write_trades([
            {"ts": f"{today}T05:27:57+00:00", "kind": "entry",
             "market_ticker": "KXLOWTCHI-26APR25-T48",
             "action": "BUY_YES", "entry_price": 0.08, "count": 6, "cost": 0.48},
            {"ts": f"{today}T05:43:05+00:00", "kind": "entry",
             "market_ticker": "KXLOWTCHI-26APR25-T48",
             "action": "BUY_YES", "entry_price": 0.08, "count": 3, "cost": 0.24},
        ])
        added = pb._reconcile_from_trades_log()
        self.assertEqual(added, 1)


class TestDirectionalConsistency(unittest.TestCase):
    """Don't bet against your own model. The MIN_EDGE/MAX_EDGE math assumes
    a calibrated model; with the +1.24°F NBP-cool bias we see in min_bot,
    edge alone misleads when action and model direction disagree.

    First-day data 2026-04-26 (cascade-corrected n=21): BUY_NO entries
    where mp ≥ 50% lost 5/5 (-$1.53). BUY_YES with mp ≤ 50% lost the
    one trade in sample (CHI-T41).

    Test pattern: stub `place_kalshi_order` to a sentinel that records
    when it was called. If the gate blocked, place_kalshi_order is never
    called. If the gate passed, the order attempt is recorded (then we
    return None to fail-fast out of execute_opportunity)."""

    def setUp(self):
        with pb._positions_lock:
            pb._open_positions.clear()
        with pb._cycle_budget_lock:
            pb._cycle_new_count = 0
            pb._today_exposure_usd = 0.0
            pb._today_date_utc = ""
        pb._reset_cycle_budget()
        self._order_calls: list[tuple] = []
        self._orig_place = pb.place_kalshi_order
        pb.place_kalshi_order = lambda *a, **kw: (
            self._order_calls.append((a, kw)) or None
        )

    def tearDown(self):
        pb.place_kalshi_order = self._orig_place

    def _opp(self, action, model_prob, **overrides):
        # mu chosen 2.5°F from bracket midpoint (45.5) so ABS DISTANCE GATE
        # doesn't fire — these tests target the directional filter only.
        base = {
            "market_ticker": "KXLOWTNYC-26APR25-B45.5",
            "event_ticker": "KXLOWTNYC-26APR25",
            "action": action,
            "model_prob": model_prob,
            "edge": 0.25,
            "entry_price": 0.30 if action == "BUY_NO" else 0.10,
            "yes_bid": 65, "yes_ask": 70,
            "no_bid": 28, "no_ask": 32,
            "mu": 43.0 if action == "BUY_NO" else 45.5,
            "sigma": 2.5, "mu_source": "nws_primary",
            "running_min": None, "post_sunrise_lock": False,
            "disagreement": 1.0,
            "floor": 45.0, "cap": 46.0,
            "station": "KNYC", "date_str": "2026-04-25", "label": "New York",
        }
        base.update(overrides)
        return base

    def test_buy_no_with_high_mp_blocked(self):
        """BUY_NO with mp ≥ 50% must be skipped — model says YES is more likely."""
        pb.execute_opportunity(self._opp("BUY_NO", 0.55))
        self.assertEqual(self._order_calls, [], "place_kalshi_order should not have been called")

    def test_buy_no_with_low_mp_allowed_through_gate(self):
        """BUY_NO with mp < 50% (and mu far enough from bracket) reaches order placement."""
        pb.execute_opportunity(self._opp("BUY_NO", 0.30))
        self.assertEqual(len(self._order_calls), 1, "place_kalshi_order should have been called once")

    def test_buy_yes_with_low_mp_blocked(self):
        """BUY_YES with mp ≤ 50% must be skipped — model says NO is more likely."""
        pb.execute_opportunity(self._opp("BUY_YES", 0.40))
        self.assertEqual(self._order_calls, [])

    def test_buy_yes_with_high_mp_allowed_through_gate(self):
        """BUY_YES with mp > 50% reaches the order-placement step."""
        pb.execute_opportunity(self._opp("BUY_YES", 0.70))
        self.assertEqual(len(self._order_calls), 1)

    def test_exact_50_blocks_both_actions(self):
        """At mp=0.50 the model is indifferent — block both directions
        rather than betting on a coin flip."""
        pb.execute_opportunity(self._opp("BUY_NO", 0.50))
        pb.execute_opportunity(self._opp("BUY_YES", 0.50))
        self.assertEqual(self._order_calls, [])


class TestAbsDistanceGate(unittest.TestCase):
    """ABS DISTANCE GATE (BUY_NO only): skip when |mu − bracket_mid| < 1°F.
    Ported from V2 max-bot. The directional consistency filter alone catches
    the "model agrees with bracket" case; ABS DISTANCE catches "model is so
    near the bracket boundary that small forecast error flips outcome" even
    when model_prob looks low. PHIL-B44.5 lost on 2026-04-25 with mp=18%
    but mu=44.6 right on bracket midpoint 44.5."""

    def setUp(self):
        with pb._positions_lock:
            pb._open_positions.clear()
        with pb._cycle_budget_lock:
            pb._cycle_new_count = 0
            pb._today_exposure_usd = 0.0
            pb._today_date_utc = ""
        pb._reset_cycle_budget()
        self._order_calls: list[tuple] = []
        self._orig_place = pb.place_kalshi_order
        pb.place_kalshi_order = lambda *a, **kw: (
            self._order_calls.append((a, kw)) or None
        )

    def tearDown(self):
        pb.place_kalshi_order = self._orig_place

    def _opp(self, action, mu, floor, cap, model_prob=None, **overrides):
        if model_prob is None:
            model_prob = 0.30 if action == "BUY_NO" else 0.65
        base = {
            "market_ticker": "KXLOWTNYC-26APR25-B45.5",
            "event_ticker": "KXLOWTNYC-26APR25",
            "action": action,
            "model_prob": model_prob,
            "edge": 0.25,
            "entry_price": 0.30 if action == "BUY_NO" else 0.10,
            "yes_bid": 65, "yes_ask": 70,
            "no_bid": 28, "no_ask": 32,
            "mu": mu, "sigma": 2.5, "mu_source": "nws_primary",
            "running_min": None, "post_sunrise_lock": False,
            "disagreement": 1.0,
            "floor": floor, "cap": cap,
            "station": "KNYC", "date_str": "2026-04-25", "label": "New York",
        }
        base.update(overrides)
        return base

    def test_constant_is_one(self):
        self.assertEqual(pb.MIN_ABS_DISTANCE_F, 1.0)

    def test_buy_no_blocked_at_bracket_center(self):
        """PHIL-B44.5 reconstruction: mu exactly at bracket midpoint."""
        pb.execute_opportunity(self._opp("BUY_NO", mu=44.5, floor=44.0, cap=45.0))
        self.assertEqual(self._order_calls, [])

    def test_buy_no_blocked_just_under_threshold(self):
        """mu 0.1°F from midpoint must block."""
        pb.execute_opportunity(self._opp("BUY_NO", mu=44.6, floor=44.0, cap=45.0))
        self.assertEqual(self._order_calls, [])

    def test_buy_no_passes_at_threshold(self):
        """mu exactly 1.0°F from midpoint passes (strict less-than)."""
        pb.execute_opportunity(self._opp("BUY_NO", mu=43.5, floor=44.0, cap=45.0))
        self.assertEqual(len(self._order_calls), 1)

    def test_buy_no_passes_well_above_threshold(self):
        """mu 2°F from midpoint passes."""
        pb.execute_opportunity(self._opp("BUY_NO", mu=42.5, floor=44.0, cap=45.0))
        self.assertEqual(len(self._order_calls), 1)

    def test_buy_yes_not_gated_at_center(self):
        """BUY_YES at bracket center is the SWEET SPOT, must not be blocked."""
        pb.execute_opportunity(self._opp("BUY_YES", mu=44.5, floor=44.0, cap=45.0))
        self.assertEqual(len(self._order_calls), 1)

    def test_buy_no_tail_low_uses_cap_as_mid(self):
        """T-low (cap=47.5, no floor): mid is the cap. mu=47.4 → blocked."""
        pb.execute_opportunity(self._opp("BUY_NO", mu=47.4, floor=None, cap=47.5))
        self.assertEqual(self._order_calls, [])

    def test_buy_no_tail_low_passes_far(self):
        """T-low: mu well below cap is the BUY_NO sweet spot."""
        pb.execute_opportunity(self._opp("BUY_NO", mu=44.0, floor=None, cap=47.5))
        self.assertEqual(len(self._order_calls), 1)

    def test_buy_no_tail_high_uses_floor_as_mid(self):
        """T-high (floor=66.5, no cap): mid is the floor. mu=66.6 → blocked."""
        pb.execute_opportunity(self._opp("BUY_NO", mu=66.6, floor=66.5, cap=None))
        self.assertEqual(self._order_calls, [])


class TestCapBumpsApril26(unittest.TestCase):
    """Cap bump from $1/$15 to $3/$30 on 2026-04-26."""

    def test_max_bet_is_3(self):
        self.assertEqual(pb.MAX_BET_USD, 3.00)

    def test_daily_cap_is_30(self):
        self.assertEqual(pb.DAILY_EXPOSURE_CAP_USD, 30.00)


class TestRecordCandidateKind(unittest.TestCase):
    """record_candidate must write kind=candidate, not the bracket type."""

    def test_kind_is_candidate_not_bracket(self):
        # Capture what _append_jsonl receives
        captured = []
        orig = pb._append_jsonl
        pb._append_jsonl = lambda path, rec: captured.append(rec)
        try:
            pb.record_candidate({
                "kind": "bracket",  # opp's bracket kind
                "market_ticker": "KXLOWTNYC-26APR25-B42.5",
                "floor": 42.0, "cap": 43.0,
                "model_prob": 0.20, "edge": 0.10,
                "yes_bid": 5, "yes_ask": 10,
            })
        finally:
            pb._append_jsonl = orig
        self.assertEqual(len(captured), 1)
        rec = captured[0]
        self.assertEqual(rec["kind"], "candidate", "discriminator must be 'candidate'")
        self.assertEqual(rec["bracket_kind"], "bracket", "opp.kind preserved as bracket_kind")


if __name__ == "__main__":
    unittest.main(verbosity=2)
