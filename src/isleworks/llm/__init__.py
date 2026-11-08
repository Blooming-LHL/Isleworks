"""Bounded JSON HTTP gateway. No implicit model calls or environment discovery."""
from __future__ import annotations

import copy
import json
import inspect
import hashlib
import http.client
import math
import re
import socket
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable
from uuid import uuid4
from .deepseek import DeepSeekAdapter, AdapterResponseError


class BudgetExceeded(RuntimeError):
    def __init__(self, message: str, *, error_detail: dict | None = None,
                 call_id: str | None = None):
        super().__init__(message)
        self.error_detail = error_detail
        self.call_id = call_id


class ModelError(RuntimeError):
    def __init__(self, message: str, *, fatal: bool = False,
                 error_detail: dict | None = None, call_id: str | None = None,
                 response_excerpt: str | None = None,
                 response_truncated: bool = False):
        super().__init__(message)
        self.fatal = fatal
        self.error_detail = error_detail
        self.call_id = call_id
        self.response_excerpt = response_excerpt
        self.response_truncated = response_truncated


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ModelError("Model endpoint redirects are not allowed")


class ModelGateway:
    def __init__(self, endpoint: str, model: str, api_key: str | None = None, *,
                 max_calls: int = 180, input_chars: int = 24000, output_tokens: int = 2000,
                 timeout: float = 30, requestor: Callable[..., Any] | None = None,
                 on_record: Callable[[dict], None] | None = None,
                 thinking: bool | None = None, output_contract: dict | None = None,
                 decision_mode: str = "tool_strict", provider: str = "deepseek",
                 decision_gate: Callable[[], None] | None = None):
        url = urllib.parse.urlparse(endpoint)
        if url.username or url.password or url.fragment or url.query or not url.hostname:
            raise ValueError("Invalid model endpoint")
        if url.scheme != "https" and not (url.scheme == "http" and url.hostname in ("localhost", "127.0.0.1", "::1")):
            raise ValueError("Only HTTPS or exact loopback HTTP model endpoints are allowed")
        for name, value in (("max_calls", max_calls), ("input_chars", input_chars), ("output_tokens", output_tokens)):
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if not isinstance(model, str) or not model or timeout <= 0:
            raise ValueError("A model and positive timeout are required")
        self.endpoint, self.model, self.api_key = endpoint, model, api_key
        self.max_calls, self.input_chars = max_calls, input_chars
        self.output_tokens, self.timeout, self.thinking = output_tokens, timeout, thinking
        self.requestor, self.on_record = requestor, on_record
        if decision_mode not in ("tool_strict", "json_object"):
            raise ValueError("Unknown decision mode")
        if provider != "deepseek":
            raise ValueError("Unsupported model provider")
        self.provider, self.decision_mode = provider, decision_mode
        self.output_contract = output_contract
        # Route compatibility is validated only when a structured decision is requested.
        self.adapter = DeepSeekAdapter(endpoint, decision_mode="json_object")
        self.decision_gate = decision_gate
        self.attempts = 0
        self.records: list[dict] = []
        self._lock = threading.Lock()

    def decision_identity(self, output_contract: dict | None = None) -> dict:
        contract = self.output_contract if output_contract is None else output_contract
        adapter = DeepSeekAdapter(self.endpoint,
                                   decision_mode=self.decision_mode if contract is not None else "json_object")
        return adapter.identity(contract)

    def _safe(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {self._safe(k): self._safe(v) for k, v in value.items()
                    if str(k).lower() not in ("api_key", "authorization", "token", "secret")}
        if isinstance(value, list):
            return [self._safe(v) for v in value]
        if isinstance(value, str) and self.api_key:
            return value.replace(self.api_key, "[REDACTED]")
        if isinstance(value, float) and not math.isfinite(value):
            return "[NON_FINITE]"
        return value

    def _excerpt(self, value: Any) -> tuple[str | None, bool]:
        if value is None:
            return None, False
        safe = self._safe(value)
        if not isinstance(safe, str):
            try:
                safe = json.dumps(safe, ensure_ascii=False, allow_nan=False,
                                  separators=(",", ":"))
            except (TypeError, ValueError):
                return None, False
        # Redact common bearer/key-shaped values before applying the bound.
        safe = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1[REDACTED]", safe)
        safe = re.sub(r"(?i)(api[_-]?key|token|secret)(\s*[:=]\s*)[^,;\s}]+",
                      r"\1\2[REDACTED]", safe)
        return safe[:2000], len(safe) > 2000

    @staticmethod
    def _detail(stage: str, code: str, message: str, path: str | None = None) -> dict:
        detail = {"stage": stage, "code": code, "path": path, "message": message}
        return detail

    def _feedback(self, detail: dict) -> str:
        safe = self._safe(detail)
        errors = safe.get("errors") or [safe]
        return ("Previous model response was rejected. Failed candidate is in the preceding context. "
                "Correct the errors without changing valid choices. Return the complete required object. "
                + json.dumps({"errors": errors}, ensure_ascii=False, allow_nan=False))

    def _contains_credential(self, value: Any) -> bool:
        if not self.api_key:
            return False
        if isinstance(value, str):
            return self.api_key in value
        if isinstance(value, dict):
            return any(self._contains_credential(k) or self._contains_credential(v) for k, v in value.items())
        if isinstance(value, (list, tuple)):
            return any(self._contains_credential(v) for v in value)
        return False

    def _record(self, record: dict) -> None:
        safe = self._safe(copy.deepcopy(record))
        if self.on_record:
            self.on_record(copy.deepcopy(safe))
        with self._lock:
            self.records.append(safe)

    def request(self, system: str, user: str, *, purpose: str = "decision",
                validator: Callable[[dict], Any] | None = None, trace: dict | None = None,
                output_contract: dict | None = None, contract_version: str | None = None) -> dict[str, Any]:
        if purpose not in ("decision", "review"):
            raise ValueError("Unknown model purpose")
        if len(system) + len(user) > self.input_chars:
            raise ModelError("Model input exceeds configured character limit",
                             error_detail=self._detail("request", "input_limit",
                                                       "Model input exceeds configured character limit"))
        contract = self.output_contract if output_contract is None else output_contract
        adapter = DeepSeekAdapter(self.endpoint,
                                   decision_mode=self.decision_mode if contract is not None else "json_object")
        identity = adapter.identity(contract)
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        requestor_user = user
        last_detail = last_call_id = None
        for retry in range(2):
            if purpose == "decision" and contract is not None and self.decision_gate is not None:
                self.decision_gate()
            with self._lock:
                if self.attempts >= self.max_calls:
                    raise BudgetExceeded("Model attempt budget exhausted",
                                         error_detail=last_detail, call_id=last_call_id)
                self.attempts += 1
                number = self.attempts
            record = {"id": uuid4().hex, "purpose": purpose, "model": self.model,
                      "attempt": number, "retry": retry, "status": "pending", **identity,
                      "contract_version": contract_version, "system": system,
                      "input": requestor_user, "output_token_limit": self.output_tokens,
                      "messages_hash": hashlib.sha256(
                          json.dumps(self._safe(messages), ensure_ascii=False).encode()).hexdigest()}
            if trace:
                record.update({key: trace[key] for key in ("resident_id", "day", "context_hash") if key in trace})
            self._record(record)  # Must persist before any provider call; failure is not retryable.
            result = None
            failed_output = None
            detail = None
            usage, adapter_metadata, validation_metadata = {}, {}, {}
            uncertain = False
            try:
                if self.requestor is not None:
                    kwargs = {"model": self.model, "max_tokens": self.output_tokens, "timeout": self.timeout}
                    try:
                        if "messages" in inspect.signature(self.requestor).parameters:
                            kwargs["messages"] = copy.deepcopy(self._safe(messages))
                    except (ValueError, TypeError):
                        pass
                    result = self.requestor(system, requestor_user, **kwargs)
                else:
                    payload = adapter.payload(system, user, model=self.model,
                                              max_tokens=self.output_tokens, contract=contract,
                                              messages=self._safe(messages))
                    if self.thinking is not None and not (contract is not None and self.decision_mode == "tool_strict"):
                        payload["thinking"] = {"type": "enabled" if self.thinking else "disabled"}
                    headers = {"Content-Type": "application/json"}
                    if self.api_key:
                        headers["Authorization"] = "Bearer " + self.api_key
                    request = urllib.request.Request(adapter.route, json.dumps(payload).encode(), headers)
                    with urllib.request.build_opener(_NoRedirect()).open(request, timeout=self.timeout) as response:
                        raw = response.read(4 * 1024 * 1024 + 1)
                        if len(raw) > 4 * 1024 * 1024:
                            raise AdapterResponseError("response_too_large", "$", "Provider response exceeds size limit")
                        failed_output = raw.decode("utf-8", errors="replace")
                        result = json.loads(raw)
                if not isinstance(result, dict):
                    raise AdapterResponseError("expected_object", "$", "Expected provider response object")
                usage_raw = result.get("usage", {})
                if usage_raw is None:
                    usage_raw = {}
                if not isinstance(usage_raw, dict):
                    raise AdapterResponseError("invalid_usage", "$.usage", "Expected usage object")
                usage = {key: value for key, value in usage_raw.items()
                         if type(value) is int and value >= 0}
                if isinstance(result.get("id"), str):
                    record["provider_request_id"] = result["id"][:200]
                choices = result.get("choices")
                if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                    record["finish_reason"] = choices[0].get("finish_reason")
                strict = contract is not None and self.decision_mode == "tool_strict"
                if strict:
                    failed_output = result
                    parsed, adapter_metadata = adapter.extract(result, contract=contract)
                    record["finish_reason"] = result["choices"][0]["finish_reason"]
                    failed_output = parsed
                elif "choices" in result:
                    try:
                        choice = result["choices"][0]
                        content = choice["message"]["content"]
                    except (KeyError, IndexError, TypeError):
                        raise AdapterResponseError("invalid_envelope", "$.choices",
                                                   "Expected choices[0].message.content") from None
                    failed_output = content
                    if not isinstance(content, str):
                        raise AdapterResponseError("invalid_content", "$.choices[0].message.content",
                                                   "Expected message.content string")
                    record["finish_reason"] = choice.get("finish_reason")
                    if choice.get("finish_reason") not in (None, "stop"):
                        raise AdapterResponseError("truncated", "$.choices[0].finish_reason",
                                                   "Model output was truncated or interrupted")
                    parsed = json.loads(content)
                else:
                    # Injectable direct-dictionary requestors support offline JSON tests.
                    parsed = result
                    failed_output = parsed
                if not isinstance(parsed, dict):
                    raise AdapterResponseError("expected_object", "$", "Expected structured object")
                if (validator is not None or contract is not None) and self._contains_credential(parsed):
                    detail = self._detail("validation", "credential_echo", "Model output contains a credential", "$")
                    raise ValueError("Credential-bearing candidate cannot be persisted")
                if validator is not None:
                    try:
                        validated = validator(parsed)
                        if isinstance(validated, dict):
                            validation_metadata = validated
                    except (ValueError, TypeError, KeyError, IndexError) as exc:
                        detail = self._detail(
                            "precondition" if type(exc).__name__ == "DecisionError" else "validation",
                            getattr(exc, "code", "invalid"),
                            getattr(exc, "message", "Response failed schema validation"),
                            getattr(exc, "path", None))
                        if isinstance(getattr(exc, "errors", None), list):
                            detail["errors"] = exc.errors
                        raise
            except (ValueError, KeyError, TypeError, IndexError, AttributeError, OSError, http.client.HTTPException, ModelError) as exc:
                stop = False
                if detail is None:
                    if isinstance(exc, AdapterResponseError):
                        detail = self._detail(exc.stage, exc.code, exc.message, exc.path)
                    elif isinstance(exc, json.JSONDecodeError):
                        detail = self._detail("json_decode", "invalid_json",
                                              f"Invalid JSON at line {exc.lineno} column {exc.colno}")
                    elif isinstance(exc, urllib.error.HTTPError):
                        detail = self._detail("transport", "http", f"HTTP {exc.code}")
                    elif isinstance(exc, (OSError, http.client.HTTPException)):
                        reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
                        if isinstance(reason, ssl.SSLError):
                            code, message = "tls", "TLS verification or handshake failed"
                        elif isinstance(reason, socket.gaierror):
                            code, message = "dns", "DNS lookup failed"
                        elif isinstance(reason, (TimeoutError, socket.timeout)):
                            code, message = "timeout", "Model request timed out"
                        else:
                            code, message = "transport", "Model transport failed"
                        detail = self._detail("transport", code, message)
                        uncertain = (contract is not None or purpose == "review") and (
                            code == "timeout" or (code == "transport" and
                            not isinstance(reason, ConnectionRefusedError)))
                    else:
                        detail = self._detail("response_envelope", "invalid_response",
                                              "Provider response could not be processed")
                if isinstance(exc, urllib.error.HTTPError):
                    stop = exc.code < 500 and exc.code != 429
                    error = f"HTTP {exc.code}"
                else:
                    error = type(exc).__name__
                if uncertain:
                    stop = True
                    detail["uncertain"] = True
                detail = self._safe(detail)
                excerpt, truncated = self._excerpt(failed_output)
                failed = {**record, "status": "error", "error": error, "error_detail": detail,
                          "call_id": record["id"], "usage": usage, "uncertain": uncertain, "fatal": stop}
                if "errors" in detail:
                    failed["errors"] = detail["errors"]
                if excerpt is not None:
                    failed.update(response_excerpt=excerpt, response_truncated=truncated)
                if adapter_metadata:
                    failed["adapter_metadata"] = adapter_metadata
                self._record(failed)
                last_detail, last_call_id = detail, record["id"]
                if stop or retry == 1:
                    raise ModelError(f"Model request failed ({error}); no automatic provider substitution",
                                     fatal=stop, error_detail=detail, call_id=record["id"],
                                     response_excerpt=excerpt, response_truncated=truncated) from None
                if detail["stage"] in ("validation", "precondition", "json_decode", "response_envelope", "tool_arguments"):
                    feedback = self._feedback(detail)
                    proposed = copy.deepcopy(messages)
                    valid_calls = None
                    if contract is not None and self.decision_mode == "tool_strict" and isinstance(result, dict):
                        try:
                            calls = result["choices"][0]["message"]["tool_calls"]
                            if isinstance(calls, list) and len(calls) == 1:
                                call = calls[0]
                                if (isinstance(call, dict) and call.get("type") == "function"
                                        and isinstance(call.get("id"), str) and call["id"]
                                        and isinstance(call.get("function"), dict)
                                        and isinstance(call["function"].get("arguments"), str)):
                                    valid_calls = calls
                        except (KeyError, IndexError, TypeError):
                            pass
                    if valid_calls is not None:
                        proposed.append({"role": "assistant", "content": None,
                                         "tool_calls": self._safe(valid_calls)})
                        proposed.append({"role": "tool", "tool_call_id": valid_calls[0]["id"],
                                         "content": feedback})
                    else:
                        original = failed_output
                        if not isinstance(original, str):
                            original = json.dumps(self._safe(original), ensure_ascii=False)
                        proposed.append({"role": "assistant", "content": self._safe(original)})
                        proposed.append({"role": "user", "content": feedback})
                    serialized = json.dumps(self._safe(proposed), ensure_ascii=False)
                    if len(serialized) > self.input_chars:
                        raise ModelError("Full correction context exceeds configured character limit",
                                         error_detail={**detail, "retry_skipped": "retry_context_limit"},
                                         call_id=record["id"], response_excerpt=excerpt,
                                         response_truncated=truncated) from None
                    messages = proposed
                    # Preserve the old injectable signature while retaining the full failed candidate.
                    requestor_user = user + "\n" + feedback + "\nFailed candidate: " + (
                        self._safe(failed_output) if isinstance(failed_output, str)
                        else json.dumps(self._safe(failed_output), ensure_ascii=False))
                continue
            completed = {**record, "status": "completed", "response": parsed, "usage": usage,
                         "validation_metadata": validation_metadata, "adapter_metadata": adapter_metadata}
            self._record(completed)  # A journal failure here must never repeat a completed paid call.
            return parsed
        raise ModelError("Model request failed")


__all__ = ["ModelGateway", "BudgetExceeded", "ModelError", "DeepSeekAdapter"]
