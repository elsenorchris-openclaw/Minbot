"""Tests for the 2026-05-06 min_bot backtest stack interface.

Verifies tools/backtest_filters.py mirrors V1+V2's --stack scaffolding:
- SCENARIOS dict has the expected deployed-filter entries
- LIVE_CHAIN matches deployed scenarios (deploy gate)
- stacked_predicate ORs correctly
- predicates correctly identify expected min_bot patterns
"""
import importlib.util
import os
import unittest

TOOL_PATH = os.environ.get(
    "MIN_BOT_TOOL_PATH",
    "/home/ubuntu/paper_min_bot/tools/backtest_filters.py")


def _load_tool():
    spec = importlib.util.spec_from_file_location("min_bot_bt", TOOL_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestScenariosDict(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_tool()

    def test_scenarios_dict_exists(self):
        self.assertTrue(hasattr(self.mod, "SCENARIOS"))
        self.assertGreater(len(self.mod.SCENARIOS), 1)

    def test_scenarios_include_core_min_bot_filters(self):
        for required in ("model_prob_oor", "max_edge_exceeded",
                         "buy_no_max_mp", "buy_yes_min_mp",
                         "obs_confirmed_loser", "live"):
            self.assertIn(required, self.mod.SCENARIOS,
                f"min_bot SCENARIOS missing '{required}'")

    def test_each_scenario_callable_and_safe(self):
        for name, (fn, desc) in self.mod.SCENARIOS.items():
            self.assertTrue(callable(fn))
            # Empty dict shouldn't raise
            res = fn({})
            self.assertIn(bool(res), (True, False))


class TestLiveChain(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_tool()

    def test_live_chain_exists_and_non_empty(self):
        self.assertTrue(hasattr(self.mod, "LIVE_CHAIN"))
        self.assertGreater(len(self.mod.LIVE_CHAIN), 0)

    def test_live_chain_members_in_scenarios(self):
        for name in self.mod.LIVE_CHAIN:
            self.assertIn(name, self.mod.SCENARIOS)

    def test_live_meta_combines_members(self):
        rec = {"action": "BUY_NO", "model_prob": 0.50}  # fires buy_no_max_mp
        self.assertTrue(self.mod.SCENARIOS["buy_no_max_mp"][0](rec))
        self.assertTrue(self.mod.SCENARIOS["live"][0](rec))


class TestPredicateMechanics(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_tool()

    def test_buy_no_max_mp(self):
        fn = self.mod.SCENARIOS["buy_no_max_mp"][0]
        # BUY_NO + mp > 0.20 -> directional disagreement -> BLOCK
        self.assertTrue(fn({"action": "BUY_NO", "model_prob": 0.30}))
        self.assertFalse(fn({"action": "BUY_NO", "model_prob": 0.10}))
        self.assertFalse(fn({"action": "BUY_YES", "model_prob": 0.50}))

    def test_buy_yes_min_mp(self):
        fn = self.mod.SCENARIOS["buy_yes_min_mp"][0]
        # BUY_YES + mp < 0.65 -> directional disagreement -> BLOCK
        self.assertTrue(fn({"action": "BUY_YES", "model_prob": 0.55}))
        self.assertFalse(fn({"action": "BUY_YES", "model_prob": 0.70}))
        self.assertFalse(fn({"action": "BUY_NO", "model_prob": 0.30}))

    def test_max_edge_exceeded(self):
        fn = self.mod.SCENARIOS["max_edge_exceeded"][0]
        self.assertTrue(fn({"edge": 0.75}))
        self.assertFalse(fn({"edge": 0.50}))

    def test_obs_confirmed_loser(self):
        fn = self.mod.SCENARIOS["obs_confirmed_loser"][0]
        # rm at 49 IN [48, 50] bracket -> confirmed loser
        self.assertTrue(fn({"running_min_at_entry": 49, "floor": 48, "cap": 50}))
        # rm above bracket -> not confirmed
        self.assertFalse(fn({"running_min_at_entry": 51, "floor": 48, "cap": 50}))


class TestStackedPredicate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_tool()

    def test_stacked_predicate_ors_correctly(self):
        sp = self.mod.stacked_predicate("buy_no_max_mp,max_edge_exceeded")
        self.assertTrue(sp({"action": "BUY_NO", "model_prob": 0.30}))
        self.assertTrue(sp({"edge": 0.80}))
        self.assertFalse(sp({"action": "BUY_NO", "model_prob": 0.10, "edge": 0.30}))

    def test_unknown_raises(self):
        with self.assertRaises(SystemExit):
            self.mod.stacked_predicate("nonexistent_filter")


class TestDataLoading(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_tool()

    def test_load_settled_returns_list(self):
        # If settlements.jsonl doesn't exist on this system, returns empty list
        pool = self.mod.load_settled()
        self.assertIsInstance(pool, list)

    def test_compute_pool_stats_handles_empty(self):
        s = self.mod.compute_pool_stats([])
        self.assertEqual(s["n"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
