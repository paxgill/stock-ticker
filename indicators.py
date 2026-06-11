"""
Technical indicator library for PG Stock Analysis.

Pure functions: pandas Series in, pandas Series (or small float dicts) out.
No I/O, no global state, no ML — every value is reproducible and explainable.
All smoothed indicators (RSI, ATR, ADX) use Wilder's smoothing (an EMA with
alpha = 1/period), which is the definition every charting platform uses, so
values line up with what users see on TradingView / StockCharts.

Warm-up periods are returned as NaN rather than fabricated, so callers can
detect "not enough history" with pd.isna().
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# ─── Building blocks ──────────────────────────────────────────────────────────
def ema(series: pd.Series, span: int) -> pd.Series:
    """Standard exponential moving average (adjust=False — recursive form)."""
    return series.ewm(span=span, adjust=False).mean()


def wilder_ema(series: pd.Series, period: int) -> pd.Series:
    """
    Wilder's smoothing = an EMA with alpha = 1/period.

    Uses pandas' C-vectorized ewm (fast — important for the day-by-day
    backtester). This seeds on the first observation rather than the SMA of the
    first `period` values; the two conventions differ only during the warm-up
    and converge to identical values within ~5×period bars, so on real history
    (250+ bars) the numbers match TradingView / StockCharts. For the exact
    textbook warm-up value, see `wilder_ema_seeded`.
    """
    return series.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def wilder_ema_seeded(series: pd.Series, period: int) -> pd.Series:
    """
    Reference implementation of Wilder's smoothing seeded with the SMA of the
    first `period` observations (the exact textbook definition). Slower (Python
    recurrence); used for verification, not the hot path. Leading NaNs are
    skipped; interior NaNs are treated as 0.
    """
    arr = series.to_numpy(dtype=float)
    out = np.full(len(arr), np.nan)
    valid = ~np.isnan(arr)
    if not valid.any():
        return pd.Series(out, index=series.index)
    first = int(np.argmax(valid))
    if len(arr) - first < period:
        return pd.Series(out, index=series.index)
    seed_idx = first + period - 1
    out[seed_idx] = np.nanmean(arr[first:seed_idx + 1])
    for t in range(seed_idx + 1, len(arr)):
        cur = arr[t]
        if np.isnan(cur):
            cur = 0.0
        out[t] = (out[t - 1] * (period - 1) + cur) / period
    return pd.Series(out, index=series.index)


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """Wilder's True Range = max(H-L, |H-prevC|, |L-prevC|)."""
    prev_close = close.shift(1)
    ranges = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1)
    return ranges.max(axis=1)


# ─── Momentum / oscillators ───────────────────────────────────────────────────
def wilder_rsi(closes: pd.Series, period: int = 14) -> pd.Series:
    """
    Relative Strength Index using Wilder's smoothing (the canonical definition).
    Returns a Series in [0, 100]; warm-up bars are NaN.
    """
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = wilder_ema(gain, period)
    avg_loss = wilder_ema(loss, period)
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    # When avg_loss == 0 (only gains), RS is inf → RSI 100; pandas yields NaN there.
    rsi = rsi.where(avg_loss != 0, 100.0)
    rsi = rsi.where(avg_gain != 0, rsi)          # keep 0 when only losses
    rsi = rsi.mask((avg_gain == 0) & (avg_loss != 0), 0.0)
    return rsi


def macd(closes: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> dict:
    """MACD line, signal line, and histogram (all Series)."""
    line = ema(closes, fast) - ema(closes, slow)
    sig = ema(line, signal)
    hist = line - sig
    return {"macd": line, "signal": sig, "hist": hist}


# ─── Trend strength ───────────────────────────────────────────────────────────
def adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> dict:
    """
    Average Directional Index with +DI / -DI (Wilder).
    Returns {'adx', 'plus_di', 'minus_di'} as Series. ADX measures trend
    *strength* regardless of direction; +DI/-DI give the direction.
    """
    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0),
        index=high.index,
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0),
        index=high.index,
    )

    atr_ = wilder_ema(true_range(high, low, close), period)
    plus_di = 100 * wilder_ema(plus_dm, period) / atr_
    minus_di = 100 * wilder_ema(minus_dm, period) / atr_

    di_sum = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / di_sum
    adx_series = wilder_ema(dx, period)
    return {"adx": adx_series, "plus_di": plus_di, "minus_di": minus_di}


