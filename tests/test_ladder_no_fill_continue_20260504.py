"""Regression test for the 2026-05-04 OPTION B ladder fix.

Before: ladder broke on the first 0-fill price level. PHX-26MAY04-B64.5
filled 1/34 contracts at 50c, then ladder fetched ask=51c, got 0 fill,
broke the loop, and left 33 contracts orphaned for 10+ hours while the
market re-priced toward NO=94c (rendering all subsequent addons
unprofitable).

After: ladder continues to next price level on 0-fill, gated only by
the existing edge-floor + MAX_BET-cap + LADDER_MAX_RETRIES terminators.
Risk is bounded: max walk is LADDER_MAX_RETRIES (3) cents above the
initial fill price.
"""
import os
import re
import unittest
from pathlib import Path

BOT_PATH = os.environ.get(
    "PAPER_MIN_BOT_PATH", "/home/ubuntu/paper_min_bot/paper_min_bot.py"
)


def src():
    return Path(BOT_PATH).read_text()


class TestLadderNoFillContinues(unittest.TestCase):
    """The ladder must walk to the next price level when a rung returns 0 fill."""

    def test_no_fill_branch_does_not_break(self):
        """Find the ladder's `if _r_filled > 0: ... else: ...` block and
        verify the else branch does NOT contain `break` (the bug we fixed)."""
        s = src()
        # Locate the ladder no-fill branch by its log line pattern. The fixed
        # version says "trying next rung"; the broken version said "stopping".
        idx = s.find("ladder no-fill on")
        self.assertNotEqual(idx, -1, "ladder no-fill log line must exist")
        # The 'else' that this log lives inside must not be followed by `break`.
        # Walk forward from the log line up to the next non-blank line; verify
        # it's NOT a bare `break` statement.
        # Look at the next 200 chars after the log line.
        snippet = s[idx:idx + 500]
        self.assertNotIn("— stopping", snippet,
            "Old behavior: ladder no-fill would log 'stopping' and break. "
            "Option B (2026-05-04) replaces this with 'trying next rung'.")
        self.assertIn("trying next rung", snippet,
            "Option B fix: log line should say 'trying next rung'.")

    def test_no_fill_branch_falls_through_to_loop_continuation(self):
        """The else branch must end with the normal loop continuation —
        last_ask_c update — not break."""
        s = src()
        # Find the else block following _r_filled. Pattern:
        #   if _r_filled > 0:  ...
        #   else:
        #       try: kalshi_delete(...)
        #       ...
        #       log("ladder no-fill ...")
        #   last_ask_c = _new_ask_c    <-- this should follow at outer indent
        m = re.search(
            r"if _r_filled > 0:.*?else:.*?ladder no-fill.*?\n\s+last_ask_c = _new_ask_c",
            s, re.DOTALL,
        )
        self.assertIsNotNone(m,
            "After Option B, the else branch must fall through to "
            "`last_ask_c = _new_ask_c` (at outer indent), letting the "
            "for loop continue with the next retry. Earlier `break` removed.")

    def test_loop_still_bounded_by_max_retries(self):
        """LADDER_MAX_RETRIES still bounds the loop (no infinite walk)."""
        s = src()
        m = re.search(r"^LADDER_MAX_RETRIES = (\d+)\s*$", s, re.MULTILINE)
        self.assertIsNotNone(m, "LADDER_MAX_RETRIES constant must be present")
        self.assertGreaterEqual(int(m.group(1)), 1,
            "LADDER_MAX_RETRIES must be >= 1 to allow at least one retry")
        self.assertLessEqual(int(m.group(1)), 10,
            "LADDER_MAX_RETRIES must be <= 10 to bound worst-case latency "
            "(each rung adds ORDER_FILL_TIMEOUT_SEC of waiting)")
        self.assertIn("for _retry in range(LADDER_MAX_RETRIES):", s,
            "Ladder loop must use range(LADDER_MAX_RETRIES) to bound iterations")

    def test_loop_still_terminates_on_edge_drop(self):
        """The edge-floor terminator (new_edge < MIN_EDGE) must still break."""
        s = src()
        # Should have a `break` in the edge-check branch. Pattern in ladder:
        #   if _new_edge < MIN_EDGE:
        #       log(...)
        #       break
        m = re.search(
            r"if _new_edge < MIN_EDGE:\s*\n\s+log\(.*?\)\s*\n\s+break",
            s, re.DOTALL,
        )
        self.assertIsNotNone(m,
            "Edge floor must still break the ladder. If walking the ask up "
            "pushes new_edge below MIN_EDGE, the loop must terminate.")

    def test_loop_still_terminates_on_max_bet_cap(self):
        """The MAX_BET cap terminator must still break.
        2026-05-04: budget reference is now pessimistic (max of bot tally
        and worst-case placed-rung cost) against _LADDER_BUDGET_CAP =
        _max_cap_usd * 1.05. See PHX-26MAY04-B81.5 incident."""
        s = src()
        # Pattern: `if _budget_left < _new_price: ...; break`
        m = re.search(
            r"_budget_left\s*=\s*_LADDER_BUDGET_CAP\s*-\s*_pessimistic_cost\s*\n"
            r"\s+if _budget_left < _new_price:\s*\n"
            r"\s+log\(.*?\)\s*\n"
            r"\s+break",
            s, re.DOTALL,
        )
        self.assertIsNotNone(m,
            "MAX_BET cap must still break the ladder when pessimistic budget runs out.")


if __name__ == "__main__":
    unittest.main()
