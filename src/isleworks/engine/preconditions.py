"""Read-only checks for today's already-compiled resident intent."""
from __future__ import annotations

from typing import Any

from isleworks.domain import Plan, PlanValidationError, NET_FIBER_COST, NET_DURABILITY


class DecisionError(PlanValidationError):
    def __init__(self, errors: list[dict[str, str]]):
        self.errors = errors
        first = errors[0]
        super().__init__(first["code"], first["path"], first["message"])


def validate_today(plan: Plan, context: dict[str, Any]) -> dict[str, Any]:
    """Validate deterministic, known facts for the current day only.

    The context intentionally mirrors the resident observation and accepts
    either raw dictionaries or a ``WorldState`` under ``state``.
    """
    day = context.get("day", getattr(context.get("state"), "day", 0) + 1)
    rid = context.get("resident_id", plan.resident_id)
    intent = plan.for_day(day)
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    if intent is None:
        errors.append({"code":"missing_today","path":"$.schedule","message":"Plan has no intent for today"})
        raise DecisionError(errors)
    if plan.resident_id != rid or intent.resident_id != rid:
        errors.append({"code":"owner_mismatch","path":"$.resident_id","message":"Plan owner does not match context"})
    me = context.get("self", {})
    state = context.get("state")
    if not me and state is not None and rid in state.residents:
        r=state.residents[rid]; me={"fish":r.fish,"fiber":r.fiber,"craft_blocks":r.craft_blocks,
                                    "fiber_yield":r.fiber_yield,"specialty":r.specialty}
    craft_blocks = me.get("craft_blocks", 4)
    fiber = me.get("fiber", 0); fish = me.get("fish", 0)
    fiber_yield = me.get("fiber_yield", 1)
    rules = context.get("public", {}).get("rules", {})
    if not rules and context.get("config") is not None:
        rules = context["config"].to_dict()
    if not rules.get("nets_enabled", True) and "craft" in intent.labor:
        errors.append({"code":"craft_disabled","path":"$.days[0].labor","message":"Crafting is disabled"})
    # Market reservations are checked before labor and aggregate all orders.
    orders = list(intent.orders)
    buy_cost = sum(o.quantity * o.price for o in orders if o.side == "buy")
    if buy_cost > fish:
        errors.append({"code":"insufficient_buy_reservation","path":"$.days[0].orders","message":"Buy reservations exceed current fish"})
    sell_fiber = sum(o.quantity for o in orders if o.side == "sell" and o.good == "fiber")
    if sell_fiber > fiber:
        errors.append({"code":"insufficient_fiber_reservation","path":"$.days[0].orders","message":"Sell reservations exceed current fiber"})
    own_nets = context.get("own_nets")
    if own_nets is None and state is not None:
        own_nets = [{"id":n.id,"owner":n.owner,"durability":n.durability} for n in state.nets.values() if n.owner == rid]
    own_nets = {n["id"] if isinstance(n,dict) else n.id: n for n in (own_nets or [])}
    sold_ids=[]
    for i,o in enumerate(orders):
        if o.side == "sell" and o.good == "net":
            sold_ids.extend(o.net_ids)
            for nid in o.net_ids:
                n=own_nets.get(nid)
                durability = n.get("durability") if isinstance(n,dict) else getattr(n,"durability",None)
                owner = n.get("owner") if isinstance(n, dict) else getattr(n, "owner", None)
                if n is None or owner != rid or durability != NET_DURABILITY:
                    errors.append({"code":"net_not_owned_or_fresh","path":f"$.days[0].orders[{i}].net_ids","message":"Net must be an owned fresh net"})
    if len(sold_ids) != len(set(sold_ids)):
        errors.append({"code":"duplicate_net_sale","path":"$.days[0].orders","message":"A net cannot be sold twice"})
    # A craft consumes four fiber and the resident's specialty-dependent run
    # of contiguous blocks. Earlier labor may gather fiber; market fills stay
    # conditional and are therefore warnings, never assumed inventory.
    labor = list(intent.labor)
    buy_fiber = sum(o.quantity for o in orders if o.side == "buy" and o.good == "fiber")
    lower = fiber - sell_fiber
    upper = fiber + buy_fiber
    idx = 0
    while idx < 4:
        action = labor[idx]
        if action == "gather":
            lower += fiber_yield; upper += fiber_yield; idx += 1; continue
        if action != "craft":
            idx += 1; continue
        start = idx
        if idx + craft_blocks > 4 or any(a != "craft" for a in labor[idx:idx + craft_blocks]):
            errors.append({"code":"insufficient_contiguous_craft_blocks","path":f"$.days[0].labor[{start}]","message":"Craft requires exactly the contiguous specialty blocks"})
            idx += 1
            continue
        if upper < NET_FIBER_COST:
            errors.append({"code":"insufficient_fiber_for_craft","path":f"$.days[0].labor[{start}]","message":"Craft lacks four fiber even if buys fill"})
        elif lower < NET_FIBER_COST:
            warnings.append({"code":"market_dependent_craft","path":f"$.days[0].labor[{start}]","message":"Craft resource availability depends on market fills"})
        lower = max(0, lower - NET_FIBER_COST); upper = max(0, upper - NET_FIBER_COST)
        idx += craft_blocks
    # Future schedule is intentionally not predicted here.
    return {"day": day, "resident_id": rid, "warnings": warnings, "future_days_unchecked": max(0, len(plan.schedule) - 1)} if not errors else (_raise(errors))


def _raise(errors):
    raise DecisionError(errors)


__all__ = ["DecisionError", "validate_today"]
