"""Deterministic, versioned metrics derived from committed resource events."""
from __future__ import annotations

from collections import Counter, defaultdict


def _dict(value):
    return value.to_dict() if hasattr(value, "to_dict") else value


def build_metrics(config, initial_state, final_state, events):
    initial, final = _dict(initial_state), _dict(final_state)
    config_data = _dict(config)
    evs = [_dict(e) for e in events]
    totals = Counter()
    by_day = defaultdict(list)
    exits = {}
    for e in evs:
        by_day[e["day"]].append(e)
        data, kind = e["data"], e["kind"]
        if kind == "fish":
            totals["food_produced"] += sum(v.get("fish", 0) for v in data.get("deltas", {}).values())
        if kind == "consume":
            totals["food_consumed"] += data["amount"]
            totals["shortage"] += data["shortage"]
            totals["shortage_days"] += int(data["shortage"] > 0)
        if kind == "trade":
            totals["trades"] += 1
            totals["trade_value"] += data["quantity"] * data["price"]
        if kind == "craft":
            totals["nets_made"] += 1
        if kind == "net_broken":
            totals["nets_broken"] += 1
        if kind == "exit":
            exits.setdefault(e["actor"], e["day"])
        if kind in ("fish", "gather", "craft", "rest"):
            blocks = data.get("blocks", 1)
            totals["labor_blocks"] += blocks if kind != "rest" else 0
            totals["rest_blocks"] += blocks if kind == "rest" else 0
        if kind == "invalid_action":
            totals["invalid_actions"] += 1
    count = len(initial["residents"])
    days = final["day"]
    stock = sum(r["fish"] for r in initial["residents"].values())
    fiber = sum(r["fiber"] for r in initial["residents"].values())
    alive = sum(r["active"] for r in initial["residents"].values())
    daily = {"0": {"food_stock": stock, "fiber_stock": fiber, "active": alive, "trades": 0}}
    for day in range(1, days+1):
        row = {"food_produced": 0, "food_consumed": 0, "trades": 0, "shortage": 0, "nets_made": 0}
        for e in by_day[day]:
            data, kind = e["data"], e["kind"]
            for delta in data.get("deltas", {}).values():
                stock += delta.get("fish", 0)
                fiber += delta.get("fiber", 0)
            if kind == "fish":
                row["food_produced"] += sum(d.get("fish", 0) for d in data.get("deltas", {}).values())
            if kind == "consume":
                row["food_consumed"] += data["amount"]
                row["shortage"] += data["shortage"]
            if kind == "trade":
                row["trades"] += 1
            if kind == "craft":
                row["nets_made"] += 1
            if kind == "exit":
                alive -= 1
        row.update(food_stock=stock, fiber_stock=fiber, active=alive)
        daily[str(day)] = row
    metrics = dict(totals)
    metrics.update(
        resident_days=sum(min(days, exits.get(rid, days)) for rid in initial["residents"]),
        resident_day_capacity=count * config_data["days"],
        final_active=sum(r["active"] for r in final["residents"].values()),
        final_food=sum(r["fish"] for r in final["residents"].values()),
        final_fiber=sum(r["fiber"] for r in final["residents"].values()),
        final_nets=sum(n["durability"] > 0 for n in final["nets"].values()),
        completed_days=days,
    )
    for key in ("shortage", "trades", "nets_made", "food_produced", "food_consumed", "labor_blocks", "rest_blocks", "invalid_actions"):
        metrics.setdefault(key, 0)
    # Bounded evidence covers the whole run as well as rare, decision-relevant events.
    sample = []
    important = [e for e in evs if e["kind"] in ("exit", "craft", "net_broken", "invalid_action")]
    sample.extend(important[:8] + important[-8:])
    if evs:
        sample.extend(evs[min(len(evs)-1, round(i*(len(evs)-1)/11))] for i in range(12))
    evidence = {e["id"]: e for e in sample}
    return {"metric_version": "1", "metrics": metrics, "daily": daily, "evidence": evidence,
            "facts": {"initial_day": initial["day"], "final_day": days,
                      "fish_unit": "0.001 fish", "initial_food": sum(r["fish"] for r in initial["residents"].values()),
                      "rules_version": config_data["rules_version"]}}
