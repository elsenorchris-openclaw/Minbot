"""Tests for min_bot Open-Meteo dynamic-TTL helpers + batched fetch refactor.

Plan C (2026-04-30): min_bot's HRRR refresh switched to a single batched
20-city request (vs the prior per-city loop which made 20 HTTP calls per
refresh). Cache-trigger TTL also swapped from a static 600s to a dynamic
helper that drops to 5s during HH:43-55 UTC pub window.

NBM stayed at 3600s general TTL (best_match doesn't have a tight pub window),
but was also moved to batched fetching.
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path("/home/ubuntu/paper_min_bot")
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class _FrozenClock:
    def __init__(self, frozen: datetime):
        self._frozen = frozen

    def now(self, tz=None):
        if tz is None:
            return self._frozen.replace(tzinfo=None)
        return self._frozen.astimezone(tz)


def _import_helpers():
    """Extract HRRR_TTL_SEC, HRRR_TTL_SEC_NEW_RUN_WINDOW, _hrrr_dynamic_ttl
    by parsing min_bot source — avoids running global init."""
    import ast

    src = (REPO_ROOT / "paper_min_bot.py").read_text()
    tree = ast.parse(src)
    targets = {
        "HRRR_TTL_SEC", "HRRR_TTL_SEC_NEW_RUN_WINDOW",
        "NBM_OM_TTL_SEC",
        "_hrrr_dynamic_ttl",
    }
    nodes = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in targets:
                    nodes.append(node)
                    break
        elif isinstance(node, ast.FunctionDef) and node.name in targets:
            nodes.append(node)
    ns: dict = {"datetime": datetime, "timezone": timezone}
    src_block = "\n".join(ast.unparse(n) for n in nodes)
    exec(compile(src_block, "<helpers>", "exec"), ns)
    return ns


def _ttl_at(ns: dict, hour: int, minute: int) -> int:
    frozen = datetime(2026, 4, 30, hour, minute, 0, tzinfo=timezone.utc)
    real_dt = ns["datetime"]
    ns["datetime"] = _FrozenClock(frozen)
    try:
        return ns["_hrrr_dynamic_ttl"]()
    finally:
        ns["datetime"] = real_dt


class HrrrDynamicTtlTests(unittest.TestCase):
    def setUp(self):
        self.ns = _import_helpers()

    def test_pub_window_lower_edge(self):
        self.assertEqual(_ttl_at(self.ns, 12, 43), self.ns["HRRR_TTL_SEC_NEW_RUN_WINDOW"])

    def test_pub_window_upper_edge(self):
        self.assertEqual(_ttl_at(self.ns, 12, 55), self.ns["HRRR_TTL_SEC_NEW_RUN_WINDOW"])

    def test_outside_window_just_before(self):
        self.assertEqual(_ttl_at(self.ns, 12, 42), self.ns["HRRR_TTL_SEC"])

    def test_outside_window_just_after(self):
        self.assertEqual(_ttl_at(self.ns, 12, 56), self.ns["HRRR_TTL_SEC"])

    def test_outside_window_top_of_hour(self):
        self.assertEqual(_ttl_at(self.ns, 12, 0), self.ns["HRRR_TTL_SEC"])

    def test_constants_match_plan_c_baseline(self):
        # Plan C: 60s general / 5s pub-window
        self.assertEqual(self.ns["HRRR_TTL_SEC"], 60)
        self.assertEqual(self.ns["HRRR_TTL_SEC_NEW_RUN_WINDOW"], 5)

    def test_nbm_ttl_unchanged_at_3600(self):
        # min_bot uses best_match for "NBM" cache (free-tier model). No tight
        # pub window applies, so we kept 3600s and just batched the fetches.
        self.assertEqual(self.ns["NBM_OM_TTL_SEC"], 3600)


class BatchedFetchSignatureTests(unittest.TestCase):
    """Verify the source declares the batched-fetch helper and that it's
    wired up by the two refresh-public functions. We can't run the actual
    HTTP without a sandbox, but a static check against the file confirms
    the per-city loop is gone (the regression we'd most worry about)."""

    def test_batched_helper_function_defined(self):
        src = (REPO_ROOT / "paper_min_bot.py").read_text()
        self.assertIn("def _fetch_open_meteo_batched(", src)

    def test_refresh_nbm_uses_batched_helper(self):
        src = (REPO_ROOT / "paper_min_bot.py").read_text()
        # The function should now call the batched helper, NOT iterate
        # CITIES.items() per-city.
        idx = src.find("def refresh_nbm_om_forecasts")
        end = src.find("\n\ndef ", idx + 1)
        body = src[idx:end]
        self.assertIn("_fetch_open_meteo_batched", body)
        # The old per-city pattern was `for series, meta in CITIES.items():`
        # If it's still there in the public refresh function we have a
        # regression — the bot would still cost 20× more requests.
        self.assertNotIn("for series, meta in CITIES.items():", body)

    def test_refresh_hrrr_uses_batched_call(self):
        src = (REPO_ROOT / "paper_min_bot.py").read_text()
        idx = src.find("def refresh_hrrr_forecasts")
        end = src.find("\n\ndef ", idx + 1)
        body = src[idx:end]
        # New pattern: lats = ",".join(...) and one httpx.get
        self.assertIn('lats = ",".join', body)
        # Multi-location response handling: zip(CITIES.keys(), data)
        self.assertIn("zip(CITIES.keys(), data)", body)
        # apikey= must be in the params (paid endpoint, ToS-compliant)
        self.assertIn('"apikey"', body)

    def test_paid_endpoint_only_for_hrrr(self):
        # HRRR requires customer-api (paid). NBM stays on free best_match.
        src = (REPO_ROOT / "paper_min_bot.py").read_text()
        idx = src.find("def refresh_hrrr_forecasts")
        end = src.find("\n\ndef ", idx + 1)
        body = src[idx:end]
        self.assertIn("customer-api.open-meteo.com", body,
                      "HRRR must use paid Customer endpoint")


if __name__ == "__main__":
    unittest.main(verbosity=2)
