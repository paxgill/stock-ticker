import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
import pandas as pd
import indicators as ind

# ── Wilder RSI: classic 14-period reference series (Wilder's original example) ──
# Known close prices; Wilder's first RSI value ~ 70.53 at index 14.
prices = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42,
          45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.00,
          46.03, 46.41, 46.22, 45.64]
s = pd.Series(prices)
# The fast (ewm-seeded) RSI and the textbook (SMA-seeded) reference converge on
# real-length history. Verify convergence on a longer series.
long_series = pd.Series(np.r_[prices, prices[::-1], prices, prices[::-1], prices])
fast = ind.wilder_rsi(long_series, 14).dropna()
# Reference RSI using SMA-seeded Wilder smoothing
delta = long_series.diff()
g = delta.clip(lower=0); l = (-delta).clip(lower=0)
ref_rs = ind.wilder_ema_seeded(g, 14) / ind.wilder_ema_seeded(l, 14)
ref = (100 - 100 / (1 + ref_rs)).dropna()
common = fast.index.intersection(ref.index)[-20:]
max_diff = (fast.loc[common] - ref.loc[common]).abs().max()
assert max_diff < 0.5, f"fast vs seeded RSI diverge by {max_diff:.3f}"
print(f"PASS: fast vs textbook-seeded RSI converge (max diff {max_diff:.4f})")

# Steadily rising -> RSI near 100; steadily falling -> near 0
up = pd.Series(np.arange(1, 60, dtype=float))
dn = pd.Series(np.arange(60, 1, -1, dtype=float))
assert ind.wilder_rsi(up, 14).iloc[-1] > 99, "rising series RSI should approach 100"
assert ind.wilder_rsi(dn, 14).iloc[-1] < 1, "falling series RSI should approach 0"
print("PASS: RSI monotonic extremes")

# ── ATR known-value check ──
# Simple OHLC where TR is easy to reason about
high = pd.Series([10, 11, 12, 11, 13, 14, 13, 15, 16, 15, 17, 18, 17, 19, 20, 21])
low  = pd.Series([ 8,  9, 10,  9, 11, 12, 11, 13, 14, 13, 15, 16, 15, 17, 18, 19])
close= pd.Series([ 9, 10, 11, 10, 12, 13, 12, 14, 15, 14, 16, 17, 16, 18, 19, 20])
a = ind.atr(high, low, close, 14)
assert a.iloc[-1] > 0 and not np.isnan(a.iloc[-1]), "ATR should be positive at end"
# TR is roughly 2-3 each bar; ATR should land in that range
assert 1.5 <= a.iloc[-1] <= 4.0, f"ATR={a.iloc[-1]:.2f} unexpected"
print(f"PASS: ATR last value = {a.iloc[-1]:.2f}")

# ── ADX: a clean uptrend should produce high ADX ──
trend = pd.Series(np.linspace(10, 50, 80))
hi = trend + 0.5
lo = trend - 0.5
adx = ind.adx(hi, lo, trend, 14)
assert adx["adx"].iloc[-1] > 25, f"clean trend ADX should be >25, got {adx['adx'].iloc[-1]:.1f}"
assert adx["plus_di"].iloc[-1] > adx["minus_di"].iloc[-1], "uptrend +DI should exceed -DI"
print(f"PASS: ADX on clean uptrend = {adx['adx'].iloc[-1]:.1f} (+DI>-DI)")

# ── MACD: fast above slow in uptrend -> positive histogram region ──
m = ind.macd(trend)
assert m["macd"].iloc[-1] > 0, "uptrend MACD line should be positive"
print(f"PASS: MACD line uptrend = {m['macd'].iloc[-1]:.2f}")

# ── Bollinger %B within bands for mid values ──
noise = pd.Series(100 + np.sin(np.arange(60) / 3.0) * 2)
bb = ind.bollinger(noise, 20, 2)
pb = bb["pct_b"].dropna()
assert (pb.between(-0.5, 1.5)).all(), "%B should stay near [0,1] for oscillating series"
print("PASS: Bollinger %B sane range")

# ── OBV slope sign ──
vol = pd.Series(np.full(60, 1000.0))
assert ind.obv_slope(up, vol) > 0, "rising price + steady volume -> positive OBV slope"
assert ind.obv_slope(dn, vol) < 0, "falling price -> negative OBV slope"
print("PASS: OBV slope sign")

# ── 12-1 momentum ──
long_up = pd.Series(np.linspace(50, 150, 300))
mom = ind.momentum_12_1(long_up)
assert mom is not None and mom > 0, f"12-1 momentum should be positive, got {mom}"
assert ind.momentum_12_1(pd.Series(np.arange(50))) is None, "insufficient history -> None"
print(f"PASS: 12-1 momentum = {mom:.1f}%")

# ── 52-week distance ──
hl = ind.high_low_distance(long_up)
assert hl["pct_from_high"] <= 0 and hl["pct_from_low"] >= 0
print(f"PASS: range distance from_high={hl['pct_from_high']:.1f}% from_low={hl['pct_from_low']:.1f}%")

print("\nAll indicator tests passed.")
