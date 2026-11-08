import json
import unittest

from isleworks.llm import BudgetExceeded, ModelError, ModelGateway


class FailureGatewayTest(unittest.TestCase):
    def test_completed_journal_failure_never_repeats_a_model_request(self):
        requests = []

        def callback(record):
            if record["status"] == "completed":
                raise OSError("storage unavailable")

        def requestor(*args, **kwargs):
            requests.append(1)
            return {"ok": True}

        gateway = ModelGateway("http://localhost:1", "fake", requestor=requestor,
                               on_record=callback, max_calls=2)
        with self.assertRaises(OSError):
            gateway.request("s", "u")
        self.assertEqual(requests, [1])
        self.assertEqual(gateway.attempts, 1)

    def test_invalid_json_gets_sanitized_feedback_and_terminal_detail(self):
        calls, records = [], []

        def requestor(system, user, **kwargs):
            calls.append(user)
            content = "{bad" if len(calls) == 1 else '{"ok": true}'
            return {"choices": [{"message": {"content": content}}]}

        gateway = ModelGateway("http://localhost:1", "fake", requestor=requestor,
                              on_record=records.append, max_calls=2)
        self.assertEqual(gateway.request("system", "input", trace={"resident_id": "r1", "day": 3}),
                         {"ok": True})
        self.assertIn("Previous model response was rejected", calls[1])
        self.assertEqual(records[1]["error_detail"]["stage"], "json_decode")
        self.assertEqual(records[1]["resident_id"], "r1")

    def test_validation_error_and_excerpt_are_bounded_and_redacted(self):
        records = []

        def requestor(*args, **kwargs):
            return {"secret": "SECRET", "blob": "x" * 3000}

        def validator(parsed):
            error = ValueError("ignored raw exception")
            error.path, error.code, error.message = "plan.day", "required", "day is required"
            raise error

        gateway = ModelGateway("http://localhost:1", "fake", api_key="SECRET",
                              requestor=requestor, on_record=records.append, max_calls=2)
        with self.assertRaises(ModelError) as raised:
            gateway.request("s", "u", validator=validator)
        self.assertEqual(raised.exception.error_detail["stage"], "validation")
        self.assertNotIn("ignored raw exception", str(raised.exception.error_detail))
        self.assertLessEqual(len(raised.exception.response_excerpt), 2000)
        self.assertNotIn("SECRET", str(records))

    def test_transport_failure_and_budget_preserve_last_detail(self):
        def requestor(*args, **kwargs):
            raise TimeoutError()

        gateway = ModelGateway("http://localhost:1", "fake", requestor=requestor,
                              max_calls=2)
        with self.assertRaises(ModelError) as raised:
            gateway.request("s", "u")
        self.assertEqual(raised.exception.error_detail["code"], "timeout")
        budget_gateway = ModelGateway("http://localhost:1", "fake", requestor=requestor,
                                      max_calls=1)
        with self.assertRaises(BudgetExceeded) as budget:
            budget_gateway.request("s", "u")
        self.assertEqual(budget.exception.error_detail["code"], "timeout")
        self.assertIsNotNone(budget.exception.call_id)


if __name__ == "__main__":
    unittest.main()
