"""Provider-specific wire format; no world rules or resource mutation."""
from __future__ import annotations

import copy
import hashlib
import json
import urllib.parse
from typing import Any


class AdapterResponseError(ValueError):
    def __init__(self, code: str, path: str, message: str, *, stage: str = "response_envelope"):
        super().__init__(message)
        self.code, self.path, self.message, self.stage = code, path, message, stage


class DeepSeekAdapter:
    adapter_version = "deepseek-v0.3.1"

    def __init__(self, endpoint: str, *, decision_mode: str = "tool_strict"):
        if decision_mode not in ("tool_strict", "json_object"):
            raise ValueError("Unsupported decision output mode")
        self.decision_mode = decision_mode
        self.route = self._route(endpoint, decision_mode)

    @staticmethod
    def _route(endpoint: str, mode: str) -> str:
        url = urllib.parse.urlparse(endpoint)
        if url.username or url.password or url.query or url.fragment or not url.hostname:
            raise ValueError("Invalid model endpoint")
        if url.scheme != "https" and not (
                url.scheme == "http" and url.hostname in ("localhost", "127.0.0.1", "::1")):
            raise ValueError("Only HTTPS or exact loopback HTTP endpoints are allowed")
        path = url.path.rstrip("/")
        if mode == "tool_strict":
            if url.hostname == "api.deepseek.com":
                if path in ("", "/chat/completions", "/beta", "/beta/chat/completions"):
                    path = "/beta/chat/completions"
                else:
                    raise ValueError("Unsupported official DeepSeek strict route; use the root or beta base URL")
            elif not (path.endswith("/beta/chat/completions")
                      or url.hostname in ("localhost", "127.0.0.1", "::1")):
                raise ValueError("Strict custom endpoints require an explicit beta route")
        return urllib.parse.urlunparse((url.scheme, url.netloc, path or "/chat/completions", "", "", ""))

    def identity(self, contract: dict | None) -> dict:
        schema_hash = None if contract is None else hashlib.sha256(
            json.dumps(contract, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        return {"provider": "deepseek", "output_mode": self.decision_mode,
                "adapter_version": self.adapter_version, "effective_route": self.route,
                "schema_projection_hash": schema_hash}

    def payload(self, system: str, user: str, *, model: str, max_tokens: int,
                contract: dict | None, messages: list[dict] | None = None) -> dict:
        body = {"model": model, "messages": copy.deepcopy(messages) if messages is not None else [
            {"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": max_tokens, "stream": False}
        if contract is None or self.decision_mode == "json_object":
            body["response_format"] = {"type": "json_object"}
        else:
            body.update(thinking={"type": "disabled"},
                        tools=[{"type": "function", "function": {
                            "name": "submit_decision", "strict": True,
                            "description": "Submit this resident's decision.", "parameters": copy.deepcopy(contract)}}],
                        tool_choice={"type": "function", "function": {"name": "submit_decision"}})
        return body

    def extract(self, result: dict, *, contract: dict | None) -> tuple[Any, dict]:
        if contract is None or self.decision_mode == "json_object":
            return result, {}
        def fail(code, path, message, stage="response_envelope"):
            raise AdapterResponseError(code, path, message, stage=stage)
        choices = result.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            fail("invalid_choices", "$.choices", "Expected exactly one response choice")
        choice = choices[0]
        if choice.get("finish_reason") != "tool_calls":
            fail("unexpected_finish_reason", "$.choices[0].finish_reason", "Expected tool_calls completion")
        message = choice.get("message")
        if not isinstance(message, dict):
            fail("invalid_message", "$.choices[0].message", "Expected assistant message object")
        calls = message.get("tool_calls")
        if not isinstance(calls, list) or len(calls) != 1 or not isinstance(calls[0], dict):
            fail("invalid_tool_count", "$.choices[0].message.tool_calls", "Expected exactly one tool call")
        call = calls[0]
        if call.get("type") != "function" or not isinstance(call.get("id"), str) or not call["id"]:
            fail("invalid_tool_identity", "$.choices[0].message.tool_calls[0]", "Tool call requires a real id and function type")
        function = call.get("function")
        if not isinstance(function, dict) or function.get("name") != "submit_decision":
            fail("unexpected_tool", "$.tool_calls[0].function.name", "Expected submit_decision")
        arguments = function.get("arguments")
        if not isinstance(arguments, str):
            fail("invalid_arguments", "$.tool_calls[0].function.arguments", "Expected JSON text arguments", "tool_arguments")
        try:
            candidate = json.loads(arguments)
        except json.JSONDecodeError as exc:
            fail("invalid_json", "$.tool_calls[0].function.arguments",
                 f"Invalid JSON at line {exc.lineno} column {exc.colno}", "tool_arguments")
        return candidate, {"tool_call_id": call["id"]}

