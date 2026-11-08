import unittest

from isleworks.agents.decision import (
    CONTRACT_VERSION, DECISION_SCHEMA, StructuredDecisionError, prepare_decision,
)
from isleworks.engine.preconditions import DecisionError, validate_today


def candidate(**changes):
    value = {"goal": "food", "rationale": "steady", "days": [
        {"labor": ["fish", "fish", "gather", "gather"], "orders": []}
    ]}
    value.update(changes)
    return value


class DecisionV3Test(unittest.TestCase):
    def test_schema_is_strict_supported_subset(self):
        self.assertFalse(DECISION_SCHEMA["additionalProperties"])
        self.assertNotIn("minItems", str(DECISION_SCHEMA))
        self.assertEqual(CONTRACT_VERSION, "resident_decision_v1")

    def test_compilation_binds_identity_and_is_retry_deterministic(self):
        context = {"resident_id": "r1", "day": 3, "run_id": "run", "decision_revision": 2}
        plan, metadata = prepare_decision(candidate(), context)
        retry, retry_metadata = prepare_decision(candidate(), context)
        self.assertEqual(plan.to_dict(), retry.to_dict())
        self.assertEqual(metadata["binding_hash"], retry_metadata["binding_hash"])
        self.assertEqual(plan.resident_id, "r1")
        self.assertEqual(plan.created_day, 3)

    def test_registered_wrapper_and_multiple_errors(self):
        plan, metadata = prepare_decision({"type": "json_object", "decision": candidate(),
                                           "resident_id": "r1"}, {"resident_id": "r1", "day": 1})
        self.assertIn("removed_transport_type", metadata["normalization"])
        self.assertIn("unwrapped_decision", metadata["normalization"])
        bad = candidate(days=[{"labor": ["air"], "orders": [{"side": "buy"}]}])
        with self.assertRaises(StructuredDecisionError) as exc:
            prepare_decision(bad, {"resident_id": "r1", "day": 1})
        self.assertGreaterEqual(len(exc.exception.errors), 3)

    def test_today_preflight_checks_craft_and_warns_on_market_dependency(self):
        raw = candidate(days=[{"labor": ["craft", "craft", "craft", "craft"], "orders": [
            {"side": "buy", "good": "fiber", "quantity": 4, "price": 100, "net_ids": []}
        ]}])
        plan, _ = prepare_decision(raw, {"resident_id": "r1", "day": 1,
            "self": {"fish": 1000, "fiber": 0, "fiber_yield": 1, "craft_blocks": 4},
            "public": {"rules": {"nets_enabled": True}}})
        result = validate_today(plan, {"resident_id": "r1", "day": 1,
            "self": {"fish": 1000, "fiber": 0, "fiber_yield": 1, "craft_blocks": 4},
            "public": {"rules": {"nets_enabled": True}}})
        self.assertEqual(result["warnings"][0]["code"], "market_dependent_craft")

    def test_today_rejects_unknown_net_and_buy_overcommit(self):
        raw = candidate(days=[{"labor": ["rest"] * 4, "orders": [
            {"side": "buy", "good": "fiber", "quantity": 2, "price": 1000, "net_ids": []},
            {"side": "sell", "good": "net", "quantity": 1, "price": 100, "net_ids": ["missing"]},
        ]}])
        plan, _ = prepare_decision(raw, {"resident_id": "r1", "day": 1,
            "self": {"fish": 10000, "fiber": 0},
            "own_nets": [{"id": "missing", "owner": "r1", "durability": 20}],
            "public": {"rules": {"nets_enabled": True}}})
        with self.assertRaises(DecisionError) as exc:
            validate_today(plan, {"resident_id": "r1", "day": 1,
                "self": {"fish": 1, "fiber": 0}, "own_nets": [],
                "public": {"rules": {"nets_enabled": True}}})
        self.assertGreaterEqual(len(exc.exception.errors), 2)


if __name__ == "__main__":
    unittest.main()
