import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
import pandas as pd
import app as appmod

client = appmod.app.test_client()

# /api/presets
r = client.get("/api/presets")
assert r.status_code == 200
presets = r.get_json()
assert len(presets) == 5 and all("believes" in p and "weights" in p for p in presets)
print(f"PASS: /api/presets returns {len(presets)} presets with methodology notes")

# /api/profiles/from-preset/<key>
r = client.post("/api/profiles/from-preset/trend_following", json={"is_active": True})
assert r.status_code == 201, r.get_data(as_text=True)
prof = r.get_json()
assert prof["preset_key"] == "trend_following" and prof["is_active"]
assert abs(prof["ma_weight"] - 0.55) < 1e-6
print(f"PASS: from-preset created profile id={prof['id']} preset={prof['preset_key']}")

# unknown preset -> 404
r = client.post("/api/profiles/from-preset/nope", json={})
assert r.status_code == 404
print("PASS: unknown preset -> 404")

# preset_key persisted in /api/profiles
r = client.get("/api/profiles")
assert any(p.get("preset_key") == "trend_following" for p in r.get_json())
print("PASS: preset_key persisted and serialized")

# /api/backtest with monkeypatched fetch_history (offline)
def make_hist(closes):
    closes = pd.Series(closes, dtype=float)
    idx = pd.date_range("2021-06-01", periods=len(closes), freq="B")
    opens = closes.shift(1).fillna(closes.iloc[0])
    highs = pd.concat([opens, closes], axis=1).max(axis=1) * 1.01
    lows = pd.concat([opens, closes], axis=1).min(axis=1) * 0.99
    return pd.DataFrame({"Open": opens.values, "High": highs.values, "Low": lows.values,
                         "Close": closes.values, "Volume": np.full(len(closes), 1e6)}, index=idx)

rng = np.random.default_rng(3)
closes = 50 + np.linspace(0, 40, 760) + 5*np.sin(np.arange(760)/22) + np.cumsum(rng.normal(0, 0.3, 760))
appmod.fetch_history = lambda symbol, period="3y": make_hist(closes)

r = client.post("/api/backtest", json={"symbol": "TEST", "preset_key": "trend_following",
                                       "start": "2022-06-01", "end": "2023-12-31", "cost_bps": 5})
assert r.status_code == 200, r.get_data(as_text=True)
bt = r.get_json()
assert "equity_curve" in bt and "stats" in bt and "trades" in bt
assert "hypothetical" in bt["disclaimer"].lower()
s = bt["stats"]
assert set(["hit_rate", "profit_factor", "max_drawdown_pct", "exposure_pct",
            "total_return_pct", "buy_hold_return_pct"]).issubset(s.keys())
print(f"PASS: /api/backtest -> {s['trades']} trades, "
      f"strat {s['total_return_pct']}% vs B&H {s['buy_hold_return_pct']}%, "
      f"DD {s['max_drawdown_pct']}%, curve {len(bt['equity_curve'])} pts")

# missing symbol -> 400
r = client.post("/api/backtest", json={})
assert r.status_code == 400
print("PASS: backtest missing symbol -> 400")

# /api/suggestions includes new regime field (offline single ticker)
with appmod.app.app_context():
    appmod.WatchlistItem.query.delete()
    appmod.db.session.add(appmod.WatchlistItem(symbol="TEST", name="Test"))
    appmod.db.session.commit()
r = client.get("/api/suggestions")
sg = r.get_json()
assert isinstance(sg, list) and sg and "regime" in sg[0] and "confirmations" in sg[0]
assert "suggested_stop" in sg[0] and "families" in sg[0]
print(f"PASS: /api/suggestions enriched — regime='{sg[0]['regime']['label']}' "
      f"signal={sg[0]['signal']} conf={sg[0]['confidence']}")

# cleanup
with appmod.app.app_context():
    appmod.AnalysisProfile.query.delete()
    appmod.WatchlistItem.query.delete()
    appmod.db.session.commit()

print("\nAll route tests passed.")
