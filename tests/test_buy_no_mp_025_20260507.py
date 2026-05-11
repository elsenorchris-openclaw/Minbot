"""Regression test for DIRECTIONAL_BUY_NO_MAX_MP. Constant pinned at 0.25.

History: 0.40 (2026-04-27) → 0.20 (2026-04-29 night) → 0.25 (2026-05-07) →
0.30 (2026-05-10) → 0.25 (2026-05-11 eve REVERT). The 0.30 raise was
reverted same-session as a stack-interaction failure (paired H_2_0 +
DIRECTIONAL loosens removed both layers on the BUY_NO mp 0.25-0.30 band
and KSEA-26MAY11-B50.5 lost -$50 MTM through the gap).
"""
import os, re, unittest, sys
from pathlib import Path

BOT_PATH = os.environ.get(
    "MIN_BOT_PATH", "/home/ubuntu/paper_min_bot/paper_min_bot.py")


def src():
    return Path(BOT_PATH).read_text()


class TestDirectionalBuyNoMaxMp(unittest.TestCase):
    def test_constant_value(self):
        s = src()
        m = re.search(
            r"^DIRECTIONAL_BUY_NO_MAX_MP\s*=\s*0\.25\b",
            s, re.MULTILINE)
        self.assertIsNotNone(m,
            "DIRECTIONAL_BUY_NO_MAX_MP must be 0.25 (reverted from 0.30 on 2026-05-11 eve).")

    def test_constant_history_documented(self):
        s = src()
        # Comment must mention the deploy chain (history is helpful for future audits)
        self.assertIn("0.25 (2026-05-11 eve REVERT)", s,
            "Constant history chain must document the 2026-05-11 revert.")

    def test_filter_logic_present(self):
        s = src()
        m = re.search(
            r"if action == \"BUY_NO\" and mp > DIRECTIONAL_BUY_NO_MAX_MP:",
            s)
        self.assertIsNotNone(m,
            "Filter logic must still gate BUY_NO entries on the constant.")


class TestBucketBoundaryBehavior(unittest.TestCase):
    """Functional: threshold reverted to 0.25 on 2026-05-11 eve. Blocks mp 0.27
    (today's SEA-class) and still passes mp 0.22 (well below threshold)."""

    def test_module_imports_without_error(self):
        sys.path.insert(0, "/home/ubuntu/paper_min_bot")
        if "paper_min_bot" in sys.modules:
            del sys.modules["paper_min_bot"]
        import paper_min_bot as m
        self.assertEqual(m.DIRECTIONAL_BUY_NO_MAX_MP, 0.25)

    def test_threshold_blocks_027_passes_022(self):
        sys.path.insert(0, "/home/ubuntu/paper_min_bot")
        if "paper_min_bot" in sys.modules:
            del sys.modules["paper_min_bot"]
        import paper_min_bot as m
        # mp 0.27 must BE blocked (SEA-class entry; above threshold)
        self.assertTrue(0.27 > m.DIRECTIONAL_BUY_NO_MAX_MP)
        # mp 0.22 must NOT be blocked (well below threshold)
        self.assertFalse(0.22 > m.DIRECTIONAL_BUY_NO_MAX_MP)
        # mp 0.25 must NOT be blocked (boundary equal, not greater than)
        self.assertFalse(0.25 > m.DIRECTIONAL_BUY_NO_MAX_MP)


if __name__ == "__main__":
    unittest.main()
