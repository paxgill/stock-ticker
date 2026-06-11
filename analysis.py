"""
Regime-aware, rule-based trade suggestion engine.

All logic is transparent and explainable — no ML, no black boxes. Every signal
contributes a score in [-1, +1] with a human-readable reason; confidence is a
regime-adjusted weighted mean mapped to 0-100, gated by two confirmation rules.

NOTE: output is algorithmic and is NOT financial advice. Users must do their own
research. The same disclaimer (DISCLAIMER) is surfaced in the UI.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import indicators as ind

DISCLAIMER = (
    "Algorithmic output, not financial advice. Do your own research before trading."
)


# ─── Legacy helpers (kept for options_signals.py and /api/quote) ──────────────
def compute_rsi(closes: pd.Series, period: int = 14) -> float:
    """Simple-moving-average RSI (legacy). New engine uses Wilder RSI."""
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    val = rsi.iloc[-1]
    return float(val) if not pd.isna(val) else 50.0


def compute_rvol(volumes: pd.Series, period: int = 20) -> float:
    """Relative volume: latest bar vs N-bar average."""
    if len(volumes) < period:
        return 1.0
    avg = float(volumes.tail(period).mean())
    return float(volumes.iloc[-1]) / avg if avg > 0 else 1.0


def rvol_tier(rvol: float) -> str:
    if rvol < 0.75:
        return "Low"
    if rvol < 1.25:
        return "Normal"
    if rvol < 2.0:
        return "Elevated"
    return "In Play"


def smooth_prices(closes: pd.Series, window_size: int = 5, smooth_type: str = "mean") -> pd.Series:
    """
    Causal (no look-ahead) rolling smoothing of a price Series. Returns a
    same-length Series; warm-up bars use whatever data is available (min_periods=1).
    """
    if window_size <= 1:
        return closes.copy()
    if smooth_type == "median":
        return closes.rolling(window=window_size, min_periods=1).median()
    return closes.rolling(window=window_size, min_periods=1).mean()


# ─── Small numeric helpers ────────────────────────────────────────────────────
def _clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _last(series: pd.Series, default=None):
    """Last non-NaN value of a Series, or default."""
    if series is None or len(series) == 0:
        return default
    s = series.dropna()
    return float(s.iloc[-1]) if len(s) else default


def _dir(score: float) -> str:
    if score > 0.05:
        return "bull"
    if score < -0.05:
        return "bear"
    return "neutral"


# ─── Regime detection (Phase 2b) ──────────────────────────────────────────────
REGIME_FAMILY_MULT = {
    # family weight multipliers — trend signals fail in ranges, mean-reversion
    # signals fail in trends, so reliability is regime-dependent.
    "Trending / calm":     {"trend": 1.0, "momentum": 1.0, "volume_flow": 1.0, "oscillator": 0.7},
    "Trending / volatile": {"trend": 1.0, "momentum": 1.0, "volume_flow": 1.0, "oscillator": 0.7},
    "Ranging / calm":      {"trend": 0.5, "momentum": 0.7, "volume_flow": 1.0, "oscillator": 1.3},
    "Ranging / volatile":  {"trend": 0.5, "momentum": 0.7, "volume_flow": 1.0, "oscillator": 1.1},
    "Undetermined":        {"trend": 1.0, "momentum": 1.0, "volume_flow": 1.0, "oscillator": 1.0},
}
REGIME_HAIRCUT = {"Trending / volatile": 0.85, "Ranging / volatile": 0.70}
REGIME_STOP_MULT = {  # ATR multiple for suggested stops
    "Trending / calm": 1.5, "Trending / volatile": 2.0,
    "Ranging / calm": 1.2, "Ranging / volatile": 1.2, "Undetermined": 1.5,
}


def detect_regime(hist: pd.DataFrame) -> dict:
    """
    Classify the latest bar into one of four regimes using ADX (trend strength)
    and ATR% relative to its own 50-day median (volatility). Returns the label,
    its inputs, the directional bias, and the per-family weight multipliers.
    """
    out = {
        "label": "Undetermined", "adx": None, "plus_di": None, "minus_di": None,
        "atr_pct": None, "atr_pct_median": None, "atr": None, "trending": False,
        "volatile": False, "direction": None,
        "family_mult": REGIME_FAMILY_MULT["Undetermined"],
        "haircut": 1.0, "stop_atr_mult": 1.5,
    }
    if hist is None or len(hist) < 30 or not {"High", "Low", "Close"}.issubset(hist.columns):
        return out

    high = hist["High"].astype(float)
    low = hist["Low"].astype(float)
    close = hist["Close"].astype(float)

    adx_d = ind.adx(high, low, close, 14)
    adx_val = _last(adx_d["adx"])
    plus_di = _last(adx_d["plus_di"])
    minus_di = _last(adx_d["minus_di"])

    atr_series = ind.atr(high, low, close, 14)        # absolute ATR (reused downstream)
    atr_val = _last(atr_series)
    atr_pct_series = atr_series / close * 100
    atr_pct_val = _last(atr_pct_series)
    atr_pct_median = float(atr_pct_series.tail(50).median()) if atr_pct_series.notna().any() else None

    if adx_val is None or atr_pct_val is None or atr_pct_median is None:
        return out

    trending = adx_val >= 25
    volatile = atr_pct_val > atr_pct_median

    if trending and not volatile:
        label = "Trending / calm"
    elif trending and volatile:
        label = "Trending / volatile"
    elif not trending and not volatile:
        label = "Ranging / calm"
    else:
        label = "Ranging / volatile"

    direction = None
    if trending and plus_di is not None and minus_di is not None:
        direction = "up" if plus_di >= minus_di else "down"

    out.update({
        "label": label, "adx": round(adx_val, 1),
        "plus_di": round(plus_di, 1) if plus_di is not None else None,
        "minus_di": round(minus_di, 1) if minus_di is not None else None,
        "atr_pct": round(atr_pct_val, 2), "atr_pct_median": round(atr_pct_median, 2),
        "atr": round(atr_val, 4) if atr_val is not None else None,
        "trending": trending, "volatile": volatile, "direction": direction,
        "family_mult": REGIME_FAMILY_MULT[label],
        "haircut": REGIME_HAIRCUT.get(label, 1.0),
        "stop_atr_mult": REGIME_STOP_MULT[label],
    })
    return out


# ─── Composite scoring engine (Phase 2c) ──────────────────────────────────────
RISK_PCT = {"Conservative": 0.5, "Moderate": 1.0, "Aggressive": 2.0}


def analyze_ticker(symbol: str, hist: pd.DataFrame, profile: dict) -> dict | None:
    """
    Regime-aware composite analysis. Returns a signal dict (or None on
    insufficient data) that is backward compatible with the existing UI
    (signal/confidence/reasoning/signals[]/price/thresholds) and adds regime,
    family contributions, confirmation outcomes, a suggested stop, and a
    position-size hint.
    """
    if hist is None or len(hist) < 20:
        return None
    if not {"High", "Low", "Close", "Volume"}.issubset(hist.columns):
        return None

    closes = hist["Close"].astype(float)
    highs = hist["High"].astype(float)
    lows = hist["Low"].astype(float)
    volumes = hist["Volume"].astype(float)

    price = float(closes.iloc[-1])
    prev_price = float(closes.iloc[-2]) if len(closes) > 1 else price
    price_up = price >= prev_price

    rvol = compute_rvol(volumes)
    rvol_label = rvol_tier(rvol)
    low_volume = rvol < 0.75

    regime = detect_regime(hist)
    fam_mult = regime["family_mult"]

    # ── Indicator values ──────────────────────────────────────────────────────
    rsi = _last(ind.wilder_rsi(closes, 14), 50.0)
    macd_d = ind.macd(closes)
    macd_hist = _last(macd_d["hist"], 0.0)
    macd_line = _last(macd_d["macd"], 0.0)
    # Reuse the ATR already computed during regime detection (avoids recompute)
    atr_val = regime["atr"] if regime["atr"] is not None else _last(ind.atr(highs, lows, closes, 14), 0.0)
    bb = ind.bollinger(closes, 20, 2)
    pct_b = _last(bb["pct_b"], 0.5)
    obv_sl = ind.obv_slope(closes, volumes, 20)
    mom12 = ind.momentum_12_1(closes)
    mom_n = ind.momentum_simple(closes, int(profile.get("momentum_days", 10)))

    ma20 = float(closes.tail(20).mean()) if len(closes) >= 20 else None
    ma50 = float(closes.tail(50).mean()) if len(closes) >= 50 else None
    ma200 = float(closes.tail(200).mean()) if len(closes) >= 200 else None

    # ── Build signals grouped by family ───────────────────────────────────────
    members: list[dict] = []   # each: name, family, score[-1,1], reason, extras

    # TREND family ------------------------------------------------------------
    if ma50 is not None:
        golden = death = False
        if ma200 is not None:
            is_bull = ma50 > ma200
            ma50_s = ind.sma(closes, 50)
            ma200_s = ind.sma(closes, 200)
            golden_s = (ma50_s.shift(1) < ma200_s.shift(1)) & (ma50_s >= ma200_s)
            death_s = (ma50_s.shift(1) > ma200_s.shift(1)) & (ma50_s <= ma200_s)
            golden = bool(golden_s.iloc[-20:].any())
            death = bool(death_s.iloc[-20:].any())
            score = 1.0 if is_bull else -1.0
            if golden:
                tag = "☀ Golden Cross (last 20d)"
            elif death:
                tag = "☽ Death Cross (last 20d)"
            else:
                tag = "MA50 > MA200" if is_bull else "MA50 < MA200"
            reason = f"MA50 ${ma50:.2f} {'>' if is_bull else '<'} MA200 ${ma200:.2f} — {tag}"
        else:
            is_bull = ma20 > ma50
            score = 0.7 if is_bull else -0.7
            reason = (f"MA20 ${ma20:.2f} {'>' if is_bull else '<'} MA50 ${ma50:.2f} — "
                      f"{'bullish' if is_bull else 'bearish'} short-term structure")
        members.append({"name": "MA Structure", "family": "trend", "score": score,
                        "reason": reason, "golden_cross": golden, "death_cross": death})

    # MACD
    if atr_val > 0:
        macd_score = _clip(macd_hist / (0.5 * atr_val))
    else:
        macd_score = _clip(np.sign(macd_hist))
    members.append({
        "name": "MACD", "family": "trend", "score": macd_score,
        "reason": (f"MACD line {macd_line:+.2f}, histogram {macd_hist:+.2f} — "
                   f"{'bullish' if macd_score > 0 else 'bearish' if macd_score < 0 else 'flat'} momentum"),
    })

    # ADX / DI direction
    if regime["adx"] is not None:
        adx_v = regime["adx"]
        if adx_v >= 20 and regime["plus_di"] is not None:
            di_dir = 1.0 if regime["plus_di"] >= regime["minus_di"] else -1.0
            adx_score = di_dir * min(1.0, adx_v / 50.0)
            reason = (f"ADX {adx_v:.0f} ({'strong' if adx_v >= 25 else 'building'} trend), "
                      f"+DI {regime['plus_di']:.0f} vs -DI {regime['minus_di']:.0f}")
        else:
            adx_score = 0.0
            reason = f"ADX {adx_v:.0f} — no decisive trend (range conditions)"
        members.append({"name": "ADX / DI", "family": "trend", "score": adx_score, "reason": reason})

    # MOMENTUM family ---------------------------------------------------------
    if mom12 is not None:
        m12s = _clip(mom12 / 30.0)
        members.append({"name": "12-1 Momentum", "family": "momentum", "score": m12s,
                        "reason": f"12-1 momentum {mom12:+.1f}% (trailing year, ex-last-month)"})
    if mom_n is not None:
        n = int(profile.get("momentum_days", 10))
        mns = _clip(mom_n / 8.0)
        members.append({"name": "Short Momentum", "family": "momentum", "score": mns,
                        "reason": f"{n}-day return {mom_n:+.1f}%"})

    # VOLUME-FLOW family ------------------------------------------------------
    obv_score = _clip(obv_sl * 4.0)
    members.append({"name": "OBV Flow", "family": "volume_flow", "score": obv_score,
                    "reason": (f"On-balance volume slope {obv_sl:+.3f} — "
                               f"{'accumulation' if obv_score > 0 else 'distribution' if obv_score < 0 else 'flat'}")})

    if len(volumes) >= 20:
        avg_vol = float(volumes.tail(20).mean())
        vol_ratio = float(volumes.iloc[-1]) / avg_vol if avg_vol > 0 else 1.0
        thr = float(profile.get("volume_spike_threshold", 1.5))
        if vol_ratio >= thr:
            vscore = (0.8 if price_up else -0.8) * min(1.0, vol_ratio / (2 * thr) + 0.5)
            reason = f"Volume {vol_ratio:.1f}× 20d avg on {'up' if price_up else 'down'} day — confirmed move"
        else:
            vscore = 0.0
            reason = f"Volume {vol_ratio:.1f}× 20d avg — no spike ({thr}× threshold)"
        if low_volume:
            reason += f" · RVOL {rvol:.2f}× (thin, less reliable)"
        members.append({"name": "Volume Spike", "family": "volume_flow", "score": vscore,
                        "reason": reason, "vol_ratio": round(vol_ratio, 2), "rvol": round(rvol, 2)})

    # OSCILLATOR family -------------------------------------------------------
    ob = float(profile.get("rsi_overbought", 70))
    os_ = float(profile.get("rsi_oversold", 30))
    if regime["trending"]:
        # Pullback timer: in an uptrend a low RSI is a buy-the-dip, not a reversal.
        trend_up = regime["direction"] != "down"
        if trend_up:
            if rsi < 40:
                rsi_score, note = 0.8, "bullish pullback in uptrend"
            elif rsi > 80:
                rsi_score, note = -0.3, "extended — overbought in uptrend"
            else:
                rsi_score, note = 0.2, "healthy in uptrend"
        else:
            if rsi > 60:
                rsi_score, note = -0.8, "bearish bounce in downtrend"
            elif rsi < 20:
                rsi_score, note = 0.3, "washed out in downtrend"
            else:
                rsi_score, note = -0.2, "weak in downtrend"
        rsi_reason = f"RSI {rsi:.0f} — {note} (pullback timer)"
    else:
        # Mean-reversion: extremes lead price back toward the mean.
        if rsi <= os_:
            rsi_score = 1.0
        elif rsi >= ob:
            rsi_score = -1.0
        else:
            mid = (ob + os_) / 2
            rsi_score = _clip((mid - rsi) / (mid - os_))
        rsi_reason = f"RSI {rsi:.0f} — mean-reversion ({os_:.0f}/{ob:.0f} bands)"
    members.append({"name": "RSI", "family": "oscillator", "score": rsi_score,
                    "reason": rsi_reason, "rsi": round(rsi, 1)})

    # Bollinger %B
    bscore = _clip((0.5 - pct_b) * 2.0)
    members.append({"name": "Bollinger %B", "family": "oscillator", "score": bscore,
                    "reason": (f"%B {pct_b:.2f} — near {'lower band (cheap)' if pct_b < 0.5 else 'upper band (rich)'}")})

    # ── Aggregate into families ───────────────────────────────────────────────
    base_weights = {
        "trend": float(profile.get("ma_weight", 0.25)),
        "momentum": float(profile.get("momentum_weight", 0.25)),
        "volume_flow": float(profile.get("volume_weight", 0.25)),
        "oscillator": float(profile.get("rsi_weight", 0.25)),
    }

    families: dict[str, dict] = {}
    for fam in ("trend", "momentum", "volume_flow", "oscillator"):
        fam_members = [m for m in members if m["family"] == fam]
        if not fam_members:
            continue
        fam_score = sum(m["score"] for m in fam_members) / len(fam_members)
        eff_weight = base_weights[fam] * fam_mult.get(fam, 1.0)
        families[fam] = {
            "score": round(fam_score, 3),
            "base_weight": round(base_weights[fam], 3),
            "regime_mult": fam_mult.get(fam, 1.0),
            "eff_weight": round(eff_weight, 4),
            "contribution": round(eff_weight * fam_score, 4),
            "members": [m["name"] for m in fam_members],
        }

    total_w = sum(f["eff_weight"] for f in families.values())
    if total_w <= 0:
        return None
    weighted = sum(f["eff_weight"] * f["score"] for f in families.values()) / total_w  # [-1,1]

    # ── Confidence 0-100 with regime haircut + thin-volume dampening ──────────
    confidence = (weighted + 1) / 2 * 100
    haircut = regime["haircut"]
    if haircut != 1.0:
        confidence = 50 + (confidence - 50) * haircut
    if low_volume:
        confidence = confidence + (50 - confidence) * 0.20
    confidence = round(confidence)

    # ── Thresholds (kept) ─────────────────────────────────────────────────────
    risk = profile.get("risk_tolerance", "Moderate")
    thresholds = {
        "Conservative": {"buy": 75, "sell": 28},
        "Moderate":     {"buy": 65, "sell": 35},
        "Aggressive":   {"buy": 55, "sell": 42},
    }
    t = thresholds.get(risk, thresholds["Moderate"])

    proposed = "BUY" if confidence >= t["buy"] else "SELL" if confidence <= t["sell"] else "HOLD"

    # ── Confirmation rules (Phase 2c) ─────────────────────────────────────────
    overall_dir = np.sign(weighted)
    agreeing = [f for f, d in families.items()
                if np.sign(d["score"]) == overall_dir and abs(d["score"]) > 0.15]
    confirm_notes = []
    capped = False

    # Rule (i): a BUY/SELL needs ≥2 independent families agreeing.
    if proposed in ("BUY", "SELL") and len(agreeing) < 2:
        confirm_notes.append(
            f"Only {len(agreeing)} signal family agrees — needs 2 to confirm; capped to HOLD")
        proposed = "HOLD"
        capped = True

    # Rule (ii): a signal against the prevailing regime trend is capped at HOLD.
    regime_gate = False
    if regime["direction"] == "up" and proposed == "SELL":
        confirm_notes.append("SELL opposes an up-trending regime — capped to HOLD")
        proposed, capped, regime_gate = "HOLD", True, True
    elif regime["direction"] == "down" and proposed == "BUY":
        confirm_notes.append("BUY opposes a down-trending regime — capped to HOLD")
        proposed, capped, regime_gate = "HOLD", True, True

    signal = proposed

    # ── Suggested stop + position size (illustrative) ─────────────────────────
    stop = None
    sizing = None
    if atr_val > 0:
        k = regime["stop_atr_mult"]
        long_side = weighted >= 0
        stop_dist = k * atr_val
        stop_price = price - stop_dist if long_side else price + stop_dist
        stop_pct = stop_dist / price * 100 if price else 0.0
        stop = {
            "price": round(stop_price, 2),
            "distance": round(stop_dist, 2),
            "pct": round(stop_pct, 2),
            "atr": round(atr_val, 2),
            "atr_multiple": k,
            "side": "long" if long_side else "short",
            "basis": f"{k}× ATR ({regime['label']} regime)",
        }
        risk_pct = RISK_PCT.get(risk, 1.0)
        pos_pct = min(100.0, risk_pct / stop_pct * 100) if stop_pct > 0 else None
        sizing = {
            "risk_pct": risk_pct,
            "position_pct": round(pos_pct, 1) if pos_pct else None,
            "note": (f"Illustrative: risking {risk_pct}% of portfolio with a "
                     f"{stop_pct:.1f}% stop ≈ a {pos_pct:.0f}% position." if pos_pct
                     else "Illustrative only."),
        }

    # ── UI-compatible signals[] (direction strings + reasons) ─────────────────
    all_signals = []
    for m in members:
        sig = {
            "name": m["name"],
            "family": m["family"],
            "direction": _dir(m["score"]),
            "score": round(m["score"], 3),
            "reason": m["reason"],
        }
        for extra in ("golden_cross", "death_cross", "vol_ratio", "rvol", "rsi"):
            if extra in m:
                sig[extra] = m[extra]
        all_signals.append(sig)

    # reasoning summary: strongest-contributing families
    ranked_fams = sorted(families.items(), key=lambda kv: abs(kv[1]["contribution"]), reverse=True)
    fam_label = {"trend": "Trend", "momentum": "Momentum",
                 "volume_flow": "Volume flow", "oscillator": "Oscillator"}
    lead = [fam_label[f] for f, d in ranked_fams if abs(d["score"]) > 0.15]
    reasoning = (" + ".join(lead[:3]) if lead else "Mixed signals")
    if capped:
        reasoning += " (confirmation gate applied)"

    return {
        "symbol":         symbol,
        "signal":         signal,
        "confidence":     confidence,
        "reasoning":      reasoning,
        "signals":        all_signals,
        "price":          price,
        "thresholds":     t,
        "risk_tolerance": risk,
        "rvol":           round(rvol, 2),
        "rvol_tier":      rvol_label,
        # ── new in Phase 2 ──
        "regime":         regime,
        "families":       families,
        "weighted_score": round(weighted, 3),
        "confirmations": {
            "families_agreeing": len(agreeing),
            "required": 2,
            "agreeing_families": [fam_label[f] for f in agreeing],
            "capped_to_hold": capped,
            "regime_gate_applied": regime_gate,
            "notes": confirm_notes,
        },
        "suggested_stop": stop,
        "position_sizing": sizing,
        "disclaimer":     DISCLAIMER,
    }


def detect_surge_crash(
    hist: pd.DataFrame,
    detect_type: str = "surge",
    num_output: int = 5,
) -> tuple[list[str], list[float]]:
    """
    Find the largest single-day price moves in a yfinance history DataFrame.

    A daily move is defined as  Close - Open  for that bar, which captures
    the intraday directional range without being distorted by overnight gaps.

    Parameters
    ----------
    hist        : pd.DataFrame — yfinance .history() result with 'Open' and
                  'Close' columns; the index must be DatetimeIndex or
                  date-castable.
    detect_type : 'surge'  — largest positive  (Close > Open) days, biggest first
                  'crash'  — largest negative  (Close < Open) days, most-negative first
    num_output  : int — how many results to return (clamped to available rows)

    Returns
    -------
    (dates, changes) — two parallel lists
        dates   : list[str]   ISO date strings ('YYYY-MM-DD')
        changes : list[float] daily Close-Open dollar change, sorted by magnitude
    """
    if hist is None or hist.empty:
        return [], []

    required = {"Open", "Close"}
    if not required.issubset(hist.columns):
        return [], []

    df = hist[["Open", "Close"]].copy()
    df["change"] = df["Close"].astype(float) - df["Open"].astype(float)

    detect_type = (detect_type or "surge").lower()
    if detect_type == "crash":
        df = df[df["change"] < 0].sort_values("change", ascending=True)
    else:
        df = df[df["change"] > 0].sort_values("change", ascending=False)

    num_output = max(1, int(num_output))
    df = df.head(num_output)

    def _to_date_str(idx_val) -> str:
        try:
            return idx_val.date().isoformat()
        except AttributeError:
            return str(idx_val)[:10]

    dates = [_to_date_str(i) for i in df.index]
    changes = [round(float(v), 4) for v in df["change"]]
    return dates, changes


DEFAULT_PROFILE = {
    "risk_tolerance": "Moderate",
    "horizon": "Swing",
    "ma_weight": 0.25,
    "volume_weight": 0.25,
    "rsi_weight": 0.25,
    "momentum_weight": 0.25,
    "volume_spike_threshold": 1.5,
    "rsi_overbought": 70.0,
    "rsi_oversold": 30.0,
    "momentum_days": 10,
    "max_trades_per_day": 3,
}


# ─── Research-grounded presets (Phase 2d) ─────────────────────────────────────
# Each preset describes the STYLE of research it follows — no claim of
# endorsement by any firm. Weights map onto the four signal families
# (ma_weight=trend, momentum_weight, volume_weight=flow, rsi_weight=oscillator).
PRESETS = {
    "momentum_12_1": {
        "key": "momentum_12_1",
        "name": "Cross-sectional momentum (12-1)",
        "research": "Jegadeesh–Titman academic momentum convention",
        "believes": ("Stocks that led the market over the past year (excluding the "
                     "most recent month) tend to keep leading. Trades slowly and "
                     "avoids short-term reversal."),
        "risk_tolerance": "Aggressive", "horizon": "Long-term",
        "ma_weight": 0.30, "momentum_weight": 0.50,
        "volume_weight": 0.10, "rsi_weight": 0.10,
        "volume_spike_threshold": 1.5, "rsi_overbought": 75, "rsi_oversold": 25,
        "momentum_days": 126, "max_trades_per_day": 1,
    },
    "mean_reversion_rsi2": {
        "key": "mean_reversion_rsi2",
        "name": "Short-term mean reversion",
        "research": "Connors-style RSI(2) research family",
        "believes": ("Sharp short-term selloffs inside a longer uptrend tend to "
                     "snap back. Only buys dips above the 200-day average."),
        "risk_tolerance": "Aggressive", "horizon": "Short-term",
        "ma_weight": 0.15, "momentum_weight": 0.10,
        "volume_weight": 0.15, "rsi_weight": 0.60,
        "volume_spike_threshold": 1.5, "rsi_overbought": 90, "rsi_oversold": 10,
        "momentum_days": 4, "max_trades_per_day": 3,
    },
    "trend_following": {
        "key": "trend_following",
        "name": "Trend following",
        "research": "CTA / managed-futures style",
        "believes": ("Ride established trends. MA structure and ADX dominate, "
                     "momentum confirms, volume is largely ignored, stops are wide."),
        "risk_tolerance": "Aggressive", "horizon": "Long-term",
        "ma_weight": 0.55, "momentum_weight": 0.30,
        "volume_weight": 0.05, "rsi_weight": 0.10,
        "volume_spike_threshold": 2.0, "rsi_overbought": 80, "rsi_oversold": 20,
        "momentum_days": 90, "max_trades_per_day": 2,
    },
    "quality_value": {
        "key": "quality_value",
        "name": "Quality + value tilt",
        "research": "Fama-French / quality factor family",
        "believes": ("Prefer profitable, growing businesses bought at reasonable "
                     "prices. Requires a fundamental screen before any technical buy."),
        "risk_tolerance": "Moderate", "horizon": "Long-term",
        "ma_weight": 0.35, "momentum_weight": 0.25,
        "volume_weight": 0.10, "rsi_weight": 0.30,
        "volume_spike_threshold": 1.5, "rsi_overbought": 70, "rsi_oversold": 30,
        "momentum_days": 60, "max_trades_per_day": 1,
        "requires_fundamentals": True,
    },
    "low_vol_income": {
        "key": "low_vol_income",
        "name": "Low-volatility income",
        "research": "Low-volatility factor family",
        "believes": ("Favor stable, low-volatility names near support; penalize "
                     "high ATR%. Effectively a hold-screen for steady compounders."),
        "risk_tolerance": "Conservative", "horizon": "Long-term",
        "ma_weight": 0.40, "momentum_weight": 0.15,
        "volume_weight": 0.15, "rsi_weight": 0.30,
        "volume_spike_threshold": 1.5, "rsi_overbought": 70, "rsi_oversold": 35,
        "momentum_days": 60, "max_trades_per_day": 1,
    },
}


def preset_to_profile(key: str, name: str | None = None) -> dict | None:
    """Build an AnalysisProfile-shaped dict from a preset key."""
    p = PRESETS.get(key)
    if not p:
        return None
    return {
        "name": name or p["name"],
        "risk_tolerance": p["risk_tolerance"],
        "horizon": p["horizon"],
        "ma_weight": p["ma_weight"],
        "volume_weight": p["volume_weight"],
        "rsi_weight": p["rsi_weight"],
        "momentum_weight": p["momentum_weight"],
        "volume_spike_threshold": p["volume_spike_threshold"],
        "rsi_overbought": p["rsi_overbought"],
        "rsi_oversold": p["rsi_oversold"],
        "momentum_days": p["momentum_days"],
        "max_trades_per_day": p["max_trades_per_day"],
        "preset_key": key,
    }
