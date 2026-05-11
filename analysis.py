"""
Rule-based trade suggestion engine.
All logic is transparent and explainable — no ML, no black boxes.
"""
import pandas as pd


def compute_rsi(closes: pd.Series, period: int = 14) -> float:
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    val = rsi.iloc[-1]
    return float(val) if not pd.isna(val) else 50.0


def analyze_ticker(symbol: str, hist: pd.DataFrame, profile: dict) -> dict | None:
    """
    Returns signal dict or None if insufficient data.

    Signal scores are 0.0 (strong sell) → 1.0 (strong buy).
    Confidence = weighted average of all signal scores × 100.
    """
    if hist is None or len(hist) < 20:
        return None

    closes = hist["Close"].astype(float)
    volumes = hist["Volume"].astype(float)
    current_price = float(closes.iloc[-1])
    prev_price = float(closes.iloc[-2]) if len(closes) > 1 else current_price
    price_up = current_price >= prev_price

    all_signals = []
    total_weight = 0.0
    weighted_score = 0.0

    # ── 1. MA Crossover ──────────────────────────────────────────────
    w_ma = float(profile.get("ma_weight", 0.25))
    if w_ma > 0 and len(closes) >= 50:
        ma20 = float(closes.tail(20).mean())
        ma50 = float(closes.tail(50).mean())
        is_bull = ma20 > ma50
        score = 1.0 if is_bull else 0.0
        all_signals.append({
            "name": "MA Crossover",
            "direction": "bull" if is_bull else "bear",
            "score": score,
            "weight": w_ma,
            "reason": (
                f"MA20 ${ma20:.2f} {'>' if is_bull else '<'} MA50 ${ma50:.2f}"
                f" — {'Golden' if is_bull else 'Death'} Cross"
            ),
        })
        total_weight += w_ma
        weighted_score += w_ma * score

    # ── 2. Volume Spike ──────────────────────────────────────────────
    w_vol = float(profile.get("volume_weight", 0.25))
    if w_vol > 0 and len(volumes) >= 30:
        avg_vol_30 = float(volumes.tail(30).mean())
        cur_vol = float(volumes.iloc[-1])
        threshold = float(profile.get("volume_spike_threshold", 1.5))
        vol_ratio = cur_vol / avg_vol_30 if avg_vol_30 > 0 else 1.0

        if vol_ratio >= threshold:
            score = 1.0 if price_up else 0.0
            direction = "bull" if price_up else "bear"
            reason = (
                f"Volume {vol_ratio:.1f}× 30d avg"
                f" on {'up' if price_up else 'down'} day"
                f" — {'Bullish' if price_up else 'Bearish'} spike"
            )
        else:
            score = 0.5
            direction = "neutral"
            reason = f"Volume {vol_ratio:.1f}× 30d avg — no spike ({threshold}× threshold)"

        all_signals.append({
            "name": "Volume",
            "direction": direction,
            "score": score,
            "weight": w_vol,
            "reason": reason,
            "vol_ratio": round(vol_ratio, 2),
        })
        total_weight += w_vol
        weighted_score += w_vol * score

    # ── 3. RSI ───────────────────────────────────────────────────────
    w_rsi = float(profile.get("rsi_weight", 0.25))
    if w_rsi > 0 and len(closes) >= 15:
        rsi = compute_rsi(closes)
        ob = float(profile.get("rsi_overbought", 70))
        os_ = float(profile.get("rsi_oversold", 30))

        if rsi <= os_:
            score, direction = 1.0, "bull"
            reason = f"RSI {rsi:.1f} ≤ {os_:.0f} — Oversold, potential reversal"
        elif rsi >= ob:
            score, direction = 0.0, "bear"
            reason = f"RSI {rsi:.1f} ≥ {ob:.0f} — Overbought, caution"
        else:
            midpoint = (ob + os_) / 2
            raw = 0.5 + (midpoint - rsi) / (2 * (midpoint - os_)) * 0.5
            score = max(0.1, min(0.9, raw))
            direction = "neutral"
            reason = f"RSI {rsi:.1f} — Neutral ({os_:.0f}–{ob:.0f})"

        all_signals.append({
            "name": "RSI",
            "direction": direction,
            "score": round(score, 3),
            "weight": w_rsi,
            "reason": reason,
            "rsi": round(rsi, 1),
        })
        total_weight += w_rsi
        weighted_score += w_rsi * score

    # ── 4. Price Momentum ────────────────────────────────────────────
    w_mom = float(profile.get("momentum_weight", 0.25))
    if w_mom > 0:
        n = int(profile.get("momentum_days", 10))
        if len(closes) > n:
            mom_pct = (float(closes.iloc[-1]) / float(closes.iloc[-n - 1]) - 1) * 100

            if mom_pct > 5:
                score, direction = 1.0, "bull"
                reason = f"{n}d momentum +{mom_pct:.1f}% — Strong uptrend"
            elif mom_pct > 2:
                score, direction = 0.75, "bull"
                reason = f"{n}d momentum +{mom_pct:.1f}% — Positive momentum"
            elif mom_pct > -2:
                score, direction = 0.5, "neutral"
                reason = f"{n}d momentum {mom_pct:+.1f}% — Sideways"
            elif mom_pct > -5:
                score, direction = 0.25, "bear"
                reason = f"{n}d momentum {mom_pct:.1f}% — Weakening"
            else:
                score, direction = 0.0, "bear"
                reason = f"{n}d momentum {mom_pct:.1f}% — Strong downtrend"

            all_signals.append({
                "name": "Momentum",
                "direction": direction,
                "score": score,
                "weight": w_mom,
                "reason": reason,
                "pct": round(mom_pct, 2),
            })
            total_weight += w_mom
            weighted_score += w_mom * score

    if total_weight == 0:
        return None

    confidence = round(weighted_score / total_weight * 100)

    # ── Thresholds by risk tolerance ──────────────────────────────────
    risk = profile.get("risk_tolerance", "Moderate")
    thresholds = {
        "Conservative": {"buy": 75, "sell": 28},
        "Moderate":     {"buy": 65, "sell": 35},
        "Aggressive":   {"buy": 55, "sell": 42},
    }
    t = thresholds.get(risk, thresholds["Moderate"])

    if confidence >= t["buy"]:
        signal = "BUY"
    elif confidence <= t["sell"]:
        signal = "SELL"
    else:
        signal = "HOLD"

    # Build human-readable reasoning
    active_signals = [s["name"] for s in all_signals if s["direction"] != "neutral"]
    reasoning = " + ".join(active_signals) if active_signals else "Mixed signals"

    return {
        "symbol": symbol,
        "signal": signal,
        "confidence": confidence,
        "reasoning": reasoning,
        "signals": all_signals,
        "price": current_price,
        "thresholds": t,
        "risk_tolerance": risk,
    }


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
