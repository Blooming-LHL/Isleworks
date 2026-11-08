"""Additional high-risk offline regressions for the v0.3 contracts."""

import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from isleworks.agents import build_context
from isleworks.agents.decision import StructuredDecisionError, prepare_decision
from isleworks.domain import DailyIntent, Order, ScenarioConfig
from isleworks.engine import init_world, step_day
from isleworks.engine.preconditions import DecisionError
from isleworks.llm import ModelGateway, ModelError
from isleworks.runner import Runner
from isleworks.storage import Store


def raw(labor=("rest",) * 4, *, orders=None, days=1, rationale="fixture"):
    return {"goal": "survive", "rationale": rationale,
            "days": [{"labor": list(labor), "orders": list(orders or [])}
                     for _ in range(days)]}


class V3RegressionTest(unittest.TestCase):
    def context(self, config=None, rid="r5"):
        config = config or ScenarioConfig()
        state = init_world(config)
        return state, build_context(config, state, rid)

    def test_crafter_four_blocks_with_eight_fiber_makes_two_nets(self):
        config = ScenarioConfig(days=1)
        state = init_world(config)
        state.residents["r5"].fiber = 8
        intent = DailyIntent("r5", 1, ("craft",) * 4)
        commit = step_day(state, [intent] + [DailyIntent(r, 1) for r in state.residents if r != "r5"], config)
        self.assertEqual(sum(n.owner == "r5" for n in commit.state.nets.values()), 2)

    def test_unfillable_fiber_sale_keeps_craft_conditional_and_engine_crafts(self):
        config = ScenarioConfig(days=1)
        state = init_world(config)
        state.residents["r5"].fiber = 8
        sell = Order("sell-fiber", "r5", "sell", "fiber", 4, 99999)
        intent = DailyIntent("r5", 1, ("craft",) * 4, (sell,))
        _, context = self.context(config, "r5")
        context["self"]["fiber"] = 8
        plan, metadata = prepare_decision(raw(("craft",) * 4, orders=[{
            "side": "sell", "good": "fiber", "quantity": 4, "price": 99999, "net_ids": []
        }]), context)
        self.assertTrue(any(w["code"] == "market_dependent_craft" for w in metadata["warnings"]))
        commit = step_day(state, [intent] + [DailyIntent(r, 1) for r in state.residents if r != "r5"], config)
        self.assertEqual(sum(n.owner == "r5" for n in commit.state.nets.values()), 2)

    def test_buy_quantity_cannot_fabricate_four_fiber_for_craft(self):
        _, context = self.context(ScenarioConfig(days=1), "r5")
        with self.assertRaises(DecisionError) as caught:
            prepare_decision(raw(("craft",) * 4, orders=[{
                "side": "buy", "good": "fiber", "quantity": 1, "price": 1, "net_ids": []
            }]), context)
        self.assertEqual(caught.exception.errors[0]["code"], "insufficient_fiber_for_craft")

    def test_leftover_craft_block_is_rejected(self):
        _, context = self.context(ScenarioConfig(days=1), "r5")
        context["self"]["fiber"] = 8
        with self.assertRaises(DecisionError) as caught:
            prepare_decision(raw(("craft", "craft", "craft", "rest")), context)
        self.assertEqual(caught.exception.errors[0]["code"], "insufficient_contiguous_craft_blocks")

    def test_day30_three_day_decision_fails_at_precise_horizon_path(self):
        _, context = self.context(ScenarioConfig(days=30), "r1")
        context["day"] = 29
        with self.assertRaises(StructuredDecisionError) as caught:
            prepare_decision(raw(days=3), context)
        self.assertEqual(caught.exception.errors[0]["path"], "$.days")

    def test_identity_and_type_fields_are_not_fabricated(self):
        _, context = self.context(ScenarioConfig(days=1), "r1")
        for candidate in (
            {"goal": "x", "rationale": "x", "days": [{"labor": ["rest"] * 4, "orders": [], "resident_id": "r1"}]},
            {"goal": "x", "rationale": "x", "days": [{"labor": ["rest"] * 4, "orders": [], "day": 1}]},
        ):
            with self.assertRaises(StructuredDecisionError):
                prepare_decision(candidate, context)

    def test_strict_failed_candidate_is_retained_in_bounded_retry_messages(self):
        seen = []
        huge = {"goal": "x" * 2100, "rationale": "r", "days": []}

        def requestor(system, user, **kwargs):
            seen.append(user)
            return huge if len(seen) == 1 else raw()

        gateway = ModelGateway("http://localhost:1", "fake", decision_mode="json_object",
                              input_chars=24000, max_calls=2, requestor=requestor)
        with self.assertRaises(ModelError):
            gateway.request("s", "u", validator=lambda value: (_ for _ in ()).throw(ValueError("bad")))
        self.assertIn("Failed candidate", seen[-1])
        self.assertIn("x" * 2000, seen[-1])

    def test_credential_bearing_decision_is_rejected_and_not_archived(self):
        secret = "sk-test-secret"
        gateway = ModelGateway("http://localhost:1", "fake", api_key=secret,
                               decision_mode="json_object", requestor=lambda *a, **k:
                               {"goal": secret, "rationale": "x", "days": [{"labor": ["rest"] * 4, "orders": []}]})
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        store = Store(Path(folder.name) / "credential.sqlite3")
        runner = Runner(store, gateway)
        run_id = runner.create(ScenarioConfig(days=1, residents=1), mode="llm", review_enabled=False)
        self.assertEqual(runner.advance(run_id)["status"], "completed")
        self.assertNotIn(secret, json.dumps(store.archive(run_id)))

    def test_timeout_strict_request_is_uncertain_once_and_runner_continues(self):
        calls = []
        def requestor(*args, **kwargs):
            calls.append(1)
            raise TimeoutError()
        gateway = ModelGateway("http://localhost:1", "fake", decision_mode="tool_strict",
                              max_calls=3, requestor=requestor)
        with self.assertRaises(ModelError) as caught:
            gateway.request("s", "u", output_contract={"type": "object"})
        self.assertTrue(caught.exception.fatal)
        self.assertEqual(gateway.attempts, 1)
        self.assertEqual(len(calls), 1)

    def test_completed_call_records_contract_mode_and_validation_metadata(self):
        gateway = ModelGateway("http://localhost:1", "fake", decision_mode="json_object",
                               requestor=lambda *a, **k: {"type": "json_object", **raw()})
        gateway.request("s", "u", validator=lambda value: {"normalized": True, "warnings": []},
                        output_contract={"type": "object"}, contract_version="v1")
        record = gateway.records[-1]
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["output_mode"], "json_object")
        self.assertEqual(record["contract_version"], "v1")
        self.assertTrue(record["validation_metadata"]["normalized"])

    def test_order_ids_are_seed_and_rationale_independent(self):
        _, c1 = self.context(ScenarioConfig(days=1), "r1")
        _, c2 = self.context(ScenarioConfig(days=1), "r1")
        c1["run_id"], c2["run_id"] = "run-a", "run-b"
        order = {"side": "buy", "good": "fiber", "quantity": 1, "price": 1, "net_ids": []}
        p1, _ = prepare_decision(raw(orders=[order], rationale="a"), c1)
        p2, _ = prepare_decision(raw(orders=[order], rationale="b"), c2)
        self.assertEqual(p1.schedule[0].orders[0].id, p2.schedule[0].orders[0].id)


if __name__ == "__main__":
    unittest.main()
