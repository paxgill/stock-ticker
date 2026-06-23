"""
Signal Scoreboard + Time Machine orchestration (Phase 6).

Bridges the shared point-in-time engine (analysis.replay_signals), the snapshot
store (models.SignalSnapshot / PricesDaily) and the pure statistics (scoreboard).
yfinance lives in app.py; callers pass an adjusted-OHLCV DataFrame (with a
'CloseRaw' column for as-traded display) into the functions here.
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from models import db, SignalSnapshot, PricesDaily
from analysis import replay_signals, DISCLAIMER
import scoreboard as sb

_RET_FIELDS = {1: ("ret_1d", "ret_1d_no"), 5: ("ret_5d", "ret_5d_no"),
               10: ("ret_10d", "ret_10d_no"), 20: ("ret_20d", "ret_20d_no")}


def profile_key(profile_id, preset_key) -> str:
    """Coalesced uniqueness key: preset wins, else profile id, else 'active'."""
    return preset_key or (str(profile_id) if profile_id else "active")


# ─── Prices cache ─────────────────────────────────────────────────────────────
def upsert_prices(symbol: str, df: pd.DataFrame):
    existing = {p.d: p for p in PricesDaily.query.filter_by(symbol=symbol).all()}
    raw = "CloseRaw" in df.columns
    for idx, row in df.iterrows():
        d = idx.date() if hasattr(idx, "date") else idx
        vals = dict(open_adj=float(row["Open"]), high_adj=float(row["High"]),
                    low_adj=float(row["Low"]), close_adj=float(row["Close"]),
                    close_raw=float(row["CloseRaw"]) if raw else None)
        rec = existing.get(d)
        if rec:
            for k, v in vals.items():
                setattr(rec, k, v)
        else:
            db.session.add(PricesDaily(symbol=symbol, d=d, **vals))
    db.session.commit()


def prices_list(symbol: str) -> list:
    rows = PricesDaily.query.filter_by(symbol=symbol).order_by(PricesDaily.d).all()
    return [{"date": r.d, "close": r.close_adj, "open": r.open_adj, "close_raw": r.close_raw} for r in rows]


# ─── Backfill ─────────────────────────────────────────────────────────────────
def backfill_symbol(symbol, df_adj, profile, profile_id, preset_key,
                    lookback_days, source="backfill") -> int:
    """Replay point-in-time signals over the lookback window and upsert snapshots."""
    upsert_prices(symbol, df_adj)
    since = date.today() - timedelta(days=lookback_days)
    records = replay_signals(symbol, df_adj, profile, since=since)
    pkey = profile_key(profile_id, preset_key)
    prices = prices_list(symbol)
    date_to_idx = {p["date"]: i for i, p in enumerate(prices)}
    existing = {s.snapshot_date: s for s in
                SignalSnapshot.query.filter_by(symbol=symbol, profile_key=pkey).all()}
    created = 0
    for rec in records:
        if rec["analysis"] is None:
            continue
        sd = date.fromisoformat(rec["snapshot_date"])
        idx = date_to_idx.get(sd)
        fr = sb.forward_returns(prices, idx) if idx is not None else {}
        mt = sb.matured_through(len(prices), idx) if idx is not None else 0
        snap = existing.get(sd)
        if snap and snap.source == "live":
            _apply_returns(snap, fr); snap.matured_through = mt   # keep live, refresh outcomes
            continue
        fields = dict(
            signal=rec["signal"], confidence=rec["confidence"], signed_score=rec["signed_score"],
            regime=rec["regime"], price_close_adj=rec["price_close_adj"],
            price_close_raw=rec["price_close_raw"], price_next_open_adj=rec["price_next_open_adj"],
            had_ma200=rec["had_ma200"], profile_id=profile_id, preset_key=preset_key,
            profile_key=pkey, source=source, matured_through=mt)
        if snap:
            for k, v in fields.items():
                setattr(snap, k, v)
            _apply_returns(snap, fr)
        else:
            snap = SignalSnapshot(symbol=symbol, snapshot_date=sd, **fields)
            _apply_returns(snap, fr)
            db.session.add(snap)
            created += 1
    db.session.commit()
    return created


def _apply_returns(snap, fr: dict):
    for H, (cc_field, no_field) in _RET_FIELDS.items():
        h = fr.get(H)
        if h and h.get("matured"):
            if h.get("cc") is not None:
                setattr(snap, cc_field, h["cc"])
            if h.get("no") is not None:
                setattr(snap, no_field, h["no"])


def mature_symbol(symbol: str):
    """Fill any newly-matured forward returns for a symbol's snapshots (lazy on read)."""
    prices = prices_list(symbol)
    if not prices:
        return
    date_to_idx = {p["date"]: i for i, p in enumerate(prices)}
    snaps = SignalSnapshot.query.filter(SignalSnapshot.symbol == symbol,
                                        SignalSnapshot.matured_through < 20).all()
    changed = False
    for snap in snaps:
        idx = date_to_idx.get(snap.snapshot_date)
        if idx is None:
            continue
        fr = sb.forward_returns(prices, idx)
        mt = sb.matured_through(len(prices), idx)
        if mt != snap.matured_through:
            _apply_returns(snap, fr)
            snap.matured_through = mt
            changed = True
    if changed:
        db.session.commit()


