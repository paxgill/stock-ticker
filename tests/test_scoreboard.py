"""Phase 6: methodology correctness — lookahead guard, IC, hit rate, forward returns,
time-machine equivalence, availability gating, split disclosure, idempotent backfill."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DISABLE_ALERT_ENGINE"] = "1"

import numpy as np
import pandas as pd

from analysis import replay_signals, DEFAULT_PROFILE
import scoreboard as sb


def make_df(n, seed=1, with_raw=False, split_at=None, split=1.0):
    rng = np.random.default_rng(seed)
    closes = 50 + np.cumsum(np.abs(rng.normal(0.15, 0.6, n)))   # trending-ish
    idx = pd.date_range("2022-01-03", periods=n, freq="B")
    opens = np.r_[closes[0], closes[:-1]]
    highs = np.maximum(opens, closes) * 1.01
    lows = np.minimum(opens, closes) * 0.99
    data = {"Open": opens, "High": highs, "Low": lows, "Close": closes,
            "Volume": np.full(n, 1e6)}
    if with_raw:
        raw = closes.copy()
        if split_at is not None:           # raw was `split`× higher before the split
            raw[:split_at] = closes[:split_at] * split
        data["CloseRaw"] = raw
    return pd.DataFrame(data, index=idx)


# ── 1. LOOKAHEAD GUARD (the critical test) ───────────────────────────────────
full = make_df(300, seed=7)
t_idx = 250
t_date = full.index[t_idx].date()
rec_full = replay_signals("X", full, DEFAULT_PROFILE, as_of=t_date)
rec_trunc = replay_signals("X", full.iloc[:t_idx + 1], DEFAULT_PROFILE, as_of=t_date)
assert rec_full and rec_trunc, "replay produced no record"
a_full, a_trunc = rec_full[0]["analysis"], rec_trunc[0]["analysis"]
assert a_full is not None
# Future bars must NOT change the reconstruction at t — byte-identical.
assert a_full == a_trunc, "LOOKAHEAD BIAS: reconstruction at t changed when future bars were present!"
assert rec_full[0]["signed_score"] == rec_trunc[0]["signed_score"]
print(f"PASS: LOOKAHEAD GUARD — reconstruction at {t_date} is identical with/without future bars")

# ── 2. Forward-return math (hand-computed) ───────────────────────────────────
prices = [{"close": 100, "open": 100}, {"close": 101, "open": 100.5},
          {"close": 103, "open": 102}, {"close": 99, "open": 100}, {"close": 110, "open": 105},
          {"close": 108, "open": 109}]
fr = sb.forward_returns(prices, 0, horizons=[1, 5])
assert abs(fr[1]["cc"] - (101/100 - 1)) < 1e-5
assert abs(fr[1]["no"] - (101/100.5 - 1)) < 1e-5     # next-open = close[1]/open[1]
assert abs(fr[5]["cc"] - (108/100 - 1)) < 1e-5 and fr[5]["matured"]
fr_edge = sb.forward_returns(prices, 5, horizons=[1])
assert fr_edge[1]["matured"] is False and fr_edge[1]["cc"] is None   # pending, never 0
print("PASS: forward returns cc/no + matured/pending at series edge")

# ── 3. Hit rate (abs, rel, HOLD excluded, SELL inversion) ────────────────────
assert sb.hit_absolute("BUY", 0.02) == 1 and sb.hit_absolute("BUY", -0.02) == 0
assert sb.hit_absolute("SELL", -0.02) == 1 and sb.hit_absolute("SELL", 0.02) == 0
assert sb.hit_absolute("HOLD", 0.05) is None
assert sb.hit_relative("BUY", 0.01, 0.03) == 0    # up but lagged SPY -> relative miss
assert sb.hit_relative("BUY", 0.05, 0.03) == 1
hr = sb.hit_rate([1, 1, 0, None, 1])
assert hr["rate"] == 75.0 and hr["n"] == 4
print("PASS: hit rate — absolute, relative-to-bench, HOLD excluded, SELL inverted")

# ── 4. Information Coefficient (perfect / reversed / random) ─────────────────
scores = list(range(30))
perfect = sb.rank_ic(scores, [x * 2 for x in scores])
reversed_ic = sb.rank_ic(scores, [-x for x in scores])
rng = np.random.default_rng(3)
rnd = sb.rank_ic(scores, list(rng.normal(0, 1, 30)))
assert perfect["ic"] > 0.99 and perfect["interpretation"] == "meaningful"
assert reversed_ic["ic"] < -0.99 and reversed_ic["interpretation"] == "contrarian"
assert abs(rnd["ic"]) < 0.45
print(f"PASS: Rank IC — perfect={perfect['ic']}, reversed={reversed_ic['ic']}, random={rnd['ic']}")

# cross-sectional
xs = sb.cross_sectional_ic({
    "d1": [(1, 0.1), (2, 0.2), (3, 0.3), (4, 0.4), (5, 0.5)],
    "d2": [(5, 0.5), (4, 0.4), (3, 0.3), (2, 0.2), (1, 0.1)],
})
assert xs["ic"] > 0.99 and xs["n_dates"] == 2
xs_thin = sb.cross_sectional_ic({"d1": [(1, 0.1), (2, 0.2)]})   # <5 names -> skipped
assert xs_thin["ic"] is None
print(f"PASS: cross-sectional IC = {xs['ic']} over {xs['n_dates']} dates; thin date skipped")

# ── DB-backed: backfill, equivalence, availability, split, routes ────────────
import app as appmod
from datetime import date, timedelta
from analysis import replay_signals as _replay

def make_recent_df(n, seed=11, with_raw=False, split_at=None, split=1.0):
    rng = np.random.default_rng(seed)
    closes = 50 + np.cumsum(np.abs(rng.normal(0.12, 0.7, n)))
    idx = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=n)
    opens = np.r_[closes[0], closes[:-1]]
    highs = np.maximum(opens, closes) * 1.01
    lows = np.minimum(opens, closes) * 0.99
    data = {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": np.full(n, 1e6)}
    if with_raw:
        raw = closes.copy()
        if split_at is not None:
            raw[:split_at] = closes[:split_at] * split
        data["CloseRaw"] = raw
    return pd.DataFrame(data, index=idx)

SYN = {"AAA": make_recent_df(420, seed=11, with_raw=True, split_at=405, split=4.0),
       "BBB": make_recent_df(420, seed=12, with_raw=True),
       "SPY": make_recent_df(420, seed=99, with_raw=True)}
appmod._fetch_history_both = lambda symbol, period="2y": SYN.get(symbol.upper())

client = appmod.app.test_client()
with appmod.app.app_context():
    appmod.SignalSnapshot.query.delete(); appmod.PricesDaily.query.delete()
    appmod.WatchlistItem.query.delete(); appmod.AnalysisProfile.query.delete()
    appmod.db.session.add(appmod.WatchlistItem(symbol="AAA", name="AAA"))
    appmod.db.session.add(appmod.WatchlistItem(symbol="BBB", name="BBB"))
    appmod.db.session.commit()

# Idempotent backfill
r1 = client.post("/api/scoreboard/backfill", json={"lookback_days": 180})
assert r1.status_code == 200, r1.get_data(as_text=True)
created1 = r1.get_json()["created"]
assert created1 > 0
with appmod.app.app_context():
    count1 = appmod.SignalSnapshot.query.count()
r2 = client.post("/api/scoreboard/backfill", json={"lookback_days": 180})
with appmod.app.app_context():
    count2 = appmod.SignalSnapshot.query.count()
assert count1 == count2, f"backfill not idempotent: {count1} -> {count2}"
print(f"PASS: backfill created {created1} snapshots; re-run idempotent ({count1} == {count2})")

# Scoreboard shape
r = client.get("/api/scoreboard?horizon=20&benchmark=spy")
j = r.get_json()
assert r.status_code == 200 and not j["empty"]
assert "cross_sectional_ic" in j and "per_ticker" in j and "by_regime" in j and "by_type" in j
assert all("n" in p for p in j["per_ticker"])           # every metric ships its n
print(f"PASS: /api/scoreboard shape — {len(j['per_ticker'])} tickers, xs-IC keys {list(j['cross_sectional_ic'].keys())}")

# Per-symbol timeline
r = client.get("/api/scoreboard/AAA")
assert r.status_code == 200 and len(r.get_json()["timeline"]) > 0
print(f"PASS: /api/scoreboard/AAA timeline ({len(r.get_json()['timeline'])} rows)")

# Time machine — valid date, with outcome + availability + equivalence to snapshot
as_of = (date.today() - timedelta(days=40)).isoformat()
r = client.get(f"/api/timemachine/AAA?as_of={as_of}")
tm = r.get_json()
assert r.status_code == 200 and tm["analysis"] is not None
assert "outcome" in tm and "availability" in tm and "price_disclosure" in tm and "context" in tm
# Equivalence: timemachine signal equals the point-in-time snapshot the scoreboard uses
recs = _replay("AAA", SYN["AAA"], appmod.DEFAULT_PROFILE, as_of=date.fromisoformat(tm["as_of"]))
assert recs[0]["analysis"]["signal"] == tm["analysis"]["signal"]
assert recs[0]["analysis"]["confidence"] == tm["analysis"]["confidence"]
assert recs[0]["regime"] == tm["analysis"]["regime"]["label"]
print(f"PASS: time-machine equals point-in-time snapshot (signal={tm['analysis']['signal']}, regime={tm['analysis']['regime']['label']})")

# Split disclosure — AAA had a 4:1 raw/adj divergence before its split date
assert tm["price_disclosure"]["corporate_action"] is True
assert abs(tm["price_disclosure"]["adjusted"] - tm["price_disclosure"]["as_traded"]) > 1e-6
print("PASS: split/dividend disclosure surfaced; adjusted != as-traded")

# Outcome shows both cc and next-open and a verdict
o = tm["outcome"]
assert 20 in {int(k) for k in o["horizons"].keys()} or "20" in o["horizons"]
assert "verdict" in o and len(o["verdict"]) > 10
print(f"PASS: outcome panel — verdict: {o['verdict'][:60]}...")

# Too-early as_of -> 200 with availability flagging MA200 unavailable, not fabricated
early = SYN["AAA"].index[20].date().isoformat()    # only ~20 prior bars
r = client.get(f"/api/timemachine/AAA?as_of={early}")
te = r.get_json()
assert r.status_code == 200
unavail = [u["signal"] for u in te["availability"]["unavailable"]]
assert te["availability"]["had_ma200"] is False
assert any("MA200" in u for u in unavail)
print(f"PASS: too-early date gated — unavailable: {unavail}")

# Context dates present (anti-cherry-pick)
assert isinstance(tm["context"], list)
print(f"PASS: context dates present ({len(tm['context'])} other as-of outcomes)")

# Unknown symbol -> 404
appmod._fetch_history_both = lambda symbol, period="2y": SYN.get(symbol.upper())
r = client.get("/api/timemachine/ZZZNOPE?as_of=2024-01-01")
assert r.status_code == 404
print("PASS: unknown symbol -> 404")

# Empty scoreboard path (no snapshots for an unused profile/empty)
with appmod.app.app_context():
    appmod.SignalSnapshot.query.delete(); appmod.db.session.commit()
r = client.get("/api/scoreboard")
assert r.status_code == 200 and r.get_json().get("empty") is True
print("PASS: empty scoreboard returns empty:true")

# cleanup
with appmod.app.app_context():
    appmod.SignalSnapshot.query.delete(); appmod.PricesDaily.query.delete()
    appmod.WatchlistItem.query.delete(); appmod.db.session.commit()

print("\nAll scoreboard + time-machine tests passed.")
