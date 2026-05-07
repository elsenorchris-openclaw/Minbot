"""Tests for STALE_BRACKET gate (2026-05-07).

When a Kalshi market is still tradeable but its bracket date already passed
(CLI publish lag), the bot would otherwise repeatedly fire entries (saw
4 ENTRY_FILLED audit events at 04:10 UTC 2026-05-07 on 26APR28-B45.5,
26MAY01-T46/T56). Gate blocks those at execute_opportunity entry.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

_TMPDIR = tempfile.mkdtemp(prefix="stale_bracket_test_")
os.environ["HOME"] = _TMPDIR
sys.path.insert(0, "/tmp/paper_min_bot")

import paper_min_bot as pb  # noqa: E402

pb.DATA_DIR = Path(_TMPDIR) / "data"
pb.LOG_DIR = Path(_TMPDIR) / "logs"
pb.DATA_DIR.mkdir(parents=True, exist_ok=True)
pb.LOG_DIR.mkdir(parents=True, exist_ok=True)
pb.POSITIONS_FILE = pb.DATA_DIR / "positions.json"
pb.TRADES_FILE = pb.DATA_DIR / "trades.jsonl"
pb.SETTLEMENTS_FILE = pb.DATA_DIR / "settlements.jsonl"

SRC_PATH = Path(os.environ.get(
    "PAPER_MIN_BOT_PATH", "/home/ubuntu/paper_min_bot/paper_min_bot.py"))


def _src() -> str:
    return SRC_PATH.read_text()


class TestStaleBracketSourceWiring(unittest.TestCase):
    """Gate is wired into execute_opportunity, before addon-eligibility."""

    def test_constant_string_in_execute_opportunity(self):
        s = _src()
        ex_idx = s.index("def execute_opportunity(")
        ex_end = s.find("\n\ndef ", ex_idx)
        ex_block = s[ex_idx:ex_end]
        self.assertIn('"STALE_BRACKET"', ex_block)
        self.assertIn("_days_out_int(opp)", ex_block)

    def test_gate_fires_before_addon_eligibility(self):
        """If bracket is stale, addon path must not run either."""
        s = _src()
        ex_idx = s.index("def execute_opportunity(")
        ex_end = s.find("\n\ndef ", ex_idx)
        ex_block = s[ex_idx:ex_end]
        stale_pos = ex_block.index("STALE_BRACKET")
        addon_pos = ex_block.index("is_addon = False")
        self.assertLess(stale_pos, addon_pos,
            "STALE_BRACKET gate must precede addon-eligibility check")

    def test_blocks_negative_days_out(self):
        """Mocked _days_out_int returning -5 → execute_opportunity returns False."""
        opp = {
            "action": "BUY_NO",
            "entry_price": 0.30,
            "edge": 0.20,
            "market_ticker": "KXLOWTBOS-26APR28-B45.5",
            "date_str": "2026-04-28",
            "tz": "America/New_York",
            "ticker": "KXLOWTBOS-26APR28-B45.5",
            "model_prob": 0.10,
            "floor": 45.0,
            "cap": 47.0,
        }
        # Patch _days_out_int to force negative
        orig = pb._days_out_int
        pb._days_out_int = lambda _opp: -8
        try:
            result = pb.execute_opportunity(opp)
            self.assertFalse(result, "stale bracket should be blocked")
        finally:
            pb._days_out_int = orig

    def test_allows_zero_days_out(self):
        """days_out=0 (today) must NOT be blocked by STALE_BRACKET."""
        opp = {
            "action": "BUY_NO",
            "entry_price": 0.30,
            "edge": 0.20,
            "market_ticker": "KXLOWTBOS-26MAY07-B45.5",
            "date_str": "2026-05-07",
            "tz": "America/New_York",
            "ticker": "KXLOWTBOS-26MAY07-B45.5",
            "model_prob": 0.10,
            "floor": 45.0,
            "cap": 47.0,
        }
        orig = pb._days_out_int
        pb._days_out_int = lambda _opp: 0
        try:
            # We expect downstream gates / sizing to run, but the call should
            # not fast-fail at the STALE_BRACKET gate. We can't cleanly mock
            # _get_bankroll_cached etc., so we just ensure the audit-skip log
            # hasn't received a STALE_BRACKET row.
            audit_calls = []
            orig_audit = pb._audit_skip
            pb._audit_skip = lambda opp_, dec, msg: audit_calls.append(dec)
            try:
                pb.execute_opportunity(opp)
            finally:
                pb._audit_skip = orig_audit
            self.assertNotIn("STALE_BRACKET", audit_calls)
        finally:
            pb._days_out_int = orig

    def test_allows_none_days_out(self):
        """days_out=None (parse failure) must NOT be blocked — fall through."""
        opp = {
            "action": "BUY_NO",
            "entry_price": 0.30,
            "edge": 0.20,
            "market_ticker": "KXLOWTBOS-26MAY07-B45.5",
            "date_str": "bogus",
            "tz": "America/New_York",
            "ticker": "KXLOWTBOS-26MAY07-B45.5",
            "model_prob": 0.10,
            "floor": 45.0,
            "cap": 47.0,
        }
        orig = pb._days_out_int
        pb._days_out_int = lambda _opp: None
        try:
            audit_calls = []
            orig_audit = pb._audit_skip
            pb._audit_skip = lambda opp_, dec, msg: audit_calls.append(dec)
            try:
                pb.execute_opportunity(opp)
            finally:
                pb._audit_skip = orig_audit
            self.assertNotIn("STALE_BRACKET", audit_calls)
        finally:
            pb._days_out_int = orig

    def test_blocks_positive_one_negative(self):
        """days_out=-1 still blocks (yesterday's bracket — already passed)."""
        opp = {
            "action": "BUY_NO",
            "entry_price": 0.30,
            "edge": 0.20,
            "market_ticker": "KXLOWTBOS-26MAY06-B45.5",
            "date_str": "2026-05-06",
            "tz": "America/New_York",
            "ticker": "KXLOWTBOS-26MAY06-B45.5",
            "model_prob": 0.10,
            "floor": 45.0,
            "cap": 47.0,
        }
        orig = pb._days_out_int
        pb._days_out_int = lambda _opp: -1
        try:
            audit_calls = []
            orig_audit = pb._audit_skip
            pb._audit_skip = lambda opp_, dec, msg: audit_calls.append(dec)
            try:
                result = pb.execute_opportunity(opp)
            finally:
                pb._audit_skip = orig_audit
            self.assertFalse(result)
            self.assertIn("STALE_BRACKET", audit_calls)
        finally:
            pb._days_out_int = orig


if __name__ == "__main__":
    unittest.main()
