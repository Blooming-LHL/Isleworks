import unittest

from isleworks.domain import ScenarioConfig
from isleworks.evolution import (REVIEW_VERSION, ReviewValidationError,
                                  compile_review, review, review_from_response)


class ReviewV2Test(unittest.TestCase):
    def setUp(self):
        self.config = ScenarioConfig()
        self.metrics = {"metrics": {"nets_made": 11, "nets_broken": 10},
                        "evidence": {f"e{i}": {"kind": "event"} for i in range(28)},
                        "decision_counts": {"degraded": 0, "ok": 20}}

    def test_all_known_refs_are_accepted_and_deduplicated(self):
        raw = {"summary": "复盘", "claims": [
            {"metric_id": "nets_made", "evidence_ids": [f"e{i}" for i in range(11)] + ["e1"], "interpretation": "甲"},
            {"metric_id": "nets_broken", "evidence_ids": [f"e{i}" for i in range(10)], "interpretation": "乙"},
        ], "proposal": None}
        result = review_from_response(raw, self.metrics, self.config)
        self.assertEqual(len(result["claims"][0]["evidence_ids"]), 11)
        self.assertEqual(REVIEW_VERSION, "llm-2")

    def test_unknown_reference_has_safe_precise_location(self):
        raw = {"summary": "复盘", "claims": [{"metric_id": "nets_made",
                "evidence_ids": ["missing"], "interpretation": "甲"}], "proposal": None}
        with self.assertRaises(ReviewValidationError) as caught:
            review_from_response(raw, self.metrics, self.config)
        self.assertEqual((caught.exception.code, caught.exception.path),
                         ("unknown_reference", "$.claims[0].evidence_ids[0]"))

    def test_saved_response_is_reusable_and_degraded_proposal_remains_visible(self):
        raw = {"summary": "建议调整", "claims": [], "proposal": {
            "parent_hash": self.config.config_hash, "initial_fish_units_delta": 250,
            "evidence_ids": ["e0"], "hypothesis": "改善", "risk": "低"}}
        self.metrics["decision_counts"] = {"degraded": 3, "ok": 7}
        class Gateway:
            def request(self, *args, **kwargs):
                self.validator = kwargs["validator"]
                return raw
        result = review(self.metrics, self.config, Gateway())
        self.assertEqual(result["status"], "diagnostic_only")
        self.assertIsNotNone(result["proposal"])
        self.assertFalse(result["can_apply"])
        self.assertIsNone(result["candidate"])

    def test_compilation_uses_run_source_and_screens_valid_candidate(self):
        raw = {"summary": "建议调整", "claims": [], "proposal": {
            "parent_hash": self.config.config_hash, "initial_fish_units_delta": 250,
            "evidence_ids": ["e0"], "hypothesis": "改善", "risk": "低"}}
        self.metrics["run_mode"] = "rules_baseline"
        result = compile_review(raw, self.metrics, self.config)
        self.assertEqual(result["source_mode"], "rules_baseline")
        self.assertTrue(result["eligible_for_screening"])
        self.assertFalse(result["can_apply"])


if __name__ == "__main__":
    unittest.main()
