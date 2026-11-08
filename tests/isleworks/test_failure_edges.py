import json
import socket
import ssl
import threading
import unittest
import urllib.error
from unittest.mock import patch

from isleworks.llm import BudgetExceeded, ModelError, ModelGateway


class FailureEdgesTest(unittest.TestCase):
    def test_feedback_that_does_not_fit_stops_before_second_request(self):
        seen = []

        def requestor(system, user, **kwargs):
            seen.append(user)
            return {"choices": [{"message": {"content": "{"}}]}

        gateway = ModelGateway("http://localhost:1", "m", input_chars=100,
                               requestor=requestor, max_calls=2)
        original = "original user context"
        with self.assertRaises(ModelError) as raised:
            gateway.request("system", original)
        self.assertEqual(seen, [original])
        self.assertEqual(gateway.attempts, 1)
        self.assertEqual(raised.exception.error_detail["code"], "invalid_json")
        self.assertNotIn("Previous model response was rejected", seen[0])

    def test_urlerror_reason_categories_are_stable(self):
        reasons = (
            (ssl.SSLError("certificate verify failed"), "tls"),
            (socket.gaierror("name or service not known"), "dns"),
            (TimeoutError("timed out"), "timeout"),
        )
        for reason, expected in reasons:
            with self.subTest(expected=expected):
                def requestor(*args, _reason=reason, **kwargs):
                    raise urllib.error.URLError(_reason)

                gateway = ModelGateway("http://localhost:1", "m", requestor=requestor,
                                       max_calls=2)
                with self.assertRaises(ModelError) as raised:
                    gateway.request("s", "u")
                self.assertEqual(raised.exception.error_detail["code"], expected)

    def test_http_401_is_fatal_and_only_attempts_once(self):
        calls = []

        def requestor(*args, **kwargs):
            calls.append(1)
            raise urllib.error.HTTPError("http://localhost", 401, "unauthorized", {}, None)

        gateway = ModelGateway("http://localhost:1", "m", requestor=requestor, max_calls=3)
        with self.assertRaises(ModelError) as raised:
            gateway.request("s", "u")
        self.assertTrue(raised.exception.fatal)
        self.assertEqual(raised.exception.error_detail["code"], "http")
        self.assertEqual(gateway.attempts, 1)
        self.assertEqual(calls, [1])

    def test_record_callback_failure_is_fail_closed_before_requestor(self):
        requestor_calls = []

        def fail_callback(record):
            raise OSError("journal unavailable")

        gateway = ModelGateway("http://localhost:1", "m", on_record=fail_callback,
                               requestor=lambda *a, **k: requestor_calls.append(1))
        with self.assertRaises(OSError):
            gateway.request("s", "u")
        self.assertEqual(requestor_calls, [])

    def test_success_record_redacts_secret_values_even_when_secret_is_a_dict_key(self):
        records = []
        secret = "test-secret-key-123"
        gateway = ModelGateway("http://localhost:1", "m", api_key=secret,
                               requestor=lambda *a, **k: {
                                   secret: secret,
                                   "nested": {"api_key": secret},
                               }, on_record=records.append)
        self.assertEqual(gateway.request("s", "u"), {
            secret: secret,
            "nested": {"api_key": secret},
        })
        self.assertNotIn(secret, json.dumps(records))
        self.assertNotIn(secret, records[-1]["response"])

    def test_failed_response_excerpt_redacts_secret_key_paths(self):
        records = []
        secret = "test-secret-key-123"

        def validator(parsed):
            error = ValueError("schema rejected: " + secret)
            error.path = "$.secret"
            error.code = "invalid"
            error.message = "schema rejected: " + secret
            raise error

        gateway = ModelGateway("http://localhost:1", "m", api_key=secret,
                               requestor=lambda *a, **k: {"api_key": secret},
                               on_record=records.append, max_calls=2)
        with self.assertRaises(ModelError) as raised:
            gateway.request("s", "u", validator=validator)
        self.assertNotIn(secret, str(raised.exception.response_excerpt))
        self.assertNotIn(secret, str(raised.exception.error_detail))
        self.assertNotIn(secret, json.dumps(records))

    def test_malformed_usage_envelope_is_journaled_as_terminal_failure(self):
        records = []

        def requestor(*args, **kwargs):
            return {"choices": [{"message": {"content": '{"ok": true}'}}], "usage": []}

        gateway = ModelGateway("http://localhost:1", "m", requestor=requestor,
                               on_record=records.append, max_calls=2)
        with self.assertRaises(ModelError) as raised:
            gateway.request("s", "u")
        self.assertEqual(raised.exception.error_detail["code"], "invalid_usage")
        failures = [record for record in records if record.get("status") == "error"]
        self.assertEqual(len(failures), 2)
        self.assertTrue(all(record["error_detail"]["code"] == "invalid_usage" for record in failures))
        self.assertEqual(failures[-1]["call_id"], raised.exception.call_id)

    def test_invalid_content_type_can_retry_without_accepting_non_string(self):
        calls = []

        def requestor(*args, **kwargs):
            calls.append(1)
            content = {"not": "text"} if len(calls) == 1 else '{"ok": true}'
            return {"choices": [{"message": {"content": content}}]}

        gateway = ModelGateway("http://localhost:1", "m", requestor=requestor, max_calls=2)
        self.assertEqual(gateway.request("s", "u"), {"ok": True})
        self.assertEqual(calls, [1, 1])

    def test_non_json_outer_http_body_is_terminal_and_recorded(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, limit):
                return b"not-json"

        class Opener:
            def open(self, request, timeout):
                return Response()

        gateway = ModelGateway("http://localhost:1", "m", max_calls=2)
        with patch("urllib.request.build_opener", return_value=Opener()):
            with self.assertRaises(ModelError) as raised:
                gateway.request("s", "u")
        self.assertEqual(raised.exception.error_detail["stage"], "json_decode")
        self.assertEqual(gateway.attempts, 2)
        self.assertEqual(len([r for r in gateway.records if r.get("status") == "error"]), 2)

    def test_concurrent_budget_error_does_not_inherit_other_residents_failure(self):
        entered = threading.Event()
        release = threading.Event()
        outcomes = {}

        def requestor(*args, **kwargs):
            entered.set()
            release.wait(2)
            raise urllib.error.HTTPError("http://localhost", 401, "unauthorized", {}, None)

        gateway = ModelGateway("http://localhost:1", "m", requestor=requestor, max_calls=1)

        def first():
            try:
                gateway.request("s", "u", trace={"resident_id": "r1", "day": 1})
            except ModelError as exc:
                outcomes["r1"] = exc

        thread = threading.Thread(target=first)
        thread.start()
        self.assertTrue(entered.wait(2))
        with self.assertRaises(BudgetExceeded) as raised:
            gateway.request("s", "u", trace={"resident_id": "r2", "day": 1})
        release.set()
        thread.join(2)
        self.assertIsNone(raised.exception.call_id)
        self.assertEqual(outcomes["r1"].error_detail["code"], "http")
        self.assertNotEqual(raised.exception.call_id, outcomes["r1"].call_id)


if __name__ == "__main__":
    unittest.main()
