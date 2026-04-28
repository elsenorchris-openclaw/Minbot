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
        self.assertLessEqual(pb.MAX_BET_USD, 25.00)
        self.assertLessEqual(pb.DAILY_EXPOSURE_CAP_USD, 100.00)
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

    def _mock_kalshi(self, positions, market_metadata=None):
        """Build a kalshi_get mock that returns positions on /portfolio/positions
        and market_metadata on /markets/{ticker}. Test fixtures for the T-tail
        reconcile fix call /markets/{ticker} after parsing the ticker."""
        market_metadata = market_metadata or {}
        def fake_get(path, params=None):
            if path == "/trade-api/v2/portfolio/positions":
                return {"market_positions": positions}
            if path.startswith("/trade-api/v2/markets/"):
                tk = path.split("/")[-1]
                return {"market": market_metadata.get(tk, {})}
            return {}
        return fake_get

    def test_recovers_ghost_b_bracket_position(self):
        """B-bracket position: parse_market_bracket sets floor/cap directly,
        no market-metadata fetch needed."""
        pb.kalshi_get = self._mock_kalshi([{
            "ticker": "KXLOWTNYC-26APR25-B45.5",
            "position_fp": -3.0,
            "market_exposure_dollars": 0.21,
            "last_updated_ts": "2026-04-25T05:00:00Z",
        }])
        added = pb._reconcile_kalshi_positions()
        self.assertEqual(added, 1)
        self.assertIn("KXLOWTNYC-26APR25-B45.5", pb._open_positions)
        rec = pb._open_positions["KXLOWTNYC-26APR25-B45.5"]
        self.assertEqual(rec["action"], "BUY_NO")
        self.assertEqual(rec["count"], 3)
        self.assertEqual(rec["floor"], 45.0)
        self.assertEqual(rec["cap"], 46.0)
        self.assertEqual(rec["station"], "KNYC")
        self.assertEqual(rec["date_str"], "2026-04-25")
        self.assertTrue(rec["_recovered_from_kalshi"])

    def test_recovers_t_high_with_subtitle_resolution(self):
        """T-high tail (e.g. T48 = '49° or above'): parse_market_bracket alone
        leaves floor=cap=None — that produces inverted won-flag at settlement
        (in_bracket defaults True). Audit 2026-04-27 caught CHI-T48 BUY_YES
        recording +$8.28 phantom win when truth was -$0.78 loss. Fix: fetch
        market metadata, parse 'X° or above' → floor=X-0.5."""
        pb.kalshi_get = self._mock_kalshi(
            [{"ticker": "KXLOWTCHI-26APR25-T48", "position_fp": 9.0,
              "market_exposure_dollars": 0.72}],
            {"KXLOWTCHI-26APR25-T48": {"yes_sub_title": "49° or above"}}
        )
        added = pb._reconcile_kalshi_positions()
        self.assertEqual(added, 1)
        rec = pb._open_positions["KXLOWTCHI-26APR25-T48"]
        self.assertEqual(rec["action"], "BUY_YES")
        self.assertEqual(rec["floor"], 48.5)
        self.assertIsNone(rec["cap"])

    def test_recovers_t_low_with_subtitle_resolution(self):
        """T-low tail (e.g. T47 = '46° or below'): parse 'X° or below' → cap=X+0.5."""
        pb.kalshi_get = self._mock_kalshi(
            [{"ticker": "KXLOWTLAX-26APR27-T47", "position_fp": -2.0,
              "market_exposure_dollars": 0.40}],
            {"KXLOWTLAX-26APR27-T47": {"yes_sub_title": "46° or below"}}
        )
        added = pb._reconcile_kalshi_positions()
        self.assertEqual(added, 1)
        rec = pb._open_positions["KXLOWTLAX-26APR27-T47"]
        self.assertEqual(rec["action"], "BUY_NO")
        self.assertIsNone(rec["floor"])
        self.assertEqual(rec["cap"], 46.5)

    def test_t_tail_skipped_when_metadata_fetch_fails(self):
        """If yes_sub_title fetch fails or returns empty, the position must
        NOT be added with floor=cap=None — that would re-introduce the
        inversion bug. Skip and log a warning."""
        pb.kalshi_get = self._mock_kalshi(
            [{"ticker": "KXLOWTCHI-26APR25-T48", "position_fp": 9.0,
              "market_exposure_dollars": 0.72}],
            {"KXLOWTCHI-26APR25-T48": {}}  # empty metadata, no subtitle
        )
        added = pb._reconcile_kalshi_positions()
        self.assertEqual(added, 0)
        self.assertNotIn("KXLOWTCHI-26APR25-T48", pb._open_positions)

    def test_t_tail_skipped_on_unparseable_subtitle(self):
        """Subtitle present but doesn't match 'X° or above/below' → skip."""
        pb.kalshi_get = self._mock_kalshi(
            [{"ticker": "KXLOWTCHI-26APR25-T48", "position_fp": 9.0,
              "market_exposure_dollars": 0.72}],
            {"KXLOWTCHI-26APR25-T48": {"yes_sub_title": "garbled"}}
        )
        added = pb._reconcile_kalshi_positions()
        self.assertEqual(added, 0)

    def test_skips_already_tracked(self):
        pb._open_positions["KXLOWTNYC-26APR25-B45.5"] = {"market_ticker": "KXLOWTNYC-26APR25-B45.5"}
        pb.kalshi_get = self._mock_kalshi([{
            "ticker": "KXLOWTNYC-26APR25-B45.5",
            "position_fp": -3.0, "market_exposure_dollars": 0.21,
        }])
        added = pb._reconcile_kalshi_positions()
        self.assertEqual(added, 0)

    def test_skips_zero_positions(self):
        pb.kalshi_get = self._mock_kalshi([{
            "ticker": "KXLOWTNYC-26APR25-B45.5",
            "position_fp": 0.0, "market_exposure_dollars": 0.0,
        }])
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
    where mp ≥ 50% lost 5/5 (-$1.53). BUY_YES with mp ≤ 50% lost CHI-T41
    and NYC-T44 (mp 34% / 42%) — gate tightened 2026-04-27 from 0.50 →
    0.40/0.60 to drop the coin-flip dead zone.

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
        # Series KXLOWTBOS chosen so MSG WORST_CITIES tier doesn't fire.
        # mu_nbp/hrrr/nbm_om all OUTSIDE bracket so MSG consensus passes.
        base = {
            "market_ticker": "KXLOWTBOS-26APR25-B45.5",
            "event_ticker": "KXLOWTBOS-26APR25",
            "action": action,
            "model_prob": model_prob,
            "edge": 0.25,
            "entry_price": 0.30 if action == "BUY_NO" else 0.10,
            "yes_bid": 65, "yes_ask": 70,
            "no_bid": 28, "no_ask": 32,
            "mu": 43.0 if action == "BUY_NO" else 45.5,
            "mu_nbp": 43.0 if action == "BUY_NO" else 45.5,
            "mu_hrrr": 43.0 if action == "BUY_NO" else 45.5,
            "mu_nbm_om": 43.0 if action == "BUY_NO" else 45.5,
            "sigma": 2.5, "mu_source": "nws_primary",
            "running_min": None, "post_sunrise_lock": False,
            "disagreement": 1.0,
            "floor": 45.0, "cap": 46.0,
            "station": "KBOS", "date_str": "2026-04-25", "label": "Boston",
            "series": "KXLOWTBOS",
        }
        base.update(overrides)
        return base

    def test_buy_no_with_high_mp_blocked(self):
        """BUY_NO with mp > 40% must be skipped — model says YES is too likely.
        (At 0.55 BOTH directional and F2A would block; either is fine.)"""
        pb.execute_opportunity(self._opp("BUY_NO", 0.55))
        self.assertEqual(self._order_calls, [], "place_kalshi_order should not have been called")

    def test_buy_no_with_low_mp_allowed_through_gate(self):
        """BUY_NO with mp in F2A's allowed range (0.05 ≤ mp < 0.30) passes."""
        pb.execute_opportunity(self._opp("BUY_NO", 0.20))
        self.assertEqual(len(self._order_calls), 1, "place_kalshi_order should have been called once")

    def test_buy_yes_with_low_mp_blocked(self):
        """BUY_YES with mp < 60% must be skipped — model says YES is not likely enough."""
        pb.execute_opportunity(self._opp("BUY_YES", 0.40))
        self.assertEqual(self._order_calls, [])

    def test_buy_yes_with_high_mp_allowed_through_gate(self):
        """BUY_YES with mp ≥ 60% reaches the order-placement step."""
        pb.execute_opportunity(self._opp("BUY_YES", 0.70))
        self.assertEqual(len(self._order_calls), 1)

    def test_exact_50_blocks_both_actions(self):
        """At mp=0.50 both directions remain blocked: BUY_NO requires mp ≤ 0.40,
        BUY_YES requires mp ≥ 0.60. The dead zone 0.40–0.60 admits neither."""
        pb.execute_opportunity(self._opp("BUY_NO", 0.50))
        pb.execute_opportunity(self._opp("BUY_YES", 0.50))
        self.assertEqual(self._order_calls, [])


