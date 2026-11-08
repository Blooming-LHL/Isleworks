import unittest
from isleworks.domain import ScenarioConfig
from isleworks.evolution import review, validate_proposal

class EvolutionTest(unittest.TestCase):
    def test_valid_delta_and_reject_multiple(self):
        c = ScenarioConfig(); p = validate_proposal(c, {"parent_hash": c.config_hash, "initial_fish_units_delta": 250})
        self.assertEqual(p.initial_fish_units, 2250)
        with self.assertRaises(ValueError): validate_proposal(c, {"parent_hash": c.config_hash, "initial_fish_units_delta": 1, "fishing_abundance_percent": 80})

    def test_offline_review_is_explicit(self):
        r = review({"metrics": {}, "evidence": {}}, ScenarioConfig())
        self.assertEqual(r["status"], "offline"); self.assertIsNone(r["proposal"])

if __name__ == "__main__": unittest.main()
