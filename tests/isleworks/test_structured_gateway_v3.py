import json
import unittest
from unittest.mock import patch

from isleworks.llm import ModelError, ModelGateway


SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}},
          "required": ["ok"], "additionalProperties": False}


class _Response:
    def __init__(self, value):
        self.value = value
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self, _n): return json.dumps(self.value).encode()


class StructuredGatewayV3Test(unittest.TestCase):
    def test_strict_payload_route_tools_and_disabled_thinking(self):
        result = {"choices": [{"finish_reason": "tool_calls", "message": {
            "content": None, "tool_calls": [{"id": "c1", "type": "function",
            "function": {"name": "submit_decision", "arguments": '{"ok":true}'}}]}}]}
        opener = type("O", (), {"open": lambda self, request, timeout: _Response(result)})()
        with patch("urllib.request.build_opener", return_value=opener) as build:
            gateway = ModelGateway("https://api.deepseek.com", "deepseek-chat",
                                   api_key="secret", thinking=True)
            self.assertEqual(gateway.request("system", "user", output_contract=SCHEMA), {"ok": True})
        req = build.return_value.open.__self__ if False else None
        # Capture through a second opener implementation to inspect the actual request.
        seen = []
        class Capture:
            def open(self, request, timeout):
                seen.append((request.full_url, json.loads(request.data)))
                return _Response(result)
        with patch("urllib.request.build_opener", return_value=Capture()):
            ModelGateway("https://api.deepseek.com", "m").request("s", "u", output_contract=SCHEMA)
        url, payload = seen[0]
        self.assertEqual(url, "https://api.deepseek.com/beta/chat/completions")
        self.assertNotIn("response_format", payload)
        self.assertTrue(payload["tools"][0]["function"]["strict"])
        self.assertNotIn("strict", payload)
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(payload["tool_choice"]["function"]["name"], "submit_decision")

    def test_strict_rejects_wrong_tool_shape(self):
        bad = [
            {"finish_reason": "stop", "message": {"content": None, "tool_calls": []}},
            {"finish_reason": "tool_calls", "message": {"content": None, "tool_calls": []}},
            {"finish_reason": "tool_calls", "message": {"content": None, "tool_calls": [{"function": {"name": "other", "arguments": "{}"}}]}},
            {"finish_reason": "tool_calls", "message": {"content": None, "tool_calls": [{"function": {"name": "submit_decision", "arguments": "{bad"}}]}},
        ]
        for choice in bad:
            calls = []
            def requestor(*args, **kwargs):
                calls.append(1); return {"choices": [choice]}
            gateway = ModelGateway("http://localhost:1", "m", requestor=requestor)
            with self.assertRaises(ModelError):
                gateway.request("s", "u", output_contract=SCHEMA)
            self.assertLessEqual(len(calls), 2)

    def test_json_mode_has_no_strict_tools_and_correction_keeps_output(self):
        seen = []
        class Capture:
            def open(self, request, timeout):
                seen.append(json.loads(request.data))
                value = {"choices": [{"finish_reason": "stop", "message": {"content": "{bad"}}]} if len(seen) == 1 else {"choices": [{"finish_reason": "stop", "message": {"content": '{"ok":true}'}}]}
                return _Response(value)
        def validate(value):
            if not value.get("ok"): raise ValueError("missing ok")
        with patch("urllib.request.build_opener", return_value=Capture()):
            self.assertEqual(ModelGateway("https://api.deepseek.com", "m", decision_mode="json_object").request("s", "u", validator=validate), {"ok": True})
        self.assertIn("response_format", seen[0]); self.assertNotIn("tools", seen[0])
        self.assertIn("Failed candidate", seen[1]["messages"][-1]["content"])

    def test_identity_includes_beta_route_and_schema_hash(self):
        identity = ModelGateway("https://api.deepseek.com/chat/completions", "m").decision_identity(SCHEMA)
        self.assertEqual(identity["effective_route"], "https://api.deepseek.com/beta/chat/completions")
        self.assertTrue(identity["schema_projection_hash"])

    def test_requestor_receives_full_strict_retry_messages(self):
        seen = []
        def requestor(system, user, *, messages, **kwargs):
            seen.append(messages)
            if len(seen) == 1:
                return {"choices": [{"finish_reason": "tool_calls", "message": {"content": None,
                    "tool_calls": [{"id": "x", "type": "function", "function": {"name": "submit_decision", "arguments": "{bad"}}]}}]}
            return {"choices": [{"finish_reason": "tool_calls", "message": {"content": None,
                "tool_calls": [{"id": "y", "type": "function", "function": {"name": "submit_decision", "arguments": '{"ok":true}'}}]}}]}
        gateway = ModelGateway("http://localhost:1", "m", requestor=requestor)
        self.assertEqual(gateway.request("s", "u", output_contract=SCHEMA), {"ok": True})
        self.assertEqual(seen[1][2]["role"], "assistant")
        self.assertEqual(seen[1][2]["tool_calls"][0]["id"], "x")
        self.assertEqual(seen[1][3]["role"], "tool")


if __name__ == "__main__":
    unittest.main()
