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

    # ── 2026-05-04 night: eval-loop _log_skip → _audit_skip migration ────

    def test_audit_skip_helper_defined(self):
        """_audit_skip helper must exist next to _log_skip — wraps both
        the existing log behavior AND decision_log.record() in one call."""
        self.assertIn("def _audit_skip(opp: dict, decision: str, msg: str)", self.src,
            "_audit_skip helper must be defined")
        # Helper must call decision_log.record with stage='gate'
        idx = self.src.find("def _audit_skip(opp: dict, decision: str, msg: str)")
        block = self.src[idx:idx + 3000]
        self.assertIn("_dlog.record(", block)
        self.assertIn("stage='gate'", block)
        self.assertIn("bot='min_bot'", block)
        self.assertIn("blocker_reason=msg", block)
        # Cents → fraction conversion present
        self.assertIn("/ 100.0", block)
        # Wrapped in try/except so a record() error can't crash the bot
        self.assertIn("except Exception:", block)

    def test_each_gate_uses_audit_skip(self):
        """Each gate-skip-and-return-False site in execute_opportunity must
        use _audit_skip(...) (not bare _log_skip) so the eval state lands
        in bot_decisions.sqlite. This is the 14-callsite migration."""
        # Each tag must appear paired with _audit_skip
        for tag in (
            "OBS_CONFIRMED_LOSER", "MAX_EDGE_EXCEEDED",
            "MODEL_PROB_OUT_OF_RANGE",
            "DIRECTIONAL_NO_DISAGREE", "DIRECTIONAL_YES_DISAGREE",
            "NO_THIGH", "YES_TAIL_MARGIN", "ABS_DISTANCE",
            "F2A", "COASTAL_TIGHT_FLOOR",
            "MODEL_MARKET_DISAGREE", "MSG",
            "H_2_DISAGREE", "DISAGREEMENT", "MU_VS_RM",
            "BUDGET", "NO_BANKROLL",
        ):
            self.assertIn(f'_audit_skip(opp, "{tag}"', self.src,
                f"_audit_skip(opp, \"{tag}\", ...) callsite must exist")

    def test_no_gate_uses_bare_log_skip_with_return_false(self):
        """After the migration, NO `_log_skip(ticker, ...) ... return False`
        pattern should remain inside execute_opportunity (would mean a gate
        was missed). Exception: the spread filter at the end of the function
        which has its own decision_log.record() callsite for stage='filter'."""
        # Find execute_opportunity body
        idx = self.src.find("def execute_opportunity(opp: dict)")
        self.assertGreater(idx, 0)
        # Find the function end heuristically (next top-level def or 8000 chars)
        body = self.src[idx:idx + 8000]
        # Allowed: spread filter line. Disallow other bare _log_skip(ticker... + return False
        # Match: `_log_skip(ticker, ...)` followed by (optionally) blank line(s)
        # then `return False`. Scan line-by-line.
        lines = body.splitlines()
        gate_violations = []
        for i, line in enumerate(lines):
            if "_log_skip(ticker" not in line:
                continue
            if "spread" in line and "MAX_SPREAD_CENTS" in line:
                continue  # the spread filter is intentionally bare
            # Look ahead up to 8 lines for `return False`
            for j in range(i + 1, min(i + 8, len(lines))):
                if "return False" in lines[j]:
                    gate_violations.append((i, line.strip()))
                    break
        self.assertEqual(gate_violations, [],
            f"unconverted gate _log_skip→return False sites: {gate_violations}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