class TestAbsDistanceGate(unittest.TestCase):
    """ABS DISTANCE GATE (BUY_NO only): skip when |mu − bracket_mid| < 1.5°F.
    Ported from V2 max-bot at 1.0°F; tightened to 1.5°F on 2026-04-27.
    The directional consistency filter alone catches the "model agrees with
    bracket" case; ABS DISTANCE catches "model is so near the bracket boundary
    that small forecast error flips outcome" even when model_prob looks low.
    Losses driving the tighten: PHIL-B44.5 (mp=18%, abs_dist 0.1°F) and
    ATL-B61.5 (mp=19%, abs_dist 0.5°F)."""

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
        # mp default slips under F2A_PROB_HI (0.30) for BUY_NO. mu_*=None
        # disables MSG (this test class targets ABS DISTANCE in isolation).
        if model_prob is None:
            model_prob = 0.20 if action == "BUY_NO" else 0.65
        base = {
            "market_ticker": "KXLOWTBOS-26APR25-B45.5",
            "event_ticker": "KXLOWTBOS-26APR25",
            "action": action,
            "model_prob": model_prob,
            "edge": 0.25,
            "entry_price": 0.30 if action == "BUY_NO" else 0.10,
            "yes_bid": 65, "yes_ask": 70,
            "no_bid": 28, "no_ask": 32,
            "mu": mu, "sigma": 2.5, "mu_source": "nws_primary",
            "mu_nbp": None, "mu_hrrr": None, "mu_nbm_om": None,
            "running_min": None, "post_sunrise_lock": False,
            "disagreement": 1.0,
            "floor": floor, "cap": cap,
            "station": "KBOS", "date_str": "2026-04-25", "label": "Boston",
            "series": "KXLOWTBOS",
        }
        base.update(overrides)
        return base

    def test_constant_is_0_5(self):
        """Reverted from 1.5 → 0.5 on 2026-04-27 PM after Kalshi audit
        showed 1.5°F blocked 9/9 winners with dist 0.5–1.5°F (mu at edge
        but bracket misses on actual cli)."""
        self.assertEqual(pb.MIN_ABS_DISTANCE_F, 0.5)

    def test_buy_no_blocked_at_bracket_center(self):
        """PHIL-B44.5 reconstruction: mu exactly at bracket midpoint."""
        pb.execute_opportunity(self._opp("BUY_NO", mu=44.5, floor=44.0, cap=45.0))
        self.assertEqual(self._order_calls, [])

    def test_buy_no_blocked_just_under_threshold(self):
        """mu 0.1°F from midpoint must block."""
        pb.execute_opportunity(self._opp("BUY_NO", mu=44.6, floor=44.0, cap=45.0))
        self.assertEqual(self._order_calls, [])

    def test_buy_no_passes_at_old_1_0_threshold(self):
        """ATL-B61.5 with 1.0°F dist now PASSES under reverted 0.5°F gate.
        Audit data showed at-edge BUY_NO is the winner pattern (cli flips
        outside the bracket from there in 6/6 audit cases)."""
        pb.execute_opportunity(self._opp("BUY_NO", mu=43.5, floor=44.0, cap=45.0))
        self.assertEqual(len(self._order_calls), 1)

    def test_buy_no_passes_at_threshold(self):
        """mu exactly 0.5°F from midpoint passes (strict less-than)."""
        pb.execute_opportunity(self._opp("BUY_NO", mu=44.0, floor=44.0, cap=45.0))
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
    """Cap evolution: $1/$4 (live launch) → $3/$30 (2026-04-26 AM) →
    $3/$60 (2026-04-27 AM, MIN_COST_USD pairing) →
    $5/$60 (2026-04-27 PM, [$1, $5] envelope) →
    $10/$60 (2026-04-27 evening, post-V2-port wins) →
    $15/$60 (2026-04-28, after SATX-T75 +$9.10 Kelly-sized winner)."""

    def test_max_bet_is_15(self):
        self.assertEqual(pb.MAX_BET_USD, 15.00)

    def test_daily_cap_is_60(self):
        """Bumped from $30 → $60 on 2026-04-27 to absorb the ~2× capital
        deployment from the MIN_COST_USD floor without starving cycle 2 onward."""
        self.assertEqual(pb.DAILY_EXPOSURE_CAP_USD, 60.00)

    def test_min_cost_usd_is_1(self):
        """$1 cost floor enforced via ceil(MIN_COST_USD / price) post-Kelly.
        Every fill ≥ $1 (capped at MAX_BET_USD=$5)."""
        self.assertEqual(pb.MIN_COST_USD, 1.00)

    def test_min_under_max_envelope(self):
        """Chris's spec 2026-04-27: every position ∈ [$1, $5]. Floor must be
        ≤ ceiling, otherwise sizing logic gets clamped weirdly."""
        self.assertLess(pb.MIN_COST_USD, pb.MAX_BET_USD)