# ─── Scoreboard roll-up ───────────────────────────────────────────────────────
def _ret_field(snap, H, next_open=False):
    cc, no = _RET_FIELDS[H]
    return getattr(snap, no if next_open else cc)


def build_scoreboard(symbols, profile_key_val, horizon=20, benchmark_rets=None,
                     source="all") -> dict:
    """
    Full roll-up across `symbols` for one profile. `benchmark_rets` maps
    date -> {H: ret_cc} for SPY over the identical windows (None = no benchmark).
    """
    per_ticker = []
    xs_by_date = {}                 # date -> [(signed_score, ret_cc@horizon)]
    by_regime, by_type = {}, {}
    all_hit_abs, all_hit_rel = [], []

    for sym in symbols:
        mature_symbol(sym)
        q = SignalSnapshot.query.filter_by(symbol=sym, profile_key=profile_key_val)
        if source == "live":
            q = q.filter_by(source="live")
        snaps = q.order_by(SignalSnapshot.snapshot_date).all()
        if not snaps:
            per_ticker.append({"symbol": sym, "n": 0, "no_data": True})
            continue

        scores, rets_cc, hits_abs, hits_rel, hold_drift, spark = [], [], [], [], [], []
        for s in snaps:
            r_cc = _ret_field(s, horizon)
            spark.append({"date": s.snapshot_date.isoformat(), "price": s.price_close_adj,
                          "signal": s.signal, "score": s.signed_score})
            if r_cc is None:
                continue
            scores.append(s.signed_score)
            rets_cc.append(r_cc)
            bench = (benchmark_rets or {}).get(s.snapshot_date, {}).get(horizon)
            ha = sb.hit_absolute(s.signal, r_cc)
            hr = sb.hit_relative(s.signal, r_cc, bench)
            if ha is not None:
                hits_abs.append(ha); all_hit_abs.append(ha)
            if hr is not None:
                hits_rel.append(hr); all_hit_rel.append(hr)
            if s.signal == "HOLD":
                hold_drift.append(r_cc)
            # cross-sectional + breakdowns
            xs_by_date.setdefault(s.snapshot_date, []).append((s.signed_score, r_cc))
            by_regime.setdefault(s.regime or "Undetermined", []).append((s.signal, r_cc))
            by_type.setdefault(s.signal, []).append(r_cc)

        ic = sb.rank_ic(scores, rets_cc)
        fwd = {}
        for H in sb.HORIZONS:
            vals_cc = [_ret_field(s, H) for s in snaps]
            vals_no = [_ret_field(s, H, next_open=True) for s in snaps]
            fwd[H] = {"cc": sb.avg(vals_cc), "no": sb.avg(vals_no),
                      "n": len([v for v in vals_cc if v is not None])}
        per_ticker.append({
            "symbol": sym, "n": len(rets_cc), "low_sample": len(rets_cc) < sb.LOW_SAMPLE,
            "latest": {"signal": snaps[-1].signal, "confidence": snaps[-1].confidence,
                       "regime": snaps[-1].regime, "date": snaps[-1].snapshot_date.isoformat(),
                       "source": snaps[-1].source},
            "rank_ic": ic, "hit_abs": sb.hit_rate(hits_abs), "hit_rel": sb.hit_rate(hits_rel),
            "fwd": fwd, "hold_drift": sb.avg(hold_drift), "sparkline": spark,
        })

    xs_ic = {H: sb.cross_sectional_ic({d: [(p[0], p[1]) for p in pairs]
                                       for d, pairs in _xs_for_horizon(symbols, profile_key_val, H, source).items()})
             for H in sb.HORIZONS}

    regime_tbl = {reg: {"hit": sb.hit_rate([sb.hit_absolute(sig, r) for sig, r in rows]),
                        "avg_ret": sb.avg([r for _, r in rows])}
                  for reg, rows in by_regime.items()}
    type_tbl = {t: {"avg_ret": sb.avg(rows), "n": len(rows)} for t, rows in by_type.items()}

    ranked = [p for p in per_ticker if p.get("rank_ic", {}).get("ic") is not None]
    ranked.sort(key=lambda p: p["rank_ic"]["ic"], reverse=True)

    return {
        "horizon": horizon,
        "cross_sectional_ic": xs_ic,
        "per_ticker": per_ticker,
        "by_regime": regime_tbl,
        "by_type": type_tbl,
        "overall_hit_abs": sb.hit_rate(all_hit_abs),
        "overall_hit_rel": sb.hit_rate(all_hit_rel),
        "best": [{"symbol": p["symbol"], "ic": p["rank_ic"]["ic"]} for p in ranked[:3]],
        "worst": [{"symbol": p["symbol"], "ic": p["rank_ic"]["ic"]} for p in ranked[-3:]][::-1],
        "disclaimer": DISCLAIMER,
    }


