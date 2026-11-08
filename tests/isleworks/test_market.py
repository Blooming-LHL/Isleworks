import unittest

from isleworks.domain import DailyIntent, Order, ScenarioConfig
from isleworks.engine import init_world
from isleworks.market import match_orders


class MarketTests(unittest.TestCase):
    def test_matching_is_independent_of_intent_order(self):
        state = init_world(ScenarioConfig(residents=2))
        asks = DailyIntent("r1", 1, orders=(Order("a", "r1", "sell", "fiber", 2, 100),))
        bid = DailyIntent("r2", 1, orders=(Order("b", "r2", "buy", "fiber", 2, 100),))
        state.residents["r1"].fiber = 2
        first = match_orders(state, (asks, bid), 1)
        second = match_orders(state, (bid, asks), 1)
        self.assertEqual(first, second)
        self.assertEqual(first[0][0].quantity, 2)


if __name__ == "__main__":
    unittest.main()
