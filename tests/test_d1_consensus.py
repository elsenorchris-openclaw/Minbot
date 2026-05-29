"""d-1 median-consensus μ (USE_D1_CONSENSUS) — 2026-05-29.
NBM warm-biased (+1.64°F, n=248 both-halves); d-1 μ uses median-consensus of
HRRR/NBM/NBP, EXCEPT audited HRRR-primary cities (CHI/OKC/HOU/NOLA/SAT) keep HRRR."""
import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import paper_min_bot as m


class TestD1Consensus(unittest.TestCase):
    def test_hardcoded_hrrr_city_keeps_hrrr(self):
        # CHI is hardcoded HRRR-primary -> keep HRRR even if NBM/NBP warm (the 05-29 CHI bust fix)
        mu, sig, src = m._d1_consensus_mu("KXLOWTCHI", 52.7, 56.8, {"mu": 55.0, "sigma": 2.5})
        self.assertEqual(mu, 52.7)
        self.assertEqual(src, "hrrr_d1_override")

    def test_default_city_uses_median_consensus(self):
        mu, sig, src = m._d1_consensus_mu("KXLOWTNYC", 60.0, 64.0, {"mu": 62.0, "sigma": 3.0})
        self.assertEqual(mu, 62.0)  # median(60,64,62)
        self.assertEqual(src, "consensus_d1")

    def test_warm_nbm_city_deprioritized_to_consensus(self):
        # MIA hardcoded "nbm" is NOT in the HRRR carve-out -> consensus (de-prioritizes warm NBM)
        mu, sig, src = m._d1_consensus_mu("KXLOWTMIA", 70.0, 73.0, {"mu": 71.0, "sigma": 2.0})
        self.assertEqual(src, "consensus_d1")

    def test_fewer_than_two_sources_falls_back(self):
        self.assertIsNone(m._d1_consensus_mu("KXLOWTNYC", 60.0, None, None))


if __name__ == "__main__":
    unittest.main()
