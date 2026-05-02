"""Tests for SHADOW-LOG primary_outlier_diff diagnostic on trade entries.

Field added 2026-05-01 night to give us forward data for evaluating a
SKIP_PRIMARY_OUTLIER filter at e.g. 2.0°F. NOT yet a gate — just logged.

In-sample cases reproduced:
  HOU 26APR30-B68.5: HRRR=69.4 primary, NBP=74.0 / NBM=69.5 → diff 2.35
                     (settled $-29.44 LOSS — would be caught at thr 2.0)
  MIA 26MAY01-B71.5: NBP=72.0 primary, HRRR=69.6 / NBM=69.6 → diff 2.40
                     (settled $-23.82 hard-stop — would be caught at thr 2.0)
  PHX 26APR29-B62.5: NBP=64.0 primary, HRRR=60.4 / NBM=60.4 → diff 3.60
                     (settled +$10.20 WIN — would be forfeit at thr 2.0)

Validation plan: re-backtest at ~60 trades (~mid-May 2026) before deploy.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

_TMPDIR = tempfile.mkdtemp(prefix="primary_outlier_test_")
os.environ["HOME"] = _TMPDIR
sys.path.insert(0, "/home/ubuntu/paper_min_bot")

import paper_min_bot as pb
pb.DATA_DIR = Path(_TMPDIR) / "data"
pb.LOG_DIR = Path(_TMPDIR) / "logs"
pb.DATA_DIR.mkdir(parents=True, exist_ok=True)
pb.LOG_DIR.mkdir(parents=True, exist_ok=True)
pb.POSITIONS_FILE = pb.DATA_DIR / "positions.json"
pb.TRADES_FILE = pb.DATA_DIR / "trades.jsonl"
pb.SETTLEMENTS_FILE = pb.DATA_DIR / "settlements.jsonl"


def _read_source():
    return Path("/home/ubuntu/paper_min_bot/paper_min_bot.py").read_text()


# ─────────────────────────────────────────────────────────────────────────────
# Source-level invariants
# ─────────────────────────────────────────────────────────────────────────────
class TestSourceWiring(unittest.TestCase):
    def test_helper_is_defined(self):
        src = _read_source()
        self.assertIn("def _compute_primary_outlier_diff(opp: dict)", src)

    def test_field_present_in_trade_record(self):
        src = _read_source()
        e = src.index("trade_record = {")
        # Field must appear within ~3KB of the trade_record literal start
        block = src[e:e + 3500]
        self.assertIn('"primary_outlier_diff_at_entry":', block)
        self.assertIn("_compute_primary_outlier_diff(opp)", block)

    def test_helper_handles_each_mu_source_label(self):
        """Every label set in find_opportunities must map to a primary_key."""
        src = _read_source()
        # mu_source labels currently set:
        for label in ("nbp", "hrrr", "nbp_d0_override", "hrrr_d1_override", "nbm_om"):
            self.assertIn(f'mu_source = "{label}"', src,
                          f"label {label} not found in source")
        helper_block = src[src.index("def _compute_primary_outlier_diff"):]
        helper_block = helper_block[:helper_block.index("def _evaluate_gates")]
        self.assertIn('"nbp" in src', helper_block)
        self.assertIn('"hrrr" in src', helper_block)
        self.assertIn('src == "nbm_om"', helper_block)


# ─────────────────────────────────────────────────────────────────────────────
# Unit tests on _compute_primary_outlier_diff itself
# ─────────────────────────────────────────────────────────────────────────────
class TestComputePrimaryOutlierDiff(unittest.TestCase):
    def test_hou_pattern_caught(self):
        # HRRR primary, NBP outlier high
        opp = {"mu_source": "hrrr", "mu_hrrr": 69.4,
               "mu_nbp": 74.0, "mu_nbm_om": 69.5}
        self.assertAlmostEqual(pb._compute_primary_outlier_diff(opp), 2.35, places=2)

    def test_mia_pattern_caught(self):
        # NBP-d0 override primary, HRRR/NBM cluster low
        opp = {"mu_source": "nbp_d0_override", "mu_nbp": 72.0,
               "mu_hrrr": 69.6, "mu_nbm_om": 69.6}
        self.assertAlmostEqual(pb._compute_primary_outlier_diff(opp), 2.40, places=2)

    def test_phx_pattern_winner(self):
        # NBP primary, HRRR/NBM cluster low — would be forfeit at 2.0°F
        opp = {"mu_source": "nbp", "mu_nbp": 64.0,
               "mu_hrrr": 60.4, "mu_nbm_om": 60.4}
        self.assertAlmostEqual(pb._compute_primary_outlier_diff(opp), 3.60, places=2)

    def test_tight_cluster_returns_small_diff(self):
        # All sources agree → near-zero diff
        opp = {"mu_source": "nbp", "mu_nbp": 50.0,
               "mu_hrrr": 50.2, "mu_nbm_om": 49.8}
        self.assertAlmostEqual(pb._compute_primary_outlier_diff(opp), 0.0, places=1)

    def test_d1_hrrr_override_path(self):
        opp = {"mu_source": "hrrr_d1_override", "mu_hrrr": 50.0,
               "mu_nbp": 53.0, "mu_nbm_om": 51.0}
        # primary = HRRR 50, others mean = 52, diff = 2.0
        self.assertAlmostEqual(pb._compute_primary_outlier_diff(opp), 2.0, places=2)

    def test_nbm_only_path(self):
        opp = {"mu_source": "nbm_om", "mu_nbm_om": 60.0,
               "mu_nbp": 62.0, "mu_hrrr": None}
        # primary = NBM 60, others = [NBP 62], diff = 2.0
        self.assertAlmostEqual(pb._compute_primary_outlier_diff(opp), 2.0, places=2)

    def test_no_other_sources_returns_none(self):
        # Only the primary is available, no others to compare against
        opp = {"mu_source": "nbp", "mu_nbp": 50.0,
               "mu_hrrr": None, "mu_nbm_om": None}
        self.assertIsNone(pb._compute_primary_outlier_diff(opp))

    def test_unknown_mu_source_returns_none(self):
        opp = {"mu_source": "bogus", "mu_nbp": 50.0, "mu_hrrr": 51.0}
        self.assertIsNone(pb._compute_primary_outlier_diff(opp))

    def test_empty_mu_source_returns_none(self):
        opp = {"mu_source": "", "mu_nbp": 50.0, "mu_hrrr": 51.0}
        self.assertIsNone(pb._compute_primary_outlier_diff(opp))

    def test_missing_primary_value_returns_none(self):
        opp = {"mu_source": "nbp", "mu_nbp": None,
               "mu_hrrr": 51.0, "mu_nbm_om": 50.5}
        self.assertIsNone(pb._compute_primary_outlier_diff(opp))

    def test_diff_is_rounded_to_3_decimals(self):
        opp = {"mu_source": "nbp", "mu_nbp": 50.0,
               "mu_hrrr": 51.111111, "mu_nbm_om": 52.0}
        result = pb._compute_primary_outlier_diff(opp)
        # 50 vs mean(51.111, 52) = 51.5555 → diff 1.5555 → rounded
        self.assertEqual(result, round(abs(50.0 - (51.111111 + 52.0) / 2), 3))

    def test_returns_non_negative(self):
        for nbp, hrrr in [(50, 60), (60, 50), (45.5, 55.5)]:
            opp = {"mu_source": "nbp", "mu_nbp": nbp,
                   "mu_hrrr": hrrr, "mu_nbm_om": (nbp + hrrr) / 2}
            self.assertGreaterEqual(pb._compute_primary_outlier_diff(opp), 0.0)


if __name__ == "__main__":
    unittest.main()
