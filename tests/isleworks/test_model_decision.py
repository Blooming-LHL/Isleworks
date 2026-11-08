"""Opt-in single-resident Decision probe. No world day or archive is written."""
import os
import unittest
from argparse import Namespace

from isleworks.agents import LLMStrategy, build_context
from isleworks.domain import ScenarioConfig
from isleworks.engine import init_world
from isleworks.interfaces.cli import gateway


@unittest.skipUnless(os.environ.get("ISLEWORKS_LIVE_DECISION_TEST") == "1",
                     "Real Decision test requires ISLEWORKS_LIVE_DECISION_TEST=1")
class LiveDecisionTest(unittest.TestCase):
    def test_one_resident_strict_decision(self):
        client = None
        try:
            client = gateway(Namespace(allow_paid=True, base_url=None, endpoint=None, model=None,
                                       max_calls=2, decision_output="tool_strict"), limit=2)
            config = ScenarioConfig(days=1)
            context = build_context(config, init_world(config), "r1")
            context["run_id"] = "single-resident-probe"
            plan = LLMStrategy(client).plan(context)
        except Exception as exc:
            detail = getattr(exc, "error_detail", None) or {}
            category = detail.get("code") or type(exc).__name__
            raise self.failureException(
                f"Strict Decision probe failed: {category}; configuration and response hidden"
            ) from None
        self.assertTrue(plan.resident_id == "r1" and plan.created_day == 1)
        self.assertTrue(plan.for_day(1) is not None)
        self.assertLessEqual(client.attempts, 2)


if __name__ == "__main__":
    unittest.main()
