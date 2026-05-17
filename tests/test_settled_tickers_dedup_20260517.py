"""Tests for the _settled_tickers dedup guard (2026-05-17).

Bug: _reconcile_kalshi_positions re-added already-settled positions when
Kalshi's batch settlement lagged ours, causing duplicate settlement records
(KDCA-MAY14 triple, BOS/HOU/MIN/NYC double). 6 records / 5 tickers / $101
fake PnL impact.

Fix: in-memory _settled_tickers set seeded from settlements.jsonl at startup,
augmented on each new settlement write. Both _reconcile_kalshi_positions and
check_settlements consult it.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestSettledTickersDedup(unittest.TestCase):
    def setUp(self):
        # Point DATA_DIR to a temp dir before import
        self.tmp = tempfile.mkdtemp(prefix="min_bot_dedup_test_")
        os.environ["MIN_BOT_DATA_DIR_OVERRIDE"] = self.tmp
        # Fresh import each test to pick up the override
        for m in list(sys.modules):
            if m == "paper_min_bot":
                del sys.modules[m]

    def tearDown(self):
        os.environ.pop("MIN_BOT_DATA_DIR_OVERRIDE", None)
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_seed_empty_when_no_file(self):
        """No settlements.jsonl → _settled_tickers stays empty."""
        import paper_min_bot as mb
        mb.SETTLEMENTS_FILE = Path(self.tmp) / "settlements.jsonl"
        mb._settled_tickers.clear()
        n = mb._seed_settled_tickers()
        self.assertEqual(n, 0)
        self.assertEqual(len(mb._settled_tickers), 0)

    def test_seed_loads_unique_tickers(self):
        """Settlements file with 3 records, 2 unique tickers → set has 2."""
        import paper_min_bot as mb
        path = Path(self.tmp) / "settlements.jsonl"
        with open(path, "w") as f:
            f.write(json.dumps({"kind": "settlement", "market_ticker": "KXLOWTNYC-26MAY10-B40.5"}) + "\n")
            f.write(json.dumps({"kind": "settlement", "market_ticker": "KXLOWTDC-26MAY10-B45.5"}) + "\n")
            f.write(json.dumps({"kind": "settlement", "market_ticker": "KXLOWTNYC-26MAY10-B40.5"}) + "\n")  # dup
        mb.SETTLEMENTS_FILE = path
        mb._settled_tickers.clear()
        n = mb._seed_settled_tickers()
        self.assertEqual(n, 3)  # 3 records scanned
        self.assertEqual(mb._settled_tickers,
                         {"KXLOWTNYC-26MAY10-B40.5", "KXLOWTDC-26MAY10-B45.5"})

    def test_seed_ignores_non_settlement_kinds(self):
        """Records with kind != 'settlement' must not pollute the set."""
        import paper_min_bot as mb
        path = Path(self.tmp) / "settlements.jsonl"
        with open(path, "w") as f:
            f.write(json.dumps({"kind": "entry", "market_ticker": "FAKE-1"}) + "\n")
            f.write(json.dumps({"kind": "settlement", "market_ticker": "REAL-1"}) + "\n")
        mb.SETTLEMENTS_FILE = path
        mb._settled_tickers.clear()
        mb._seed_settled_tickers()
        self.assertIn("REAL-1", mb._settled_tickers)
        self.assertNotIn("FAKE-1", mb._settled_tickers)

    def test_seed_tolerates_bad_json(self):
        """A malformed line should be skipped, not crash."""
        import paper_min_bot as mb
        path = Path(self.tmp) / "settlements.jsonl"
        with open(path, "w") as f:
            f.write('{"kind": "settlement", "market_ticker": "OK-1"}\n')
            f.write('this is not json\n')
            f.write('{"kind": "settlement", "market_ticker": "OK-2"}\n')
        mb.SETTLEMENTS_FILE = path
        mb._settled_tickers.clear()
        mb._seed_settled_tickers()
        self.assertEqual(mb._settled_tickers, {"OK-1", "OK-2"})

    def test_reconcile_skips_settled_ticker(self):
        """If a ticker is in _settled_tickers, reconcile must not add it."""
        import paper_min_bot as mb
        mb._open_positions.clear()
        mb._settled_tickers.clear()
        mb._settled_tickers.add("KXLOWTDC-26MAY14-B52.5")

        # Monkey-patch kalshi_get to return a fake position for the settled
        # ticker (simulating Kalshi's lagging settlement)
        def fake_get(url, params=None):
            if "positions" in url:
                return {"market_positions": [{
                    "ticker": "KXLOWTDC-26MAY14-B52.5",
                    "position_fp": -100,  # BUY_NO 100 contracts
                    "market_exposure_dollars": 58.0,
                    "last_updated_ts": "2026-05-15T07:00:00+00:00",
                }]}
            return {}

        orig = mb.kalshi_get
        mb.kalshi_get = fake_get
        try:
            added = mb._reconcile_kalshi_positions()
        finally:
            mb.kalshi_get = orig
        self.assertEqual(added, 0)
        self.assertNotIn("KXLOWTDC-26MAY14-B52.5", mb._open_positions)


if __name__ == "__main__":
    unittest.main()
