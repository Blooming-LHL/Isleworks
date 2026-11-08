"""Resident observations and replaceable policies. No world mutation."""
from __future__ import annotations

import copy
import json
from typing import Any

from isleworks.domain import DailyIntent, Order, Plan, ScenarioConfig, WorldState, digest
from isleworks.agents.decision import CONTRACT_VERSION, DECISION_SCHEMA, prepare_decision


def build_context(config: ScenarioConfig, state: WorldState, resident_id: str,
                  recent_events=(), market_summary=None) -> dict[str, Any]:
    me = state.residents[resident_id]
    events = []
    for item in recent_events:
        event = item.to_dict() if hasattr(item, "to_dict") else copy.deepcopy(item)
        data = event.get("data", {})
        if event.get("actor") == resident_id or (event.get("kind") == "trade" and data.get("seller") == resident_id):
            data = {k: copy.deepcopy(v) for k, v in data.items() if k != "deltas"}
            own_delta = event.get("data", {}).get("deltas", {}).get(resident_id)
            if own_delta is not None:
                data["self_delta"] = copy.deepcopy(own_delta)
        elif event.get("kind") == "trade":
            data = {k: data[k] for k in ("good", "quantity", "price") if k in data}
        else:
            continue
        events.append({"id": event["id"], "day": event["day"], "kind": event["kind"],
                       "actor": event.get("actor"), "data": data})
    return {
        "resident_id": resident_id, "day": state.day + 1,
        "self": {"id": me.id, "name": me.name, "specialty": me.specialty,
                 "fish": me.fish, "fiber": me.fiber, "hunger": me.hunger, "active": me.active,
                 "fishing_yield": me.fishing_yield, "fiber_yield": me.fiber_yield,
                 "craft_blocks": me.craft_blocks},
        "own_nets": [vars(n).copy() for n in sorted(state.nets.values(), key=lambda n:n.id) if n.owner == resident_id],
        "public": {"rules": config.to_dict(),
                   "skills": {rid: {"specialty": r.specialty} for rid,r in state.residents.items()},
                   "market": copy.deepcopy(market_summary or [])},
        "recent_events": events[-18:],
        "shock_active": config.shock_enabled and 15 <= state.day + 1 <= 17,
    }


