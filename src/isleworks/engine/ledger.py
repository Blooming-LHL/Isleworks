"""Small central ledger for atomic inventory mutations."""
from __future__ import annotations

from isleworks.domain import WorldState
from isleworks.market import Trade


def change_inventory(state: WorldState, resident_id: str, *, fish: int = 0, fiber: int = 0) -> None:
    resident = state.residents[resident_id]
    if resident.fish + fish < 0 or resident.fiber + fiber < 0:
        raise ValueError("inventory cannot become negative")
    resident.fish += fish
    resident.fiber += fiber


def apply_trade(state: WorldState, trade: Trade) -> None:
    if trade.quantity <= 0 or trade.price <= 0 or trade.buyer == trade.seller:
        raise ValueError("invalid trade")
    buyer = state.residents[trade.buyer]
    seller = state.residents[trade.seller]
    cost = trade.quantity * trade.price
    # Complete preflight before changing either side.
    if buyer.fish < cost:
        raise ValueError("market preflight failed")
    if trade.good == "fiber" and seller.fiber < trade.quantity:
        raise ValueError("market preflight failed")
    if trade.good == "net":
        if len(set(trade.net_ids)) != len(trade.net_ids):
            raise ValueError("duplicate net ids")
        if len(trade.net_ids) != trade.quantity or any(
            nid not in state.nets or state.nets[nid].owner != seller.id
            or state.nets[nid].durability != 20 for nid in trade.net_ids
        ):
            raise ValueError("market preflight failed")
    buyer.fish -= cost
    seller.fish += cost
    if trade.good == "fiber":
        seller.fiber -= trade.quantity
        buyer.fiber += trade.quantity
    else:
        for nid in trade.net_ids:
            state.nets[nid].owner = buyer.id
