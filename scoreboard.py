"""
Signal Scoreboard methodology (Phase 6) — pure, testable statistics.

Every credibility claim of the scoreboard rests on this file getting the math
right and reporting it honestly: point-in-time forward returns, hit rate (with a
relative-to-benchmark variant), and rank Information Coefficient. No lookahead is
possible here because we only ever join a snapshot to PRICES THAT CAME AFTER IT.
"""
from __future__ import annotations

import pandas as pd

HORIZONS = [1, 5, 10, 20]          # trading days ≈ 1d, 1w, 2w, 1m
LOW_SAMPLE = 20                    # n below this is "noisy"


# ─── Forward returns ──────────────────────────────────────────────────────────
def forward_returns(prices: list, idx: int, horizons=HORIZONS) -> dict:
    """
    `prices` is an ascending list of {"close": adj_close, "open": adj_open} for one
    symbol; `idx` is the snapshot's position in it. Returns, per horizon H:
      ret_cc  = close[t+H]/close[t] - 1                  (close-to-close)
      ret_no  = close[t+H]/open[t+1] - 1                 (next-open execution)
      matured = t+H <= last available bar
    A horizon that is not yet matured returns None for its values (never 0).
    """
    out = {}
    n = len(prices)
    c_t = prices[idx]["close"]
    open_t1 = prices[idx + 1]["open"] if idx + 1 < n else None
    for H in horizons:
        j = idx + H
        if j < n and c_t:
            c_th = prices[j]["close"]
            cc = c_th / c_t - 1
            no = (c_th / open_t1 - 1) if open_t1 else None
            out[H] = {"cc": round(cc, 6), "no": round(no, 6) if no is not None else None, "matured": True}
        else:
            out[H] = {"cc": None, "no": None, "matured": False}
    return out


def matured_through(prices_len: int, idx: int, horizons=HORIZONS) -> int:
    """Largest horizon fully matured for a snapshot at position idx."""
    best = 0
    for H in horizons:
        if idx + H < prices_len:
            best = H
    return best


# ─── Hit rate ─────────────────────────────────────────────────────────────────
def hit_absolute(signal: str, ret) -> int | None:
    """1 = correct direction vs 0, 0 = wrong, None = excluded (HOLD or pending)."""
    if ret is None:
        return None
    if signal == "BUY":
        return 1 if ret > 0 else 0
    if signal == "SELL":
        return 1 if ret < 0 else 0
    return None                      # HOLD makes no directional claim


def hit_relative(signal: str, ret, bench_ret) -> int | None:
    """Hit measured against the benchmark over the identical window."""
    if ret is None or bench_ret is None:
        return None
    rel = ret - bench_ret
    if signal == "BUY":
        return 1 if rel > 0 else 0
    if signal == "SELL":
        return 1 if rel < 0 else 0
    return None


def hit_rate(values: list) -> dict:
    """Aggregate a list of hit_absolute/relative results (1/0/None)."""
    vals = [v for v in values if v is not None]
    n = len(vals)
    return {
        "rate": round(sum(vals) / n * 100, 1) if n else None,
        "n": n,
        "low_sample": n < LOW_SAMPLE,
    }


# ─── Information Coefficient (Spearman rank) ──────────────────────────────────
def rank_ic(scores: list, rets: list) -> dict:
    """
    Spearman rank correlation between signed scores and realized returns.
    Returns {ic, n, low_sample, interpretation}. None ic when undefined.
    """
    df = pd.DataFrame({"s": scores, "r": rets}).dropna()
    n = len(df)
    if n < 2 or df["s"].nunique() < 2 or df["r"].nunique() < 2:
        return {"ic": None, "n": n, "low_sample": n < LOW_SAMPLE, "interpretation": "insufficient data"}
    # Spearman = Pearson on average ranks (avoids a scipy dependency)
    ic = df["s"].rank().corr(df["r"].rank())
    ic = None if pd.isna(ic) else round(float(ic), 4)
    return {"ic": ic, "n": n, "low_sample": n < LOW_SAMPLE, "interpretation": interpret_ic(ic)}


def interpret_ic(ic) -> str:
    if ic is None:
        return "insufficient data"
    if ic > 0.05:
        return "meaningful"
    if ic < -0.05:
        return "contrarian"
    return "noise"


def cross_sectional_ic(by_date: dict) -> dict:
    """
    Average of per-date cross-sectional rank ICs. `by_date` maps date -> list of
    (signed_score, ret) pairs. Only dates with >= 5 names count. Returns the mean
    IC, the number of qualifying dates, and the average breadth.
    """
    per_date, breadth = [], []
    for _, pairs in by_date.items():
        scores = [p[0] for p in pairs]
        rets = [p[1] for p in pairs]
        valid = [(s, r) for s, r in zip(scores, rets) if s is not None and r is not None]
        if len(valid) < 5:
            continue
        res = rank_ic([v[0] for v in valid], [v[1] for v in valid])
        if res["ic"] is not None:
            per_date.append(res["ic"])
            breadth.append(len(valid))
    if not per_date:
        return {"ic": None, "n_dates": 0, "avg_breadth": 0, "low_sample": True,
                "interpretation": "insufficient data"}
    mean_ic = round(sum(per_date) / len(per_date), 4)
    return {
        "ic": mean_ic, "n_dates": len(per_date),
        "avg_breadth": round(sum(breadth) / len(breadth), 1),
        "low_sample": len(per_date) < LOW_SAMPLE,
        "interpretation": interpret_ic(mean_ic),
    }


def avg(values: list):
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 6) if vals else None
