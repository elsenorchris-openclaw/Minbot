"""Tests for the 2026-05-05 min_bot NBM-OM customer-API switch.

Pre-fix: `refresh_nbm_om_forecasts()` hardcoded `https://api.open-meteo.com`
(free tier, 10k/day shared-IP cap) and didn't pass an apikey. When the
free-tier daily quota was exhausted (08:23 UTC on 2026-05-05), the
fetcher retried every ~17s for 6+ hours, producing log spam.

Fix: switch to `customer-api.open-meteo.com` + apikey, matching the HRRR
fetcher pattern that V1/V2 already use. Same `OPEN_METEO_API_KEY` env var.

These tests pin the source so a future refactor can't silently revert
to the free endpoint.
"""
import os
import re
import unittest


_BOT_PATH = os.environ.get(
    "BOT_PATH", "/home/ubuntu/paper_min_bot/paper_min_bot.py"
)


class NbmOmCustomerApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(_BOT_PATH) as f:
            cls.src = f.read()

    def test_nbm_om_uses_customer_api_endpoint(self):
        """`refresh_nbm_om_forecasts` must use the paid customer-api host."""
        idx = self.src.find("def refresh_nbm_om_forecasts()")
        self.assertGreater(idx, 0)
        block = self.src[idx:idx + 1500]
        self.assertIn("customer-api.open-meteo.com", block,
            "refresh_nbm_om_forecasts must use customer-api endpoint")
        # And NOT the free endpoint
        self.assertNotIn('"https://api.open-meteo.com/v1/forecast"', block,
            "free api.open-meteo.com endpoint must be removed from this function")

    def test_nbm_om_loads_and_passes_apikey(self):
        """Must call _load_open_meteo_key() and pass the key as apikey."""
        idx = self.src.find("def refresh_nbm_om_forecasts()")
        block = self.src[idx:idx + 1500]
        self.assertIn("_load_open_meteo_key()", block,
            "refresh_nbm_om_forecasts must call _load_open_meteo_key()")
        self.assertIn("apikey=_OPEN_METEO_API_KEY", block,
            "refresh_nbm_om_forecasts must pass apikey kwarg")

    def test_nbm_om_disables_when_key_missing(self):
        """Must short-circuit (with a warn) when OPEN_METEO_API_KEY isn't
        in .env, so a misconfigured deploy doesn't 401 in a tight loop."""
        idx = self.src.find("def refresh_nbm_om_forecasts()")
        block = self.src[idx:idx + 1500]
        self.assertRegex(
            block,
            r"if not _OPEN_METEO_API_KEY:",
            "must guard the fetch on the key being loaded",
        )
        self.assertIn("NBM-OM disabled:", block,
            "must emit a warn-level log when key is missing")

    def test_no_other_free_open_meteo_endpoint_remaining(self):
        """Belt-and-suspenders: bot-wide, no live call should hit the free
        api.open-meteo.com host. (Tests + comments mentioning it are fine,
        but no `httpx.get` / `requests.get` to that bare host.)"""
        # Match the full URL string used in production callsites
        for live_form in (
            '"https://api.open-meteo.com/v1/forecast"',
            "'https://api.open-meteo.com/v1/forecast'",
        ):
            self.assertNotIn(live_form, self.src,
                f"live free-endpoint URL string {live_form} must be gone")

    def test_nbm_om_uses_real_nbm_model_not_best_match(self):
        """2026-05-07: model identifier must be `ncep_nbm_conus`, NOT
        `best_match`. Open-Meteo's `best_match` is an auto-picker that
        returns HRRR for US short-range, so calling it under the NBM-OM
        label silently aliased min_bot's third forecast source to its
        second (HRRR). V1/V2 both use `ncep_nbm_conus` — same key here."""
        idx = self.src.find("def refresh_nbm_om_forecasts()")
        block = self.src[idx:idx + 1500]
        self.assertIn('model="ncep_nbm_conus"', block,
            "refresh_nbm_om_forecasts must use real NBM model identifier")
        self.assertNotIn('model="best_match"', block,
            "best_match auto-picker must not be used (returns HRRR for US "
            "short-range, defeats the purpose of NBM as a third source)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
