"""min_bot integration tests for the 2026-05-04 decision_log Phase B wiring.

Source-string regression guards for 2 callsites:
  1. Spread filter (cents): _dlog.record(stage='filter', decision='FILTERED_SPREAD')
     min_bot uses cents (0-100); the call must convert to fraction (0.0-1.0)
     before recording so the schema is consistent with V1/V2.
  2. Trade record write: _dlog.record(stage='exec', decision='ENTRY_FILLED')
     after _append_jsonl(_trades_file_today(), trade_record).

All callsites guarded by `if _dlog is not None:` and wrapped in try/except.
"""
import os
import re
import unittest


_BOT_PATH = os.environ.get("BOT_PATH", "/home/ubuntu/paper_min_bot/paper_min_bot.py")


class MinBotDecisionLogIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(_BOT_PATH) as f:
            cls.src = f.read()

    def test_imports_decision_log_via_shared_tools(self):
        self.assertIn("sys.path.insert(0, '/home/ubuntu/shared_tools')", self.src)
        self.assertIn("import decision_log as _dlog", self.src)
        self.assertRegex(
            self.src,
            r"try:\s*\n\s*import decision_log as _dlog\s*\n\s*except Exception:\s*\n\s*_dlog = None",
        )

    def test_spread_filter_calls_record(self):
        idx = self.src.find("spread {spread}c > {MAX_SPREAD_CENTS}c")
        self.assertGreater(idx, 0)
        block = self.src[idx:idx + 3500]
        self.assertIn("_dlog.record(", block)
        self.assertIn("bot='min_bot'", block)
        self.assertIn("stage='filter'", block)
        self.assertIn("decision='FILTERED_SPREAD'", block)
        self.assertIn("filter_name='SPREAD'", block)
        # Must convert cents to fraction
        self.assertIn("filter_value=spread / 100.0", block)
        self.assertIn("filter_threshold=MAX_SPREAD_CENTS / 100.0", block)

    def test_spread_filter_guarded_and_try_except(self):
        idx = self.src.find("spread {spread}c > {MAX_SPREAD_CENTS}c")
        block = self.src[idx:idx + 3500]
        self.assertIn("if _dlog is not None:", block)
        pat = re.compile(r"try:.*?_dlog\.record\(.*?\).*?except Exception:", re.DOTALL)
        self.assertRegex(block, pat)

    def test_trade_record_calls_record(self):
        idx = self.src.find("_append_jsonl(_trades_file_today(), trade_record)")
        self.assertGreater(idx, 0)
        block = self.src[idx:idx + 4000]
        self.assertIn("_dlog.record(", block)
        self.assertIn("bot='min_bot'", block)
        self.assertIn("stage='exec'", block)
        self.assertIn("decision='ENTRY_FILLED'", block)
        # Cents → fraction conversion required for cross-bot comparability
        self.assertIn("/ 100.0", block)
        # Min-bot specific: running_min, mu_blended, sigma_final present
        self.assertIn("running_min=opp.get('running_min')", block)
        self.assertIn("mu_blended=opp['mu']", block)

    def test_trade_record_guarded_and_try_except(self):
        idx = self.src.find("_append_jsonl(_trades_file_today(), trade_record)")
        block = self.src[idx:idx + 4000]
        self.assertIn("if _dlog is not None:", block)
        pat = re.compile(r"try:.*?_dlog\.record\(.*?\).*?except Exception:", re.DOTALL)
        self.assertRegex(block, pat)

    def test_existing_trade_record_path_preserved(self):
        self.assertIn("_append_jsonl(_trades_file_today(), trade_record)", self.src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
