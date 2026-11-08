import unittest

from isleworks.domain import DailyIntent, ScenarioConfig
from isleworks.engine import init_world, step_day


class EngineTests(unittest.TestCase):
    def test_step_clones_and_consumes(self):
        config = ScenarioConfig(residents=1, initial_fish_units=2000)
        state = init_world(config)
        commit = step_day(state, (DailyIntent("r1", 1),), config)
        self.assertEqual(state.day, 0)
        self.assertEqual(commit.state.day, 1)
        self.assertEqual(commit.state.residents["r1"].fish, 1000)

    def test_wrong_day_is_rejected_without_mutation(self):
        config = ScenarioConfig(residents=1)
        state = init_world(config)
        with self.assertRaises(ValueError):
            step_day(state, (DailyIntent("r1", 2),), config)
        self.assertEqual(state.day, 0)


if __name__ == "__main__":
    unittest.main()
