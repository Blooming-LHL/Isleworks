import unittest
from isleworks.agents import RuleStrategy, build_context, needs_replan
from isleworks.domain import Resident, ScenarioConfig, WorldState

class DecisionsTest(unittest.TestCase):
    def test_context_hides_peer_inventory(self):
        s = WorldState(residents={"a": Resident("a", "A", "fisher"), "b": Resident("b", "B", "forager", fish=9999)})
        c = build_context(ScenarioConfig(), s, "a")
        self.assertNotIn("9999", str(c)); self.assertEqual(c["day"], 1)

    def test_rule_plan_and_replan(self):
        s = WorldState(residents={"a": Resident("a", "A", "fisher")})
        c = build_context(ScenarioConfig(), s, "a"); p = RuleStrategy().plan(c)
        self.assertEqual(p.for_day(1).resident_id, "a")
        self.assertTrue(needs_replan(None, c))

if __name__ == "__main__": unittest.main()