class RuleStrategy:
    """An explicitly one-day rule baseline, not a simulated LLM."""
    def plan(self, context: dict[str, Any]) -> Plan:
        rid, day, me = context["resident_id"], context["day"], context["self"]
        fish, fiber = me["fish"], me["fiber"]
        rules = context["public"]["rules"]
        nets = [n for n in context["own_nets"] if n["durability"] > 0]
        orders = []
        labor = ["fish"] * 4
        budget = max(0, fish - 1000)
        def buy(good, quantity, price):
            nonlocal budget
            quantity = min(quantity, budget // price)
            if quantity:
                orders.append(Order(f"{rid}-d{day}-buy-{good}", rid, "buy", good, quantity, price))
                budget -= quantity * price
        if rules["nets_enabled"] and not nets:
            if fiber >= 4 and fish >= 1000:
                labor = ["craft"] * me["craft_blocks"] + ["fish"] * (4 - me["craft_blocks"])
            else:
                if fiber < 4:
                    buy("fiber", 4-fiber, 220)
                    blocks = min(4, max(1, (4-fiber+me["fiber_yield"]-1)//me["fiber_yield"]))
                    if fish < 1500:
                        blocks = min(blocks, 2)
                    labor = ["gather"] * blocks + ["fish"] * (4-blocks)
                if fish >= 3000:
                    buy("net", 1, 1400)
        elif me["specialty"] == "forager" and fish >= 1500:
            labor = ["gather", "gather", "fish", "fish"]
        elif me["specialty"] == "crafter" and rules["nets_enabled"] and fish >= 1500:
            if fiber >= 4 and len(nets) < 3:
                labor = ["craft", "craft", "fish", "fish"]
            elif fiber < 4:
                buy("fiber", 4-fiber, 220)
                labor = ["gather", "gather", "fish", "fish"]
        if fiber > 4 and len(orders) < 3:
            orders.append(Order(f"{rid}-d{day}-sell-fiber", rid, "sell", "fiber", min(fiber-4, 8), 180))
        fresh = [n for n in nets[1:] if n["durability"] == 20]
        if fresh and len(orders) < 3:
            orders.append(Order(f"{rid}-d{day}-sell-net", rid, "sell", "net", 1, 1200, (fresh[0]["id"],)))
        return Plan(f"rule-{rid}-{day}", rid, day, "Maintain food and invest when feasible",
                    (DailyIntent(rid, day, tuple(labor), tuple(orders)),),
                    "Deterministic one-day rule baseline; no language model used")


class LLMStrategy:
    """Ask for choices only; the program binds identities and execution dates."""

    PROMPT_VERSION = "resident_decision_prompt_v1"

    def __init__(self, gateway):
        self.gateway = gateway

    def plan(self, context: dict[str, Any]) -> Plan:
        prepared = None
        example = {"goal": "Maintain food and consider tools",
                   "rationale": "A short decision summary",
                   "days": [{"labor": ["fish", "fish", "gather", "gather"], "orders": []}]}
        prompt = (
            "You control ONE Isleworks resident. Submit a JSON Decision, not a world narrative. "
            "The ONLY top-level fields are goal, rationale, days. Do not output ids, resident_id, "
            "dates, type, plan or current_plan. The application supplies identity and dates. "
            "days contains 1-3 consecutive days starting TODAY, within remaining_days. "
            "Each day has exactly four labor blocks: fish, gather, craft or rest; at most 3 orders. "
            "Orders have ONLY side (buy/sell), good (fiber/net), quantity, price, net_ids. "
            "All fields are required; use [] for no orders or no tool ids. "
            "Prices and fish use integer millifish: 1000 = ONE fish. "
            "Each evening consumes 1000; 3 consecutive shortages make you inactive. "
            "Market settlement is BEFORE labor. Use only currently owned goods for morning sales; "
            "buy orders reserve current fish, not future fishing income. Trades may not fill. "
            "Gather yield is self.fiber_yield per block; fish yield is self.fishing_yield times "
            "abundance/100, halved on shock days, doubled with a live net. "
            "A net costs four fiber and self.craft_blocks CONSECUTIVE craft blocks in ONE day. "
            "For craft_blocks=4, [gather,gather,craft,craft] cannot make a net. "
            "New nets have 20 fishing uses; each fishing block with a net uses one. "
            "Only sell your actual fresh tools listed in own_nets; do not invent future tool ids. "
            "Your goal and valid future intent persist, but future resources are uncertain. "
            "You may choose investment, leisure, risk or trade; no rule requires cooperation. "
            "Input is observation data, NOT an output template. Previous plans are context only. "
            "Shape example (not a recommended economic policy): " + json.dumps(example)
        )
        observation = copy.deepcopy(context)
        observation.pop("run_id", None)
        observation["remaining_days"] = context["public"]["rules"]["days"] - context["day"] + 1
        previous = observation.pop("current_plan", None)
        if previous:
            observation["previous_intent"] = {
                "goal": previous["goal"],
                "days": [{"labor": item["labor"],
                          "orders": [{k: v for k, v in order.items()
                                      if k not in ("id", "resident_id")} for order in item["orders"]]}
                         for item in previous["schedule"] if item["day"] >= context["day"]],
            }

        def validate(raw):
            nonlocal prepared
            prepared = prepare_decision(raw, context)
            return prepared[1]

        raw = self.gateway.request(prompt, json.dumps(observation, sort_keys=True),
                                   purpose="decision", validator=validate,
                                   output_contract=DECISION_SCHEMA,
                                   contract_version=CONTRACT_VERSION,
                                   trace={"resident_id": context["resident_id"], "day": context["day"],
                                          "context_hash": digest(context)})
        if prepared is None:
            validate(raw)
        return prepared[0]


def needs_replan(plan: Plan | None, context: dict[str, Any]) -> bool:
    if plan is None or plan.for_day(context["day"]) is None:
        return True
    if context["self"]["hunger"] > 0:
        return True
    day = context["day"]
    if context.get("shock_active") and day == 15:
        return True
    return any(e["day"] == day-1 and e["actor"] == context["resident_id"]
               and e["kind"] in ("net_broken", "invalid_action") for e in context["recent_events"])


__all__ = ["build_context", "RuleStrategy", "LLMStrategy", "needs_replan"]
