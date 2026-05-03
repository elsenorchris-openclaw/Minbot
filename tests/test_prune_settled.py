"""Tests for _prune_settled_past_positions (2026-05-03).

Trigger: end-of-day audit found 31 settled-but-not-pruned positions in
positions.json (Apr 30 → May 2 dates, all settled=True). Bot was filtering
them correctly during scans but the file grew unbounded between restarts.
TTL-prune in _load_positions only fires after POSITION_TTL_DAYS=3.

Conservative criteria — only prune when ALL of:
  - pos.get("settled") truthy
  - pos.get("date_str") non-empty
  - date_str < today (UTC)
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

# Ensure /tmp/paper_min_bot is on path (per memory: tests import from there)
sys.path.insert(0, "/tmp/paper_min_bot")

import paper_min_bot as pmb  # noqa: E402


class PruneSettledPastPositionsTests(unittest.TestCase):
    def setUp(self):
        # Snapshot original state then reset
        self._orig_positions = dict(pmb._open_positions)
        pmb._open_positions = {}
        # Redirect POSITIONS_FILE to a temp location so tests don't
        # touch live data
        self._tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self._tmp.close()
        self._tmp_path = Path(self._tmp.name)
        self._orig_file = pmb.POSITIONS_FILE
        pmb.POSITIONS_FILE = self._tmp_path

    def tearDown(self):
        pmb._open_positions = self._orig_positions
        pmb.POSITIONS_FILE = self._orig_file
        try:
            self._tmp_path.unlink()
        except FileNotFoundError:
            pass

    def _today(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _yesterday(self) -> str:
        return (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

    def _tomorrow(self) -> str:
        return (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")

    # ── core behavior ────────────────────────────────────────────────

    def test_settled_past_day_is_removed(self):
        pmb._open_positions = {
            "KXLOWTLAX-26MAY01-B58.5": {
                "settled": True,
                "date_str": self._yesterday(),
                "action": "BUY_NO",
            }
        }
        n = pmb._prune_settled_past_positions()
        self.assertEqual(n, 1)
        self.assertNotIn("KXLOWTLAX-26MAY01-B58.5", pmb._open_positions)

    def test_settled_today_is_kept(self):
        """Today's climate-day may not be over yet for all TZs; bot may
        still want to read late-day reports."""
        tk = f"KXLOWTLAX-{self._today()[2:].replace('-','')}-B58.5"
        pmb._open_positions = {
            tk: {"settled": True, "date_str": self._today(), "action": "BUY_NO"}
        }
        n = pmb._prune_settled_past_positions()
        self.assertEqual(n, 0)
        self.assertIn(tk, pmb._open_positions)

    def test_settled_future_dated_is_kept(self):
        """Forward-dated settled position is anomalous but don't auto-delete —
        flag for visibility."""
        pmb._open_positions = {
            "future-tk": {
                "settled": True,
                "date_str": self._tomorrow(),
                "action": "BUY_NO",
            }
        }
        n = pmb._prune_settled_past_positions()
        self.assertEqual(n, 0)
        self.assertIn("future-tk", pmb._open_positions)

    def test_unsettled_past_day_is_kept(self):
        """Past-date but unsettled = stuck record, keep for ops visibility."""
        pmb._open_positions = {
            "stuck-tk": {
                "settled": False,
                "date_str": self._yesterday(),
                "action": "BUY_NO",
            }
        }
        n = pmb._prune_settled_past_positions()
        self.assertEqual(n, 0)
        self.assertIn("stuck-tk", pmb._open_positions)

    def test_settled_no_date_str_is_kept(self):
        """Defensive: don't drop positions with missing/empty date_str."""
        pmb._open_positions = {
            "no-date-tk": {"settled": True, "date_str": "", "action": "BUY_NO"}
        }
        n = pmb._prune_settled_past_positions()
        self.assertEqual(n, 0)
        self.assertIn("no-date-tk", pmb._open_positions)

    def test_missing_settled_key_is_kept(self):
        """Position dict with no `settled` key is treated as unsettled."""
        pmb._open_positions = {
            "no-settled-tk": {"date_str": self._yesterday(), "action": "BUY_NO"}
        }
        n = pmb._prune_settled_past_positions()
        self.assertEqual(n, 0)
        self.assertIn("no-settled-tk", pmb._open_positions)

    def test_empty_dict_no_error(self):
        pmb._open_positions = {}
        n = pmb._prune_settled_past_positions()
        self.assertEqual(n, 0)

    def test_settled_truthy_but_not_bool_is_removed(self):
        """`settled` is checked via truthy semantics — anything non-falsy
        qualifies. (Bot writes booleans, but defensive check.)"""
        pmb._open_positions = {
            "truthy-tk": {
                "settled": "yes",  # non-bool but truthy
                "date_str": self._yesterday(),
                "action": "BUY_NO",
            }
        }
        n = pmb._prune_settled_past_positions()
        self.assertEqual(n, 1)

    def test_persists_to_disk_when_pruning(self):
        """If anything is pruned, _save_positions writes the new state to
        positions.json. Verify by reading back."""
        pmb._open_positions = {
            "removed-tk": {
                "settled": True,
                "date_str": self._yesterday(),
                "action": "BUY_NO",
            },
            "kept-tk": {
                "settled": False,
                "date_str": self._today(),
                "action": "BUY_NO",
            },
        }
        pmb._prune_settled_past_positions()
        # File should contain only the kept position
        with open(self._tmp_path) as f:
            persisted = json.load(f)
        self.assertNotIn("removed-tk", persisted)
        self.assertIn("kept-tk", persisted)

    def test_no_disk_write_when_nothing_pruned(self):
        """If no records match prune criteria, don't write to disk
        unnecessarily."""
        pmb._open_positions = {
            "kept-tk": {"settled": False, "date_str": self._yesterday()}
        }
        # Ensure file doesn't exist before
        try:
            self._tmp_path.unlink()
        except FileNotFoundError:
            pass
        pmb._prune_settled_past_positions()
        # File should not have been created
        self.assertFalse(self._tmp_path.exists(),
                         "no-op prune should not write positions.json")

    # ── reproduce the 31-stale-records scenario ──────────────────────

    def test_repro_31_stale_records_scenario(self):
        """Simulate the 2026-05-03 audit scenario: positions from Apr 30,
        May 1, May 2 all settled=True, plus today's positions
        (settled=False) and tomorrow's d+1 entries."""
        today_str = self._today()
        yest_str = self._yesterday()
        # 10 settled past-day positions
        positions = {
            f"settled-past-{i}": {
                "settled": True,
                "date_str": yest_str,
                "action": "BUY_NO",
                "count": 10 + i,
            } for i in range(10)
        }
        # 5 today, settled (kept)
        positions.update({
            f"settled-today-{i}": {
                "settled": True,
                "date_str": today_str,
                "action": "BUY_NO",
            } for i in range(5)
        })
        # 5 today, unsettled (kept)
        positions.update({
            f"open-today-{i}": {
                "settled": False,
                "date_str": today_str,
                "action": "BUY_NO",
            } for i in range(5)
        })
        # 1 tomorrow, unsettled (kept — d+1)
        positions["d-plus-1"] = {
            "settled": False,
            "date_str": self._tomorrow(),
            "action": "BUY_NO",
        }
        pmb._open_positions = positions

        n = pmb._prune_settled_past_positions()
        self.assertEqual(n, 10, f"expected 10 pruned, got {n}")
        # Verify exactly the right positions remain
        self.assertEqual(len(pmb._open_positions), 11)
        for i in range(5):
            self.assertIn(f"settled-today-{i}", pmb._open_positions)
            self.assertIn(f"open-today-{i}", pmb._open_positions)
        self.assertIn("d-plus-1", pmb._open_positions)
        for i in range(10):
            self.assertNotIn(f"settled-past-{i}", pmb._open_positions)


