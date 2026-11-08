"""Pure deterministic order matching for the Isleworks daily market."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Iterable

from isleworks.domain import DailyIntent, Order, WorldState


@dataclass(frozen=True)
class Trade:
    buy_order: str
    sell_order: str
    buyer: str
    seller: str
    good: str
    quantity: int
    price: int
    net_ids: tuple[str, ...] = ()


def _tie(seed: int, day: int, order: Order) -> str:
    return hashlib.sha256(f"{seed}:{day}:{order.id}".encode()).hexdigest()


def match_orders(state: WorldState, intents: Iterable[DailyIntent], day: int,
                 seed: int = 42) -> tuple[tuple[Trade, ...], tuple[str, ...]]:
    """Return proposals and rejected order IDs without mutating ``state``.

    Reservations are aggregated against the day-start balances.  Invalid or
    overcommitted orders are excluded; valid orders continue to match.
    """
    orders = [o for i in intents for o in i.orders if state.residents.get(i.resident_id, None) is not None and state.residents[i.resident_id].active]
    counts: dict[str, int] = {}
    for o in orders: counts[o.id] = counts.get(o.id, 0) + 1
    duplicate_ids = {oid for oid, count in counts.items() if count > 1}
    seen: set[str] = set()
    rejected: set[str] = set()
    valid: list[Order] = []
    for o in orders:
        if o.id in duplicate_ids or o.id in seen or o.resident_id not in state.residents:
            rejected.add(o.id)
            continue
        seen.add(o.id)
        if o.good == "net" and o.side == "sell":
            if any(n not in state.nets or state.nets[n].owner != o.resident_id for n in o.net_ids):
                rejected.add(o.id); continue
        valid.append(o)

    # Reservations are checked before matching, and remain fixed during matching.
    for owner in sorted(state.residents):
        resident_orders = [o for o in valid if o.resident_id == owner]
        fish_need = sum(o.quantity * o.price for o in resident_orders if o.side == "buy")
        if fish_need > state.residents[owner].fish:
            rejected.update(o.id for o in resident_orders if o.side == "buy")
        by_good: dict[str, int] = {"fiber": 0, "net": 0}
        for o in resident_orders:
            if o.side == "sell": by_good[o.good] += o.quantity
        if by_good["fiber"] > state.residents[owner].fiber:
            rejected.update(o.id for o in resident_orders if o.side == "sell" and o.good == "fiber")
        net_orders = [o for o in resident_orders if o.side == "sell" and o.good == "net"]
        if len({n for o in net_orders for n in o.net_ids}) != sum(o.quantity for o in net_orders):
            rejected.update(o.id for o in net_orders)
        if any(state.nets[n].durability != 20 for o in net_orders for n in o.net_ids):
            rejected.update(o.id for o in net_orders)

    active = [o for o in valid if o.id not in rejected]
    trades: list[Trade] = []
    for good in ("fiber", "net"):
        buys = sorted((o for o in active if o.good == good and o.side == "buy"),
                      key=lambda o: (-o.price, _tie(seed, day, o)))
        sells = sorted((o for o in active if o.good == good and o.side == "sell"),
                       key=lambda o: (o.price, _tie(seed, day, o)))
        buy_left = {o.id: o.quantity for o in buys}
        sell_left = {o.id: o.quantity for o in sells}
        sell_offset = {o.id: 0 for o in sells}
        for b in buys:
            while buy_left[b.id] > 0:
                candidates = [s for s in sells if sell_left[s.id] and b.price >= s.price
                              and b.resident_id != s.resident_id]
                if not candidates:
                    break
                s = candidates[0]
                qty = min(buy_left[b.id], sell_left[s.id])
                start = sell_offset[s.id]
                ids = s.net_ids[start:start + qty] if good == "net" else ()
                trades.append(Trade(b.id, s.id, b.resident_id, s.resident_id, good, qty, s.price, ids))
                buy_left[b.id] -= qty
                sell_left[s.id] -= qty
                sell_offset[s.id] += qty
    return tuple(trades), tuple(sorted(rejected))


__all__ = ["Trade", "match_orders"]