class TestSizingMinCostFloor(unittest.TestCase):
    """The $1 cost floor ensures every Kelly-sized entry deploys ≥ MIN_COST_USD,
    regardless of how int(bet_usd / price) rounded down. The cap at MAX_BET_USD
    keeps the floor from blowing the ceiling on cheap contracts."""

    def setUp(self):
        with pb._positions_lock:
            pb._open_positions.clear()
        with pb._cycle_budget_lock:
            pb._cycle_new_count = 0
            pb._today_exposure_usd = 0.0
            pb._today_date_utc = ""
        pb._reset_cycle_budget()
        self._captured: list[tuple] = []
        self._orig_place = pb.place_kalshi_order
        # Capture (ticker, side, count, price_cents) — that's the call signature.
        pb.place_kalshi_order = lambda ticker, side, count, price_cents: (
            self._captured.append((ticker, side, count, price_cents)) or None
        )

    def tearDown(self):
        pb.place_kalshi_order = self._orig_place

    def _opp(self, action, price, edge=0.25, model_prob=None, mu=None,
             floor=44.0, cap=45.0, **overrides):
        if model_prob is None:
            model_prob = 0.20 if action == "BUY_NO" else 0.70
        if mu is None:
            # 3°F from bracket mid keeps ABS DISTANCE GATE clear.
            mid = (floor + cap) / 2.0 if (floor is not None and cap is not None) else (floor or cap)
            mu = mid - 3.0 if action == "BUY_NO" else mid
        # Build matching ask cents on the side we're buying.
        ask_cents = int(round(price * 100))
        base = {
            "market_ticker": "KXLOWTNYC-26APR25-B45.5",
            "event_ticker": "KXLOWTNYC-26APR25",
            "action": action,
            "model_prob": model_prob,
            "edge": edge,
            "entry_price": price,
            "yes_bid": ask_cents - 3 if action == "BUY_YES" else 65,
            "yes_ask": ask_cents if action == "BUY_YES" else 70,
            "no_bid": ask_cents - 3 if action == "BUY_NO" else 28,
            "no_ask": ask_cents if action == "BUY_NO" else 32,
            "mu": mu, "sigma": 2.5, "mu_source": "nws_primary",
            "running_min": None, "post_sunrise_lock": False,
            "disagreement": 1.0,
            "floor": floor, "cap": cap,
            "station": "KNYC", "date_str": "2026-04-25", "label": "New York",
        }
        base.update(overrides)
        return base

    def test_low_price_sized_to_at_least_one_dollar(self):
        """price=5c, edge=20% → Kelly bet_usd ~ $0.19 floored to $0.50,
        int(0.50/0.05)=10 → cost $0.50. Ceil(1/0.05)=20 bumps count to 20 → $1.00."""
        pb.execute_opportunity(self._opp("BUY_NO", price=0.05, edge=0.25))
        self.assertEqual(len(self._captured), 1)
        _, _, count, price_cents = self._captured[0]
        self.assertGreaterEqual(count * price_cents, 100,
                                f"intended cost {count*price_cents}c < 100c")

    def test_mid_price_sized_to_at_least_one_dollar(self):
        """price=30c → ceil(1/0.30)=4 → 4×30c=$1.20 ≥ $1."""
        pb.execute_opportunity(self._opp("BUY_NO", price=0.30, edge=0.25))
        self.assertEqual(len(self._captured), 1)
        _, _, count, price_cents = self._captured[0]
        self.assertGreaterEqual(count * price_cents, 100)

    def test_high_price_sized_to_at_least_one_dollar(self):
        """price=70c → ceil(1/0.70)=2 → 2×70c=$1.40 ≥ $1 (and ≤ $3)."""
        pb.execute_opportunity(self._opp("BUY_YES", price=0.70, edge=0.25, model_prob=0.85))
        self.assertEqual(len(self._captured), 1)
        _, _, count, price_cents = self._captured[0]
        self.assertGreaterEqual(count * price_cents, 100)
        self.assertLessEqual(count * price_cents, int(round(pb.MAX_BET_USD * 100)))

    def test_floor_does_not_blow_max_bet_cap(self):
        """price=10c, edge near MAX_EDGE → Kelly wants high count. Floor must
        not push past MAX_BET_USD cap. ceil(1/0.10)=10 → $1.00. But Kelly with
        edge=0.40 might want more; cap clamp keeps it at int(3/0.10)=30 → $3.00."""
        pb.execute_opportunity(self._opp("BUY_NO", price=0.10, edge=0.40))
        self.assertEqual(len(self._captured), 1)
        _, _, count, price_cents = self._captured[0]
        self.assertLessEqual(count * price_cents, int(round(pb.MAX_BET_USD * 100)),
                             f"intended cost {count*price_cents}c > MAX_BET_USD")
        self.assertGreaterEqual(count * price_cents, 100)

    def test_count_is_always_at_least_one(self):
        """Even when MIN_COST_USD/price < 1 (e.g. price=$2 hypothetical),
        count must be at least 1 contract."""
        # price > $1 hypothetical: ceil(1/0.95)=2, but MAX_BET cap of int(3/0.95)=3.
        # We can't easily test price>$1 since Kalshi has no such contracts, but
        # the math is: count >= 1 always.
        pb.execute_opportunity(self._opp("BUY_YES", price=0.85, edge=0.25, model_prob=0.85))
        self.assertEqual(len(self._captured), 1)
        _, _, count, _ = self._captured[0]
        self.assertGreaterEqual(count, 1)


class TestDirectionalGateTightened(unittest.TestCase):
    """Directional gate tightened from 0.50 → 0.40/0.60 on 2026-04-27.
    BUY_NO requires mp ≤ 0.40; BUY_YES requires mp ≥ 0.60."""

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
        base = {
            "market_ticker": "KXLOWTNYC-26APR25-B45.5",
            "event_ticker": "KXLOWTNYC-26APR25",
            "action": action,
            "model_prob": model_prob,
            "edge": 0.25,
            "entry_price": 0.30 if action == "BUY_NO" else 0.10,
            "yes_bid": 65, "yes_ask": 70,
            "no_bid": 28, "no_ask": 32,
            "mu": 41.5 if action == "BUY_NO" else 45.5,
            "sigma": 2.5, "mu_source": "nws_primary",
            "running_min": None, "post_sunrise_lock": False,
            "disagreement": 1.0,
            "floor": 45.0, "cap": 46.0,
            "station": "KNYC", "date_str": "2026-04-25", "label": "New York",
        }
        base.update(overrides)
        return base

    def test_buy_no_at_old_boundary_now_blocked(self):
        """mp=0.45 was admitted by the old 0.50 gate; now blocked."""
        pb.execute_opportunity(self._opp("BUY_NO", 0.45))
        self.assertEqual(self._order_calls, [])

    def test_buy_no_just_above_new_threshold_blocked(self):
        """mp=0.41 must block."""
        pb.execute_opportunity(self._opp("BUY_NO", 0.41))
        self.assertEqual(self._order_calls, [])

    def test_buy_no_below_f2a_threshold_passes(self):
        """Effective BUY_NO threshold is now F2A_PROB_HI=0.30 (stricter than
        directional 0.40). mp=0.29 below F2A → both gates pass."""
        pb.execute_opportunity(self._opp("BUY_NO", 0.29))
        self.assertEqual(len(self._order_calls), 1)

    def test_buy_yes_at_old_boundary_now_blocked(self):
        """CHI-T41 reconstruction: mp=0.34 BUY_YES lost. Now blocked."""
        pb.execute_opportunity(self._opp("BUY_YES", 0.34))
        self.assertEqual(self._order_calls, [])

    def test_buy_yes_just_below_new_threshold_blocked(self):
        """mp=0.59 must block (NYC-T44 reconstruction at mp=0.42 also blocked)."""
        pb.execute_opportunity(self._opp("BUY_YES", 0.59))
        self.assertEqual(self._order_calls, [])

    def test_buy_yes_at_new_threshold_passes(self):
        """mp=0.60 is the threshold (strict less-than blocks)."""
        pb.execute_opportunity(self._opp("BUY_YES", 0.60))
        self.assertEqual(len(self._order_calls), 1)


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


# ═══════════════════════════════════════════════════════════════════════
# V2 PORTS (2026-04-27 PM): Kelly anchor, F2A, MSG, obs_alive, hard stop
# ═══════════════════════════════════════════════════════════════════════

