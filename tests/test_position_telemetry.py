"""Tests for the 2026-05-02 RICH POSITION TELEMETRY addition.

Pure-additive feature: every open position gets a `live` sub-dict
(per-cycle snapshot) updated each scan, plus per-position high-water marks
(peak_mtm_pct / trough_mtm_pct / peak_running_min / peak_bid_side_c). Also
appends a per-cycle JSONL row to data/position_telemetry_YYYY-MM-DD.jsonl
for backtest replay.

Verify:
  1. _compute_position_telemetry pure (no mutation), uniform schema.
  2. Forecast re-resolver mirrors scan_and_trade priority logic.
  3. _update_open_positions_telemetry merges + saves + logs.
  4. Existing decision logic (entry / exit / settlement) untouched.
  5. Source-code regression guards on the call site in scan_and_trade.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

_TMPDIR = tempfile.mkdtemp(prefix="telemetry_test_")
os.environ["HOME"] = _TMPDIR
sys.path.insert(0, "/home/ubuntu/paper_min_bot")

import paper_min_bot as pb  # noqa: E402

pb.DATA_DIR = Path(_TMPDIR) / "data"
pb.LOG_DIR = Path(_TMPDIR) / "logs"
pb.DATA_DIR.mkdir(parents=True, exist_ok=True)
pb.LOG_DIR.mkdir(parents=True, exist_ok=True)
pb.POSITIONS_FILE = pb.DATA_DIR / "positions.json"


def _src():
    return Path("/home/ubuntu/paper_min_bot/paper_min_bot.py").read_text()


# ── 1. Source-wiring regression guards ─────────────────────────────────

class TestSourceWiring(unittest.TestCase):
    def test_telemetry_helpers_present(self):
        s = _src()
        self.assertIn("def _compute_position_telemetry(", s)
        self.assertIn("def _update_open_positions_telemetry(", s)
        self.assertIn("def _resolve_live_min_forecast(", s)

    def test_call_site_in_scan_and_trade(self):
        s = _src()
        # Must be called inside the scan-cycle entry point with mkt_by_ticker.
        self.assertIn("_update_open_positions_telemetry(mkt_by_ticker)", s)

    def test_call_site_wrapped_in_try(self):
        """Telemetry must never break the scan cycle on failure."""
        s = _src()
        # Find the call and check it's inside a try/except
        idx = s.find("_update_open_positions_telemetry(mkt_by_ticker)")
        self.assertGreater(idx, 0)
        prefix = s[max(0, idx - 200):idx]
        self.assertIn("try:", prefix)


# ── 2. _compute_position_telemetry pure-function tests ─────────────────

class TestComputeTelemetry(unittest.TestCase):
    def test_returns_dict_with_ts_even_when_inputs_minimal(self):
        """Even with empty pos and no market quote, returns a uniform dict
        with ts so the caller can rely on the schema."""
        snap = pb._compute_position_telemetry({}, None)
        self.assertIn("ts", snap)
        self.assertIn("ts_iso", snap)

    def test_no_mutation_of_input_pos(self):
        pos = {
            "series": "KXLOWTLAX", "station": "KLAX",
            "date_str": "2026-05-01", "floor": 58.0, "cap": 59.0,
            "action": "BUY_NO", "entry_price": 0.55, "count": 54,
            "mu": 57.0, "sigma": 1.0, "model_prob": 0.06,
            "mu_source": "nbp",
        }
        before = dict(pos)
        pb._compute_position_telemetry(pos, None)
        self.assertEqual(pos, before, "_compute_position_telemetry must not mutate pos")

    def test_market_state_extraction(self):
        pos = {"action": "BUY_NO", "entry_price": 0.55, "count": 54,
               "station": "KLAX", "date_str": "2026-05-01"}
        mkt = {"yes_bid": 99, "yes_ask": 100, "no_bid": 1, "no_ask": 2}
        with patch.object(pb, "get_running_min", return_value=None), \
             patch.object(pb, "_resolve_live_min_forecast", return_value=None):
            snap = pb._compute_position_telemetry(pos, mkt)
        self.assertEqual(snap["yes_bid_c"], 99)
        self.assertEqual(snap["no_bid_c"], 1)
        self.assertEqual(snap["spread_c"], 1)
        self.assertEqual(snap["current_bid_side_c"], 1)  # BUY_NO → no_bid
        self.assertAlmostEqual(snap["current_price"], 0.01)

    def test_buy_yes_uses_yes_bid_for_current_price(self):
        pos = {"action": "BUY_YES", "entry_price": 0.30, "count": 4,
               "station": "KLAX", "date_str": "2026-05-01"}
        mkt = {"yes_bid": 25, "yes_ask": 27, "no_bid": 73, "no_ask": 75}
        with patch.object(pb, "get_running_min", return_value=None), \
             patch.object(pb, "_resolve_live_min_forecast", return_value=None):
            snap = pb._compute_position_telemetry(pos, mkt)
        self.assertEqual(snap["current_bid_side_c"], 25)
        self.assertAlmostEqual(snap["current_price"], 0.25)

    def test_mtm_pct_calculation(self):
        """current_mtm_pct = (current - entry) / entry. Positive = winning."""
        pos = {"action": "BUY_NO", "entry_price": 0.50, "count": 10,
               "station": "KLAX", "date_str": "2026-05-01"}
        mkt = {"yes_bid": 25, "yes_ask": 30, "no_bid": 70, "no_ask": 75}
        with patch.object(pb, "get_running_min", return_value=None), \
             patch.object(pb, "_resolve_live_min_forecast", return_value=None):
            snap = pb._compute_position_telemetry(pos, mkt)
        # current = no_bid/100 = 0.70. mtm_pct = (0.70 - 0.50)/0.50 = +0.40 (winning 40%)
        self.assertAlmostEqual(snap["current_mtm_pct"], 0.40, places=4)

    def test_running_min_recorded_when_available(self):
        pos = {"action": "BUY_NO", "entry_price": 0.55, "count": 1,
               "station": "KLAX", "date_str": "2026-05-01",
               "series": "KXLOWTLAX"}
        with patch.object(pb, "get_running_min", return_value=57.92), \
             patch.object(pb, "_resolve_live_min_forecast", return_value=None):
            snap = pb._compute_position_telemetry(pos, None)
        self.assertAlmostEqual(snap["running_min"], 57.92)

    def test_forecast_recomputation_includes_components(self):
        pos = {
            "action": "BUY_NO", "entry_price": 0.55, "count": 1,
            "station": "KLAX", "date_str": "2026-05-01",
            "series": "KXLOWTLAX", "floor": 58.0, "cap": 59.0,
        }
        mkt = {"yes_bid": 80, "yes_ask": 85, "no_bid": 15, "no_ask": 20}
        live_fc = {
            "mu": 57.5, "sigma": 1.2, "mu_source": "nbp",
            "disagreement": 0.5,
            "nbp_mu": 57.5, "nbp_sigma": 1.2,
            "hrrr": 57.0, "nbm_om": 58.0,
        }
        with patch.object(pb, "get_running_min", return_value=58.5), \
             patch.object(pb, "_resolve_live_min_forecast", return_value=live_fc), \
             patch.object(pb, "_climate_date_nws", return_value="2026-05-01"):
            snap = pb._compute_position_telemetry(pos, mkt)
        self.assertEqual(snap["mu"], 57.5)
        self.assertEqual(snap["sigma"], 1.2)
        self.assertEqual(snap["mu_source"], "nbp")
        self.assertEqual(snap["nbp_mu"], 57.5)
        self.assertEqual(snap["hrrr"], 57.0)
        self.assertEqual(snap["nbm_om"], 58.0)
        # model_prob recomputed
        self.assertIn("model_prob", snap)
        self.assertIsInstance(snap["model_prob"], float)
        # edge recomputed: BUY_NO edge = yes_bid/100 - model_prob
        self.assertIn("edge", snap)
        expected_edge = 80 / 100.0 - snap["model_prob"]
        self.assertAlmostEqual(snap["edge"], expected_edge, places=4)

    def test_buy_yes_edge_formula(self):
        """For BUY_YES: edge = model_prob - yes_ask/100"""
        pos = {
            "action": "BUY_YES", "entry_price": 0.20, "count": 1,
            "station": "KLAX", "date_str": "2026-05-01",
            "series": "KXLOWTLAX", "floor": 58.0, "cap": 59.0,
        }
        mkt = {"yes_bid": 18, "yes_ask": 22, "no_bid": 78, "no_ask": 82}
        live_fc = {"mu": 58.5, "sigma": 0.5, "mu_source": "nbp",
                   "disagreement": 0, "nbp_mu": 58.5, "nbp_sigma": 0.5,
                   "hrrr": None, "nbm_om": None}
        with patch.object(pb, "get_running_min", return_value=None), \
             patch.object(pb, "_resolve_live_min_forecast", return_value=live_fc), \
             patch.object(pb, "_climate_date_nws", return_value="2026-05-01"):
            snap = pb._compute_position_telemetry(pos, mkt)
        self.assertIn("model_prob", snap)
        expected_edge = snap["model_prob"] - 22 / 100.0
        self.assertAlmostEqual(snap["edge"], expected_edge, places=4)


# ── 3. _resolve_live_min_forecast priority + inflation logic ───────────

class TestResolveForecast(unittest.TestCase):
    def setUp(self):
        # Patch out the real cache lookups
        self._patches = [
            patch.object(pb, "get_nbp_forecast"),
            patch.object(pb, "get_nbm_om_min"),
            patch.object(pb, "get_hrrr_min"),
        ]
        self._mocks = [p.start() for p in self._patches]
        self.mock_nbp = self._mocks[0]
        self.mock_nbm = self._mocks[1]
        self.mock_hrrr = self._mocks[2]

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def test_returns_none_when_no_source(self):
        self.mock_nbp.return_value = None
        self.mock_nbm.return_value = None
        self.mock_hrrr.return_value = None
        out = pb._resolve_live_min_forecast("KXLOWTLAX", "KLAX", "2026-05-01", True)
        self.assertIsNone(out)

    def test_d0_default_uses_hrrr(self):
        """Day-0 default path: HRRR primary, NBP sigma. Need a series NOT in
        PER_SERIES_D0_PRIMARY (which is the NBP-override list)."""
        self.mock_nbp.return_value = {"mu": 60.0, "sigma": 2.0}
        self.mock_hrrr.return_value = 58.0
        self.mock_nbm.return_value = 59.0
        # Pick a series not in the d-0 NBP override list
        non_override = next((s for s in ("KXLOWTATL", "KXLOWTAUS", "KXLOWTHOU",
                                          "KXLOWTDAL", "KXLOWTCHI", "KXLOWTOKC",
                                          "KXLOWTSAT", "KXLOWTDEN", "KXLOWTMIN")
                              if s not in pb.PER_SERIES_D0_PRIMARY), None)
        self.assertIsNotNone(non_override, "couldn't find a non-NBP-override series")
        out = pb._resolve_live_min_forecast(non_override, "KSTATION", "2026-05-01", True)
        self.assertEqual(out["mu"], 58.0)  # HRRR
        self.assertEqual(out["mu_source"], "hrrr")

    def test_disagreement_inflates_sigma(self):
        """When sources disagree by >2°F, sigma inflates."""
        self.mock_nbp.return_value = {"mu": 60.0, "sigma": 2.0}
        self.mock_hrrr.return_value = 55.0  # 5°F disagreement
        self.mock_nbm.return_value = 60.0
        out = pb._resolve_live_min_forecast("KXLOWTBOS", "KBOS", "2026-05-01", True)
        # disagreement = 5°F → inflation = min(1.5, 1 + (5-2)*0.15) = 1.45
        # sigma was 2.0, becomes 2.0 * 1.45 = 2.9 (before per-series mult)
        self.assertGreater(out["sigma"], 2.0)
        self.assertGreaterEqual(out["disagreement"], 5.0)

    def test_d0_nbp_override_for_configured_station(self):
        """NYC is in PER_SERIES_D0_PRIMARY={'KXLOWTNY':'nbp'} (or similar).
        Verify the per-station override path actually fires for one of them."""
        # Pick a series that IS in the override
        for series, val in pb.PER_SERIES_D0_PRIMARY.items():
            if val == "nbp":
                target_series = series
                break
        else:
            self.skipTest("no nbp d0 override configured")
            return
        self.mock_nbp.return_value = {"mu": 50.0, "sigma": 1.5}
        self.mock_hrrr.return_value = 52.0
        self.mock_nbm.return_value = 51.0
        out = pb._resolve_live_min_forecast(
            target_series, "KSTATION", "2026-05-01", True)
        self.assertEqual(out["mu_source"], "nbp_d0_override")
        self.assertEqual(out["mu"], 50.0)


# ── 4. _update_open_positions_telemetry integration ────────────────────

class TestUpdateOpenPositionsTelemetry(unittest.TestCase):
    def setUp(self):
        # Reset open positions
        pb._open_positions.clear()
        # Use a per-test telemetry log file
        self._old_data_dir = pb.DATA_DIR
        self._tmp = tempfile.mkdtemp(prefix="telem_test_")
        pb.DATA_DIR = Path(self._tmp)
        pb.POSITIONS_FILE = pb.DATA_DIR / "positions.json"

    def tearDown(self):
        pb.DATA_DIR = self._old_data_dir
        pb.POSITIONS_FILE = self._old_data_dir / "positions.json"

    def test_no_op_with_no_open_positions(self):
        """Empty positions: function returns silently, no exception."""
        pb._update_open_positions_telemetry({})
        # No log file should be created
        log_files = list(pb.DATA_DIR.glob("position_telemetry_*.jsonl"))
        self.assertEqual(len(log_files), 0)

    def test_writes_telemetry_log_row_per_open_position(self):
        pb._open_positions["TICKER1"] = {
            "action": "BUY_NO", "entry_price": 0.55, "count": 1,
            "station": "KLAX", "date_str": "2026-05-01",
            "series": "KXLOWTLAX", "floor": 58.0, "cap": 59.0,
            "mu": 57.0, "sigma": 1.0, "model_prob": 0.06,
        }
        market_quotes = {
            "TICKER1": {"yes_bid": 99, "yes_ask": 100, "no_bid": 1, "no_ask": 2},
        }
        with patch.object(pb, "get_running_min", return_value=58.5), \
             patch.object(pb, "_resolve_live_min_forecast", return_value=None):
            pb._update_open_positions_telemetry(market_quotes)
        log_files = list(pb.DATA_DIR.glob("position_telemetry_*.jsonl"))
        self.assertEqual(len(log_files), 1)
        with open(log_files[0]) as f:
            rows = [json.loads(line) for line in f]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["market_ticker"], "TICKER1")
        self.assertEqual(rows[0]["entry_price"], 0.55)
        self.assertEqual(rows[0]["yes_bid_c"], 99)

    def test_updates_pos_live_dict_with_high_water_marks(self):
        pb._open_positions["TICKER1"] = {
            "action": "BUY_NO", "entry_price": 0.50, "count": 1,
            "station": "KLAX", "date_str": "2026-05-01",
            "series": "KXLOWTLAX", "floor": 58.0, "cap": 59.0,
        }
        # First cycle: rm=60, mtm = (0.30 - 0.50)/0.50 = -0.40 (losing 40%)
        with patch.object(pb, "get_running_min", return_value=60.0), \
             patch.object(pb, "_resolve_live_min_forecast", return_value=None):
            pb._update_open_positions_telemetry({
                "TICKER1": {"yes_bid": 70, "yes_ask": 75, "no_bid": 30, "no_ask": 35}})
        live1 = pb._open_positions["TICKER1"]["live"]
        self.assertEqual(live1["cycles"], 1)
        self.assertAlmostEqual(live1["peak_running_min"], 60.0)
        self.assertAlmostEqual(live1["trough_mtm_pct"], -0.40, places=3)
        self.assertAlmostEqual(live1["peak_mtm_pct"], -0.40, places=3)
        # Second cycle: rm drops to 57 (lower min), market reprices NO higher
        with patch.object(pb, "get_running_min", return_value=57.0), \
             patch.object(pb, "_resolve_live_min_forecast", return_value=None):
            pb._update_open_positions_telemetry({
                "TICKER1": {"yes_bid": 5, "yes_ask": 8, "no_bid": 92, "no_ask": 95}})
        live2 = pb._open_positions["TICKER1"]["live"]
        self.assertEqual(live2["cycles"], 2)
        # rm went lower → peak_running_min updates
        self.assertAlmostEqual(live2["peak_running_min"], 57.0)
        # mtm now positive (0.92-0.50)/0.50 = +0.84 → peak_mtm updates
        self.assertAlmostEqual(live2["peak_mtm_pct"], 0.84, places=3)
        # trough stays at the worst seen
        self.assertAlmostEqual(live2["trough_mtm_pct"], -0.40, places=3)

    def test_skips_settled_positions(self):
        pb._open_positions["DONE"] = {
            "action": "BUY_NO", "entry_price": 0.55, "count": 1,
            "station": "KLAX", "date_str": "2026-04-30",
            "series": "KXLOWTLAX", "floor": 58.0, "cap": 59.0,
            "settled": True,
        }
        with patch.object(pb, "get_running_min", return_value=58.5), \
             patch.object(pb, "_resolve_live_min_forecast", return_value=None):
            pb._update_open_positions_telemetry({
                "DONE": {"yes_bid": 99, "yes_ask": 100, "no_bid": 1, "no_ask": 2}})
        # Settled position → no live dict added
        self.assertNotIn("live", pb._open_positions["DONE"])
        # No telemetry log written
        log_files = list(pb.DATA_DIR.glob("position_telemetry_*.jsonl"))
        self.assertEqual(len(log_files), 0)

    def test_handles_missing_market_quote_gracefully(self):
        pb._open_positions["NOMKT"] = {
            "action": "BUY_NO", "entry_price": 0.55, "count": 1,
            "station": "KLAX", "date_str": "2026-05-01",
            "series": "KXLOWTLAX", "floor": 58.0, "cap": 59.0,
        }
        with patch.object(pb, "get_running_min", return_value=58.0), \
             patch.object(pb, "_resolve_live_min_forecast", return_value=None):
            pb._update_open_positions_telemetry({})
        # Still writes telemetry (with whatever non-market data is available)
        live = pb._open_positions["NOMKT"]["live"]
        self.assertEqual(live["cycles"], 1)
        self.assertAlmostEqual(live["running_min"], 58.0)
        # No market fields set
        self.assertNotIn("yes_bid_c", live)


# ── 5. JSONL log row schema ────────────────────────────────────────────

class TestTelemetryLogSchema(unittest.TestCase):
    def setUp(self):
        pb._open_positions.clear()
        self._tmp = tempfile.mkdtemp(prefix="telem_schema_")
        pb.DATA_DIR = Path(self._tmp)
        pb.POSITIONS_FILE = pb.DATA_DIR / "positions.json"

    def test_log_row_carries_entry_context(self):
        """Each telemetry log row needs entry context for backtest replay."""
        pb._open_positions["TKR"] = {
            "action": "BUY_NO", "entry_price": 0.55, "count": 54,
            "station": "KLAX", "date_str": "2026-05-01",
            "series": "KXLOWTLAX", "floor": 58.0, "cap": 59.0,
            "mu": 57.0, "sigma": 1.0, "model_prob": 0.06,
            "mu_source": "nbp",
        }
        with patch.object(pb, "get_running_min", return_value=58.5), \
             patch.object(pb, "_resolve_live_min_forecast", return_value=None):
            pb._update_open_positions_telemetry({
                "TKR": {"yes_bid": 80, "yes_ask": 85, "no_bid": 15, "no_ask": 20}})
        log_path = list(pb.DATA_DIR.glob("position_telemetry_*.jsonl"))[0]
        row = json.loads(log_path.read_text().strip())
        # Entry context (immutable across cycles for a given position)
        self.assertEqual(row["entry_price"], 0.55)
        self.assertEqual(row["entry_model_prob"], 0.06)
        self.assertEqual(row["entry_mu"], 57.0)
        self.assertEqual(row["entry_sigma"], 1.0)
        self.assertEqual(row["entry_mu_source"], "nbp")
        # Live state (changes per cycle)
        self.assertIn("ts", row)
        self.assertIn("yes_bid_c", row)
        self.assertEqual(row["cycles_since_entry"], 1)
        # Position metadata
        self.assertEqual(row["market_ticker"], "TKR")
        self.assertEqual(row["floor"], 58.0)
        self.assertEqual(row["cap"], 59.0)


# ── 6. No-mutation guarantees ──────────────────────────────────────────

class TestNoBehaviorChange(unittest.TestCase):
    def test_telemetry_helpers_dont_call_kalshi_or_db_writes(self):
        """Sanity: telemetry must read-only. No db.execute(INSERT/UPDATE/DELETE)
        and no kalshi_post in the telemetry helpers."""
        s = _src()
        block_start = s.find("def _compute_position_telemetry(")
        # Find the next def after this one to bound the block
        next_def = s.find("\ndef ", block_start + 10)
        block_end = next_def if next_def > 0 else len(s)
        # Also include _resolve_live_min_forecast and _update_open_positions_telemetry
        merged = s[block_start:block_end]
        ru_start = s.find("def _resolve_live_min_forecast(")
        merged += s[ru_start:s.find("\ndef ", ru_start + 10)]
        upd_start = s.find("def _update_open_positions_telemetry(")
        merged += s[upd_start:s.find("\ndef ", upd_start + 10)]
        # Forbidden:
        for forbidden in ("kalshi_post(", "kalshi_delete(", "db.upsert_running",
                          "db.insert_observation", "INSERT INTO ", "DELETE FROM ",
                          "UPDATE running"):
            self.assertNotIn(forbidden, merged,
                             f"Telemetry helpers must not call {forbidden}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