# ─── Volatility ───────────────────────────────────────────────────────────────
def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Average True Range (Wilder)."""
    return wilder_ema(true_range(high, low, close), period)


def atr_pct(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """ATR as a percentage of close — comparable across price levels."""
    return atr(high, low, close, period) / close * 100


def bollinger(closes: pd.Series, period: int = 20, num_std: float = 2.0) -> dict:
    """
    Bollinger Bands plus %B and bandwidth.
    %B = position within the bands (0 = lower, 1 = upper).
    bandwidth = (upper - lower) / mid — low values flag a 'squeeze'.
    """
    mid = closes.rolling(period, min_periods=period).mean()
    sd = closes.rolling(period, min_periods=period).std(ddof=0)
    upper = mid + num_std * sd
    lower = mid - num_std * sd
    span = (upper - lower).replace(0, np.nan)
    pct_b = (closes - lower) / span
    bandwidth = span / mid
    return {"mid": mid, "upper": upper, "lower": lower,
            "pct_b": pct_b, "bandwidth": bandwidth}


# ─── Volume flow ──────────────────────────────────────────────────────────────
def obv(closes: pd.Series, volumes: pd.Series) -> pd.Series:
    """On-Balance Volume: cumulative volume signed by daily price direction."""
    direction = np.sign(closes.diff()).fillna(0.0)
    return (direction * volumes).cumsum()


def obv_slope(closes: pd.Series, volumes: pd.Series, window: int = 20) -> float:
    """
    Normalised slope of OBV over the last `window` bars (least-squares fit
    divided by mean |OBV| so the sign and rough magnitude are scale-free).
    Positive = accumulation, negative = distribution.
    """
    o = obv(closes, volumes).dropna()
    if len(o) < window:
        return 0.0
    y = o.tail(window).to_numpy(dtype=float)
    x = np.arange(len(y), dtype=float)
    slope = np.polyfit(x, y, 1)[0]
    scale = np.mean(np.abs(y)) or 1.0
    return float(slope / scale)


# ─── Position within range ────────────────────────────────────────────────────
def high_low_distance(closes: pd.Series, window: int = 252) -> dict:
    """Percent distance of the latest close from its trailing high and low."""
    tail = closes.tail(window)
    hi = float(tail.max())
    lo = float(tail.min())
    cur = float(closes.iloc[-1])
    return {
        "high": hi,
        "low": lo,
        "pct_from_high": (cur - hi) / hi * 100 if hi else 0.0,
        "pct_from_low": (cur - lo) / lo * 100 if lo else 0.0,
    }


def momentum_12_1(closes: pd.Series, lookback: int = 252, skip: int = 21) -> float | None:
    """
    Academic '12-1' momentum: total return over the trailing `lookback` days
    EXCLUDING the most recent `skip` days (avoids short-term reversal noise).
    Returns percent, or None when history is insufficient.
    """
    if len(closes) < lookback + 1:
        return None
    end = float(closes.iloc[-1 - skip])
    start = float(closes.iloc[-1 - lookback])
    if start == 0:
        return None
    return (end / start - 1) * 100


def momentum_simple(closes: pd.Series, n: int = 10) -> float | None:
    """Plain trailing-n-day percent return."""
    if len(closes) <= n:
        return None
    prev = float(closes.iloc[-n - 1])
    if prev == 0:
        return None
    return (float(closes.iloc[-1]) / prev - 1) * 100


def sma(series: pd.Series, n: int) -> pd.Series:
    return series.rolling(n, min_periods=n).mean()