class TestKellyAnchorBankroll(unittest.TestCase):
    """Pre-fix: bet_usd = kelly * MAX_BET_USD (anchored to cap, sized as if
    bankroll = $5). Post-fix: bet_usd = kelly * bankroll, capped at MAX_BET_USD."""

    def setUp(self):
        with pb._positions_lock:
            pb._open_positions.clear()
        with pb._cycle_budget_lock:
            pb._cycle_new_count = 0
            pb._today_exposure_usd = 0.0
            pb._today_date_utc = ""
        pb._reset_cycle_budget()
        # Stub bankroll cache to a known value (avoid kalshi_get call)
        pb._cached_bankroll = 25.0
        pb._bankroll_cache_ts = 9999999999  # never expire during test
        self._captured: list[tuple] = []
        self._orig_place = pb.place_kalshi_order
        pb.place_kalshi_order = lambda ticker, side, count, price_cents: (
            self._captured.append((ticker, side, count, price_cents)) or None
        )

    def tearDown(self):
        pb.place_kalshi_order = self._orig_place
        pb._cached_bankroll = 0.0
        pb._bankroll_cache_ts = 0.0

    def _opp(self, **overrides):
        base = {
            "market_ticker": "KXLOWTNYC-26APR25-B45.5",
            "event_ticker": "KXLOWTNYC-26APR25",
            "action": "BUY_NO",
            "model_prob": 0.20,
            "edge": 0.25,
            "entry_price": 0.50,
            "yes_bid": 47, "yes_ask": 50,
            "no_bid": 48, "no_ask": 50,
            "mu": 41.0, "sigma": 2.5, "mu_source": "nbp",
            "running_min": None, "post_sunrise_lock": False,
            "disagreement": 1.0,
            "floor": 45.0, "cap": 46.0,
            "station": "KNYC", "date_str": "2026-04-25", "label": "New York",
            "series": "KXLOWTNYC",
        }
        base.update(overrides)
        return base

    def test_bankroll_25_sizes_above_old_max_bet_anchor(self):
        """With $25 bankroll, edge 25%, price 50c → kelly 0.125 → bet_usd
        $3.13 (5 contracts × 50c = $2.50 max integer). Pre-fix would have
        produced $0.625 → 1 contract → $0.50 (then floored to $1)."""
        pb.execute_opportunity(self._opp())
        self.assertEqual(len(self._captured), 1)
        _, _, count, price_cents = self._captured[0]
        cost = count * price_cents
        self.assertGreater(cost, 100, f"expected cost >$1, got {cost}c")
        # Should reach beyond the $1 floor (at 25% bankroll Kelly produces $3+ bet)
        self.assertGreaterEqual(cost, 200, f"expected cost ≥$2 with $25 bankroll, got {cost}c")

    def test_bankroll_5_keeps_old_behavior(self):
        """With $5 bankroll (≈ MAX_BET_USD), behavior matches pre-fix anchor —
        every fill hits the $1 floor."""
        pb._cached_bankroll = 5.0
        pb.execute_opportunity(self._opp())
        self.assertEqual(len(self._captured), 1)
        _, _, count, price_cents = self._captured[0]
        cost = count * price_cents
        # Floor enforced
        self.assertGreaterEqual(cost, 100)

    def test_max_bet_usd_caps_kelly(self):
        """Even with high bankroll + high Kelly, never exceed MAX_BET_USD."""
        pb._cached_bankroll = 1000.0
        pb.execute_opportunity(self._opp(edge=0.40))
        self.assertEqual(len(self._captured), 1)
        _, _, count, price_cents = self._captured[0]
        cost = count * price_cents
        self.assertLessEqual(cost, int(round(pb.MAX_BET_USD * 100)))


class TestObsConfirmedAlive(unittest.TestCase):
    """`_check_obs_confirmed_alive`: rm has decisively settled the bracket.
    When True at entry: forecast gates bypassed, Kelly × SIGNAL_KELLY_MULT,
    edge floor drops to OBS_ALIVE_MIN_EDGE."""

    def setUp(self):
        with pb._positions_lock:
            pb._open_positions.clear()
        with pb._cycle_budget_lock:
            pb._cycle_new_count = 0
            pb._today_exposure_usd = 0.0
            pb._today_date_utc = ""
        pb._reset_cycle_budget()
        pb._cached_bankroll = 25.0
        pb._bankroll_cache_ts = 9999999999
        self._captured: list[tuple] = []
        self._orig_place = pb.place_kalshi_order
        pb.place_kalshi_order = lambda ticker, side, count, price_cents: (
            self._captured.append((ticker, side, count, price_cents)) or None
        )

    def tearDown(self):
        pb.place_kalshi_order = self._orig_place
        pb._cached_bankroll = 0.0
        pb._bankroll_cache_ts = 0.0

    def test_buy_no_bbracket_alive_when_rm_well_below_floor(self):
        """B-bracket [44,45] with rm=40 (4°F below floor): rm<floor−3 → alive."""
        opp = {"action": "BUY_NO", "floor": 44.0, "cap": 45.0, "running_min": 40.0}
        self.assertTrue(pb._check_obs_confirmed_alive(opp))

    def test_buy_no_bbracket_not_alive_at_2f_below_floor(self):
        """B-bracket [44,45] with rm=42 (2°F below floor): too close, not alive."""
        opp = {"action": "BUY_NO", "floor": 44.0, "cap": 45.0, "running_min": 42.0}
        self.assertFalse(pb._check_obs_confirmed_alive(opp))

    def test_buy_no_thigh_alive_when_rm_far_below_floor(self):
        """T-high (floor=57.5, cap=None): rm=53 → rm<floor−3 → alive."""
        opp = {"action": "BUY_NO", "floor": 57.5, "cap": None, "running_min": 53.0}
        self.assertTrue(pb._check_obs_confirmed_alive(opp))

    def test_buy_yes_tlow_alive_when_rm_below_cap_minus_buffer(self):
        """T-low (cap=43.5, floor=None): rm=42 (≤ cap−1) → YES wins → alive."""
        opp = {"action": "BUY_YES", "floor": None, "cap": 43.5, "running_min": 42.0}
        self.assertTrue(pb._check_obs_confirmed_alive(opp))

    def test_buy_yes_tlow_not_alive_at_cap_boundary(self):
        """rm=43 with cap=43.5 → rm=cap−0.5, NOT ≤ cap−1.0 (1°F obs/CLI buffer)."""
        opp = {"action": "BUY_YES", "floor": None, "cap": 43.5, "running_min": 43.0}
        self.assertFalse(pb._check_obs_confirmed_alive(opp))

    def test_buy_yes_thigh_never_alive_min_temp(self):
        """T-high (floor=56.5, cap=None): rm=60.08 above floor+1.0 even
        post-sunrise should NOT confirm alive on a min-temp market.
        rm is monotonically decreasing — evening cooling can drop temp below
        the threshold before LST midnight. Reconstructs the KOKC-26APR28-T56
        phantom (rm=60.08 at 16:04Z bypassed all gates; NBP next-day forecast
        45°F)."""
        opp = {"action": "BUY_YES", "floor": 56.5, "cap": None,
               "running_min": 60.08, "tz": "America/Chicago"}
        self.assertFalse(pb._check_obs_confirmed_alive(opp))

    def test_buy_yes_thigh_never_alive_even_far_above(self):
        """T-high with rm 10°F above floor and post-sunrise: still no bypass."""
        opp = {"action": "BUY_YES", "floor": 50.0, "cap": None,
               "running_min": 60.0, "tz": "America/New_York"}
        self.assertFalse(pb._check_obs_confirmed_alive(opp))

    def test_no_rm_returns_false(self):
        """No rm → no obs evidence → not alive."""
        opp = {"action": "BUY_NO", "floor": 44.0, "cap": 45.0, "running_min": None}
        self.assertFalse(pb._check_obs_confirmed_alive(opp))

    def test_alive_bypasses_directional_gate(self):
        """Entry path: BUY_NO with mp=0.60 (would normally block via directional
        gate at 0.40) but rm well below bracket → alive → bypass → enters."""
        opp = {
            "market_ticker": "KXLOWTNYC-26APR25-B45.5",
            "event_ticker": "KXLOWTNYC-26APR25",
            "action": "BUY_NO", "model_prob": 0.60, "edge": 0.10,
            "entry_price": 0.50, "yes_bid": 47, "yes_ask": 50,
            "no_bid": 48, "no_ask": 50, "mu": 50.0, "sigma": 2.5,
            "mu_source": "nbp", "running_min": 40.0,  # well below bracket
            "post_sunrise_lock": False, "disagreement": 1.0,
            "floor": 45.0, "cap": 46.0,
            "station": "KNYC", "date_str": "2026-04-25", "label": "NYC",
            "series": "KXLOWTNYC",
        }
        pb.execute_opportunity(opp)
        self.assertEqual(len(self._captured), 1, "obs_alive should bypass the 0.40 directional gate")

    def test_alive_lowers_edge_floor(self):
        """Edge=0.06 (below MIN_EDGE=0.20) but obs_alive → uses
        OBS_ALIVE_MIN_EDGE=0.05 → enters."""
        opp = {
            "market_ticker": "KXLOWTNYC-26APR25-B45.5",
            "event_ticker": "KXLOWTNYC-26APR25",
            "action": "BUY_NO", "model_prob": 0.20, "edge": 0.06,
            "entry_price": 0.50, "yes_bid": 47, "yes_ask": 50,
            "no_bid": 48, "no_ask": 50, "mu": 41.0, "sigma": 2.5,
            "mu_source": "nbp", "running_min": 40.0,
            "post_sunrise_lock": False, "disagreement": 1.0,
            "floor": 45.0, "cap": 46.0,
            "station": "KNYC", "date_str": "2026-04-25", "label": "NYC",
            "series": "KXLOWTNYC",
        }
        pb.execute_opportunity(opp)
        self.assertEqual(len(self._captured), 1, "obs_alive should lower edge floor to 0.05")

    def test_okc_t56_replay_blocked_post_fix(self):
        """KOKC-26APR28-T56 reconstruction: rm=60.08, floor=56.5, BUY_YES,
        edge=12.4% (below MIN_EDGE=20%). Pre-fix: obs_alive bypassed gates and
        Kelly-boosted to 23×$0.16. Post-fix: T-high BUY_YES never triggers
        obs_alive on min markets → edge_floor stays at MIN_EDGE → blocked."""
        opp = {
            "market_ticker": "KXLOWTOKC-26APR28-T56",
            "event_ticker": "KXLOWTOKC-26APR28",
            "action": "BUY_YES", "model_prob": 0.284, "edge": 0.124,
            "entry_price": 0.16, "yes_bid": 14, "yes_ask": 16,
            "no_bid": 84, "no_ask": 86, "mu": 54.9, "sigma": 3.0,
            "mu_source": "hrrr", "running_min": 60.08,
            "post_sunrise_lock": False, "disagreement": 2.0,
            "floor": 56.5, "cap": None,
            "station": "KOKC", "date_str": "2026-04-28", "label": "Oklahoma City",
            "series": "KXLOWTOKC", "tz": "America/Chicago",
            "is_today": True,
        }
        pb.execute_opportunity(opp)
        self.assertEqual(self._captured, [],
                         "post-fix: T-high BUY_YES below MIN_EDGE should be "
                         "blocked (no obs_alive bypass on min-temp T-high)")


