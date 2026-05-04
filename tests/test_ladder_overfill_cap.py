"""Tests for the 2026-05-04 ladder overfill-cap fix (V1 + V2 + min_bot).

Background: V1 PHX-26MAY04-B81.5 was 78 contracts / $41.58 against a $30
MAX_BET_USD cap. Trace: initial 42@52c placed (resting), 6 partial fills
during wait, ladder kicks in with cumulative_filled=6, places 36@53c
(executed at Kalshi but kalshi_get for fill_count_fp returned 0 due to
~100 ms order-endpoint propagation race), declared "no-fill" and bumped
to 36@54c (executed). End state on Kalshi: 6+36+36=78 fills. Bot's local
tally: 42 / $22.50 until next reconcile.

Fix: track `_placed_max_cost` — sum of (count * price) for the initial
order PLUS every subsequent rung — and use `max(cumulative_cost,
_placed_max_cost)` as the pessimistic budget reference. Stop the ladder
if pessimistic + next-rung-max would exceed MAX_BET_USD * 1.05.

Tests are pure source-string regression guards — no module imports, no
runtime simulation (the ladder block is deeply nested inside execute_*
which is hard to spin up in a unit test). They pin the structure of the
fix so a future refactor can't silently revert it.
"""
import os
import re
import unittest


_TARGETS = {
    "v1": os.environ.get("V1_PATH", "/home/ubuntu/kalshi_weather_bot.py"),
    "v2": os.environ.get("V2_PATH", "/home/ubuntu/obs-pipeline-bot/kalshi_weather_bot_v2.py"),
    "min_bot": os.environ.get("MIN_BOT_PATH", "/home/ubuntu/paper_min_bot/paper_min_bot.py"),
}


def _load(name):
    """Return source for the named bot or skip if not present."""
    path = _TARGETS[name]
    if not os.path.exists(path):
        raise unittest.SkipTest(f"{name} source not present at {path}")
    with open(path) as f:
        return f.read()


class LadderOverfillCapV1Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = _load("v1")

    def test_placed_max_cost_initialized_before_loop(self):
        """`_placed_max_cost = count * (price_cents / 100.0)` must appear
        before the `for _retry in range(LADDER_MAX_RETRIES):` loop."""
        m = re.search(
            r"_placed_max_cost\s*=\s*count\s*\*\s*\(price_cents\s*/\s*100\.0\)",
            self.src,
        )
        self.assertIsNotNone(m,
            "V1: `_placed_max_cost = count * (price_cents / 100.0)` not found")
        loop_pos = self.src.find("for _retry in range(LADDER_MAX_RETRIES):")
        self.assertGreater(loop_pos, 0, "ladder loop marker not found")
        self.assertLess(m.start(), loop_pos,
            "_placed_max_cost must be initialized BEFORE the ladder loop")

    def test_ladder_budget_cap_constant(self):
        """`_LADDER_BUDGET_CAP = MAX_BET_USD * 1.05` constant must be defined."""
        self.assertIn("_LADDER_BUDGET_CAP = MAX_BET_USD * 1.05", self.src,
            "V1: 1.05x slop budget cap constant must be defined")

    def test_pessimistic_cost_used_for_budget_left(self):
        """Inside the ladder loop, budget_left must use
        `max(cumulative_cost, _placed_max_cost)` — not raw cumulative_cost."""
        self.assertRegex(
            self.src,
            r"_pessimistic_cost\s*=\s*max\(cumulative_cost,\s*_placed_max_cost\)",
            "V1: _pessimistic_cost must be max(cumulative_cost, _placed_max_cost)")
        self.assertRegex(
            self.src,
            r"_budget_left\s*=\s*_LADDER_BUDGET_CAP\s*-\s*_pessimistic_cost",
            "V1: _budget_left must subtract pessimistic from cap")

    def test_placed_max_cost_bumped_after_place_order(self):
        """After a successful `place_order` returns a `_retry_oid`, the
        worst-case tracker must be bumped by `_max_count * _new_price`."""
        # Look for the bump statement
        self.assertIn("_placed_max_cost += _max_count * _new_price", self.src,
            "V1: _placed_max_cost must be bumped after place_order succeeds")

    def test_old_naive_budget_check_removed(self):
        """The old `_budget_left = MAX_BET_USD - cumulative_cost` line
        must be gone (it's the bug — uses bot's stale cumulative_cost)."""
        self.assertNotRegex(
            self.src,
            r"_budget_left\s*=\s*MAX_BET_USD\s*-\s*cumulative_cost",
            "V1: old naive budget check must be replaced by pessimistic version")


class LadderOverfillCapV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = _load("v2")

    def test_placed_max_cost_initialized_before_loop(self):
        m = re.search(
            r"_placed_max_cost\s*=\s*count\s*\*\s*\(price_cents\s*/\s*100\.0\)",
            self.src,
        )
        self.assertIsNotNone(m, "V2: _placed_max_cost init not found")
        loop_pos = self.src.find("for _retry in range(LADDER_MAX_RETRIES):")
        self.assertGreater(loop_pos, 0)
        self.assertLess(m.start(), loop_pos)

    def test_ladder_budget_cap_constant(self):
        self.assertIn("_LADDER_BUDGET_CAP = MAX_BET_USD * 1.05", self.src)

    def test_pessimistic_cost_used_for_budget_left(self):
        self.assertRegex(
            self.src,
            r"_pessimistic_cost\s*=\s*max\(cumulative_cost,\s*_placed_max_cost\)",
        )
        self.assertRegex(
            self.src,
            r"_budget_left\s*=\s*_LADDER_BUDGET_CAP\s*-\s*_pessimistic_cost",
        )

    def test_placed_max_cost_bumped_after_place_order(self):
        self.assertIn("_placed_max_cost += _max_count * _new_price", self.src)

    def test_old_naive_budget_check_removed(self):
        self.assertNotRegex(
            self.src,
            r"_budget_left\s*=\s*MAX_BET_USD\s*-\s*cumulative_cost",
        )


class LadderOverfillCapMinBotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = _load("min_bot")

    def test_placed_max_cost_initialized_before_loop(self):
        m = re.search(
            r"_placed_max_cost\s*=\s*count\s*\*\s*\(price_cents\s*/\s*100\.0\)",
            self.src,
        )
        self.assertIsNotNone(m, "min_bot: _placed_max_cost init not found")
        loop_pos = self.src.find("for _retry in range(LADDER_MAX_RETRIES):")
        self.assertGreater(loop_pos, 0)
        self.assertLess(m.start(), loop_pos)

    def test_ladder_budget_cap_uses_action_cap(self):
        """min_bot has a per-action cap (BUY_NO uses MAX_BET_USD, BUY_YES
        uses MAX_BET_BUY_YES_USD). The 1.05x slop must apply to whichever
        cap was selected for this action."""
        self.assertIn("_LADDER_BUDGET_CAP = _max_cap_usd * 1.05", self.src,
            "min_bot: cap multiplier must apply to per-action _max_cap_usd")

    def test_pessimistic_cost_used_for_budget_left(self):
        self.assertRegex(
            self.src,
            r"_pessimistic_cost\s*=\s*max\(cumulative_cost,\s*_placed_max_cost\)",
        )
        self.assertRegex(
            self.src,
            r"_budget_left\s*=\s*_LADDER_BUDGET_CAP\s*-\s*_pessimistic_cost",
        )

    def test_placed_max_cost_bumped_after_place_order(self):
        self.assertIn("_placed_max_cost += _max_count * _new_price", self.src)

    def test_old_naive_budget_check_removed(self):
        self.assertNotRegex(
            self.src,
            r"_budget_left\s*=\s*_max_cap_usd\s*-\s*cumulative_cost",
        )


class LadderOverfillCapBudgetMathTests(unittest.TestCase):
    """Pure-Python sanity check on the budget arithmetic, replaying the
    PHX-26MAY04-B81.5 trace. No bot import — just verify that with the
    new logic, the pessimistic check would have caught the over-fill."""

    def test_phx_trace_blocks_second_rung(self):
        # PHX trace:
        #   Initial: 42x @ 52c — kelly=$22.20, max if fully filled = $21.84
        #   After 6 maker fills: cumulative_cost=$3.12 (bot's view)
        #   Rung 1: place 36x @ 53c — max $19.08
        #   Rung 2 attempted: place 36x @ 54c — max $19.44
        # MAX_BET_USD = 30.00, cap = $31.50.
        MAX_BET = 30.00
        cap = MAX_BET * 1.05
        count = 42
        price_cents = 52

        # Initial worst-case
        placed_max = count * (price_cents / 100.0)  # $21.84
        cumulative_cost = 6 * 0.52  # $3.12 (bot's view after maker fills)

        # Before rung 1 (53c), check budget
        new_price_1 = 0.53
        pessimistic_before_r1 = max(cumulative_cost, placed_max)
        budget_left = cap - pessimistic_before_r1
        # placed_max ($21.84) > cumulative ($3.12) → pessimistic $21.84
        self.assertEqual(pessimistic_before_r1, placed_max)
        # Budget left = $31.50 - $21.84 = $9.66
        self.assertAlmostEqual(budget_left, 9.66, places=2)
        # At 53c, 9.66 / 0.53 = 18 contracts max (not 36!)
        max_count_r1 = int(budget_left / new_price_1)
        self.assertEqual(max_count_r1, 18,
            "Rung 1 should be size-capped at 18 contracts (not the 36 the buggy code placed)")

        # Simulate placing 18 @ 53c
        placed_max += 18 * new_price_1  # +$9.54 → $31.38

        # Before rung 2 (54c), check budget
        pessimistic_before_r2 = max(cumulative_cost, placed_max)
        budget_left = cap - pessimistic_before_r2
        # Budget left = $31.50 - $31.38 = $0.12 < $0.54
        self.assertLess(budget_left, 0.54,
            "Rung 2 must be blocked — budget left $0.12 < 54c")

    def test_normal_partial_fill_still_ladders(self):
        """Sanity: the cap must not block the ladder in the legitimate case
        where the initial only partially filled and there's plenty of headroom."""
        # Hypothetical: 20x @ 30c (max $6.00), 10 fills cumulative_cost=$3.00
        MAX_BET = 30.00
        cap = MAX_BET * 1.05  # $31.50
        count = 20
        price_cents = 30

        placed_max = count * (price_cents / 100.0)  # $6.00
        cumulative_cost = 3.00

        new_price = 0.31
        pessimistic = max(cumulative_cost, placed_max)
        budget_left = cap - pessimistic
        self.assertGreater(budget_left, new_price,
            "Legit ladder use must have budget headroom")
        # 10 remainder, $25.50 budget left, all 10 fit
        remainder = count - 10
        max_count = min(remainder, int(budget_left / new_price))
        self.assertEqual(max_count, 10,
            "Ladder should chase the full remainder when budget allows")


if __name__ == "__main__":
    unittest.main(verbosity=2)
