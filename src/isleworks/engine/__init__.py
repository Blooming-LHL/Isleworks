"""Deterministic, clone-before-commit Isleworks world engine."""
from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from isleworks.domain import (DailyIntent, DayCommit, Event, Net, Resident,
                              ScenarioConfig, WorldState, digest, NET_FIBER_COST)
from isleworks.market import Trade, match_orders
from isleworks.engine.ledger import apply_trade, change_inventory


def init_world(config: ScenarioConfig) -> WorldState:
    config = _config(config)
    specialties = ("fisher", "fisher", "forager", "forager", "crafter", "crafter")
    residents = {f"r{i+1}": Resident(f"r{i+1}", f"Resident {i+1}", specialties[i % 6], config.initial_fish_units)
                 for i in range(config.residents)}
    state = WorldState(residents=residents)
    state.validate()
    return state


def step_day(state: WorldState, intents: Iterable[DailyIntent], config: ScenarioConfig,
             seed: int = 42) -> DayCommit:
    config = _config(config)
    before = state.state_hash
    work = state.clone()
    work.validate()
    if work.day >= config.days:
        raise ValueError("world has reached configured day limit")
    intents = tuple(intents)
    _validate_intents(work, intents)
    by_owner = {i.resident_id: i for i in intents}
    intents = tuple(by_owner.get(rid, DailyIntent(rid, work.day + 1)) for rid in sorted(work.residents))
    day = work.day + 1
    events: list[Event] = []

    def emit(kind: str, actor: str | None, data: dict) -> None:
        nonlocal work
        work.event_seq += 1
        events.append(Event(f"e{work.event_seq}", day, kind, actor, data))

    trades, rejected = match_orders(work, intents, day, seed)
    order_owners = {order.id: intent.resident_id for intent in intents for order in intent.orders}
    for order_id in rejected:
        emit("invalid_action", order_owners[order_id], {"order_id": order_id, "reason": "reservation_or_ownership"})
    for trade in trades:
        _settle(work, trade)
        deltas = {trade.buyer: {"fish": -trade.quantity * trade.price},
                  trade.seller: {"fish": trade.quantity * trade.price}}
        if trade.good == "fiber":
            deltas[trade.buyer]["fiber"] = trade.quantity
            deltas[trade.seller]["fiber"] = -trade.quantity
        emit("trade", trade.buyer, {"good": trade.good, "quantity": trade.quantity,
                                     "price": trade.price, "seller": trade.seller,
                                     "deltas": deltas})

    for intent in sorted(intents, key=lambda i: i.resident_id):
        resident = work.residents[intent.resident_id]
        if not resident.active: continue
        blocks = intent.labor
        idx = 0
        while idx < 4:
            action = blocks[idx]
            if action == "fish":
                net = next((n for n in sorted(work.nets.values(), key=lambda n: n.id)
                            if n.owner == resident.id and n.durability > 0), None)
                shock = 50 if config.shock_enabled and 15 <= day <= 17 else 100
                multiplier = 2 if net and config.nets_enabled else 1
                numerator = resident.fishing_yield * config.fishing_abundance_percent * shock * multiplier
                if numerator % 10000:
                    raise ValueError("fishing yield is not exactly representable in millifish")
                amount = numerator // 10000
                if net and config.nets_enabled:
                    net.durability -= 1
                    emit("net_wear", resident.id, {"net_id": net.id, "durability": net.durability, "deltas": {resident.id: {"fish": 0, "fiber": 0}}})
                    if net.durability == 0: emit("net_broken", resident.id, {"net_id": net.id, "deltas": {resident.id: {"fish": 0, "fiber": 0}}})
                change_inventory(work, resident.id, fish=amount)
                emit("fish", resident.id, {"deltas": {resident.id: {"fish": amount, "fiber": 0}}})
            elif action == "gather":
                amount = resident.fiber_yield; change_inventory(work, resident.id, fiber=amount)
                emit("gather", resident.id, {"deltas": {resident.id: {"fish": 0, "fiber": amount}}})
            elif action == "craft":
                needed = resident.craft_blocks
                if idx + needed <= 4 and all(a == "craft" for a in blocks[idx:idx + needed]) and resident.fiber >= NET_FIBER_COST and config.nets_enabled:
                    change_inventory(work, resident.id, fiber=-NET_FIBER_COST)
                    nid = f"net-{resident.id}-{day}-{idx}"
                    work.nets[nid] = Net(nid, resident.id)
                    emit("craft", resident.id, {"net_id": nid, "blocks": needed, "deltas": {resident.id: {"fish": 0, "fiber": -NET_FIBER_COST}}})
                    idx += needed - 1
                else:
                    emit("invalid_action", resident.id, {"action": "craft", "blocks": 1, "reason": "needs_contiguous_blocks_and_four_fiber", "deltas": {resident.id: {"fish": 0, "fiber": 0}}})
            else:
                emit("rest", resident.id, {"deltas": {resident.id: {"fish": 0, "fiber": 0}}})
            idx += 1

    for rid in sorted(work.residents):
        r = work.residents[rid]
        if not r.active: continue
        consumed = min(r.fish, 1000); change_inventory(work, rid, fish=-consumed)
        if consumed < 1000: r.hunger += 1
        else: r.hunger = 0
        emit("consume", rid, {"amount": consumed, "shortage": 1000 - consumed,
                              "deltas": {rid: {"fish": -consumed, "fiber": 0}}})
        if r.hunger >= 3:
            r.active = False
            emit("exit", rid, {"reason": "hunger", "deltas": {rid: {"fish": 0, "fiber": 0}}})
    work.day = day
    work.validate()
    _check_resource_events(state, work, events)
    return DayCommit(before, work, tuple(events), tuple(sorted(intents, key=lambda i: i.resident_id)))


def _config(config: ScenarioConfig) -> ScenarioConfig:
    if not isinstance(config, ScenarioConfig): raise TypeError("config must be ScenarioConfig")
    return config


def _validate_intents(state: WorldState, intents: tuple[DailyIntent, ...]) -> None:
    owners = set(state.residents)
    seen: set[str] = set()
    order_ids: set[str] = set()
    for intent in intents:
        if intent.resident_id not in owners: raise ValueError("Unknown intent resident")
        if intent.day != state.day + 1: raise ValueError("Intent day mismatch")
        if intent.resident_id in seen: raise ValueError("Duplicate resident intent")
        seen.add(intent.resident_id)
        for order in intent.orders:
            if order.id in order_ids: raise ValueError("Duplicate order id")
            order_ids.add(order.id)


def _settle(state: WorldState, trade: Trade) -> None:
    apply_trade(state, trade)


def _check_resource_events(before: WorldState, after: WorldState, events: list[Event]) -> None:
    totals: dict[str, dict[str, int]] = defaultdict(lambda: {"fish": 0, "fiber": 0})
    for event in events:
        for rid, delta in event.data.get("deltas", {}).items():
            totals[rid]["fish"] += delta.get("fish", 0)
            totals[rid]["fiber"] += delta.get("fiber", 0)
    for rid, resident in after.residents.items():
        old = before.residents[rid]
        if resident.fish - old.fish != totals[rid]["fish"] or resident.fiber - old.fiber != totals[rid]["fiber"]:
            raise ValueError("resource event ledger mismatch")