class TestObsConfirmedLoser(unittest.TestCase):
    """`_check_obs_confirmed_loser`: pre-empt entries where rm has already
    moved against our action. Mirror of `_check_obs_confirmed_alive`. Fires
    before forecast gates (only when alive is False — they're symmetric)."""

    def test_buy_no_bbracket_loser_when_rm_in_bracket(self):
        """B-bracket [44,45], rm=44.5 (currently in bracket → YES winning)."""
        opp = {"action": "BUY_NO", "floor": 44.0, "cap": 45.0, "running_min": 44.5}
        self.assertTrue(pb._check_obs_confirmed_loser(opp))

    def test_buy_no_bbracket_not_loser_when_rm_outside(self):
        opp_below = {"action": "BUY_NO", "floor": 44.0, "cap": 45.0, "running_min": 43.0}
        opp_above = {"action": "BUY_NO", "floor": 44.0, "cap": 45.0, "running_min": 46.0}
        self.assertFalse(pb._check_obs_confirmed_loser(opp_below))
        self.assertFalse(pb._check_obs_confirmed_loser(opp_above))

    def test_buy_no_thigh_loser_when_rm_above_floor(self):
        """LAX-T54 reconstruction (cost $3.44 round-trip): floor=54.5, rm=57.2.
        Bot bought BUY_NO based on HRRR mu=53.1; gate would have blocked it."""
        opp = {"action": "BUY_NO", "floor": 54.5, "cap": None, "running_min": 57.2}
        self.assertTrue(pb._check_obs_confirmed_loser(opp))

    def test_buy_no_thigh_not_loser_when_rm_at_floor(self):
        """rm exactly at floor (not strictly greater) — borderline OK."""
        opp = {"action": "BUY_NO", "floor": 54.5, "cap": None, "running_min": 54.5}
        self.assertFalse(pb._check_obs_confirmed_loser(opp))

    def test_buy_no_tlow_loser_when_rm_at_or_below_cap(self):
        """T-low cap=43.5 (YES if cli ≤ 43): rm=43 → loser; rm=44 → not."""
        opp_loser = {"action": "BUY_NO", "floor": None, "cap": 43.5, "running_min": 43.0}
        opp_safe = {"action": "BUY_NO", "floor": None, "cap": 43.5, "running_min": 44.0}
        self.assertTrue(pb._check_obs_confirmed_loser(opp_loser))
        self.assertFalse(pb._check_obs_confirmed_loser(opp_safe))

    def test_buy_yes_bbracket_loser_when_rm_below_floor(self):
        """B-bracket: rm went below floor; rm only goes down → YES (in bracket) lost."""
        opp = {"action": "BUY_YES", "floor": 44.0, "cap": 45.0, "running_min": 43.0}
        self.assertTrue(pb._check_obs_confirmed_loser(opp))

    def test_buy_yes_thigh_loser_when_rm_below_floor(self):
        """T-high (YES if cli ≥ X, floor=X-0.5): rm < floor → low went below
        threshold, can't recover. YES lost."""
        opp = {"action": "BUY_YES", "floor": 54.5, "cap": None, "running_min": 53.0}
        self.assertTrue(pb._check_obs_confirmed_loser(opp))

    def test_no_rm_returns_false(self):
        """No obs evidence → not loser (default-allow)."""
        self.assertFalse(pb._check_obs_confirmed_loser(
            {"action": "BUY_NO", "floor": 44.0, "cap": 45.0, "running_min": None}))

    def test_loser_blocks_entry_in_execute_opportunity(self):
        """End-to-end: LAX-T54 reconstruction. rm=57.2 should block at the
        OBS_CONFIRMED_LOSER gate before any forecast gate fires."""
        with pb._positions_lock:
            pb._open_positions.clear()
        with pb._cycle_budget_lock:
            pb._cycle_new_count = 0
            pb._today_exposure_usd = 0.0
            pb._today_date_utc = ""
        pb._reset_cycle_budget()
        pb._cached_bankroll = 25.0
        pb._bankroll_cache_ts = 9999999999
        captured: list[tuple] = []
        orig_place = pb.place_kalshi_order
        pb.place_kalshi_order = lambda *a, **kw: (captured.append((a, kw)) or None)
        try:
            opp = {
                "market_ticker": "KXLOWTLAX-26APR27-T54",
                "event_ticker": "KXLOWTLAX-26APR27",
                "action": "BUY_NO", "model_prob": 0.24, "edge": 0.20,
                "entry_price": 0.56, "yes_bid": 42, "yes_ask": 44,
                "no_bid": 54, "no_ask": 56,
                "mu": 53.1, "sigma": 2.0, "mu_source": "hrrr",
                "running_min": 57.2,  # rm WELL above floor 54.5 (T-high)
                "post_sunrise_lock": False, "disagreement": 1.0,
                "floor": 54.5, "cap": None,
                "station": "KLAX", "date_str": "2026-04-27", "label": "LA",
                "series": "KXLOWTLAX",
            }
            pb.execute_opportunity(opp)
            self.assertEqual(captured, [],
                             "OBS_CONFIRMED_LOSER should block — bot held at floor before "
                             "forecast gates evaluate")
        finally:
            pb.place_kalshi_order = orig_place
            pb._cached_bankroll = 0.0


