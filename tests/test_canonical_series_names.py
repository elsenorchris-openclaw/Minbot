"""Regression test: verify every per-series dict in min_bot uses canonical
Kalshi ticker names (no typos like KXLOWTLAS for Vegas).

Triggered 2026-05-09 audit: KXLOWTLAS appeared in 4 places across
paper_min_bot.py + tools/auto_select_per_series_primary.py, silently
breaking auto-primary lookup for Vegas (forecast fell through to default
'hrrr' instead of selected 'nbp', leaving 0.18°F MAE on the table d-0
and 2.01°F MAE d-1).
"""
from __future__ import annotations
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent

# Canonical 20 Kalshi LOW-temp series tickers (verified 2026-05-09 against
# weather_trades.jsonl: every series with non-zero trades is listed below).
CANONICAL_KXLOW_SERIES = frozenset({
    "KXLOWTNYC", "KXLOWTSFO", "KXLOWTSEA", "KXLOWTLAX", "KXLOWTMIA",
    "KXLOWTHOU", "KXLOWTPHX", "KXLOWTPHIL", "KXLOWTOKC", "KXLOWTBOS",
    "KXLOWTCHI", "KXLOWTMIN", "KXLOWTDEN", "KXLOWTLV",  # NOT KXLOWTLAS
    "KXLOWTATL", "KXLOWTDC", "KXLOWTDAL", "KXLOWTAUS",
    "KXLOWTSATX", "KXLOWTNOLA",
})


def _extract_dict_keys(src: str, dict_name: str) -> set[str]:
    """Find `dict_name = {...}` in source, return all KXLOW* keys."""
    pattern = rf'\b{re.escape(dict_name)}\b[^{{]*\{{(.*?)\n\}}'
    m = re.search(pattern, src, re.DOTALL)
    if not m:
        return set()
    body = m.group(1)
    return set(re.findall(r'"(KXLOW[A-Z]+)"\s*:', body))


class TestCanonicalSeriesNames(unittest.TestCase):
    """No KXLOWTLAS or other typos in any per-series dict."""

    def setUp(self):
        self.bot_src = (ROOT / "paper_min_bot.py").read_text()
        self.tool_src = (ROOT / "tools" / "auto_select_per_series_primary.py").read_text()

    def test_no_kxlowtlas_in_bot(self):
        """KXLOWTLAS must not appear as a dict key anywhere in paper_min_bot.py.
        (It can appear in comments — substring match would false-positive.)"""
        # Match `"KXLOWTLAS"` as a key (followed by colon).
        bad = re.findall(r'"KXLOWTLAS"\s*:', self.bot_src)
        self.assertEqual(bad, [], "KXLOWTLAS used as dict key in paper_min_bot.py")

    def test_no_kxlowtlas_in_tool(self):
        bad = re.findall(r'"KXLOWTLAS"\s*:', self.tool_src)
        self.assertEqual(bad, [], "KXLOWTLAS used as dict key in auto_select tool")

    def test_series_to_icao_complete(self):
        """_SERIES_TO_ICAO must have every canonical KXLOW series."""
        keys = _extract_dict_keys(self.bot_src, "_SERIES_TO_ICAO")
        missing = CANONICAL_KXLOW_SERIES - keys
        extra = keys - CANONICAL_KXLOW_SERIES
        self.assertEqual(missing, set(), f"_SERIES_TO_ICAO missing: {missing}")
        self.assertEqual(extra, set(), f"_SERIES_TO_ICAO has non-canonical keys: {extra}")

    def test_per_series_d0_primary_canonical(self):
        """PER_SERIES_D0_PRIMARY (subset of canonical) has no typos."""
        keys = _extract_dict_keys(self.bot_src, "PER_SERIES_D0_PRIMARY")
        bad = keys - CANONICAL_KXLOW_SERIES
        self.assertEqual(bad, set(), f"PER_SERIES_D0_PRIMARY has typos: {bad}")

    def test_per_series_d1_primary_canonical(self):
        keys = _extract_dict_keys(self.bot_src, "PER_SERIES_D1_PRIMARY")
        bad = keys - CANONICAL_KXLOW_SERIES
        self.assertEqual(bad, set(), f"PER_SERIES_D1_PRIMARY has typos: {bad}")

    def test_nbp_station_map_canonical(self):
        keys = _extract_dict_keys(self.bot_src, "NBP_STATION_MAP")
        bad = keys - CANONICAL_KXLOW_SERIES
        self.assertEqual(bad, set(), f"NBP_STATION_MAP has typos: {bad}")

    def test_tool_hardcoded_d0_canonical(self):
        keys = _extract_dict_keys(self.tool_src, "HARDCODED_D0_PRIMARY")
        bad = keys - CANONICAL_KXLOW_SERIES
        self.assertEqual(bad, set(), f"HARDCODED_D0_PRIMARY has typos: {bad}")

    def test_tool_hardcoded_d1_canonical(self):
        keys = _extract_dict_keys(self.tool_src, "HARDCODED_D1_PRIMARY")
        bad = keys - CANONICAL_KXLOW_SERIES
        self.assertEqual(bad, set(), f"HARDCODED_D1_PRIMARY has typos: {bad}")

    def test_tool_series_to_icao_complete(self):
        keys = _extract_dict_keys(self.tool_src, "SERIES_TO_ICAO")
        missing = CANONICAL_KXLOW_SERIES - keys
        extra = keys - CANONICAL_KXLOW_SERIES
        self.assertEqual(missing, set(), f"tool SERIES_TO_ICAO missing: {missing}")
        self.assertEqual(extra, set(), f"tool SERIES_TO_ICAO has non-canonical keys: {extra}")


class TestVegasResolves(unittest.TestCase):
    """End-to-end: get_d0_primary('KXLOWTLV') resolves correctly through
    the auto-primary chain — confirming the Vegas typo is fixed."""

    def test_vegas_d0_resolves_to_nbp_via_hardcoded(self):
        """With no auto-primary JSON loaded, get_d0_primary('KXLOWTLV')
        falls through to PER_SERIES_D0_PRIMARY which has nbp."""
        import sys
        # Don't del sys.modules["paper_min_bot"] — that breaks any other
        # test file that captured a `pb = paper_min_bot` reference at import
        # time. Plain import is sufficient (module is loaded once per run).
        sys.path.insert(0, str(ROOT))
        try:
            import paper_min_bot as bot
            # Invalidate the auto-primary cache so we exercise the
            # hardcoded fallback path (which is what the typo broke).
            with bot._auto_primary_lock:
                bot._auto_primary_cache["valid"] = False
                bot._auto_primary_cache["d0"] = {}
                bot._auto_primary_cache["d1"] = {}
            # ICAO lookup must succeed (was returning None pre-fix)
            self.assertEqual(bot._SERIES_TO_ICAO.get("KXLOWTLV"), "KLAS",
                             "KXLOWTLV should map to KLAS")
            # d-0 hardcoded fallback should resolve to nbp
            self.assertEqual(bot.PER_SERIES_D0_PRIMARY.get("KXLOWTLV"), "nbp",
                             "Vegas d-0 hardcoded should be nbp")
        finally:
            if str(ROOT) in sys.path:
                sys.path.remove(str(ROOT))


if __name__ == "__main__":
    unittest.main()
