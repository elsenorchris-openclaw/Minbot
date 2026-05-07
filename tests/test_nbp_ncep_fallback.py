"""Tests for the 2026-05-07 NBP NCEP-primary + Last-Modified port from V2.

Pre-port: `_nbp_fetch_latest_bulletin()` was S3-only. When the S3 mirror
lagged behind NCEP (e.g. 5/7 01Z 4h+ late at the AWS end while nomads.ncep
already had it), min_bot was forced onto a stale fallback cycle and fired
"STALE CACHE FALLBACK" Discord alerts. V1+V2 both already used NCEP-first
parallel HEAD; this test pins min_bot's parity.

Pinned: NCEP base URL constant, parallel-HEAD helper, Last-Modified dict,
and that `_nbp_next_cycle_available()` no longer hardcodes the S3 host.
"""
from __future__ import annotations

import os
import unittest

_BOT_PATH = os.environ.get("BOT_PATH", "/home/ubuntu/paper_min_bot/paper_min_bot.py")


class NbpNcepFallbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(_BOT_PATH) as f:
            cls.src = f.read()

    def test_ncep_base_constant_defined(self):
        self.assertIn(
            '_NBP_NCEP_BASE = "https://nomads.ncep.noaa.gov/pub/data/nccf/com/blend/prod"',
            self.src,
            "_NBP_NCEP_BASE constant must point at the NCEP nomads endpoint",
        )

    def test_s3_base_constant_defined(self):
        self.assertIn(
            '_NBP_S3_BASE = "https://noaa-nbm-grib2-pds.s3.amazonaws.com"',
            self.src,
            "_NBP_S3_BASE constant must remain (S3 is still the fallback)",
        )

    def test_last_modified_dict_defined(self):
        self.assertIn("_nbp_last_modified: dict[str, str]", self.src,
            "Last-Modified cache dict must exist")

    def test_parallel_head_helper_defined(self):
        self.assertIn("def _nbp_parallel_head(", self.src,
            "parallel HEAD helper must exist")
        self.assertIn("ThreadPoolExecutor(max_workers=2", self.src,
            "helper must use a 2-thread pool")
        self.assertIn("as_completed(", self.src,
            "helper must use as_completed for first-200-wins semantics")

    def test_fetch_uses_parallel_head(self):
        """`_nbp_fetch_latest_bulletin` must HEAD via the parallel helper, not
        bare GET-against-S3."""
        idx = self.src.find("def _nbp_fetch_latest_bulletin(")
        self.assertGreater(idx, 0)
        end = self.src.find("\ndef ", idx + 1)
        block = self.src[idx:end]
        self.assertIn("_nbp_parallel_head(", block)
        self.assertIn("_NBP_NCEP_BASE", block)
        self.assertIn("_NBP_S3_BASE", block)

    def test_fetch_uses_last_modified_to_skip_redownload(self):
        idx = self.src.find("def _nbp_fetch_latest_bulletin(")
        end = self.src.find("\ndef ", idx + 1)
        block = self.src[idx:end]
        self.assertIn("_nbp_last_modified.get(winner_url)", block,
            "must check cached Last-Modified before re-downloading")
        self.assertIn("_nbp_last_modified[winner_url] = winner_lm", block,
            "must update Last-Modified after a successful new download")

    def test_fetch_logs_source(self):
        """Log line must include `via NCEP` or `via S3` so we can see which
        endpoint won at runtime."""
        idx = self.src.find("def _nbp_fetch_latest_bulletin(")
        end = self.src.find("\ndef ", idx + 1)
        block = self.src[idx:end]
        self.assertIn('"NCEP" if "nomads" in winner_url else "S3"', block)
        self.assertIn('via {src}', block)

    def test_fetch_heartbeat_on_unchanged(self):
        """When Last-Modified unchanged AND fetch returns None, the cache_ts
        must be bumped so the stale-cache watcher doesn't false-alarm during
        the natural inter-cycle gap."""
        idx = self.src.find("def _nbp_fetch_latest_bulletin(")
        end = self.src.find("\ndef ", idx + 1)
        block = self.src[idx:end]
        # Heartbeat before early-return on unchanged
        self.assertRegex(
            block,
            r"_nbp_last_modified\.get\(winner_url\) == winner_lm.*\n.*\n.*_nbp_cache_ts = time\.time\(\)",
            "heartbeat assignment must follow the unchanged-Last-Modified check",
        )

    def test_next_cycle_probe_uses_parallel_head(self):
        """`_nbp_next_cycle_available()` must also try NCEP, not only S3."""
        idx = self.src.find("def _nbp_next_cycle_available(")
        end = self.src.find("\ndef ", idx + 1)
        block = self.src[idx:end]
        self.assertIn("_nbp_parallel_head(", block,
            "next-cycle probe must use parallel-HEAD helper")
        # Old hard-coded S3-only probe must be gone
        self.assertNotIn(
            'url = (f"https://noaa-nbm-grib2-pds.s3.amazonaws.com/"',
            block,
            "old single-endpoint S3 HEAD probe must be removed",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