class LoadPositionsIntegrationTest(unittest.TestCase):
    """Verify _load_positions calls the new prune function."""

    def setUp(self):
        self._orig_positions = dict(pmb._open_positions)
        self._tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self._tmp.close()
        self._tmp_path = Path(self._tmp.name)
        self._orig_file = pmb.POSITIONS_FILE
        pmb.POSITIONS_FILE = self._tmp_path

    def tearDown(self):
        pmb._open_positions = self._orig_positions
        pmb.POSITIONS_FILE = self._orig_file
        try:
            self._tmp_path.unlink()
        except FileNotFoundError:
            pass

    def test_load_positions_prunes_settled_past(self):
        """End-to-end: positions.json contains a settled past-day record →
        _load_positions drops it."""
        yest = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        positions_on_disk = {
            "settled-past-tk": {
                "settled": True,
                "date_str": yest,
                "action": "BUY_NO",
            },
            "open-today-tk": {
                "settled": False,
                "date_str": today,
                "action": "BUY_NO",
            },
        }
        with open(self._tmp_path, "w") as f:
            json.dump(positions_on_disk, f)

        pmb._open_positions = {}
        pmb._load_positions()

        # After load, settled-past should be gone
        self.assertIn("open-today-tk", pmb._open_positions)
        self.assertNotIn("settled-past-tk", pmb._open_positions)


if __name__ == "__main__":
    unittest.main()
