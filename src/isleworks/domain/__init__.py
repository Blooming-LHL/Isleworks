"""Shared, dependency-free world contracts. Fish amounts are integer millifish."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any

FISH = 1000
RULES_VERSION = "0.1.0"
NET_FIBER_COST = 4
NET_DURABILITY = 20
ACTIONS = frozenset({"fish", "gather", "craft", "rest"})


class PlanValidationError(ValueError):
    """A safe, structured explanation of why a plan payload is invalid."""

    def __init__(self, code: str, path: str, message: str):
        self.code, self.path, self.message = code, path, message
        super().__init__(message)


def _plan_error(code: str, path: str, message: str) -> None:
    raise PlanValidationError(code, path, message)


def _object(value: Any, path: str, allowed: set[str], required: set[str]) -> dict[str, Any]:
    if type(value) is not dict:
        _plan_error("wrong_type", path, "Expected an object")
    unknown = sorted(set(value) - allowed)
    if unknown:
        _plan_error("unknown_field", f"{path}.{unknown[0]}", "Unknown field")
    missing = sorted(required - set(value))
    if missing:
        _plan_error("missing_field", f"{path}.{missing[0]}", "Required field is missing")
    return value


def _string(value: Any, path: str) -> str:
    if type(value) is not str:
        _plan_error("wrong_type", path, "Expected a string")
    return value


def _int(value: Any, path: str) -> int:
    if type(value) is not int:
        _plan_error("wrong_type", path, "Expected an integer")
    return value


def _enum(value: Any, path: str, values: frozenset[str], message: str) -> str:
    value = _string(value, path)
    if value not in values:
        _plan_error("invalid_value", path, message)
    return value


def validate_plan_payload(raw: Any) -> "Plan":
    """Validate and construct a Plan, retaining JSON paths for bad payloads."""
    root = _object(raw, "$", {"id", "resident_id", "created_day", "goal", "schedule",
                                "rationale", "replacement_reason"},
                   {"id", "resident_id", "created_day", "goal", "schedule"})
    for key in ("id", "resident_id", "goal"):
        value = _string(root[key], f"$.{key}")
        if key in ("id", "resident_id") and not value:
            _plan_error("invalid_value", f"$.{key}", "Value must not be empty")
        if key == "id" and len(value) > 120:
            _plan_error("invalid_value", "$.id", "Value exceeds the supported length")
        if key == "goal" and len(value) > 2000:
            _plan_error("invalid_value", "$.goal", "Value exceeds the supported length")
    created_day = _int(root["created_day"], "$.created_day")
    if not 1 <= created_day <= 30:
        _plan_error("invalid_value", "$.created_day", "Day is outside the supported range")
    if "rationale" in root:
        rationale = _string(root["rationale"], "$.rationale")
        if len(rationale) > 3000:
            _plan_error("invalid_value", "$.rationale", "Value exceeds the supported length")
    if "replacement_reason" in root:
        _string(root["replacement_reason"], "$.replacement_reason")
    schedule = root["schedule"]
    if type(schedule) is not list:
        _plan_error("wrong_type", "$.schedule", "Expected a list")
    if not 1 <= len(schedule) <= 3:
        _plan_error("invalid_value", "$.schedule", "Schedule must contain one to three days")
    intents: list[dict[str, Any]] = []
    for index, raw_intent in enumerate(schedule):
        path = f"$.schedule[{index}]"
        intent = _object(raw_intent, path, {"resident_id", "day", "labor", "orders"},
                         {"resident_id", "day"})
        _string(intent["resident_id"], f"{path}.resident_id")
        intent_day = _int(intent["day"], f"{path}.day")
        if not 1 <= intent_day <= 30:
            _plan_error("invalid_value", f"{path}.day", "Day is outside the supported range")
        if intent["resident_id"] != root["resident_id"]:
            _plan_error("invariant", f"{path}.resident_id", "Schedule owner must match the plan")
        if intent_day != created_day + index:
            _plan_error("invariant", f"{path}.day", "Schedule days must be consecutive")
        labor = intent.get("labor", ["rest"] * 4)
        if type(labor) is not list:
            _plan_error("wrong_type", f"{path}.labor", "Expected a list")
        if len(labor) != 4:
            _plan_error("invalid_value", f"{path}.labor", "Exactly four labor blocks are required")
        for block, action in enumerate(labor):
            _enum(action, f"{path}.labor[{block}]", ACTIONS, "Unknown labor action")
        orders = intent.get("orders", [])
        if type(orders) is not list:
            _plan_error("wrong_type", f"{path}.orders", "Expected a list")
        if len(orders) > 3:
            _plan_error("invalid_value", f"{path}.orders", "At most three orders are allowed")
        normalized_orders: list[dict[str, Any]] = []
        for order_index, raw_order in enumerate(orders):
            order_path = f"{path}.orders[{order_index}]"
            order = _object(raw_order, order_path,
                            {"id", "resident_id", "side", "good", "quantity", "price", "net_ids"},
                            {"id", "resident_id", "side", "good", "quantity", "price"})
            order_id = _string(order.get("id"), f"{order_path}.id")
            if not order_id or len(order_id) > 120:
                _plan_error("invalid_value", f"{order_path}.id", "Value is empty or too long")
            order_owner = _string(order.get("resident_id"), f"{order_path}.resident_id")
            if order_owner != intent["resident_id"]:
                _plan_error("invariant", f"{order_path}.resident_id", "Order owner must match the intent")
            _enum(order.get("side"), f"{order_path}.side", frozenset({"buy", "sell"}), "Unknown order side")
            _enum(order.get("good"), f"{order_path}.good", frozenset({"fiber", "net"}), "Unknown order good")
            for field in ("quantity", "price"):
                number = _int(order.get(field), f"{order_path}.{field}")
                maximum = 10000 if field == "quantity" else 100000
                if not 1 <= number <= maximum:
                    _plan_error("invalid_value", f"{order_path}.{field}", "Value is outside the supported range")
            net_ids = order.get("net_ids", [])
            if type(net_ids) is not list:
                _plan_error("wrong_type", f"{order_path}.net_ids", "Expected a list")
            for net_index, net_id in enumerate(net_ids):
                _string(net_id, f"{order_path}.net_ids[{net_index}]")
            if len(set(net_ids)) != len(net_ids):
                _plan_error("invalid_value", f"{order_path}.net_ids", "Duplicate net ids are not allowed")
            if net_ids and (order["good"] != "net" or order["side"] != "sell"):
                _plan_error("invariant", f"{order_path}.net_ids", "Only net sell orders can name tools")
            if order["good"] == "net" and order["side"] == "sell" and len(net_ids) != order["quantity"]:
                _plan_error("invariant", f"{order_path}.net_ids", "Net sell quantity must match tool ids")
            normalized_orders.append(order)
        # Exercise the existing order invariants, while retaining the mapping
        # shape expected by DailyIntent.from_dict (including its defaults).
        for order in normalized_orders:
            try:
                Order.from_dict({**order, "net_ids": tuple(order.get("net_ids", []))})
            except (ValueError, TypeError, KeyError):
                _plan_error("invalid_value", f"{path}.orders", "Order violates a domain invariant")
        intents.append({**intent, "labor": labor, "orders": normalized_orders})
    try:
        return Plan.from_dict({**root, "schedule": intents})
    except (ValueError, TypeError, KeyError) as exc:
        # Constructor messages are intentionally not exposed: they may contain
        # provider-controlled values and do not identify a JSON location.
        text = str(exc)
        if "owner/date" in text:
            _plan_error("invariant", "$.schedule", "Schedule owner and dates must match the plan")
        _plan_error("invalid_value", "$", "Plan violates a domain invariant")


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def integer(value: Any, name: str, minimum: int = 0, maximum: int = 10**12) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in [{minimum}, {maximum}]")
    return value


def fish_units(value: Any) -> int:
    amount = Decimal(str(value)) * FISH
    if not amount.is_finite() or amount != amount.to_integral_value() or amount < 0:
        raise ValueError("Fish must be nonnegative and exactly representable in 0.001 units")
    return int(amount)


@dataclass(frozen=True)
class ScenarioConfig:
    name: str = "baseline"
    days: int = 30
    residents: int = 6
    initial_fish_units: int = 2000
    fishing_abundance_percent: int = 100
    nets_enabled: bool = True
    shock_enabled: bool = False
    parent_hash: str | None = None
    rules_version: str = RULES_VERSION

    def __post_init__(self) -> None:
        integer(self.days, "days", 1, 30)
        integer(self.residents, "residents", 1, 60)
        integer(self.initial_fish_units, "initial_fish_units", 0, 100000)
        if type(self.fishing_abundance_percent) is not int or self.fishing_abundance_percent not in (80, 100, 120):
            raise ValueError("Abundance must be 80, 100 or 120 percent")
        if type(self.nets_enabled) is not bool or type(self.shock_enabled) is not bool:
            raise ValueError("Scenario switches must be booleans")
        if not isinstance(self.name, str) or not self.name or len(self.name) > 100:
            raise ValueError("Invalid scenario name")
        if self.rules_version != RULES_VERSION:
            raise ValueError("Unsupported rules version")
        if self.parent_hash is not None and (not isinstance(self.parent_hash, str) or len(self.parent_hash) != 64):
            raise ValueError("Invalid parent hash")

    @property
    def config_hash(self) -> str:
        return digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ScenarioConfig:
        return cls(**value)


@dataclass
class Resident:
    id: str
    name: str
    specialty: str
    fish: int = 2000
    fiber: int = 0
    hunger: int = 0
    active: bool = True

    @property
    def fishing_yield(self) -> int:
        return 750 if self.specialty == "fisher" else 500

    @property
    def fiber_yield(self) -> int:
        return 2 if self.specialty == "forager" else 1

    @property
    def craft_blocks(self) -> int:
        return 2 if self.specialty == "crafter" else 4


@dataclass
class Net:
    id: str
    owner: str
    durability: int = NET_DURABILITY


@dataclass
class WorldState:
    day: int = 0
    residents: dict[str, Resident] = field(default_factory=dict)
    nets: dict[str, Net] = field(default_factory=dict)
    event_seq: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"day": self.day, "event_seq": self.event_seq,
                "residents": {k: asdict(v) for k, v in sorted(self.residents.items())},
                "nets": {k: asdict(v) for k, v in sorted(self.nets.items())}}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> WorldState:
        state = cls(day=value["day"], event_seq=value["event_seq"],
                    residents={k: Resident(**v) for k, v in value["residents"].items()},
                    nets={k: Net(**v) for k, v in value["nets"].items()})
        state.validate()
        return state

    @property
    def state_hash(self) -> str:
        return digest(self.to_dict())

    def clone(self) -> WorldState:
        return copy.deepcopy(self)

    def validate(self) -> None:
        integer(self.day, "day", 0, 30)
        integer(self.event_seq, "event_seq")
        for key, resident in self.residents.items():
            if key != resident.id or resident.specialty not in ("fisher", "forager", "crafter"):
                raise ValueError("Invalid resident identity or specialty")
            integer(resident.fish, "fish")
            integer(resident.fiber, "fiber")
            integer(resident.hunger, "hunger")
            if type(resident.active) is not bool:
                raise ValueError("Invalid resident status")
        for key, net in self.nets.items():
            if key != net.id or net.owner not in self.residents:
                raise ValueError("Invalid net ownership")
            integer(net.durability, "durability", 0, 20)


@dataclass(frozen=True)
class Order:
    id: str
    resident_id: str
    side: str
    good: str
    quantity: int
    price: int
    net_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id or len(self.id) > 120:
            raise ValueError("Invalid order id")
        if not isinstance(self.resident_id, str) or self.side not in ("buy", "sell") or self.good not in ("fiber", "net"):
            raise ValueError("Invalid order identity, side or good")
        integer(self.quantity, "quantity", 1, 10000)
        integer(self.price, "price", 1, 100000)
        if type(self.net_ids) is not tuple or any(not isinstance(n, str) for n in self.net_ids):
            raise ValueError("net_ids must be a tuple of ids")
        if len(set(self.net_ids)) != len(self.net_ids):
            raise ValueError("Duplicate net id")
        if self.net_ids and (self.good != "net" or self.side != "sell"):
            raise ValueError("Only net sell orders can name tools")
        if self.good == "net" and self.side == "sell" and len(self.net_ids) != self.quantity:
            raise ValueError("Every net sold needs a unique tool id")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Order:
        data = dict(value)
        data["net_ids"] = tuple(data.get("net_ids", ()))
        return cls(**data)


@dataclass(frozen=True)
class DailyIntent:
    resident_id: str
    day: int
    labor: tuple[str, ...] = ("rest", "rest", "rest", "rest")
    orders: tuple[Order, ...] = ()

    def __post_init__(self) -> None:
        integer(self.day, "intent day", 1, 30)
        if not isinstance(self.resident_id, str) or not self.resident_id:
            raise ValueError("Invalid intent owner")
        if type(self.labor) is not tuple or len(self.labor) != 4 or any(a not in ACTIONS for a in self.labor):
            raise ValueError("Exactly four valid labor blocks required")
        if type(self.orders) is not tuple or len(self.orders) > 3 or any(not isinstance(o, Order) or o.resident_id != self.resident_id for o in self.orders):
            raise ValueError("At most three owner-matched orders allowed")

    def to_dict(self) -> dict[str, Any]:
        return {"resident_id": self.resident_id, "day": self.day, "labor": list(self.labor),
                "orders": [o.to_dict() for o in self.orders]}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> DailyIntent:
        data = dict(value)
        data["labor"] = tuple(data.get("labor", ("rest",) * 4))
        data["orders"] = tuple(Order.from_dict(o) for o in data.get("orders", ()))
        return cls(**data)


@dataclass(frozen=True)
class Plan:
    id: str
    resident_id: str
    created_day: int
    goal: str
    schedule: tuple[DailyIntent, ...]
    rationale: str = ""
    replacement_reason: str = "plan_expired"

    def __post_init__(self) -> None:
        integer(self.created_day, "plan day", 1, 30)
        if not isinstance(self.id, str) or not self.id or not isinstance(self.goal, str) or len(self.goal) > 2000:
            raise ValueError("Invalid plan identity or goal")
        if type(self.schedule) is not tuple or not 1 <= len(self.schedule) <= 3:
            raise ValueError("Plans cover one to three days")
        for offset, intent in enumerate(self.schedule):
            if intent.resident_id != self.resident_id or intent.day != self.created_day + offset:
                raise ValueError("Plan owner/date mismatch")
        if not isinstance(self.rationale, str) or len(self.rationale) > 3000:
            raise ValueError("Invalid rationale")

    def for_day(self, day: int) -> DailyIntent | None:
        return next((intent for intent in self.schedule if intent.day == day), None)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "resident_id": self.resident_id, "created_day": self.created_day,
                "goal": self.goal, "schedule": [i.to_dict() for i in self.schedule],
                "rationale": self.rationale, "replacement_reason": self.replacement_reason}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Plan:
        data = dict(value)
        data["schedule"] = tuple(DailyIntent.from_dict(i) for i in data["schedule"])
        return cls(**data)


@dataclass(frozen=True)
class Event:
    id: str
    day: int
    kind: str
    actor: str | None
    data: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DayCommit:
    before_hash: str
    state: WorldState
    events: tuple[Event, ...]
    intents: tuple[DailyIntent, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"before_hash": self.before_hash, "state": self.state.to_dict(),
                "state_hash": self.state.state_hash, "events": [e.to_dict() for e in self.events],
                "intents": [i.to_dict() for i in self.intents]}
