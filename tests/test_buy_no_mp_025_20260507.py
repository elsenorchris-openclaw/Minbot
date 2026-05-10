"""Regression test for 2026-05-07 DIRECTIONAL_BUY_NO_MAX_MP 0.20 → 0.25.

Backtest evidence:
  - mp bucket [0.20, 0.25): 67% win rate, +$5.38 avg (n=3) — STILL profitable
  - mp bucket [0.25, 0.30): empty — clean cliff
  - mp bucket [0.30, 0.50): 0% win rate (3L) — clean losers, still blocked
  - mp bucket [0.50, 1.00): 0% win rate (1L) — clean loser, still blocked

Lift estimate: +$1.83 minimum (4 cleanly caught losers at mp > 0.25)
                up to +$14 if [0.20, 0.25) signal generalizes.
Helps:hurts: 4:0 (perfect — only blocks losers in this data).
"""
import os, re, unittest, sys
from pathlib import Path

BOT_PATH = os.environ.get(
    "MIN_BOT_PATH", "/home/ubuntu/paper_min_bot/paper_min_bot.py")


def src():
    return Path(BOT_PATH).read_text()


class TestDirectionalBuyNoMaxMpRaised(unittest.TestCase):
    def test_constant_value_025(self):
        s = src()
        m = re.search(
            r"^DIRECTIONAL_BUY_NO_MAX_MP\s*=\s*0\.30\b",
            s, re.MULTILINE)
        self.assertIsNotNone(m,
            "DIRECTIONAL_BUY_NO_MAX_MP must be 0.30 (was 0.25).")

    def test_constant_history_documented(self):
        s = src()
        # Comment must mention the deploy chain (history is helpful for future audits)
        self.assertIn("0.25 (2026-05-07) → 0.30 (2026-05-10)", s,
            "Constant history chain must be documented in the comment.")

    def test_filter_logic_present(self):
        s = src()
        m = re.search(
            r"if action == \"BUY_NO\" and mp > DIRECTIONAL_BUY_NO_MAX_MP:",
            s)
        self.assertIsNotNone(m,
            "Filter logic must still gate BUY_NO entries on the constant.")


class TestBucketBoundaryBehavior(unittest.TestCase):
    """Functional: threshold raised to 0.30 on 2026-05-10. Accepts mp 0.28
    (was blocked at 0.25) and still blocks mp 0.32 (above new threshold)."""

    def test_module_imports_without_error(self):
        sys.path.insert(0, "/home/ubuntu/paper_min_bot")
        if "paper_min_bot" in sys.modules:
            del sys.modules["paper_min_bot"]
        import paper_min_bot as m
        self.assertEqual(m.DIRECTIONAL_BUY_NO_MAX_MP, 0.30)

    def test_threshold_accepts_028_blocks_032(self):
        sys.path.insert(0, "/home/ubuntu/paper_min_bot")
        if "paper_min_bot" in sys.modules:
            del sys.modules["paper_min_bot"]
        import paper_min_bot as m
        # mp 0.28 must NOT be blocked by directional gate (was blocked at 0.25)
        self.assertFalse(0.28 > m.DIRECTIONAL_BUY_NO_MAX_MP)
        # mp 0.32 must STILL be blocked (above threshold)
        self.assertTrue(0.32 > m.DIRECTIONAL_BUY_NO_MAX_MP)
        # mp 0.25 must NOT be blocked (boundary case still safe)
        self.assertFalse(0.25 > m.DIRECTIONAL_BUY_NO_MAX_MP)


if __name__ == "__main__":
    unittest.main()
