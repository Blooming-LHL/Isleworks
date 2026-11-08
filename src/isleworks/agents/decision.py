"""Resident Decision v1 contract and deterministic compiler."""
from __future__ import annotations

import copy
from typing import Any

from isleworks.domain import (DailyIntent, Order, Plan, PlanValidationError,
                               digest)

CONTRACT_VERSION = "resident_decision_v1"
ACTIONS = ("fish", "gather", "craft", "rest")

# DeepSeek strict's supported JSON-schema subset: deliberately no array/string
# length keywords; those remain local validation rules.
DECISION_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["goal", "rationale", "days"],
    "properties": {
        "goal": {"type": "string"}, "rationale": {"type": "string"},
        "days": {"type": "array", "items": {"type": "object", "additionalProperties": False,
            "required": ["labor", "orders"], "properties": {
                "labor": {"type": "array", "items": {"type": "string", "enum": list(ACTIONS)}},
                "orders": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                    "required": ["side", "good", "quantity", "price", "net_ids"],
                    "properties": {"side": {"type": "string", "enum": ["buy", "sell"]},
                        "good": {"type": "string", "enum": ["fiber", "net"]},
                        "quantity": {"type": "integer", "minimum": 1, "maximum": 10000},
                        "price": {"type": "integer", "minimum": 1, "maximum": 100000},
                        "net_ids": {"type": "array", "items": {"type": "string"}}}}}}}}}
}


class StructuredDecisionError(PlanValidationError):
    """Validation failure containing all independently discoverable errors."""
    def __init__(self, errors: list[dict[str, str]]):
        self.errors = errors
        first = errors[0] if errors else {"code": "invalid", "path": "$", "message": "Invalid Decision"}
        super().__init__(first["code"], first["path"], first["message"])


def _err(errors, code, path, message):
    errors.append({"code": code, "path": path, "message": message})


def _normalize(raw: Any, context: dict[str, Any]) -> tuple[Any, list[str]]:
    warnings: list[str] = []
    if not isinstance(raw, dict):
        raise StructuredDecisionError([{"code": "wrong_type", "path": "$", "message": "Expected an object"}])
    value = copy.deepcopy(raw)
    if value.get("type") == "json_object":
        del value["type"]; warnings.append("removed_transport_type")
    elif "type" in value:
        raise StructuredDecisionError([{"code": "unknown_field", "path": "$.type", "message": "Unknown transport marker"}])
    wrappers = [k for k in ("decision", "plan", "current_plan") if k in value]
    if wrappers:
        if len(wrappers) != 1 or (set(value) - {wrappers[0], "resident_id"}):
            raise StructuredDecisionError([{"code": "ambiguous_wrapper", "path": "$", "message": "Ambiguous Decision wrapper"}])
        inner = value[wrappers[0]]
        if not isinstance(inner, dict):
            raise StructuredDecisionError([{"code": "wrong_type", "path": f"$.{wrappers[0]}", "message": "Wrapper must contain an object"}])
        extras = set(value) - {wrappers[0], "resident_id"}
        if extras or ("resident_id" in value and value["resident_id"] != context.get("resident_id")):
            raise StructuredDecisionError([{"code": "ambiguous_wrapper", "path": "$", "message": "Wrapper identity or fields conflict"}])
        value = inner; warnings.append(f"unwrapped_{wrappers[0]}")
    return value, warnings