class TestF2AGate(unittest.TestCase):
    """F2A asymmetry gate (BUY_NO only). 4 sub-checks: prob_lo, prob_hi,
    sigma_min, dist_min. All return None on pass, str on block."""

    def _opp(self, **overrides):
        base = {
            "action": "BUY_NO",
            "model_prob": 0.20,
            "sigma": 2.5,
            "mu": 40.0,
            "floor": 44.0, "cap": 45.0,
        }
        base.update(overrides)
        return base

    def test_buy_yes_not_gated(self):
        self.assertIsNone(pb._check_f2a_gate(self._opp(action="BUY_YES")))

    def test_passes_normal_case(self):
        # mp=0.20, sigma=2.5, mu=40 → dist to floor=44 = 4°F. All pass.
        self.assertIsNone(pb._check_f2a_gate(self._opp()))

    def test_blocks_prob_too_low(self):
        r = pb._check_f2a_gate(self._opp(model_prob=0.04))
        self.assertIsNotNone(r)
        self.assertIn("price-asymmetry", r)

    def test_blocks_prob_too_high(self):
        r = pb._check_f2a_gate(self._opp(model_prob=0.30))
        self.assertIsNotNone(r)
        self.assertIn("F2A", r)
        self.assertIn("YES too likely", r)

    def test_blocks_sigma_too_tight(self):
        r = pb._check_f2a_gate(self._opp(sigma=1.4))
        self.assertIsNotNone(r)
        self.assertIn("sigma", r.lower())

    def test_at_edge_mu_passes(self):
        """V2 F2A blocks mu within 0.5°F of nearest edge; min-bot does NOT
        port that (audit found at-edge mu is the BUY_NO sweet spot — mu=44.0
        with bracket [44,45] is mu *at floor*, which won 6/6 in audit data)."""
        # mu=44.0 with bracket [44,45] → at floor edge. Should pass F2A.
        self.assertIsNone(pb._check_f2a_gate(self._opp(mu=44.0)))


class TestMSGGate(unittest.TestCase):
    """Multi-source consensus (BUY_NO only). Counts how many of {NBP, HRRR,
    NBM} predict YES (loss for us). Per-city tiers + outlier margin cap."""

    def _opp(self, **overrides):
        base = {
            "action": "BUY_NO",
            "floor": 44.0, "cap": 45.0,
            "series": "KXLOWTBOS",  # not in MSG_WORST_CITIES
            "mu_nbp": 40.0, "mu_hrrr": 41.0, "mu_nbm_om": 39.0,  # all outside bracket
        }
        base.update(overrides)
        return base

    def test_buy_yes_not_gated(self):
        self.assertIsNone(pb._check_msg_gate(self._opp(action="BUY_YES")))

    def test_zero_sources_in_yes_passes(self):
        self.assertIsNone(pb._check_msg_gate(self._opp()))

    def test_one_source_in_yes_passes_default(self):
        # Default max_consensus = 2; 1 source in YES = pass
        self.assertIsNone(pb._check_msg_gate(self._opp(mu_nbp=44.5)))  # in [44,45]

    def test_two_sources_in_yes_passes_default(self):
        # Default cities allow up to 2 disagreeing sources
        self.assertIsNone(pb._check_msg_gate(self._opp(mu_nbp=44.5, mu_hrrr=44.5)))

    def test_three_sources_in_yes_blocks_default(self):
        # All 3 in YES → > MSG_MAX_CONSENSUS_DEFAULT (2) → block
        r = pb._check_msg_gate(self._opp(mu_nbp=44.5, mu_hrrr=44.5, mu_nbm_om=44.5))
        self.assertIsNotNone(r)
        self.assertIn("MSG", r)

    def test_worst_city_blocks_at_one_source(self):
        # WORST_CITIES require unanimity (max=0). 1 source in YES → block.
        r = pb._check_msg_gate(self._opp(series="KXLOWTNYC", mu_nbp=44.5))
        self.assertIsNotNone(r)

    def test_outlier_margin_blocks(self):
        # Single source 4°F into bracket center (margin > 3) → block
        r = pb._check_msg_gate(self._opp(mu_nbp=44.5, mu_hrrr=39.0, mu_nbm_om=39.0))
        # mu_nbp=44.5 in [44,45], margin = max(0.5, 0.5) = 0.5; not over 3
        # need wider bracket or stronger outlier
        opp = self._opp(floor=44.0, cap=50.0, mu_nbp=47.0, mu_hrrr=39.0, mu_nbm_om=39.0)
        # margin = max(47-44, 50-47) = 3 (not > 3, just =, so skip this assertion)
        # Force higher margin:
        opp = {"action": "BUY_NO", "floor": 44.0, "cap": 60.0,
               "series": "KXLOWTBOS",
               "mu_nbp": 50.0, "mu_hrrr": 39.0, "mu_nbm_om": 39.0}
        # margin = max(50-44, 60-50) = 10 > 3 → block via margin
        # but yes_count = 1 (only nbp in yes), default max=2 → no consensus block
        # margin sub-check should fire
        r = pb._check_msg_gate(opp)
        self.assertIsNotNone(r, f"expected margin block on 10°F outlier, got None")

    def test_t_high_counts_sources_above_floor(self):
        # T-high (floor=57.5, cap=None): YES if mu ≥ 57.5
        r = pb._check_msg_gate({
            "action": "BUY_NO", "floor": 57.5, "cap": None,
            "series": "KXLOWTBOS",
            "mu_nbp": 60.0, "mu_hrrr": 60.0, "mu_nbm_om": 60.0,
        })
        self.assertIsNotNone(r, "all sources above T-high floor → consensus block")

    def test_t_low_counts_sources_below_cap(self):
        # T-low (cap=43.5, floor=None): YES if mu ≤ 43.5
        r = pb._check_msg_gate({
            "action": "BUY_NO", "floor": None, "cap": 43.5,
            "series": "KXLOWTBOS",
            "mu_nbp": 40.0, "mu_hrrr": 40.0, "mu_nbm_om": 40.0,
        })
        self.assertIsNotNone(r, "all sources below T-low cap → consensus block")

    def test_too_few_sources_skips_gate(self):
        # < 2 sources → can't evaluate consensus → pass
        self.assertIsNone(pb._check_msg_gate({
            "action": "BUY_NO", "floor": 44.0, "cap": 45.0,
            "series": "KXLOWTBOS",
            "mu_nbp": 44.5, "mu_hrrr": None, "mu_nbm_om": None,
        }))


