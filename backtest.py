"""
Walk-forward backtester for PG Stock Analysis.

Replays analyze_ticker() on an expanding window, one trading day at a time, and
simulates a long-only swing strategy:

  • Decisions are made on the close of day t using ONLY data through day t.
  • Orders execute at the OPEN of day t+1 (no look-ahead).
  • A position carries the ATR-based suggested stop from its entry day; if a
    later day's low pierces the stop, the trade exits at the stop (or the open
    if the bar gapped through it).

Results are HYPOTHETICAL. Slippage/commissions are excluded unless the caller
supplies a per-side basis-point cost. Nothing here is persisted or ranked.
"""
from __future__ import annotations

from datetime import date

import pandas as pd

from analysis import replay_signals, DISCLAIMER


def _parse_date(s, default):
    if not s:
        return default
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return default


def run_backtest(symbol: str, hist: pd.DataFrame, profile: dict,
                 start=None, end=None, cost_bps: float = 0.0) -> dict:
    """
    Returns a dict with equity curves, the trade list, and summary statistics.
    `hist` should already include warm-up history before `start`.
    """
    if hist is None or len(hist) < 60:
        return {"error": "Not enough history to backtest."}

    hist = hist.dropna(subset=["Open", "High", "Low", "Close"])
    dates = list(hist.index)
    opens = hist["Open"].astype(float).tolist()
    highs = hist["High"].astype(float).tolist()
    lows = hist["Low"].astype(float).tolist()
    closes = hist["Close"].astype(float).tolist()
    n = len(closes)

    d0 = _parse_date(start, dates[0].date() if hasattr(dates[0], "date") else None)
    d1 = _parse_date(end, dates[-1].date() if hasattr(dates[-1], "date") else None)

    def _d(i):
        return dates[i].date() if hasattr(dates[i], "date") else dates[i]

    in_window = [(d0 is None or _d(i) >= d0) and (d1 is None or _d(i) <= d1) for i in range(n)]
    if not any(in_window):
        return {"error": "No trading days in the selected range."}
    first_idx = next(i for i in range(n) if in_window[i])
    cost = max(0.0, cost_bps) / 10_000.0

    # Route every per-day signal through the shared point-in-time primitive.
    records = replay_signals(symbol, hist, profile, since=d0)
    by_idx = {r["bar_index"]: r for r in records}

    # ── Simulate ──────────────────────────────────────────────────────────────
    trades = []
    entry = None
    i = first_idx
    while i <= n - 2:
        if not in_window[i]:
            i += 1
            continue

        rec = by_idx.get(i)
        res = rec["analysis"] if rec else None
        sig = res["signal"] if res else "HOLD"
        stop_price = res["suggested_stop"]["price"] if (res and res.get("suggested_stop")) else None

        if entry is None:
            if sig == "BUY":
                entry = {"idx": i + 1, "date": str(_d(i + 1)),
                         "price": opens[i + 1], "stop": stop_price}
            i += 1
        else:
            # Only start checking the stop the day AFTER entry execution.
            if i > entry["idx"] and entry["stop"] is not None and lows[i] <= entry["stop"]:
                xp = entry["stop"] if opens[i] > entry["stop"] else opens[i]
                trades.append(_mk_trade(entry, i, str(_d(i)), xp, "stop"))
                entry = None
                i += 1
            elif sig == "SELL":
                trades.append(_mk_trade(entry, i + 1, str(_d(i + 1)), opens[i + 1], "signal"))
                entry = None
                i += 1
            else:
                i += 1

    # Close any open position at the final in-window close.
    if entry is not None:
        last = n - 1
        trades.append(_mk_trade(entry, last, str(_d(last)), closes[last], "end-of-test"))

    # ── Equity curves ─────────────────────────────────────────────────────────
    entry_by_idx = {t["entry_idx"]: t for t in trades}
    exit_by_idx = {t["exit_idx"]: t for t in trades}
    strat = 1.0
    held = None
    bh_base = closes[first_idx]
    curve = []
    for i in range(first_idx, n):
        if not in_window[i]:
            continue
        if held is None and i in entry_by_idx:
            held = entry_by_idx[i]
            strat *= (closes[i] / held["entry_price"]) * (1 - cost)   # open→close, entry cost
        elif held is not None:
            if i == held["exit_idx"]:
                strat *= (held["exit_price"] / closes[i - 1]) * (1 - cost)  # close→exit, exit cost
                held = None
            else:
                strat *= closes[i] / closes[i - 1]
        curve.append({
            "date": str(_d(i)),
            "strategy": round(strat, 5),
            "buy_hold": round(closes[i] / bh_base, 5),
        })

    stats = _compute_stats(trades, curve, in_window, first_idx, n)
    return {
        "symbol": symbol,
        "start": str(d0) if d0 else None,
        "end": str(d1) if d1 else None,
        "cost_bps": cost_bps,
        "trades": trades,
        "equity_curve": curve,
        "stats": stats,
        "disclaimer": "Hypothetical results — past simulated performance is not "
                      "indicative of future returns. " + DISCLAIMER,
    }


def _mk_trade(entry, exit_idx, exit_date, exit_price, reason):
    ret = (exit_price / entry["price"] - 1) * 100 if entry["price"] else 0.0
    return {
        "entry_idx": entry["idx"], "entry_date": entry["date"],
        "entry_price": round(entry["price"], 2),
        "exit_idx": exit_idx, "exit_date": exit_date,
        "exit_price": round(exit_price, 2),
        "return_pct": round(ret, 2), "exit_reason": reason,
        "stop": round(entry["stop"], 2) if entry.get("stop") is not None else None,
    }


def _compute_stats(trades, curve, in_window, first_idx, n):
    total = len(trades)
    wins = [t for t in trades if t["return_pct"] > 0]
    losses = [t for t in trades if t["return_pct"] <= 0]
    gross_win = sum(t["return_pct"] for t in wins)
    gross_loss = abs(sum(t["return_pct"] for t in losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (None if gross_win == 0 else float("inf"))

    # Max drawdown of the strategy equity curve
    peak = 0.0
    max_dd = 0.0
    for pt in curve:
        eq = pt["strategy"]
        peak = max(peak, eq)
        if peak > 0:
            max_dd = max(max_dd, (peak - eq) / peak)

    days_in_market = sum(t["exit_idx"] - t["entry_idx"] for t in trades)
    window_days = sum(1 for i in range(first_idx, n) if in_window[i])
    final_strat = curve[-1]["strategy"] if curve else 1.0
    final_bh = curve[-1]["buy_hold"] if curve else 1.0

    return {
        "trades": total,
        "wins": len(wins),
        "losses": len(losses),
        "hit_rate": round(len(wins) / total * 100, 1) if total else 0.0,
        "profit_factor": (round(profit_factor, 2) if isinstance(profit_factor, float)
                          and profit_factor not in (float("inf"),) else profit_factor),
        "avg_win_pct": round(gross_win / len(wins), 2) if wins else 0.0,
        "avg_loss_pct": round(-gross_loss / len(losses), 2) if losses else 0.0,
        "max_drawdown_pct": round(max_dd * 100, 1),
        "exposure_pct": round(days_in_market / window_days * 100, 1) if window_days else 0.0,
        "total_return_pct": round((final_strat - 1) * 100, 1),
        "buy_hold_return_pct": round((final_bh - 1) * 100, 1),
    }
