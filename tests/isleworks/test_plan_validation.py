import unittest

from isleworks.domain import PlanValidationError, validate_plan_payload


def payload(**changes):
    value = {
        "id": "plan-1", "resident_id": "r1", "created_day": 1, "goal": "stay fed",
        "schedule": [{"resident_id": "r1", "day": 1}],
    }
    value.update(changes)
    return value


class PlanValidationTest(unittest.TestCase):
    def assertError(self, value, code, path):
        with self.assertRaises(PlanValidationError) as caught:
            validate_plan_payload(value)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(caught.exception.path, path)
        self.assertNotIn(repr(value), caught.exception.message)

    def test_valid_payload_preserves_optional_defaults(self):
        plan = validate_plan_payload(payload())
        self.assertEqual(plan.rationale, "")
        self.assertEqual(plan.replacement_reason, "plan_expired")
        self.assertEqual(plan.schedule[0].labor, ("rest",) * 4)
        self.assertEqual(plan.schedule[0].orders, ())

    def test_missing_and_unknown_fields(self):
        value = payload()
        del value["goal"]
        self.assertError(value, "missing_field", "$.goal")
        value = payload(extra=True)
        self.assertError(value, "unknown_field", "$.extra")

    def test_nested_types_and_safe_bool_integer_rejection(self):
        value = payload(schedule=[{"resident_id": "r1", "day": 1,
                                   "labor": [True, "rest", "rest", "rest"]}])
        self.assertError(value, "wrong_type", "$.schedule[0].labor[0]")
        value = payload(schedule=[{"resident_id": "r1", "day": 1,
                                  "orders": [{"id": "o", "resident_id": "r1",
                                               "side": "buy", "good": "fiber",
                                               "quantity": 1, "price": True}]}])
        self.assertError(value, "wrong_type", "$.schedule[0].orders[0].price")

    def test_wrong_action_list_and_owner_date(self):
        value = payload(schedule=[{"resident_id": "r1", "day": 1,
                                   "labor": ["fish", "bogus", "rest", "rest"]}])
        self.assertError(value, "invalid_value", "$.schedule[0].labor[1]")
        value = payload(schedule=[{"resident_id": "other", "day": 1}])
        self.assertError(value, "invariant", "$.schedule[0].resident_id")
        value = payload(schedule=[{"resident_id": "r1", "day": 2}])
        self.assertError(value, "invariant", "$.schedule[0].day")

    def test_horizon_and_constructor_length_paths(self):
        value = payload(schedule=[{"resident_id": "r1", "day": 1},
                                  {"resident_id": "r1", "day": 2},
                                  {"resident_id": "r1", "day": 3}])
        self.assertEqual(validate_plan_payload(value).schedule[-1].day, 3)
        self.assertError(payload(id=""), "invalid_value", "$.id")
        self.assertError(payload(goal="x" * 2001), "invalid_value", "$.goal")
        self.assertError(payload(rationale="x" * 3001), "invalid_value", "$.rationale")

    def test_nested_order_id_and_net_ids(self):
        order = {"id": "", "resident_id": "r1", "side": "buy", "good": "fiber",
                 "quantity": 1, "price": 100}
        self.assertError(payload(schedule=[{"resident_id": "r1", "day": 1, "orders": [order]}]),
                         "invalid_value", "$.schedule[0].orders[0].id")
        order["id"] = "o"
        order["net_ids"] = ["n"]
        self.assertError(payload(schedule=[{"resident_id": "r1", "day": 1, "orders": [order]}]),
                         "invariant", "$.schedule[0].orders[0].net_ids")


if __name__ == "__main__":
    unittest.main()
