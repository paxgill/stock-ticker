"""
FIFO lot accounting for the trade journal.

Pure functions over a list of trade dicts so they can be unit-tested without a
database. A "trade" is {action, shares, price, date} where action is one of
Buy / Sell / Short / Cover (we account Buy→Sell long lots here; Short/Cover are
treated symmetrically as negative lots).
"""
from __future__ import annotations

from collections import deque


def _ordered(trades: list) -> list:
    return sorted(trades, key=lambda t: (str(t.get("date") or ""), t.get("id", 0)))


def build_open_lots(trades: list) -> deque:
    """
    Replay Buys/Sells in date order and return the FIFO queue of still-open long
    lots as a deque of [shares, price]. Sells consume the oldest lots first.
    """
    lots: deque = deque()
    for t in _ordered(trades):
        action = str(t.get("action", "")).lower()
        shares = float(t.get("shares") or 0)
        price = float(t.get("price") or 0)
        if shares <= 0:
            continue
        if action in ("buy", "cover"):
            lots.append([shares, price])
        elif action in ("sell", "short"):
            remaining = shares
            while remaining > 1e-9 and lots:
                lot = lots[0]
                take = min(lot[0], remaining)
                lot[0] -= take
                remaining -= take
                if lot[0] <= 1e-9:
                    lots.popleft()
            # oversell with no lots → ignored (can't realize against nothing)
    return lots


def fifo_realized(open_lots: deque, sell_shares: float, sell_price: float) -> float:
    """Realized P&L for selling `sell_shares` at `sell_price` against FIFO lots."""
    remaining = float(sell_shares)
    pnl = 0.0
    lots = deque([list(l) for l in open_lots])   # copy
    while remaining > 1e-9 and lots:
        lot = lots[0]
        take = min(lot[0], remaining)
        pnl += take * (sell_price - lot[1])
        lot[0] -= take
        remaining -= take
        if lot[0] <= 1e-9:
            lots.popleft()
    return round(pnl, 2)


def blended_cost(open_lots: deque) -> tuple[float, float]:
    """Return (total_shares, weighted_avg_cost) of the open lots."""
    total = sum(l[0] for l in open_lots)
    if total <= 0:
        return 0.0, 0.0
    cost = sum(l[0] * l[1] for l in open_lots) / total
    return round(total, 4), round(cost, 4)


def fifo_preview(prior_trades: list, sell_shares: float, sell_price: float) -> dict:
    """
    Given the prior trades for one symbol, compute the realized P&L of a new sell
    plus the blended cost basis of the open lots it would consume from.
    """
    lots = build_open_lots(prior_trades)
    shares, cost = blended_cost(lots)
    realized = fifo_realized(lots, sell_shares, sell_price)
    return {
        "realized_pnl": realized,
        "open_shares": shares,
        "blended_cost": cost,
        "sufficient_lots": shares + 1e-9 >= float(sell_shares),
    }
