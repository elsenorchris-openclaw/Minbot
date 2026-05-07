"""Tests for 2026-05-06 _cli_aligned_rmin wiring in min_bot.

Verifies:
1. USE_CLI_ALIGNED_RMIN flipped True
2. _get_metar_running_min helper exists (queries awc/ldm sources)
3. _cli_aligned_rmin accepts station+climate_date params
4. calc_bracket_probability_min has centralized cli-align at entry
5. _check_obs_confirmed_alive uses cli-aligned rm
6. _check_obs_confirmed_loser uses cli-aligned rm
7. _check_position_obs_confirmed_loser_for_exit uses cli-aligned rm
"""
import os
import re
import unittest
from pathlib import Path

BOT_PATH = os.environ.get(
    "MIN_BOT_PATH", "/home/ubuntu/paper_min_bot/paper_min_bot.py")


def src():
    return Path(BOT_PATH).read_text()


class TestFlagAndHelpers(unittest.TestCase):
    def test_flag_flipped_on(self):
        s = src()
        m = re.search(r"^USE_CLI_ALIGNED_RMIN = True\b", s, re.MULTILINE)
        self.assertIsNotNone(m,
            "USE_CLI_ALIGNED_RMIN must be flipped to True (was False).")

    def test_get_metar_running_min_defined(self):
        s = src()
        self.assertIn("def _get_metar_running_min(station: str, climate_date: str)", s,
            "_get_metar_running_min helper must exist.")

    def test_get_metar_uses_awc_ldm_sources_only(self):
        s = src()
        # Must filter source IN ('awc','ldm') so we don't pull madis-truncated rows
        self.assertIn("source IN ('awc','ldm')", s,
            "METAR rmin lookup must filter to awc/ldm sources only.")

    def test_cli_aligned_rmin_accepts_station_date(self):
        s = src()
        m = re.search(
            r"def _cli_aligned_rmin\(running_min: Optional\[float\],\s+station: Optional\[str\] = None,\s+climate_date: Optional\[str\] = None\)",
            s)
        self.assertIsNotNone(m,
            "_cli_aligned_rmin must accept station + climate_date kwargs.")

    def test_cli_aligned_rmin_uses_metar_lookup(self):
        s = src()
        # When station+date provided, helper should call _get_metar_running_min
        self.assertIn("rm_metar = _get_metar_running_min(station, climate_date)", s,
            "_cli_aligned_rmin must call _get_metar_running_min when args present.")


class TestCalcBracketProbAlignment(unittest.TestCase):
    """Centralized fix in calc_bracket_probability_min."""

    def test_function_signature_accepts_station_date(self):
        s = src()
        m = re.search(
            r"def calc_bracket_probability_min\(.*?\*,\s+station: Optional\[str\] = None,\s+date_str: Optional\[str\] = None,?\s*\) -> float:",
            s, re.DOTALL)
        self.assertIsNotNone(m,
            "calc_bracket_probability_min must accept station + date_str kwargs.")

    def test_function_aligns_rm_at_entry(self):
        s = src()
        # Centralized fix: align rm before any downstream use
        m = re.search(
            r"if running_min is not None and station and date_str and USE_CLI_ALIGNED_RMIN:.*?_aligned_rm = _cli_aligned_rmin\(running_min, station, date_str\)",
            s, re.DOTALL)
        self.assertIsNotNone(m,
            "calc_bracket_probability_min must apply cli-align at function entry.")

    def test_callers_pass_station_date(self):
        s = src()
        # Locate call-name match positions; for each, scan forward to the
        # closing `)` that ends the call (depth-aware) and check kwargs.
        call_positions = [m.start() for m in re.finditer(r"calc_bracket_probability_min\(", s)]
        call_blocks = []
        for start in call_positions:
            # Skip the def line
            line_start = s.rfind("\n", 0, start) + 1
            if "def " in s[line_start:start]:
                continue
            depth = 0
            i = start + len("calc_bracket_probability_min(") - 1
            while i < len(s):
                c = s[i]
                if c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                    if depth == 0:
                        call_blocks.append(s[start:i+1])
                        break
                i += 1
        self.assertGreaterEqual(len(call_blocks), 2,
            f"Expected ≥2 call sites, found {len(call_blocks)}")
        for block in call_blocks:
            self.assertIn("station=", block,
                "Each calc_bracket_probability_min call must pass station=")
            self.assertIn("date_str=", block,
                "Each call must pass date_str=")