class TestPositionExitLogic(unittest.TestCase):
    """check_open_positions_for_exit: hard-stop trigger + obs-winner override."""

    def setUp(self):
        with pb._positions_lock:
            pb._open_positions.clear()
        self._orig_place = pb.place_kalshi_sell_order
        self._orig_wait = pb.wait_for_fill
        self._orig_save = pb._save_positions
        self._orig_send = pb.discord_send
        self._orig_append = pb._append_jsonl
        self._captured_orders: list[tuple] = []
        self._captured_records: list[dict] = []
        pb.place_kalshi_sell_order = lambda ticker, side, count, price_cents: (
            self._captured_orders.append((ticker, side, count, price_cents)) or "FAKE_OID"
        )
        pb.wait_for_fill = lambda oid, expected, timeout: ("filled", expected)
        pb._save_positions = lambda: None
        pb.discord_send = lambda msg: None
        pb._append_jsonl = lambda path, rec: self._captured_records.append(rec)

    def tearDown(self):
        pb.place_kalshi_sell_order = self._orig_place
        pb.wait_for_fill = self._orig_wait
        pb._save_positions = self._orig_save
        pb.discord_send = self._orig_send
        pb._append_jsonl = self._orig_append

    def _make_pos(self, ticker, action, entry_price, count, **overrides):
        base = {
            "ts": "2026-04-27T01:00:00Z",
            "kind": "entry",
            "market_ticker": ticker,
            "action": action,
            "entry_price": entry_price,
            "count": count,
            "cost": entry_price * count,
            "station": "KNYC", "date_str": "2026-04-27", "label": "NYC",
            "tz": "America/New_York",
        }
        base.update(overrides)
        return base

    def test_hard_stop_fires_on_bracket_at_80pct_loss(self):
        """B-bracket position with MTM down 80% → hard stop fires."""
        pos = self._make_pos("KXLOWTNYC-26APR27-B45.5", "BUY_NO", 0.50, 2,
                             floor=45.0, cap=46.0)
        with pb._positions_lock:
            pb._open_positions[pos["market_ticker"]] = pos
        # Current bid 10c → loss = (50−10)/50 = 80% → exit
        markets = {pos["market_ticker"]: {"market_ticker": pos["market_ticker"],
                                          "yes_bid": 90, "no_bid": 10}}
        n = pb.check_open_positions_for_exit(markets)
        self.assertEqual(n, 1)
        self.assertEqual(len(self._captured_orders), 1)
        ticker, side, count, price_c = self._captured_orders[0]
        self.assertEqual(side, "no")
        self.assertEqual(count, 2)
        self.assertEqual(price_c, 10)
        # Exit record written
        self.assertEqual(len(self._captured_records), 1)
        self.assertEqual(self._captured_records[0]["kind"], "exit")
        self.assertEqual(self._captured_records[0]["reason"], "hard_stop")

    def test_hard_stop_does_not_fire_at_70pct_loss_on_bracket(self):
        """B-bracket loss 70% < 80% threshold → no exit."""
        pos = self._make_pos("KXLOWTNYC-26APR27-B45.5", "BUY_NO", 0.50, 2,
                             floor=45.0, cap=46.0)
        with pb._positions_lock:
            pb._open_positions[pos["market_ticker"]] = pos
        markets = {pos["market_ticker"]: {"market_ticker": pos["market_ticker"],
                                          "yes_bid": 85, "no_bid": 15}}
        n = pb.check_open_positions_for_exit(markets)
        self.assertEqual(n, 0)
        self.assertEqual(len(self._captured_orders), 0)

    def test_hard_stop_fires_on_tail_at_70pct_loss(self):
        """Tail (single-bound) uses tail threshold 70% (lottery payoff justifies tighter)."""
        pos = self._make_pos("KXLOWTNYC-26APR27-T48", "BUY_YES", 0.20, 5,
                             floor=48.5, cap=None)  # T-high with single floor
        with pb._positions_lock:
            pb._open_positions[pos["market_ticker"]] = pos
        # Loss = (20 − 6) / 20 = 70% → exit
        markets = {pos["market_ticker"]: {"market_ticker": pos["market_ticker"],
                                          "yes_bid": 6, "no_bid": 94}}
        n = pb.check_open_positions_for_exit(markets)
        self.assertEqual(n, 1)

    def test_obs_winner_override_blocks_hard_stop(self):
        """Position deeply MTM-loss but rm confirms our side wins → HOLD, no exit."""
        pos = self._make_pos("KXLOWTNYC-26APR27-B45.5", "BUY_NO", 0.50, 2,
                             floor=45.0, cap=46.0,
                             running_min=40.0)  # rm below floor − 1 → BUY_NO winner
        with pb._positions_lock:
            pb._open_positions[pos["market_ticker"]] = pos
        markets = {pos["market_ticker"]: {"market_ticker": pos["market_ticker"],
                                          "yes_bid": 99, "no_bid": 1}}
        n = pb.check_open_positions_for_exit(markets)
        self.assertEqual(n, 0, "obs winner should override hard stop")

    def test_buy_yes_thigh_no_winner_override_min_temp(self):
        """T-high BUY_YES with rm above floor: hold-side override REMOVED
        2026-04-28. rm is monotonically decreasing on min markets — current
        rm above floor doesn't survive overnight cooling. Hard stop should
        run normally instead of holding."""
        pos = self._make_pos("KXLOWTOKC-26APR28-T56", "BUY_YES", 0.50, 5,
                             floor=56.5, cap=None,
                             running_min=60.08,
                             tz="America/Chicago")
        with pb._positions_lock:
            pb._open_positions[pos["market_ticker"]] = pos
        # MTM loss 80% on a tail (>70% threshold for tails)
        markets = {pos["market_ticker"]: {"market_ticker": pos["market_ticker"],
                                          "yes_bid": 10, "no_bid": 90}}
        n = pb.check_open_positions_for_exit(markets)
        self.assertEqual(n, 1,
                         "T-high BUY_YES post-fix should NOT get the hold-side "
                         "override; hard_stop should fire on tail-loss ≥ 70%")

    def test_buy_yes_tlow_winner_override_still_works(self):
        """Regression: T-low BUY_YES winner case still overrides hard-stop.
        rm monotonically decreases — once below cap, position is locked."""
        pos = self._make_pos("KXLOWTBOS-26APR28-T40", "BUY_YES", 0.50, 5,
                             floor=None, cap=39.5,
                             running_min=38.0,  # ≤ cap − 1 → confirmed YES winner
                             tz="America/New_York")
        with pb._positions_lock:
            pb._open_positions[pos["market_ticker"]] = pos
        markets = {pos["market_ticker"]: {"market_ticker": pos["market_ticker"],
                                          "yes_bid": 5, "no_bid": 95}}
        n = pb.check_open_positions_for_exit(markets)
        self.assertEqual(n, 0, "T-low BUY_YES winner override unchanged by fix")

    def test_settled_positions_skipped(self):
        """settled=True positions are not re-evaluated."""
        pos = self._make_pos("KXLOWTNYC-26APR27-B45.5", "BUY_NO", 0.50, 2,
                             floor=45.0, cap=46.0, settled=True)
        with pb._positions_lock:
            pb._open_positions[pos["market_ticker"]] = pos
        markets = {pos["market_ticker"]: {"market_ticker": pos["market_ticker"],
                                          "yes_bid": 99, "no_bid": 1}}
        n = pb.check_open_positions_for_exit(markets)
        self.assertEqual(n, 0)

    def test_no_quote_skips_position(self):
        """Position whose ticker isn't in market quotes (e.g. settled on Kalshi
        but not yet in our settle loop) is skipped silently."""
        pos = self._make_pos("KXLOWTNYC-26APR27-B45.5", "BUY_NO", 0.50, 2,
                             floor=45.0, cap=46.0)
        with pb._positions_lock:
            pb._open_positions[pos["market_ticker"]] = pos
        n = pb.check_open_positions_for_exit({})
        self.assertEqual(n, 0)