# ─── Time Machine ─────────────────────────────────────────────────────────────
_AVAIL_CHECKS = [
    ("MA200 trend / Golden-Death cross", 200),
    ("12-1 momentum", 253),
    ("ADX / regime classification", 28),
    ("MACD", 26),
    ("RSI / Bollinger", 20),
]


def _availability(n_bars: int) -> dict:
    unavailable = [{"signal": name, "reason": f"needs {need} trading days; only {n_bars} existed on this date"}
                   for name, need in _AVAIL_CHECKS if n_bars < need]
    return {"n_bars": n_bars, "had_ma200": n_bars >= 200,
            "full_panel": not unavailable, "unavailable": unavailable}


def _df_to_prices(df) -> list:
    raw = "CloseRaw" in df.columns
    out = []
    for idx, row in df.iterrows():
        d = idx.date() if hasattr(idx, "date") else idx
        out.append({"date": d, "close": float(row["Close"]), "open": float(row["Open"]),
                    "close_raw": float(row["CloseRaw"]) if raw else None})
    return out


def _spy_return(spy_prices, sd, H, key_idx=None):
    """SPY close-to-close return over the same H-day window starting at date sd."""
    if not spy_prices:
        return None
    d2i = {p["date"]: i for i, p in enumerate(spy_prices)}
    i = d2i.get(sd)
    if i is None:                       # nearest prior SPY bar
        prior = [p for p in spy_prices if p["date"] <= sd]
        if not prior:
            return None
        i = len(prior) - 1
    j = i + H
    if j >= len(spy_prices) or not spy_prices[i]["close"]:
        return None
    return round(spy_prices[j]["close"] / spy_prices[i]["close"] - 1, 6)


def _verdict(signal, ret_cc, spy_ret):
    if ret_cc is None:
        return "Outcome still pending — not enough trading days have passed yet."
    pct = ret_cc * 100
    dir_word = "rose" if ret_cc > 0 else "fell" if ret_cc < 0 else "was flat"
    base = f"Signal said {signal}. Over the next 20 days price {dir_word} {abs(pct):.1f}%"
    if spy_ret is not None:
        diff = (ret_cc - spy_ret) * 100
        base += f", {'beating' if diff >= 0 else 'lagging'} SPY by {abs(diff):.1f}%"
    correct = (signal == "BUY" and ret_cc > 0) or (signal == "SELL" and ret_cc < 0)
    if signal in ("BUY", "SELL"):
        base += "." if correct else " — the signal missed."
        if correct and not base.endswith("."):
            base += "."
    else:
        base += " (HOLD makes no directional call)."
    return base


