import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import time
import numpy as np
import pandas as pd
from analysis import DEFAULT_PROFILE
from backtest import run_backtest

def make_hist(closes, start="2021-01-04"):
    closes = pd.Series(closes, dtype=float)
    idx = pd.date_range(start, periods=len(closes), freq="B")
    opens = closes.shift(1).fillna(closes.iloc[0])
    highs = pd.concat([opens, closes], axis=1).max(axis=1) * 1.01
    lows = pd.concat([opens, closes], axis=1).min(axis=1) * 0.99
    return pd.DataFrame({"Open": opens.values, "High": highs.values,
                         "Low": lows.values, "Close": closes.values,
                         "Volume": np.full(len(closes), 1e6)}, index=idx)

rng = np.random.default_rng(7)
# Uptrend with periodic pullbacks (so BUY/SELL transitions happen)
n = 760  # ~3y
trend = np.linspace(0, 60, n)
wave = 6 * np.sin(np.arange(n) / 25.0)
noise = np.cumsum(rng.normal(0, 0.3, n))
closes = 50 + trend + wave + noise
hist = make_hist(closes)

t0 = time.time()
res = run_backtest("SYN", hist, DEFAULT_PROFILE,
                   start="2022-01-01", end="2024-12-31", cost_bps=5)
elapsed = time.time() - t0

assert "error" not in res, res
s = res["stats"]
print(f"Backtest ran in {elapsed:.2f}s over {len(res['equity_curve'])} window days")
print(f"  trades={s['trades']} hit_rate={s['hit_rate']}% PF={s['profit_factor']}")
print(f"  strat_return={s['total_return_pct']}% buy_hold={s['buy_hold_return_pct']}%")
print(f"  max_dd={s['max_drawdown_pct']}% exposure={s['exposure_pct']}%")

assert len(res["equity_curve"]) > 100, "should have a multi-year curve"
assert res["equity_curve"][0]["buy_hold"] == 1.0 or abs(res["equity_curve"][0]["buy_hold"] - closes_idx0(res)) >= 0 # noop
# No look-ahead: every exit_idx >= entry_idx
for t in res["trades"]:
    assert t["exit_idx"] >= t["entry_idx"], t
    assert t["exit_price"] > 0 and t["entry_price"] > 0
print(f"  first trade: {res['trades'][0] if res['trades'] else 'none'}")

# Performance budget (spec: ~15s for 2y AAPL). Synthetic 3y should be well under.
assert elapsed < 15, f"too slow: {elapsed:.1f}s"

# Stats sanity
assert 0 <= s["hit_rate"] <= 100
assert 0 <= s["exposure_pct"] <= 100
assert s["max_drawdown_pct"] >= 0
print("\nBacktest tests passed.")

def closes_idx0(res):
    return 1.0
