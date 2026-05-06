"""Tests for the auto-select primary forecast source helpers."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Override paths BEFORE import (mirror test_paper_min.py setup)
_TMPDIR = tempfile.mkdtemp(prefix="auto_primary_test_")
os.environ["HOME"] = _TMPDIR
sys.path.insert(0, "/tmp/paper_min_bot")
import paper_min_bot as pb
pb.DATA_DIR = Path(_TMPDIR) / "data"
pb.LOG_DIR = Path(_TMPDIR) / "logs"
os.makedirs(pb.DATA_DIR, exist_ok=True)


def _write_selection_json(d0: dict, d1: dict, computed_dt=None, path=None):
    """Helper: write a selection JSON file."""
    if path is None:
        path = pb.DATA_DIR / "auto_primary_selection.json"
    if computed_dt is None:
        computed_dt = datetime.now(timezone.utc)
    selections = {}
    for icao, src in d0.items():
        selections.setdefault(icao, {})['d0_primary'] = src
    for icao, src in d1.items():
        selections.setdefault(icao, {})['d1_primary'] = src
    payload = {
        "computed_at": computed_dt.isoformat(),
        "lookback_days": 14,
        "selections": selections,
    }
    with open(path, 'w') as f:
        json.dump(payload, f)
    return path


class TestAutoPrimarySelection(unittest.TestCase):
    """get_d0_primary / get_d1_primary helpers — fallback chain."""

    def setUp(self):
        # Clear cache + manual overrides between tests
        pb._auto_primary_cache.update({
            "loaded_mtime": 0.0, "computed_ts": 0.0,
            "d0": {}, "d1": {}, "valid": False,
        })
        pb.MANUAL_PRIMARY_OVERRIDES_D0.clear()
        pb.MANUAL_PRIMARY_OVERRIDES_D1.clear()
        # Remove any lingering JSON
        json_path = pb.DATA_DIR / "auto_primary_selection.json"
        if json_path.exists():
            json_path.unlink()
        pb.AUTO_PRIMARY_SELECTION_PATH = str(json_path)

    def test_no_json_falls_back_to_hardcoded(self):
        """When auto JSON missing, helper returns hardcoded value."""
        # KSEA is hardcoded as 'nbp' for d-0
        result = pb.get_d0_primary("KXLOWTSEA")
        self.assertEqual(result, "nbp")

    def test_no_json_unknown_series_returns_none(self):
        """Series with no hardcoded entry returns None (caller uses default)."""
        result = pb.get_d0_primary("KXLOWTUNKNOWN")
        self.assertIsNone(result)

    def test_auto_json_overrides_hardcoded(self):
        """When auto JSON has KSEA d-0=hrrr, that wins over hardcoded nbp."""
        _write_selection_json(d0={"KSEA": "hrrr"}, d1={}, path=pb.AUTO_PRIMARY_SELECTION_PATH)
        result = pb.get_d0_primary("KXLOWTSEA")
        self.assertEqual(result, "hrrr")

    def test_manual_override_wins_over_auto(self):
        """Manual override beats auto JSON."""
        _write_selection_json(d0={"KSEA": "hrrr"}, d1={}, path=pb.AUTO_PRIMARY_SELECTION_PATH)
        pb.MANUAL_PRIMARY_OVERRIDES_D0["KXLOWTSEA"] = "nbm"
        result = pb.get_d0_primary("KXLOWTSEA")
        self.assertEqual(result, "nbm")

    def test_stale_json_falls_through(self):
        """JSON with computed_at > 36h ago is treated as stale."""
        old_dt = datetime.now(timezone.utc) - timedelta(hours=48)
        _write_selection_json(d0={"KSEA": "hrrr"}, d1={}, computed_dt=old_dt, path=pb.AUTO_PRIMARY_SELECTION_PATH)
        result = pb.get_d0_primary("KXLOWTSEA")
        # Should fall through to hardcoded nbp
        self.assertEqual(result, "nbp")

    def test_d1_helper_works(self):
        """Same chain for d-1 helper."""
        _write_selection_json(d0={}, d1={"KMIA": "hrrr"}, path=pb.AUTO_PRIMARY_SELECTION_PATH)
        # KMIA d-1 hardcoded is 'nbm', auto says 'hrrr' — auto wins
        result = pb.get_d1_primary("KXLOWTMIA")
        self.assertEqual(result, "hrrr")

    def test_corrupt_json_falls_through(self):
        """Malformed JSON doesn't crash; falls back to hardcoded."""
        path = pb.AUTO_PRIMARY_SELECTION_PATH
        with open(path, 'w') as f:
            f.write("not valid json {{{ ")
        # Trigger reload attempt
        result = pb.get_d0_primary("KXLOWTSEA")
        self.assertEqual(result, "nbp")  # hardcoded fallback

    def test_partial_selection_falls_through_for_unset_cells(self):
        """If JSON has KSEA d-0 but not KMIA d-0, KMIA falls through."""
        _write_selection_json(d0={"KSEA": "hrrr"}, d1={}, path=pb.AUTO_PRIMARY_SELECTION_PATH)
        # KMIA d-0 hardcoded is None (not in PER_SERIES_D0_PRIMARY)
        # so should return None (falls through to default)
        result = pb.get_d0_primary("KXLOWTMIA")
        self.assertIsNone(result)
        # KSEA d-0 should be hrrr (from JSON)
        self.assertEqual(pb.get_d0_primary("KXLOWTSEA"), "hrrr")

    def test_mtime_reload(self):
        """When JSON mtime changes, helper picks up new value."""
        path = pb.AUTO_PRIMARY_SELECTION_PATH
        _write_selection_json(d0={"KSEA": "nbm"}, d1={}, path=path)
        self.assertEqual(pb.get_d0_primary("KXLOWTSEA"), "nbm")
        # Wait a beat to ensure mtime resolution
        time.sleep(0.05)
        _write_selection_json(d0={"KSEA": "hrrr"}, d1={}, path=path)
        # Touch to force mtime update
        os.utime(path, None)
        self.assertEqual(pb.get_d0_primary("KXLOWTSEA"), "hrrr")


class TestAutoPrimaryAgeBoundary(unittest.TestCase):
    """The 36h staleness cutoff."""

    def setUp(self):
        pb._auto_primary_cache.update({
            "loaded_mtime": 0.0, "computed_ts": 0.0,
            "d0": {}, "d1": {}, "valid": False,
        })
        pb.MANUAL_PRIMARY_OVERRIDES_D0.clear()
        pb.MANUAL_PRIMARY_OVERRIDES_D1.clear()
        json_path = pb.DATA_DIR / "auto_primary_selection.json"
        if json_path.exists():
            json_path.unlink()
        pb.AUTO_PRIMARY_SELECTION_PATH = str(json_path)

    def test_30h_is_fresh(self):
        old_dt = datetime.now(timezone.utc) - timedelta(hours=30)
        _write_selection_json(d0={"KSEA": "hrrr"}, d1={}, computed_dt=old_dt, path=pb.AUTO_PRIMARY_SELECTION_PATH)
        self.assertEqual(pb.get_d0_primary("KXLOWTSEA"), "hrrr")

    def test_40h_is_stale(self):
        old_dt = datetime.now(timezone.utc) - timedelta(hours=40)
        _write_selection_json(d0={"KSEA": "hrrr"}, d1={}, computed_dt=old_dt, path=pb.AUTO_PRIMARY_SELECTION_PATH)
        # Falls through to hardcoded nbp
        self.assertEqual(pb.get_d0_primary("KXLOWTSEA"), "nbp")


if __name__ == "__main__":
    unittest.main()
