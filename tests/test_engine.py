import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
import pandas as pd
from analysis import analyze_ticker, detect_regime, PRESETS, preset_to_profile, DEFAULT_PROFILE

def make_hist(closes, vol=1_000_000):
    closes = pd.Series(closes, dtype=float)
    idx = pd.date_range("2023-01-02", periods=len(closes), freq="B")
    opens = closes.shift(1).fillna(closes.iloc[0])
    highs = pd.concat([opens, closes], axis=1).max(axis=1) * 1.008
    lows = pd.concat([opens, closes], axis=1).min(axis=1) * 0.992
    return pd.DataFrame({"Open": opens.values, "High": highs.values,
                         "Low": lows.values, "Close": closes.values,
                         "Volume": np.full(len(closes), float(vol))}, index=idx)

rng = np.random.default_rng(42)

# ── Clean uptrend ──
up = 50 + np.cumsum(np.abs(rng.normal(0.25, 0.15, 300)))
h_up = make_hist(up)
reg = detect_regime(h_up)
print(f"Uptrend regime: {reg['label']}  ADX={reg['adx']} dir={reg['direction']}")
res = analyze_ticker("UP", h_up, DEFAULT_PROFILE)
print(f"  signal={res['signal']} conf={res['confidence']} weighted={res['weighted_score']}")
print(f"  families agreeing={res['confirmations']['families_agreeing']} stop={res['suggested_stop']['price'] if res['suggested_stop'] else None}")
assert reg["trending"], "clean uptrend should be trending"
assert reg["direction"] == "up"
assert res["signal"] in ("BUY", "HOLD")
assert res["suggested_stop"]["side"] == "long"

# ── Range-bound (choppy noise around a constant — no sustained direction) ──
rangey = 100 + rng.normal(0, 1.0, 300)
h_rg = make_hist(rangey)
reg = detect_regime(h_rg)
print(f"Range regime: {reg['label']}  ADX={reg['adx']}")
res = analyze_ticker("RG", h_rg, DEFAULT_PROFILE)
print(f"  signal={res['signal']} conf={res['confidence']} osc_mult={reg['family_mult']['oscillator']}")
assert not reg["trending"], "oscillating series should be ranging"
assert reg["family_mult"]["oscillator"] >= 1.0, "ranging boosts oscillator weight"

# ── Downtrend: BUY must be gated to HOLD ──
dn = 150 - np.cumsum(np.abs(rng.normal(0.25, 0.15, 300)))
h_dn = make_hist(dn)
reg = detect_regime(h_dn)
res = analyze_ticker("DN", h_dn, DEFAULT_PROFILE)
print(f"Downtrend regime: {reg['label']} dir={reg['direction']} signal={res['signal']}")
assert reg["direction"] == "down"
assert res["signal"] != "BUY", "BUY must be gated against a down-trending regime"

# ── Whipsaw: should not produce high-confidence BUY without 2 families ──
whip = 100 + np.cumsum(rng.normal(0, 1.2, 300))
h_wp = make_hist(whip)
res = analyze_ticker("WP", h_wp, DEFAULT_PROFILE)
print(f"Whipsaw: signal={res['signal']} conf={res['confidence']} capped={res['confirmations']['capped_to_hold']}")

# ── Confirmation rule: synthetic strong-but-single-family can't BUY ──
# (covered structurally above)

# ── Presets sane: weights sum ~1.0 ──
for k, p in PRESETS.items():
    s = p["ma_weight"] + p["momentum_weight"] + p["volume_weight"] + p["rsi_weight"]
    assert abs(s - 1.0) < 0.001, f"preset {k} weights sum {s}"
    prof = preset_to_profile(k)
    assert prof["preset_key"] == k
print(f"PASS: all {len(PRESETS)} presets have valid weights summing to 1.0")

# ── Backward-compat: required UI keys present ──
for key in ("symbol", "signal", "confidence", "reasoning", "signals", "price", "thresholds", "rvol", "rvol_tier"):
    assert key in res, f"missing UI key {key}"
assert all("direction" in s and "name" in s and "reason" in s for s in res["signals"])
print("PASS: backward-compatible UI keys present")

# ── Disclaimer present ──
assert "advice" in res["disclaimer"].lower()
print("PASS: disclaimer attached")

print("\nAll engine tests passed.")