class TestQuickWinGates(unittest.TestCase):
    """3 V2-inspired quick-win gates added 2026-04-27 evening:
       - MAX_EDGE bumped 0.40 → 0.42
       - PRICE_ZONE block: BUY_NO + yes_bid ∈ [30, 40]c
       - H_2.0: d-1+ BUY_NO + disagreement > 2°F"""

    def setUp(self):
        with pb._positions_lock:
            pb._open_positions.clear()
        with pb._cycle_budget_lock:
            pb._cycle_new_count = 0
            pb._today_exposure_usd = 0.0
            pb._today_date_utc = ""
        pb._reset_cycle_budget()
        pb._cached_bankroll = 25.0
        pb._bankroll_cache_ts = 9999999999
        self._captured: list[tuple] = []
        self._orig_place = pb.place_kalshi_order
        pb.place_kalshi_order = lambda *a, **kw: (
            self._captured.append((a, kw)) or None
        )

    def tearDown(self):
        pb.place_kalshi_order = self._orig_place
        pb._cached_bankroll = 0.0

    def _opp(self, **overrides):
        # Default: clean BUY_NO that should pass all gates.
        base = {
            "market_ticker": "KXLOWTBOS-26APR28-B45.5",
            "event_ticker": "KXLOWTBOS-26APR28",
            "action": "BUY_NO",
            "model_prob": 0.20,
            "edge": 0.30,
            "entry_price": 0.50,
            "yes_bid": 18, "yes_ask": 22,
            "no_bid": 76, "no_ask": 50,
            "mu": 41.0, "sigma": 2.5, "mu_source": "nbp",
            "mu_nbp": 41.0, "mu_hrrr": 41.0, "mu_nbm_om": 41.0,
            "running_min": None, "post_sunrise_lock": False,
            "is_today": True,  # day-0 by default — H_2.0 only fires on d-1+
            "disagreement": 0.5,
            "floor": 45.0, "cap": 46.0,
            "station": "KBOS", "date_str": "2026-04-28", "label": "Boston",
            "series": "KXLOWTBOS",
        }
        base.update(overrides)
        return base

    def test_max_edge_is_0_42(self):
        self.assertEqual(pb.MAX_EDGE, 0.42)

    def test_edge_above_0_42_blocked(self):
        """Edge 0.43 > MAX_EDGE 0.42 → block."""
        pb.execute_opportunity(self._opp(edge=0.43, no_ask=20))
        self.assertEqual(self._captured, [])

    def test_edge_at_0_42_passes(self):
        """Edge 0.42 = MAX_EDGE (strict greater-than blocks). Passes."""
        # Engineer mp + entry_price for edge exactly 0.42:
        # buy_no_edge = (1 - mp) - no_ask_frac. mp=0.18, no_ask=0.40 → 0.82-0.40=0.42
        opp = self._opp(model_prob=0.18, no_ask=40, no_bid=38, entry_price=0.40,
                        edge=0.42, yes_bid=58, yes_ask=62)
        pb.execute_opportunity(opp)
        self.assertEqual(len(self._captured), 1)

    def test_price_zone_blocks_buy_no_at_yes_bid_30(self):
        """yes_bid=30 → PRICE_ZONE block (BUY_NO only)."""
        pb.execute_opportunity(self._opp(yes_bid=30, yes_ask=35,
                                          no_bid=65, no_ask=70,
                                          entry_price=0.70, edge=0.10,
                                          model_prob=0.20))
        self.assertEqual(self._captured, [])

    def test_price_zone_blocks_buy_no_at_yes_bid_40(self):
        """yes_bid=40 → PRICE_ZONE block (boundary, inclusive)."""
        pb.execute_opportunity(self._opp(yes_bid=40, yes_ask=42,
                                          no_bid=58, no_ask=60,
                                          entry_price=0.60, edge=0.20,
                                          model_prob=0.20))
        self.assertEqual(self._captured, [])

    def test_price_zone_does_not_block_at_yes_bid_29(self):
        """yes_bid=29 → just below zone, passes other gates."""
        opp = self._opp(yes_bid=29, yes_ask=33, no_bid=67, no_ask=70,
                        entry_price=0.70, edge=0.10, model_prob=0.20)
        # edge=0.10 < MIN_EDGE will block; bump model_prob to get higher edge
        opp["edge"] = 0.25; opp["model_prob"] = 0.05  # mp<MIN_MODEL_PROB blocks though
        # Use mp=0.15 (= MIN_MODEL_PROB), no_ask=0.60 → edge=0.25
        opp["model_prob"] = 0.15; opp["no_ask"] = 60; opp["entry_price"] = 0.60
        opp["yes_bid"] = 29  # just below PRICE_ZONE
        pb.execute_opportunity(opp)
        self.assertEqual(len(self._captured), 1, "yes_bid=29 should pass PRICE_ZONE")

    def test_price_zone_does_not_block_buy_yes(self):
        """PRICE_ZONE is BUY_NO-only. BUY_YES with yes_bid=35 passes."""
        opp = self._opp(action="BUY_YES", model_prob=0.70,
                        yes_bid=35, yes_ask=40, no_bid=58, no_ask=65,
                        entry_price=0.40, edge=0.30,
                        mu=45.5)  # YES sweet spot
        pb.execute_opportunity(opp)
        self.assertEqual(len(self._captured), 1)

    def test_h_2_0_blocks_d1_buy_no_when_disagree(self):
        """D-1+ BUY_NO with disagreement 2.5°F > H_2_0_DISAGREE_F=2.0°F → block."""
        pb.execute_opportunity(self._opp(is_today=False, disagreement=2.5))
        self.assertEqual(self._captured, [])

    def test_h_2_0_does_not_block_d0(self):
        """D-0 (is_today=True) NOT subject to H_2.0 — running_min handles it."""
        pb.execute_opportunity(self._opp(is_today=True, disagreement=2.5))
        # Should pass all forecast gates including H_2.0
        self.assertEqual(len(self._captured), 1)

    def test_h_2_0_does_not_block_buy_yes(self):
        """H_2.0 is BUY_NO-only. BUY_YES with disagreement 3°F passes."""
        opp = self._opp(action="BUY_YES", model_prob=0.70, mu=45.5,
                        is_today=False, disagreement=3.0,
                        entry_price=0.30, edge=0.30,
                        yes_bid=27, yes_ask=30, no_bid=68, no_ask=70)
        pb.execute_opportunity(opp)
        self.assertEqual(len(self._captured), 1)

    def test_h_2_0_at_threshold_passes(self):
        """Disagreement = H_2_0_DISAGREE_F exactly — strict greater-than blocks."""
        pb.execute_opportunity(self._opp(is_today=False, disagreement=2.0))
        self.assertEqual(len(self._captured), 1)

    def test_obs_alive_bypasses_price_zone_and_h_2_0(self):
        """Obs-confirmed-alive overrides PRICE_ZONE and H_2.0 (forecast gates)."""
        opp = self._opp(
            yes_bid=35,                    # PRICE_ZONE territory
            no_ask=65, no_bid=63,
            entry_price=0.65,
            is_today=False,
            disagreement=3.5,              # H_2.0 territory
            running_min=40.0,              # rm well below floor 45 → obs_alive
        )
        # Trigger obs_alive: BUY_NO + B-bracket + rm < floor−3
        pb.execute_opportunity(opp)
        self.assertEqual(len(self._captured), 1, "obs_alive should bypass forecast gates")


if __name__ == "__main__":
    unittest.main(verbosity=2)