def _price_disclosure(rec) -> dict:
    adj, raw = rec["price_close_adj"], rec["price_close_raw"]
    if raw and adj and abs(adj - raw) / raw > 0.02:
        return {"corporate_action": True, "adjusted": adj, "as_traded": raw,
                "note": ("A split or sizable dividend occurred between this date and today, "
                         "so the split-adjusted price shown differs from what traded live.")}
    return {"corporate_action": False, "adjusted": adj, "as_traded": raw}


def _outcome(prices, idx, rec, spy_prices):
    sd = date.fromisoformat(rec["snapshot_date"])
    fr = sb.forward_returns(prices, idx)
    n = len(prices)
    to_today = round(prices[-1]["close"] / prices[idx]["close"] - 1, 6) if prices[idx]["close"] else None
    horizons = {}
    for H in sb.HORIZONS:
        h = fr[H]
        horizons[H] = {"cc": h["cc"], "no": h["no"], "matured": h["matured"],
                       "spy_cc": _spy_return(spy_prices, sd, H) if h["matured"] else None}
    ret20 = fr[20]["cc"]
    spy20 = _spy_return(spy_prices, sd, 20)
    return {
        "horizons": horizons,
        "to_today": {"cc": to_today, "matured": True, "days": n - 1 - idx},
        "verdict": _verdict(rec["signal"], ret20, spy20),
        "borne_out": (None if ret20 is None or rec["signal"] == "HOLD"
                      else ((rec["signal"] == "BUY" and ret20 > 0) or (rec["signal"] == "SELL" and ret20 < 0))),
    }


def build_timemachine(symbol, df_adj, profile, as_of, spy_prices=None) -> dict:
    """Reconstruct the signal as of `as_of` and reveal the realized outcome."""
    recs = replay_signals(symbol, df_adj, profile, as_of=as_of)
    if not recs:
        return {"symbol": symbol, "as_of": str(as_of), "analysis": None, "outcome": None,
                "availability": {"any": False, "reason": "No price history on or before this date.",
                                 "unavailable": [], "full_panel": False, "n_bars": 0},
                "price_disclosure": None, "context": [], "disclaimer": DISCLAIMER}
    rec = recs[0]
    prices = prices_list(symbol) or _df_to_prices(df_adj)
    d2i = {p["date"]: i for i, p in enumerate(prices)}
    sd = date.fromisoformat(rec["snapshot_date"])
    idx = d2i.get(sd)
    outcome = _outcome(prices, idx, rec, spy_prices) if idx is not None else None
    return {
        "symbol": symbol, "as_of": rec["snapshot_date"],
        "analysis": rec["analysis"],
        "signed_score": rec["signed_score"],
        "availability": _availability(rec["n_bars"]),
        "outcome": outcome,
        "price_disclosure": _price_disclosure(rec),
        "context": _context_dates(symbol, df_adj, profile, prices, d2i, idx, spy_prices),
        "disclaimer": DISCLAIMER,
    }


def _context_dates(symbol, df_adj, profile, prices, d2i, target_idx, spy_prices):
    """2–3 other evenly-spaced as-of dates so one pick isn't viewed in isolation."""
    n = len(prices)
    if n < 60 or target_idx is None:
        return []
    picks = sorted({int(n * f) for f in (0.25, 0.5, 0.75)})
    out = []
    for pi in picks:
        if abs(pi - target_idx) < 10 or pi >= n - 1:
            continue
        d = prices[pi]["date"]
        recs = replay_signals(symbol, df_adj, profile, as_of=d)
        if not recs or recs[0]["analysis"] is None:
            continue
        r = recs[0]
        fr = sb.forward_returns(prices, pi)
        out.append({"as_of": r["snapshot_date"], "signal": r["signal"],
                    "confidence": r["confidence"], "ret_20d": fr[20]["cc"],
                    "spy_20d": _spy_return(spy_prices, d, 20)})
        if len(out) >= 3:
            break
    return out


def _xs_for_horizon(symbols, pkey, H, source):
    """date -> [(signed_score, ret_cc@H)] across all symbols, for cross-sectional IC."""
    out = {}
    q = SignalSnapshot.query.filter(SignalSnapshot.symbol.in_(list(symbols)),
                                    SignalSnapshot.profile_key == pkey)
    if source == "live":
        q = q.filter(SignalSnapshot.source == "live")
    for s in q.all():
        r = _ret_field(s, H)
        if r is not None and s.signed_score is not None:
            out.setdefault(s.snapshot_date, []).append((s.signed_score, r))
    return out