def _validate(raw: Any) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    if not isinstance(raw, dict): return [{"code":"wrong_type","path":"$","message":"Expected an object"}]
    required = {"goal", "rationale", "days"}; unknown = set(raw) - required
    for k in sorted(unknown): _err(errors, "unknown_field", f"$.{k}", "Unknown field")
    for k in sorted(required - set(raw)): _err(errors, "missing_field", f"$.{k}", "Required field is missing")
    for k in ("goal", "rationale"):
        if k in raw and type(raw[k]) is not str: _err(errors, "wrong_type", f"$.{k}", "Expected a string")
        elif k in raw and len(raw[k]) > (2000 if k == "goal" else 3000): _err(errors, "invalid_value", f"$.{k}", "Value exceeds the supported length")
    days = raw.get("days")
    if type(days) is not list: _err(errors, "wrong_type", "$.days", "Expected a list"); return errors
    if not 1 <= len(days) <= 3: _err(errors, "invalid_value", "$.days", "Days must contain one to three items")
    for i, day in enumerate(days):
        p=f"$.days[{i}]"
        if type(day) is not dict: _err(errors,"wrong_type",p,"Expected an object"); continue
        for k in sorted(set(day)-{"labor","orders"}): _err(errors,"unknown_field",f"{p}.{k}","Unknown field")
        for k in ("labor","orders"):
            if k not in day: _err(errors,"missing_field",f"{p}.{k}","Required field is missing")
        labor=day.get("labor")
        if type(labor) is not list: _err(errors,"wrong_type",f"{p}.labor","Expected a list")
        elif len(labor)!=4: _err(errors,"invalid_value",f"{p}.labor","Exactly four labor blocks are required")
        else:
            for j,a in enumerate(labor):
                if type(a) is not str: _err(errors,"wrong_type",f"{p}.labor[{j}]","Expected a string")
                elif a not in ACTIONS: _err(errors,"invalid_value",f"{p}.labor[{j}]","Unknown labor action")
        orders=day.get("orders")
        if type(orders) is not list: _err(errors,"wrong_type",f"{p}.orders","Expected a list"); continue
        if len(orders)>3: _err(errors,"invalid_value",f"{p}.orders","At most three orders are allowed")
        for j,o in enumerate(orders):
            q=f"{p}.orders[{j}]"
            if type(o) is not dict: _err(errors,"wrong_type",q,"Expected an object"); continue
            req={"side","good","quantity","price","net_ids"}
            for k in sorted(set(o)-req): _err(errors,"unknown_field",f"{q}.{k}","Unknown field")
            for k in sorted(req-set(o)): _err(errors,"missing_field",f"{q}.{k}","Required field is missing")
            if o.get("side") not in ("buy","sell"): _err(errors,"invalid_value",f"{q}.side","Unknown order side")
            if o.get("good") not in ("fiber","net"): _err(errors,"invalid_value",f"{q}.good","Unknown order good")
            for k in ("quantity","price"):
                if type(o.get(k)) is not int: _err(errors,"wrong_type",f"{q}.{k}","Expected an integer")
                elif not 1 <= o[k] <= (10000 if k == "quantity" else 100000): _err(errors,"invalid_value",f"{q}.{k}","Value is outside the supported range")
            if type(o.get("net_ids")) is not list or any(type(n) is not str for n in o.get("net_ids", [])):
                _err(errors,"wrong_type",f"{q}.net_ids","Expected a list of strings")
            elif len(set(o["net_ids"])) != len(o["net_ids"]): _err(errors,"invalid_value",f"{q}.net_ids","Duplicate net ids")
            if o.get("net_ids") and (o.get("good") != "net" or o.get("side") != "sell"):
                _err(errors,"invariant",f"{q}.net_ids","Only net sell orders can name tools")
            if o.get("good") == "net" and o.get("side") == "sell" and type(o.get("quantity")) is int and type(o.get("net_ids")) is list and len(o["net_ids"]) != o["quantity"]:
                _err(errors,"invariant",f"{q}.net_ids","Net sell quantity must match tool ids")
    return errors


def prepare_decision(raw: Any, context: dict[str, Any]) -> tuple[Plan, dict[str, Any]]:
    candidate, warnings = _normalize(raw, context)
    errors = _validate(candidate)
    if errors: raise StructuredDecisionError(errors)
    rid, day = context["resident_id"], context["day"]
    horizon = context.get("public", {}).get("rules", {}).get("days", context.get("horizon", 30))
    if day + len(candidate["days"]) - 1 > horizon:
        raise StructuredDecisionError([{"code":"horizon","path":"$.days","message":"Decision exceeds the world horizon"}])
    run = context.get("run_id", "local"); revision = context.get("decision_revision", context.get("revision", 0))
    candidate_hash = digest(candidate)
    binding = digest({"run_id":run,"resident_id":rid,"day":day,"revision":revision,"candidate":candidate_hash})
    schedule=[]
    for i, item in enumerate(candidate["days"]):
        orders=[]
        for j,o in enumerate(item["orders"]):
            # Tie-breaking must not acquire entropy from run UUIDs or free-form rationale.
            oid=f"order-{rid}-d{day+i}-o{j}"
            orders.append(Order(oid,rid,o["side"],o["good"],o["quantity"],o["price"],tuple(o["net_ids"])))
        schedule.append(DailyIntent(rid, day+i, tuple(item["labor"]), tuple(orders)))
    plan=Plan(f"{binding[:32]}",rid,day,candidate["goal"],tuple(schedule),candidate["rationale"],context.get("replan_reason","initial_or_expired"))
    from isleworks.engine.preconditions import validate_today
    checked = validate_today(plan, context)
    warnings.extend(checked.get("warnings", []))
    return plan, {"contract_version":CONTRACT_VERSION,"decision_hash":candidate_hash,"binding_hash":binding,
                  "normalization":[w for w in warnings if isinstance(w, str)],
                  "warnings":[w for w in warnings if isinstance(w, dict)],"normalized": candidate != raw}


from isleworks.engine.preconditions import validate_today

__all__=["CONTRACT_VERSION","DECISION_SCHEMA","StructuredDecisionError","prepare_decision","validate_today"]
