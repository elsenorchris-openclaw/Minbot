"""Regression tests for the 2026-05-03 laddered ask-chase fix.

Forward audit (3-day window) found 80%+ of intended Kelly orphaned on
first-entry BUY_NO partial fills at thin Kalshi books:
  PHX-MAY04-B64.5: 1/34 (96% orphaned, 40% edge)
  AUS-MAY03-B50.5: 2/26 (92% orphaned, 26% edge)
  SFO-MAY03-B54.5: 10/49 (80% orphaned, 20% edge)

Fix: when partial-fill on first-entry BUY_NO, walk the no_ask up by 1c
per retry and re-place the remainder, gated by:
  - new_edge = 1 - mp_yes - new_price  >= MIN_EDGE  (Chris's gate)
  - cumulative_cost <= MAX_BET_USD
  - LADDER_MAX_RETRIES (3) max attempts

Backtest (depth_per_cent=2 conservative): +$14 over 3 days, optimal at
existing MIN_EDGE=0.20 floor. Self-consistent: SFO with edge=20% mp=19%
correctly stops with "no edge room" (already at MIN_EDGE).
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


class TestLadderChase(unittest.TestCase):

    def test_ladder_max_retries_constant_present(self):
        s = src()
        m = re.search(r"^LADDER_MAX_RETRIES = (\d+)\s*$", s, re.MULTILINE)
        self.assertIsNotNone(m, "LADDER_MAX_RETRIES constant must be defined")
        # 3 is the calibrated value: caps execution time + slippage compounding
        self.assertEqual(m.group(1), "3",
                         "Default should be 3 retries (calibrated cap)")

    def test_ladder_block_present(self):
        """Laddered chase block must exist after the partial-fill cancel,
        gated correctly on first-entry BUY_NO + edge floor."""
        s = src()
        # Look for the gated entry — all four conditions must be present
        m = re.search(
            r"if \(filled > 0 and filled < count and not is_addon\s*\n"
            r"\s*and action == \"BUY_NO\"\s*\n"
            r"\s*and edge >= MIN_EDGE\):",
            s,
        )
        self.assertIsNotNone(
            m,
            "Ladder gate must require: filled > 0 (had partial), filled < count "
            "(under-filled), not is_addon (first entry only), action=BUY_NO, "
            "edge>=MIN_EDGE (only chase if entry was strong-edge)"
        )

    def test_ladder_uses_min_edge_floor_at_each_step(self):
        """At each retry, _new_edge must be checked against MIN_EDGE.
        This is Chris's gate — only chase higher prices if edge still passes."""
        s = src()
        m = re.search(
            r"_new_edge = 1\.0 - mp_yes - _new_price\s*\n"
            r"\s*if _new_edge < MIN_EDGE:",
            s,
        )
        self.assertIsNotNone(
            m,
            "Each ladder step must compute new_edge from mp_yes + new_price "
            "and abort when new_edge < MIN_EDGE."
        )

    def test_ladder_respects_max_bet_cap(self):
        """Cumulative cost must not exceed MAX_BET_USD across initial + ladder."""
        s = src()
        m = re.search(
            r"_budget_left = MAX_BET_USD - cumulative_cost\s*\n"
            r"\s*if _budget_left < _new_price:",
            s,
        )
        self.assertIsNotNone(
            m,
            "Ladder must check remaining budget against MAX_BET_USD before "
            "each retry placement."
        )

    def test_ladder_respects_max_retries(self):
        s = src()
        m = re.search(
            r"for _retry in range\(LADDER_MAX_RETRIES\):",
            s,
        )
        self.assertIsNotNone(
            m,
            "Ladder loop must use range(LADDER_MAX_RETRIES) to bound execution."
        )

    def test_ladder_progresses_against_stale_quote(self):
        """If new fetched ask <= last_ask (stale or sticky), advance by 1c
        manually so the chase doesn't loop on the same price."""
        s = src()
        m = re.search(
            r"if _new_ask_c <= last_ask_c:\s*\n"
            r"\s*_new_ask_c = last_ask_c \+ 1",
            s,
        )
        self.assertIsNotNone(
            m,
            "Ladder must guarantee progress (last_ask_c + 1) when stale "
            "ask is returned."
        )

    def test_ladder_skips_addons(self):
        """Add-ons already iterate via the existing scan loop — no ladder needed."""
        s = src()
        # Addons are excluded by the `not is_addon` guard
        self.assertIn("not is_addon", s)
        # Verify not is_addon appears in the ladder gate
        m = re.search(
            r"if \(filled > 0 and filled < count and not is_addon",
            s,
        )
        self.assertIsNotNone(m,
            "Ladder gate must explicitly exclude addons via `not is_addon`")

    def test_ladder_skips_buy_yes(self):
        """BUY_YES is capped at MAX_BET_BUY_YES_USD=$5 — chase budget is
        too small to be worth the complexity. Only BUY_NO benefits."""
        s = src()
        m = re.search(
            r"and action == \"BUY_NO\"",
            s,
        )
        self.assertIsNotNone(m,
            "Ladder gate must restrict to action=BUY_NO (BUY_YES has $5 cap).")

    def test_ladder_updates_filled_and_actual_cost(self):
        """After ladder fills, `filled` and `actual_cost` and `price`
        (weighted-avg) must be updated so downstream _budget_record + trade_record
        reflect the cumulative position, not just the initial fill."""
        s = src()
        m = re.search(
            r"if cumulative_filled > filled:\s*\n"
            r"\s*filled = cumulative_filled\s*\n"
            r"\s*actual_cost = cumulative_cost\s*\n"
            r"\s*price = actual_cost / filled if filled > 0 else price",
            s,
        )
        self.assertIsNotNone(
            m,
            "After ladder fills, filled/actual_cost/price must be updated to "
            "cumulative values (matches addon weighted-avg convention)."
        )

    def test_ladder_logs_with_trade_level(self):
        """Successful ladder fills should log at 'trade' level for Discord
        visibility (matches ENTRY/ADDON convention)."""
        s = src()
        m = re.search(
            r"log\(f\"  LADDER \+\{_r_filled\}x @ \{_new_ask_c\}c on \{ticker\}",
            s,
        )
        self.assertIsNotNone(
            m,
            "Ladder fill log line must use 'LADDER +Nx @ Pc on TICKER' format "
            "for grep-ability, at trade level for Discord."
        )

    def test_ladder_handles_fetch_failure_gracefully(self):
        """If kalshi_get throws (network error, etc.), ladder must abort
        cleanly without crashing the caller."""
        s = src()
        m = re.search(
            r"try:\s*\n"
            r"\s*_md = kalshi_get\(f\"/trade-api/v2/markets/\{ticker\}\"\)"
            r"[\s\S]{0,200}?"
            r"except Exception as _le:\s*\n"
            r"\s*log\(f\"  ladder fetch failed",
            s,
        )
        self.assertIsNotNone(
            m,
            "kalshi_get failure inside ladder must be caught and break, not "
            "propagate to caller."
        )

    def test_existing_partial_fill_log_preserved(self):
        """The original 'partial fill X/Y' log line must remain — it's an
        important audit signal even when ladder fills the gap."""
        s = src()
        m = re.search(
            r'log\(f"  partial fill \{filled\}/\{count\} on \{ticker\}; cancelled remainder"\)',
            s,
        )
        self.assertIsNotNone(
            m,
            "Original 'partial fill X/Y' log must be preserved (audit signal)."
        )


if __name__ == "__main__":
    unittest.main()