class TestObsConfirmedFunctions(unittest.TestCase):
    """All 3 obs_confirmed functions wire the helper."""

    def _function_block(self, s, fn_def):
        """Return the source block from `def fn_def` to the next top-level def/class.
        Uses indentation to find the end of the function."""
        start = s.find(fn_def)
        if start < 0:
            return None
        # Walk forward to next top-level (no leading whitespace) def/class
        # OR end of file. Function ends just before that.
        i = start + len(fn_def)
        while i < len(s):
            nl = s.find("\n", i)
            if nl < 0:
                return s[start:]
            i = nl + 1
            line = s[i:s.find("\n", i) if s.find("\n", i) >= 0 else len(s)]
            # Top-level def/class — start of the next function
            if (line.startswith("def ") or line.startswith("class ")
                    or (line.startswith("# ") and "def " in s[i:i+200])):
                return s[start:i]
        return s[start:]

    def test_check_obs_confirmed_alive_aligns(self):
        s = src()
        block = self._function_block(s, "def _check_obs_confirmed_alive(opp_or_pos: dict) -> bool:")
        self.assertIsNotNone(block, "Couldn't locate _check_obs_confirmed_alive")
        self.assertIn("_aligned = _cli_aligned_rmin(rm, _station, _date_str)", block,
            "_check_obs_confirmed_alive must align rm via cli helper.")

    def test_check_obs_confirmed_loser_aligns(self):
        s = src()
        block = self._function_block(s, "def _check_obs_confirmed_loser(opp_or_pos: dict) -> bool:")
        self.assertIsNotNone(block)
        self.assertIn("_aligned = _cli_aligned_rmin(rm, _station, _date_str)", block,
            "_check_obs_confirmed_loser must align rm via cli helper.")

    def test_position_obs_confirmed_loser_for_exit_aligns(self):
        s = src()
        block = self._function_block(s, "def _check_position_obs_confirmed_loser_for_exit(pos: dict, rm: float) -> bool:")
        self.assertIsNotNone(block)
        self.assertIn("_aligned = _cli_aligned_rmin(rm, _station, _date_str)", block,
            "_check_position_obs_confirmed_loser_for_exit must align rm.")


class TestHelperBehavior(unittest.TestCase):
    """Functional checks of the helper itself (no DB needed for None-input cases)."""

    def test_returns_none_on_none_input(self):
        import sys
        sys.path.insert(0, "/home/ubuntu/paper_min_bot")
        # Reload so flag flip takes effect
        import importlib
        if "paper_min_bot" in sys.modules:
            del sys.modules["paper_min_bot"]
        m = importlib.import_module("paper_min_bot")
        self.assertIsNone(m._cli_aligned_rmin(None))
        self.assertIsNone(m._cli_aligned_rmin(None, "KSEA", "2026-05-06"))

    def test_returns_int_when_station_date_missing(self):
        import sys
        sys.path.insert(0, "/home/ubuntu/paper_min_bot")
        import importlib
        if "paper_min_bot" in sys.modules:
            del sys.modules["paper_min_bot"]
        m = importlib.import_module("paper_min_bot")
        # Without station/date, falls through to plain int-round
        self.assertEqual(m._cli_aligned_rmin(55.4), 55.0)
        self.assertEqual(m._cli_aligned_rmin(55.94), 56.0)


if __name__ == "__main__":
    unittest.main()
