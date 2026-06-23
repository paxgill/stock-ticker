import os
import re
import csv
import json
import io
import threading
import time as _time
import logging
from datetime import datetime, date, timedelta

import requests
from flask import Flask, jsonify, request, Response
from flask_cors import CORS
import yfinance as yf
import pandas as pd

from models import (db, WatchlistItem, AnalysisProfile, PortfolioPosition, TradeLog,
                    Preference, FMPCache, QuoteCache, AlertEvent,
                    SignalSnapshot, PricesDaily)
from analysis import (
    analyze_ticker, compute_rsi, compute_rvol, rvol_tier,
    smooth_prices, detect_surge_crash, detect_regime, replay_signals,
    PRESETS, preset_to_profile, DISCLAIMER, DEFAULT_PROFILE,
)
from backtest import run_backtest
from portfolio import fifo_preview, build_open_lots, blended_cost
import snapshots as snap
import scoreboard as sb
import ai
from narratives import fetch_index_data, generate_market_summary, generate_trade_description, generate_options_plan

# ─── App / DB Setup ──────────────────────────────────────────────────────────
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DATA_DIR, exist_ok=True)

app = Flask(__name__, static_folder="static", static_url_path="")
CORS(app)

# Structured stdout logging (Railway/Render capture stdout)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
app.logger.setLevel(logging.INFO)


@app.after_request
def _log_request(resp):
    try:
        app.logger.info("%s %s -> %s", request.method, request.path, resp.status_code)
    except Exception:
        pass
    return resp

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    f"sqlite:///{os.path.join(DATA_DIR, 'terminal.db')}"
)
# Render / Railway provide postgres:// — SQLAlchemy needs postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db.init_app(app)

def _existing_columns(table_name: str) -> set:
    """Column names for a table, dialect-aware (SQLite PRAGMA / Postgres catalog)."""
    try:
        insp = db.inspect(db.engine)
        return {c["name"] for c in insp.get_columns(table_name)}
    except Exception:
        return set()


# Additive schema migrations applied at startup — no Alembic dependency.
# Each entry is (table, column, column_ddl). New columns must be nullable or
# carry a default so existing rows remain valid. Both SQLite and Postgres are
# supported: we check the live column set first (inspector reads PRAGMA
# table_info on SQLite, the information_schema on Postgres) and only ALTER when
# the column is genuinely missing — which is effectively "ADD COLUMN IF NOT
# EXISTS" on every dialect.
SCHEMA_MIGRATIONS = [
    # Phase 2: profiles created from a research preset remember which one.
    ("analysis_profiles", "preset_key", "VARCHAR(40)"),
    # Phase 4: free-form tags on watchlist items.
    ("watchlist", "tags", "VARCHAR(255)"),
]


def migrate_schema():
    """Idempotently add any columns introduced after the initial deploy."""
    for table, column, ddl in SCHEMA_MIGRATIONS:
        if column in _existing_columns(table):
            continue
        try:
            db.session.execute(db.text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
            db.session.commit()
        except Exception:
            db.session.rollback()


with app.app_context():
    db.create_all()
    migrate_schema()
    # Prune FMP cache rows older than 30 days to prevent unbounded growth
    try:
        cutoff = datetime.utcnow() - timedelta(days=30)
        FMPCache.query.filter(FMPCache.cached_at < cutoff).delete()
        db.session.commit()
    except Exception:
        db.session.rollback()


# ─── Helpers ─────────────────────────────────────────────────────────────────
def fmt_vol(vol):
    if vol is None:
        return "N/A"
    if vol >= 1_000_000_000:
        return f"{vol/1_000_000_000:.1f}B"
    if vol >= 1_000_000:
        return f"{vol/1_000_000:.1f}M"
    if vol >= 1_000:
        return f"{vol/1_000:.1f}K"
    return str(int(vol))


def get_pref(key, default=None):
    p = Preference.query.filter_by(key=key).first()
    return p.value if p else default


def set_pref(key, value):
    p = Preference.query.filter_by(key=key).first()
    if p:
        p.value = str(value)
    else:
        db.session.add(Preference(key=key, value=str(value)))
    db.session.commit()


def get_active_profile_dict():
    p = AnalysisProfile.query.filter_by(is_active=True).first()
    if p:
        return p.to_dict()
    return DEFAULT_PROFILE.copy()


# ─── FMP Fundamentals ────────────────────────────────────────────────────────
FMP_API_KEY = os.environ.get("FMP_API_KEY", "")
FMP_BASE    = "https://financialmodelingprep.com/api/v3"

# ─── Finnhub News ─────────────────────────────────────────────────────────────
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY", "")
FINNHUB_BASE    = "https://finnhub.io/api/v1"

# ─── Sector ETF Map ───────────────────────────────────────────────────────────
SECTOR_ETFS = {
    "XLK":  "Technology",
    "XLF":  "Financials",
    "XLE":  "Energy",
    "XLV":  "Health Care",
    "XLI":  "Industrials",
    "XLY":  "Consumer Discret.",
    "XLP":  "Consumer Staples",
    "XLU":  "Utilities",
    "XLRE": "Real Estate",
    "XLB":  "Materials",
    "XLC":  "Communication",
}

# ─── Strategy Presets ─────────────────────────────────────────────────────────
STRATEGY_PRESETS = [
    # ── Trend ──────────────────────────────────────────────────────────────────
    {
        "name": "Classic Balanced",
        "type": "Trend",
        "complexity": "Beginner",
        "expected_return": "Market",
        "description": "Equal weight across all four signals. A safe starting point for most investors.",
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
    },
    {
        "name": "Golden Cross Hunter",
        "type": "Trend",
        "complexity": "Intermediate",
        "expected_return": "Above Market",
        "description": "Heavy MA weighting to catch Golden/Death Cross events early with volume confirmation.",
        "risk_tolerance": "Aggressive",
        "horizon": "Swing",
        "ma_weight": 0.50,
        "volume_weight": 0.20,
        "rsi_weight": 0.15,
        "momentum_weight": 0.15,
        "volume_spike_threshold": 1.5,
        "rsi_overbought": 70.0,
        "rsi_oversold": 30.0,
        "momentum_days": 10,
        "max_trades_per_day": 5,
    },
    {
        "name": "Swing Trader",
        "type": "Trend",
        "complexity": "Beginner",
        "expected_return": "Market",
        "description": "MA-biased with balanced volume and RSI for 3–10 day swing trades.",
        "risk_tolerance": "Moderate",
        "horizon": "Swing",
        "ma_weight": 0.35,
        "volume_weight": 0.25,
        "rsi_weight": 0.20,
        "momentum_weight": 0.20,
        "volume_spike_threshold": 1.5,
        "rsi_overbought": 70.0,
        "rsi_oversold": 30.0,
        "momentum_days": 10,
        "max_trades_per_day": 3,
    },
    {
        "name": "Trend Following",
        "type": "Trend",
        "complexity": "Intermediate",
        "expected_return": "Above Market",
        "description": "MA + momentum combo for sustained trending instruments.",
        "risk_tolerance": "Aggressive",
        "horizon": "Swing",
        "ma_weight": 0.40,
        "volume_weight": 0.20,
        "rsi_weight": 0.10,
        "momentum_weight": 0.30,
        "volume_spike_threshold": 1.5,
        "rsi_overbought": 70.0,
        "rsi_oversold": 30.0,
        "momentum_days": 10,
        "max_trades_per_day": 5,
    },
    {
        "name": "Momentum Surfer",
        "type": "Trend",
        "complexity": "Intermediate",
        "expected_return": "Above Market",
        "description": "Prioritises price momentum for fast-moving trending markets.",
        "risk_tolerance": "Aggressive",
        "horizon": "Short-term",
        "ma_weight": 0.15,
        "volume_weight": 0.20,
        "rsi_weight": 0.10,
        "momentum_weight": 0.55,
        "volume_spike_threshold": 1.5,
        "rsi_overbought": 70.0,
        "rsi_oversold": 30.0,
        "momentum_days": 5,
        "max_trades_per_day": 5,
    },
    {
        "name": "Volume Breakout",
        "type": "Trend",
        "complexity": "Intermediate",
        "expected_return": "Above Market",
        "description": "Volume-confirmation breakout strategy; requires 2× average volume before signalling.",
        "risk_tolerance": "Aggressive",
        "horizon": "Short-term",
        "ma_weight": 0.15,
        "volume_weight": 0.55,
        "rsi_weight": 0.15,
        "momentum_weight": 0.15,
        "volume_spike_threshold": 2.0,
        "rsi_overbought": 70.0,
        "rsi_oversold": 30.0,
        "momentum_days": 5,
        "max_trades_per_day": 5,
    },
    # ── Mean Reversion ─────────────────────────────────────────────────────────
    {
        "name": "RSI Mean Reversion",
        "type": "Mean Reversion",
        "complexity": "Intermediate",
        "expected_return": "Market",
        "description": "High RSI weight with wide OB/OS bands to trade mean-reversion extremes.",
        "risk_tolerance": "Moderate",
        "horizon": "Short-term",
        "ma_weight": 0.10,
        "volume_weight": 0.10,
        "rsi_weight": 0.60,
        "momentum_weight": 0.20,
        "volume_spike_threshold": 1.5,
        "rsi_overbought": 75.0,
        "rsi_oversold": 25.0,
        "momentum_days": 5,
        "max_trades_per_day": 5,
    },
    {
        "name": "Oversold Bounce",
        "type": "Mean Reversion",
        "complexity": "Beginner",
        "expected_return": "Market",
        "description": "Hunts oversold setups with volume confirmation for short-term bounce trades.",
        "risk_tolerance": "Moderate",
        "horizon": "Short-term",
        "ma_weight": 0.10,
        "volume_weight": 0.20,
        "rsi_weight": 0.55,
        "momentum_weight": 0.15,
        "volume_spike_threshold": 1.8,
        "rsi_overbought": 70.0,
        "rsi_oversold": 28.0,
        "momentum_days": 5,
        "max_trades_per_day": 3,
    },
    # ── Calendar ───────────────────────────────────────────────────────────────
    {
        "name": "Earnings Season",
        "type": "Calendar",
        "complexity": "Advanced",
        "expected_return": "High",
        "description": "Short-term momentum surge around quarterly earnings catalysts with high-volume filter.",
        "risk_tolerance": "Aggressive",
        "horizon": "Short-term",
        "ma_weight": 0.10,
        "volume_weight": 0.30,
        "rsi_weight": 0.15,
        "momentum_weight": 0.45,
        "volume_spike_threshold": 2.0,
        "rsi_overbought": 80.0,
        "rsi_oversold": 20.0,
        "momentum_days": 3,
        "max_trades_per_day": 5,
    },
    {
        "name": "Sector Rotation",
        "type": "Calendar",
        "complexity": "Advanced",
        "expected_return": "Above Market",
        "description": "Long-horizon rotation using momentum + MA to rotate between sectors seasonally.",
        "risk_tolerance": "Moderate",
        "horizon": "Long-term",
        "ma_weight": 0.30,
        "volume_weight": 0.20,
        "rsi_weight": 0.20,
        "momentum_weight": 0.30,
        "volume_spike_threshold": 1.5,
        "rsi_overbought": 70.0,
        "rsi_oversold": 30.0,
        "momentum_days": 20,
        "max_trades_per_day": 2,
    },
    # ── TAA ────────────────────────────────────────────────────────────────────
    {
        "name": "Conservative Long-Term",
        "type": "TAA",
        "complexity": "Beginner",
        "expected_return": "Below Market",
        "description": "Low-turnover defensive posture. Favours MA trend signals; avoids chasing moves.",
        "risk_tolerance": "Conservative",
        "horizon": "Long-term",
        "ma_weight": 0.45,
        "volume_weight": 0.20,
        "rsi_weight": 0.20,
        "momentum_weight": 0.15,
        "volume_spike_threshold": 2.0,
        "rsi_overbought": 70.0,
        "rsi_oversold": 30.0,
        "momentum_days": 20,
        "max_trades_per_day": 1,
    },
    {
        "name": "Tactical Asset Allocation",
        "type": "TAA",
        "complexity": "Intermediate",
        "expected_return": "Market",
        "description": "Dynamically risk-managed allocation using MA trend + RSI filters.",
        "risk_tolerance": "Conservative",
        "horizon": "Long-term",
        "ma_weight": 0.35,
        "volume_weight": 0.15,
        "rsi_weight": 0.25,
        "momentum_weight": 0.25,
        "volume_spike_threshold": 1.8,
        "rsi_overbought": 68.0,
        "rsi_oversold": 32.0,
        "momentum_days": 20,
        "max_trades_per_day": 2,
    },
    # ── Volatility ─────────────────────────────────────────────────────────────
    {
        "name": "High Frequency Scalp",
        "type": "Volatility",
        "complexity": "Advanced",
        "expected_return": "High",
        "description": "Fast 3-day signals with volume + RSI filters for intraday momentum scalps.",
        "risk_tolerance": "Aggressive",
        "horizon": "Short-term",
        "ma_weight": 0.15,
        "volume_weight": 0.40,
        "rsi_weight": 0.30,
        "momentum_weight": 0.15,
        "volume_spike_threshold": 1.2,
        "rsi_overbought": 65.0,
        "rsi_oversold": 35.0,
        "momentum_days": 3,
        "max_trades_per_day": 10,
    },
    {
        "name": "Volatility Filter",
        "type": "Volatility",
        "complexity": "Advanced",
        "expected_return": "High",
        "description": "Requires extreme volume spikes (2.5×) as a volatility gate before triggering signals.",
        "risk_tolerance": "Aggressive",
        "horizon": "Short-term",
        "ma_weight": 0.10,
        "volume_weight": 0.50,
        "rsi_weight": 0.25,
        "momentum_weight": 0.15,
        "volume_spike_threshold": 2.5,
        "rsi_overbought": 70.0,
        "rsi_oversold": 30.0,
        "momentum_days": 5,
        "max_trades_per_day": 5,
    },
]

# ─── News Sentiment Keywords ───────────────────────────────────────────────────
_BULL = {'beat','beats','growth','surge','surges','rally','rallies','record',
         'profit','profits','rise','rises','strong','buy','upgrade','upgraded',
         'bullish','gain','gains','positive','exceeds','raises','outperform',
         'boost','boosted','higher','soar','soars','lifts','top'}
_BEAR = {'miss','misses','loss','losses','drop','drops','fall','falls','decline',
         'declines','down','sell','downgrade','downgraded','bearish','cut','cuts',
         'reduce','concern','concerns','risk','warning','disappoints','below',
         'weak','weakness','layoff','layoffs','slump','slumps','disappointing'}


def _fmp_cache_get(symbol: str, endpoint: str, ttl_hours: float = 24):
    row = FMPCache.query.filter_by(symbol=symbol, endpoint=endpoint).first()
    if row is None:
        return None
    if datetime.utcnow() - row.cached_at > timedelta(hours=ttl_hours):
        return None
    return json.loads(row.data) if row.data else None


def _fmp_cache_set(symbol: str, endpoint: str, data):
    row = FMPCache.query.filter_by(symbol=symbol, endpoint=endpoint).first()
    if row:
        row.data = json.dumps(data)
        row.cached_at = datetime.utcnow()
    else:
        db.session.add(FMPCache(symbol=symbol, endpoint=endpoint, data=json.dumps(data)))
    db.session.commit()


# Let the AI layer reuse the existing cache table for response caching.
ai.set_cache_backend(_fmp_cache_get, _fmp_cache_set)


def _fmp_get(path: str, symbol: str, cache_key: str, params: dict = None):
    cached = _fmp_cache_get(symbol, cache_key)
    if cached is not None:
        return cached
    try:
        p = dict(params or {})
        p["apikey"] = FMP_API_KEY
        resp = requests.get(f"{FMP_BASE}/{path}", params=p, timeout=8)
        if resp.status_code == 200:
            data = resp.json()
            _fmp_cache_set(symbol, cache_key, data)
            return data
    except Exception:
        pass
    return None


def fetch_fmp_fundamentals(symbol: str, yf_ticker=None) -> dict:
    """Return fundamentals dict. Uses FMP when key is set, yfinance otherwise."""
    base = {}
    if yf_ticker is not None:
        try:
            info = yf_ticker.info
            base = {
                "long_name":      info.get("longName"),
                "sector":         info.get("sector"),
                "market_cap":     info.get("marketCap"),
                "beta":           info.get("beta"),
                "52w_high":       info.get("fiftyTwoWeekHigh"),
                "52w_low":        info.get("fiftyTwoWeekLow"),
                "dividend_yield": info.get("dividendYield"),
                "forward_pe":     info.get("forwardPE"),
                "trailing_pe":    info.get("trailingPE"),
            }
        except Exception:
            pass

    if not FMP_API_KEY:
        try:
            info = yf_ticker.info if yf_ticker else {}
            return {
                **base,
                "revenue_growth":     info.get("revenueGrowth"),
                "earnings_growth":    info.get("earningsGrowth"),
                "profit_margins":     info.get("profitMargins"),
                "revenue_3q_growth":  None,
                "earnings_3q_growth": None,
                "fcf_positive":       None,
                "data_source":        "yfinance",
            }
        except Exception:
            return {**base, "data_source": "yfinance"}

    sym = symbol.upper()
    income   = _fmp_get(f"income-statement/{sym}",    sym, "income_q",   {"period": "quarter", "limit": 5})
    cashflow = _fmp_get(f"cash-flow-statement/{sym}", sym, "cashflow_q", {"period": "quarter", "limit": 5})
    ratios   = _fmp_get(f"ratios-ttm/{sym}",          sym, "ratios_ttm")

    fund = dict(base)
    fund["data_source"] = "fmp"

    revenue_3q = earnings_3q = fcf_positive = None
    revenue_growth = earnings_growth = profit_margins = None

    if income and len(income) >= 4:
        revs = [q.get("revenue", 0) or 0 for q in income[:4]]
        nets = [q.get("netIncome", 0) or 0 for q in income[:4]]
        revenue_3q  = revs[0] > revs[1] and revs[1] > revs[2]
        earnings_3q = nets[0] > nets[1] and nets[1] > nets[2]
        if len(income) >= 5 and revs[4] if len(income) > 4 else revs[3]:
            base_rev = revs[4] if len(income) > 4 else revs[3]
            if base_rev:
                revenue_growth = (revs[0] - base_rev) / abs(base_rev)
        base_net = nets[4] if len(income) > 4 else nets[3]
        if base_net:
            earnings_growth = (nets[0] - base_net) / abs(base_net)

    if cashflow and len(cashflow) >= 1:
        fcf = cashflow[0].get("freeCashFlow")
        if fcf is not None:
            fcf_positive = fcf > 0

    if ratios and len(ratios) >= 1:
        r = ratios[0]
        if not fund.get("trailing_pe"):
            fund["trailing_pe"] = r.get("peRatioTTM")
        profit_margins = r.get("netProfitMarginTTM")

    fund.update({
        "revenue_growth":     revenue_growth,
        "earnings_growth":    earnings_growth,
        "profit_margins":     profit_margins,
        "revenue_3q_growth":  revenue_3q,
        "earnings_3q_growth": earnings_3q,
        "fcf_positive":       fcf_positive,
    })
    return fund


def news_sentiment(text: str) -> str:
    words = set(text.lower().split())
    bull = len(words & _BULL)
    bear = len(words & _BEAR)
    if bull > bear:  return "bull"
    if bear > bull:  return "bear"
    return "neutral"


def _fetch_earnings_date(symbol: str) -> str | None:
    """Get next earnings date string (ISO), cached 24 h."""
    cached = _fmp_cache_get(symbol, "earnings")
    if cached is not None:
        return cached.get("earn_date")
    earn_date = None
    try:
        cal = yf.Ticker(symbol).calendar
        today = date.today()
        raw_dates = []
        if isinstance(cal, dict):
            raw = cal.get("Earnings Date", [])
            raw_dates = raw if isinstance(raw, list) else [raw]
        elif hasattr(cal, "loc"):
            try:
                raw = cal.loc["Earnings Date"]
                raw_dates = raw.tolist() if hasattr(raw, "tolist") else [raw]
            except (KeyError, TypeError):
                pass
        for ed in raw_dates:
            try:
                if hasattr(ed, "date"):
                    ed = ed.date()
                elif isinstance(ed, str):
                    ed = date.fromisoformat(str(ed)[:10])
                else:
                    continue
                if ed >= today and (earn_date is None or ed < earn_date):
                    earn_date = ed
            except Exception:
                continue
        earn_date = earn_date.isoformat() if earn_date else None
    except Exception:
        pass
    _fmp_cache_set(symbol, "earnings", {"earn_date": earn_date})
    return earn_date


def fetch_history(symbol: str, period: str = "65d") -> pd.DataFrame | None:
    try:
        t = yf.Ticker(symbol.upper())
        hist = t.history(period=period)
        return hist if not hist.empty else None
    except Exception:
        return None


# ─── Static / Root ───────────────────────────────────────────────────────────
@app.route("/")
def index():
    return app.send_static_file("index.html")


# ─── Ticker Validate / Search / Quote ────────────────────────────────────────
@app.route("/api/validate/<ticker>")
def validate_ticker(ticker):
    try:
        t = yf.Ticker(ticker.upper())
        fi = t.fast_info            # fast_info has no blocking HTTP call
        price = fi.last_price
        if not price or price == 0:
            return jsonify({"valid": False})
        # Never call t.info here — it has no timeout and can block 30+ seconds
        long_name = ticker.upper()
        return jsonify({"valid": True, "name": long_name, "price": round(price, 2)})
    except Exception as e:
        return jsonify({"valid": False, "error": str(e)})


def _quote_from_hist(symbol: str, hist: pd.DataFrame) -> dict:
    """Build the quote payload for one symbol from its OHLCV history."""
    closes = hist["Close"].astype(float)
    volumes = hist["Volume"].astype(float)
    current = float(closes.iloc[-1])
    prev = float(closes.iloc[-2]) if len(closes) > 1 else current
    change = current - prev
    pct = (change / prev * 100) if prev else 0
    ma20 = float(closes.tail(20).mean()) if len(closes) >= 20 else None
    ma50 = float(closes.tail(50).mean()) if len(closes) >= 50 else None
    rsi = compute_rsi(closes) if len(closes) >= 15 else None
    vol = float(volumes.iloc[-1])
    avg_vol20 = float(volumes.tail(20).mean()) if len(volumes) >= 20 else vol
    vol_ratio = round(vol / avg_vol20, 2) if avg_vol20 > 0 else 1.0
    rvol = round(compute_rvol(volumes), 2)
    n = 10
    mom_pct = round((current / float(closes.iloc[-n - 1]) - 1) * 100, 2) if len(closes) > n else None
    return {
        "symbol": symbol.upper(), "price": round(current, 2), "change": round(change, 2),
        "pct_change": round(pct, 2), "volume": fmt_vol(int(vol)), "volume_raw": int(vol),
        "vol_ratio": vol_ratio, "rvol": rvol, "rvol_tier": rvol_tier(rvol),
        "ma20": round(ma20, 2) if ma20 else None, "ma50": round(ma50, 2) if ma50 else None,
        "above_ma20": current > ma20 if ma20 else None,
        "above_ma50": current > ma50 if ma50 else None,
        "rsi": round(rsi, 1) if rsi else None, "momentum_pct": mom_pct,
        "timestamp": datetime.now().isoformat(),
    }


def _batch_history(symbols: list, period: str = "65d") -> dict:
    """
    Fetch OHLCV for many symbols in ONE yfinance call. Returns {SYMBOL: DataFrame}.
    Falls back to an empty dict on failure (callers then use cache).
    """
    out = {}
    syms = [s.upper() for s in symbols]
    if not syms:
        return out
    try:
        df = yf.download(syms, period=period, interval="1d", group_by="ticker",
                         auto_adjust=True, progress=False, threads=True)
        if df is None or df.empty:
            return out
        if len(syms) == 1:
            # single symbol → flat columns
            sub = df.dropna(how="all")
            if not sub.empty:
                out[syms[0]] = sub
        else:
            for s in syms:
                if s in df.columns.get_level_values(0):
                    sub = df[s].dropna(how="all")
                    if not sub.empty and "Close" in sub.columns:
                        out[s] = sub
    except Exception:
        pass
    return out


def _quote_cache_get(symbol: str):
    return QuoteCache.query.filter_by(symbol=symbol.upper()).first()


def _quote_cache_put(symbol: str, payload: dict):
    row = QuoteCache.query.filter_by(symbol=symbol.upper()).first()
    data = json.dumps(payload)
    if row:
        row.payload, row.fetched_at = data, datetime.utcnow()
    else:
        db.session.add(QuoteCache(symbol=symbol.upper(), payload=data))
    db.session.commit()


@app.route("/api/quote", methods=["POST"])
def get_quotes():
    """
    Cache-first, batch-fetch quotes. Serves cached quotes fresher than the
    refresh interval, batch-fetches the rest in one yfinance call, and on a
    Yahoo outage serves stale data (≤60 min) flagged with "stale": true.
    """
    data = request.json or {}
    tickers = [t.upper() for t in data.get("tickers", [])]
    if not tickers:
        return jsonify({})

    try:
        fresh_secs = int(get_pref("interval", "300"))
    except (TypeError, ValueError):
        fresh_secs = 300
    fresh_secs = max(30, fresh_secs)
    now = datetime.utcnow()

    results, to_fetch = {}, []
    for sym in tickers:
        row = _quote_cache_get(sym)
        if row and row.payload and (now - row.fetched_at).total_seconds() < fresh_secs:
            results[sym] = json.loads(row.payload)
        else:
            to_fetch.append(sym)

    if to_fetch:
        fetched = _batch_history(to_fetch, period="65d")
        for sym in to_fetch:
            hist = fetched.get(sym)
            if hist is not None and not hist.empty:
                try:
                    q = _quote_from_hist(sym, hist)
                    _quote_cache_put(sym, q)
                    results[sym] = q
                    continue
                except Exception:
                    pass
            # Fetch miss → serve stale cache (≤60 min) with a flag, else error
            row = _quote_cache_get(sym)
            if row and row.payload and (now - row.fetched_at).total_seconds() <= 3600:
                stale = json.loads(row.payload)
                stale["stale"] = True
                results[sym] = stale
            else:
                results[sym] = {"error": "No data", "symbol": sym, "stale": bool(row)}

    return jsonify(results)


# ─── Watchlist ────────────────────────────────────────────────────────────────
@app.route("/api/watchlist", methods=["GET"])
def get_watchlist():
    items = WatchlistItem.query.order_by(WatchlistItem.created_at).all()
    return jsonify([i.to_dict() for i in items])


@app.route("/api/watchlist", methods=["POST"])
def add_watchlist():
    d = request.json or {}
    sym = d.get("symbol", "").upper()
    if not sym:
        return jsonify({"error": "symbol required"}), 400
    existing = WatchlistItem.query.filter_by(symbol=sym).first()
    if existing:
        return jsonify({"error": "already exists"}), 409
    item = WatchlistItem(
        symbol=sym,
        name=d.get("name", ""),
        tier=d.get("tier", "Active Watch"),
        notes=d.get("notes", ""),
        alert_direction=d.get("alert_direction"),
        alert_price=d.get("alert_price"),
    )
    db.session.add(item)
    db.session.commit()
    return jsonify(item.to_dict()), 201


@app.route("/api/watchlist/<symbol>", methods=["PUT"])
def update_watchlist(symbol):
    item = WatchlistItem.query.filter_by(symbol=symbol.upper()).first_or_404()
    d = request.json or {}
    for field in ("name", "tier", "notes", "alert_direction", "alert_price"):
        if field in d:
            setattr(item, field, d[field])
    if "tags" in d:
        tags = d["tags"]
        if isinstance(tags, list):
            tags = ",".join(str(t).strip() for t in tags if str(t).strip())
        item.tags = (str(tags) or "")[:255]
    db.session.commit()
    return jsonify(item.to_dict())


@app.route("/api/watchlist/<symbol>", methods=["DELETE"])
def delete_watchlist(symbol):
    item = WatchlistItem.query.filter_by(symbol=symbol.upper()).first_or_404()
    db.session.delete(item)
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/watchlist/import", methods=["POST"])
def import_watchlist():
    """Bulk-add tickers from pasted text (comma/newline/space separated)."""
    raw = str((request.json or {}).get("text", ""))
    syms = [s for s in re.split(r"[\s,;]+", raw.upper()) if s and re.fullmatch(r"[A-Z0-9.\-]{1,12}", s)]
    results = []
    for sym in dict.fromkeys(syms):   # dedupe, keep order
        if WatchlistItem.query.filter_by(symbol=sym).first():
            results.append({"symbol": sym, "status": "exists"})
            continue
        ok, price = True, None
        try:
            price = yf.Ticker(sym).fast_info.last_price
            ok = bool(price)
        except Exception:
            ok = False
        if not ok:
            results.append({"symbol": sym, "status": "invalid"})
            continue
        db.session.add(WatchlistItem(symbol=sym, name=sym))
        results.append({"symbol": sym, "status": "added", "price": round(float(price), 2)})
    db.session.commit()
    return jsonify({"results": results,
                    "added": sum(1 for r in results if r["status"] == "added")})


@app.route("/api/portfolio/import", methods=["POST"])
def import_portfolio():
    """Bulk-add positions from CSV rows: symbol,shares,cost_basis[,date]."""
    raw = str((request.json or {}).get("text", ""))
    results = []
    for ln in raw.splitlines():
        ln = ln.strip()
        if not ln or ln.lower().startswith("symbol"):
            continue
        parts = [p.strip() for p in ln.split(",")]
        if len(parts) < 3:
            results.append({"row": ln, "status": "bad format"})
            continue
        sym = parts[0].upper()
        try:
            shares = float(parts[1])
            cost = float(parts[2])
        except ValueError:
            results.append({"row": ln, "status": "non-numeric shares/cost"})
            continue
        acq = None
        if len(parts) >= 4 and parts[3]:
            try:
                acq = datetime.strptime(parts[3], "%Y-%m-%d").date()
            except ValueError:
                acq = None
        db.session.add(PortfolioPosition(symbol=sym, shares=shares, cost_basis=cost, date_acquired=acq))
        results.append({"symbol": sym, "status": "added", "shares": shares, "cost_basis": cost})
    db.session.commit()
    return jsonify({"results": results,
                    "added": sum(1 for r in results if r["status"] == "added")})


@app.route("/api/portfolio/export/csv")
def export_portfolio_csv():
    """Portfolio CSV export (matches the journal export)."""
    positions = PortfolioPosition.query.order_by(PortfolioPosition.symbol).all()
    if not positions:
        return Response("No positions", mimetype="text/plain")
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=["Symbol", "Shares", "CostBasis", "DateAcquired", "Notes"])
    writer.writeheader()
    for p in positions:
        writer.writerow({"Symbol": p.symbol, "Shares": p.shares, "CostBasis": p.cost_basis,
                         "DateAcquired": p.date_acquired.isoformat() if p.date_acquired else "",
                         "Notes": p.notes or ""})
    return Response(output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=portfolio.csv"})


# ─── Strategy Presets ─────────────────────────────────────────────────────────
@app.route("/api/strategies/presets", methods=["GET"])
def get_strategy_presets():
    return jsonify({"strategies": STRATEGY_PRESETS})


# ─── Analysis Profiles ────────────────────────────────────────────────────────
@app.route("/api/profiles", methods=["GET"])
def get_profiles():
    profiles = AnalysisProfile.query.order_by(AnalysisProfile.created_at).all()
    return jsonify([p.to_dict() for p in profiles])


def _validate_weights(ma, vol, rsi, mom):
    """The four signal weights must sum to ~1.0 (UI shows them as % summing to 100)."""
    try:
        total = float(ma) + float(vol) + float(rsi) + float(mom)
    except (TypeError, ValueError):
        return "Signal weights must be numbers"
    if abs(total - 1.0) > 0.01:
        return f"Signal weights must sum to 100% (got {round(total * 100)}%)"
    return None


@app.route("/api/profiles", methods=["POST"])
def create_profile():
    d = request.json or {}
    rsi_ob = float(d.get("rsi_overbought", 70.0))
    rsi_os = float(d.get("rsi_oversold",  30.0))
    if rsi_os >= rsi_ob:
        return jsonify({"error": "RSI Oversold must be less than RSI Overbought"}), 400
    werr = _validate_weights(
        d.get("ma_weight", 0.25), d.get("volume_weight", 0.25),
        d.get("rsi_weight", 0.25), d.get("momentum_weight", 0.25),
    )
    if werr:
        return jsonify({"error": werr}), 400
    profile = AnalysisProfile(
        name=d.get("name", "New Profile"),
        risk_tolerance=d.get("risk_tolerance", "Moderate"),
        horizon=d.get("horizon", "Swing"),
        ma_weight=d.get("ma_weight", 0.25),
        volume_weight=d.get("volume_weight", 0.25),
        rsi_weight=d.get("rsi_weight", 0.25),
        momentum_weight=d.get("momentum_weight", 0.25),
        volume_spike_threshold=d.get("volume_spike_threshold", 1.5),
        rsi_overbought=rsi_ob,
        rsi_oversold=rsi_os,
        momentum_days=d.get("momentum_days", 10),
        max_trades_per_day=d.get("max_trades_per_day", 3),
        is_active=d.get("is_active", False),
    )
    if profile.is_active:
        AnalysisProfile.query.update({"is_active": False})
    db.session.add(profile)
    db.session.commit()
    return jsonify(profile.to_dict()), 201


@app.route("/api/profiles/<int:pid>", methods=["PUT"])
def update_profile(pid):
    profile = AnalysisProfile.query.get_or_404(pid)
    d = request.json or {}
    # Validate RSI thresholds when both are present or one is being updated
    rsi_ob = float(d.get("rsi_overbought", profile.rsi_overbought))
    rsi_os = float(d.get("rsi_oversold",  profile.rsi_oversold))
    if rsi_os >= rsi_ob:
        return jsonify({"error": "RSI Oversold must be less than RSI Overbought"}), 400
    # Validate the merged weight set (existing values + any incoming overrides)
    werr = _validate_weights(
        d.get("ma_weight",       profile.ma_weight),
        d.get("volume_weight",   profile.volume_weight),
        d.get("rsi_weight",      profile.rsi_weight),
        d.get("momentum_weight", profile.momentum_weight),
    )
    if werr:
        return jsonify({"error": werr}), 400
    fields = [
        "name", "risk_tolerance", "horizon", "ma_weight", "volume_weight",
        "rsi_weight", "momentum_weight", "volume_spike_threshold",
        "rsi_overbought", "rsi_oversold", "momentum_days", "max_trades_per_day",
    ]
    for f in fields:
        if f in d:
            setattr(profile, f, d[f])
    db.session.commit()
    return jsonify(profile.to_dict())


@app.route("/api/profiles/<int:pid>", methods=["DELETE"])
def delete_profile(pid):
    profile = AnalysisProfile.query.get_or_404(pid)
    db.session.delete(profile)
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/profiles/<int:pid>/activate", methods=["POST"])
def activate_profile(pid):
    AnalysisProfile.query.update({"is_active": False})
    profile = AnalysisProfile.query.get_or_404(pid)
    profile.is_active = True
    db.session.commit()
    return jsonify(profile.to_dict())


# ─── Research-grounded Presets (Phase 2d) ─────────────────────────────────────
@app.route("/api/presets")
def get_presets():
    """List the research-grounded strategy presets with their methodology notes."""
    return jsonify([
        {
            "key": p["key"], "name": p["name"], "research": p["research"],
            "believes": p["believes"], "risk_tolerance": p["risk_tolerance"],
            "horizon": p["horizon"],
            "weights": {
                "trend": p["ma_weight"], "momentum": p["momentum_weight"],
                "volume_flow": p["volume_weight"], "oscillator": p["rsi_weight"],
            },
            "requires_fundamentals": p.get("requires_fundamentals", False),
        }
        for p in PRESETS.values()
    ])


@app.route("/api/profiles/from-preset/<key>", methods=["POST"])
def create_profile_from_preset(key):
    """Create (and optionally activate) an analysis profile from a preset."""
    prof = preset_to_profile(key, (request.json or {}).get("name"))
    if not prof:
        return jsonify({"error": f"Unknown preset '{key}'"}), 404

    activate = bool((request.json or {}).get("is_active", True))
    profile = AnalysisProfile(
        name=prof["name"], risk_tolerance=prof["risk_tolerance"], horizon=prof["horizon"],
        ma_weight=prof["ma_weight"], volume_weight=prof["volume_weight"],
        rsi_weight=prof["rsi_weight"], momentum_weight=prof["momentum_weight"],
        volume_spike_threshold=prof["volume_spike_threshold"],
        rsi_overbought=prof["rsi_overbought"], rsi_oversold=prof["rsi_oversold"],
        momentum_days=prof["momentum_days"], max_trades_per_day=prof["max_trades_per_day"],
        preset_key=key, is_active=activate,
    )
    if activate:
        AnalysisProfile.query.update({"is_active": False})
    db.session.add(profile)
    db.session.commit()
    return jsonify(profile.to_dict()), 201


# ─── Compare Mode (Phase 4) ───────────────────────────────────────────────────
@app.route("/api/compare", methods=["POST"])
def compare_tickers():
    """Normalized (%-change from start) closing-price series for up to 4 tickers."""
    d = request.json or {}
    tickers = [t.upper() for t in d.get("tickers", []) if t][:4]
    rng = d.get("range", "1Y")
    period = {"1M": "1mo", "3M": "3mo", "6M": "6mo", "1Y": "1y"}.get(rng, "1y")
    if not tickers:
        return jsonify({"dates": [], "series": {}})
    data = _batch_history(tickers, period=period)
    per, all_dates = {}, set()
    for sym in tickers:
        h = data.get(sym)
        if h is None or h.empty:
            continue
        closes = h["Close"].astype(float)
        base = float(closes.iloc[0]) or 1.0
        pts = {}
        for idx, val in closes.items():
            dt = idx.date().isoformat() if hasattr(idx, "date") else str(idx)[:10]
            pts[dt] = round((float(val) / base - 1) * 100, 2)
        per[sym] = pts
        all_dates.update(pts.keys())
    dates = sorted(all_dates)
    series = {s: [per[s].get(dt) for dt in dates] for s in per}
    return jsonify({"dates": dates, "series": series})


# ─── Chart Data ──────────────────────────────────────────────────────────────
@app.route("/api/chart/<ticker>")
def chart_data(ticker):
    range_ = request.args.get("range", "1Y")
    period_map = {
        "1W": ("5d",  "1h"),
        "1M": ("1mo", "1d"),
        "1Y": ("1y",  "1d"),
        "3Y": ("3y",  "1wk"),
        "5Y": ("5y",  "1wk"),
    }
    period, interval = period_map.get(range_, ("1y", "1d"))

    try:
        t = yf.Ticker(ticker.upper())
        hist = t.history(period=period, interval=interval)
        if hist.empty:
            return jsonify({"error": "No data"}), 404

        # Dates — keep time for intraday
        if interval == "1h":
            dates = [d.strftime("%Y-%m-%d %H:%M") for d in hist.index]
        else:
            dates = [str(d.date()) if hasattr(d, "date") else str(d)[:10] for d in hist.index]

        closes  = hist["Close"].astype(float)
        volumes = hist["Volume"].astype(float)

        def rolling_ma(series, n):
            if len(series) < n:
                return [None] * len(series)
            res = series.rolling(n, min_periods=n).mean()
            return [round(float(v), 4) if not pd.isna(v) else None for v in res]

        ma20  = rolling_ma(closes, 20)
        ma50  = rolling_ma(closes, 50)
        ma200 = rolling_ma(closes, 200)

        avg_vol30 = float(volumes.tail(30).mean()) if len(volumes) >= 30 else float(volumes.mean())

        # Golden / Death cross annotations (MA50 vs MA200)
        annotations = []
        if len(closes) >= 201:
            ma50_s  = closes.rolling(50,  min_periods=50).mean()
            ma200_s = closes.rolling(200, min_periods=200).mean()
            for i in range(1, len(closes)):
                p50, c50   = ma50_s.iloc[i-1],  ma50_s.iloc[i]
                p200, c200 = ma200_s.iloc[i-1], ma200_s.iloc[i]
                if pd.isna(p50) or pd.isna(c50) or pd.isna(p200) or pd.isna(c200):
                    continue
                if p50 < p200 and c50 >= c200:
                    annotations.append({"date": dates[i], "type": "golden_cross",
                                        "label": "☀ Golden Cross", "price": round(float(closes.iloc[i]), 2)})
                elif p50 > p200 and c50 <= c200:
                    annotations.append({"date": dates[i], "type": "death_cross",
                                        "label": "☽ Death Cross",  "price": round(float(closes.iloc[i]), 2)})

        # MA convergence detection
        convergence = []
        try:
            if len(closes) >= 50:
                ma20_s = closes.rolling(20, min_periods=20).mean()
                ma50_s = closes.rolling(50, min_periods=50).mean()
                if not pd.isna(ma20_s.iloc[-1]) and not pd.isna(ma50_s.iloc[-1]) and len(closes) > 6:
                    gap_now = abs(float(ma20_s.iloc[-1]) - float(ma50_s.iloc[-1]))
                    gap_5d  = abs(float(ma20_s.iloc[-6]) - float(ma50_s.iloc[-6]))
                    if gap_5d > 0 and gap_now < gap_5d * 0.8:
                        convergence.append({"type": "ma20_ma50", "label": "MA20→MA50 Converging",
                                            "gap_pct": round(gap_now / float(closes.iloc[-1]) * 100, 2)})
            if len(closes) >= 200:
                ma50_s  = closes.rolling(50,  min_periods=50).mean()
                ma200_s = closes.rolling(200, min_periods=200).mean()
                if not pd.isna(ma50_s.iloc[-1]) and not pd.isna(ma200_s.iloc[-1]) and len(closes) > 11:
                    gap_now = abs(float(ma50_s.iloc[-1])  - float(ma200_s.iloc[-1]))
                    gap_10d = abs(float(ma50_s.iloc[-11]) - float(ma200_s.iloc[-11]))
                    if gap_10d > 0 and gap_now < gap_10d * 0.85:
                        convergence.append({"type": "ma50_ma200", "label": "MA50→MA200 Converging",
                                            "gap_pct": round(gap_now / float(closes.iloc[-1]) * 100, 2)})
        except Exception:
            pass

        fundamentals = fetch_fmp_fundamentals(ticker.upper(), t)

        # Strong buy: FMP 3Q checks when available, else yfinance growth flags
        if fundamentals.get("data_source") == "fmp":
            strong_buy = bool(
                fundamentals.get("revenue_3q_growth") is True and
                fundamentals.get("earnings_3q_growth") is True and
                fundamentals.get("fcf_positive") is True and
                len(convergence) > 0
            )
        else:
            strong_buy = bool(
                fundamentals.get("revenue_growth") and fundamentals["revenue_growth"] > 0 and
                fundamentals.get("earnings_growth") and fundamentals["earnings_growth"] > 0 and
                len(convergence) > 0
            )

        return jsonify({
            "symbol":       ticker.upper(),
            "range":        range_,
            "dates":        dates,
            "open":         [round(float(v), 4) for v in hist["Open"]],
            "high":         [round(float(v), 4) for v in hist["High"]],
            "low":          [round(float(v), 4) for v in hist["Low"]],
            "close":        [round(float(v), 4) for v in closes],
            "volume":       [int(v) for v in volumes],
            "avg_vol30":    round(avg_vol30),
            "ma20":         ma20,
            "ma50":         ma50,
            "ma200":        ma200,
            "annotations":  annotations,
            "convergence":  convergence,
            "fundamentals": fundamentals,
            "strong_buy":   strong_buy,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── Market Summary ───────────────────────────────────────────────────────────
@app.route("/api/market-summary")
def market_summary():
    indices = fetch_index_data()
    summary = generate_market_summary(indices)
    return jsonify({
        "summary": summary,
        "indices": indices,
        "generated_at": datetime.now().isoformat(),
    })


# ─── Suggestions ──────────────────────────────────────────────────────────────
@app.route("/api/suggestions")
def get_suggestions():
    watchlist = WatchlistItem.query.all()
    if not watchlist:
        return jsonify([])

    profile = get_active_profile_dict()
    results = []

    for item in watchlist:
        # 1y of history so MA200 / Golden Cross logic runs in analyze_ticker
        hist = fetch_history(item.symbol, period="1y")
        analysis = analyze_ticker(item.symbol, hist, profile)
        if analysis:
            analysis["name"] = item.name
            analysis["tier"] = item.tier
            analysis["description"] = generate_trade_description(analysis)
            results.append(analysis)

    # Sort: BUY first (desc confidence), then HOLD, then SELL
    # Unknown signal types get rank 999 so they sort to the end
    order = {"BUY": 0, "HOLD": 1, "SELL": 2}
    results.sort(key=lambda x: (order.get(x["signal"], 999), -x["confidence"]))
    return jsonify(results)


@app.route("/api/suggestions/export/csv")
def export_suggestions_csv():
    watchlist = WatchlistItem.query.all()
    profile = get_active_profile_dict()
    rows = []

    for item in watchlist:
        hist = fetch_history(item.symbol, period="1y")
        analysis = analyze_ticker(item.symbol, hist, profile)
        if analysis:
            rows.append({
                "Symbol": item.symbol,
                "Name": item.name,
                "Tier": item.tier,
                "Signal": analysis["signal"],
                "Confidence": f"{analysis['confidence']}%",
                "Price": analysis["price"],
                "Reasoning": analysis["reasoning"],
                "Profile": profile.get("name", "Default"),
                "Generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
            })

    # Mirror the trades export: plaintext message rather than a header-only file
    if not rows:
        return Response("No suggestions", mimetype="text/plain")

    fieldnames = ["Symbol", "Name", "Tier", "Signal", "Confidence",
                  "Price", "Reasoning", "Profile", "Generated"]
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=suggestions.csv"},
    )


# ─── Portfolio ────────────────────────────────────────────────────────────────
@app.route("/api/portfolio", methods=["GET"])
def get_portfolio():
    positions = PortfolioPosition.query.order_by(PortfolioPosition.created_at).all()
    return jsonify([p.to_dict() for p in positions])


@app.route("/api/portfolio/summary", methods=["POST"])
def portfolio_summary():
    """Accepts {quotes: {SYMBOL: {price, change}}} and returns aggregated stats."""
    d = request.json or {}
    quotes = d.get("quotes", {})
    positions = PortfolioPosition.query.all()

    total_value = 0.0
    total_invested = 0.0
    day_gain = 0.0
    best = worst = None

    for pos in positions:
        q = quotes.get(pos.symbol, {})
        price = q.get("price", pos.cost_basis)
        change = q.get("change", 0)
        value = pos.shares * price
        invested = pos.shares * pos.cost_basis
        pnl_pct = (value - invested) / invested * 100 if invested else 0

        total_value += value
        total_invested += invested
        day_gain += pos.shares * change

        entry = {"symbol": pos.symbol, "pnl_pct": pnl_pct}
        if best is None or pnl_pct > best["pnl_pct"]:
            best = entry
        if worst is None or pnl_pct < worst["pnl_pct"]:
            worst = entry

    total_pnl = total_value - total_invested
    total_pnl_pct = (total_pnl / total_invested * 100) if total_invested else 0

    return jsonify({
        "total_value": round(total_value, 2),
        "total_invested": round(total_invested, 2),
        "total_pnl": round(total_pnl, 2),
        "total_pnl_pct": round(total_pnl_pct, 2),
        "day_gain": round(day_gain, 2),
        "best_performer": best,
        # Omit worst_performer for single position — it would be identical to best
        "worst_performer": worst if len(positions) >= 2 else None,
    })


@app.route("/api/portfolio", methods=["POST"])
def add_position():
    d = request.json or {}
    pos = PortfolioPosition(
        symbol=d.get("symbol", "").upper(),
        shares=float(d.get("shares", 0)),
        cost_basis=float(d.get("cost_basis", 0)),
        date_acquired=datetime.strptime(d["date_acquired"], "%Y-%m-%d").date() if d.get("date_acquired") else None,
        notes=d.get("notes", ""),
    )
    db.session.add(pos)
    db.session.commit()
    return jsonify(pos.to_dict()), 201


@app.route("/api/portfolio/<int:pid>", methods=["PUT"])
def update_position(pid):
    pos = PortfolioPosition.query.get_or_404(pid)
    d = request.json or {}
    for f in ("shares", "cost_basis", "notes"):
        if f in d:
            setattr(pos, f, d[f])
    if "date_acquired" in d and d["date_acquired"]:
        pos.date_acquired = datetime.strptime(d["date_acquired"], "%Y-%m-%d").date()
    db.session.commit()
    return jsonify(pos.to_dict())


@app.route("/api/portfolio/<int:pid>", methods=["DELETE"])
def delete_position(pid):
    pos = PortfolioPosition.query.get_or_404(pid)
    db.session.delete(pos)
    db.session.commit()
    return jsonify({"ok": True})


# ─── Trade Journal ────────────────────────────────────────────────────────────
@app.route("/api/trades", methods=["GET"])
def get_trades():
    q = TradeLog.query
    if request.args.get("symbol"):
        q = q.filter_by(symbol=request.args["symbol"].upper())
    if request.args.get("action"):
        q = q.filter_by(action=request.args["action"])
    if request.args.get("start"):
        start = datetime.strptime(request.args["start"], "%Y-%m-%d").date()
        q = q.filter(TradeLog.date >= start)
    if request.args.get("end"):
        end = datetime.strptime(request.args["end"], "%Y-%m-%d").date()
        q = q.filter(TradeLog.date <= end)
    trades = q.order_by(TradeLog.date.desc()).all()
    return jsonify([t.to_dict() for t in trades])


@app.route("/api/portfolio/fifo-preview", methods=["POST"])
def fifo_preview_route():
    """Compute FIFO realized P&L + blended cost for a prospective sell."""
    d = request.json or {}
    symbol = str(d.get("symbol", "")).upper()
    try:
        shares = float(d.get("shares", 0))
        price = float(d.get("price", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "shares and price must be numbers"}), 400
    prior = [t.to_dict() for t in TradeLog.query.filter_by(symbol=symbol).all()]
    return jsonify(fifo_preview(prior, shares, price))


@app.route("/api/portfolio/lots")
def portfolio_lots():
    """Per-symbol open lots from the trade log: open shares + blended cost basis."""
    by_symbol = {}
    for t in TradeLog.query.order_by(TradeLog.date).all():
        by_symbol.setdefault(t.symbol, []).append(t.to_dict())
    out = []
    for sym, trades in by_symbol.items():
        lots = build_open_lots(trades)
        shares, cost = blended_cost(lots)
        if shares > 1e-9:
            out.append({"symbol": sym, "open_shares": shares, "blended_cost": cost,
                        "lots": [{"shares": round(l[0], 4), "price": round(l[1], 4)} for l in lots]})
    return jsonify(out)


@app.route("/api/trades", methods=["POST"])
def log_trade():
    d = request.json or {}
    symbol = d.get("symbol", "").upper()
    action = d.get("action", "Buy")
    shares = float(d.get("shares", 0))
    price = float(d.get("price", 0))
    realized = d.get("realized_pnl")

    # Auto-compute FIFO realized P&L on a closing trade when not supplied
    if realized is None and str(action).lower() in ("sell", "cover"):
        prior = [t.to_dict() for t in TradeLog.query.filter_by(symbol=symbol).all()]
        realized = fifo_preview(prior, shares, price)["realized_pnl"]

    trade = TradeLog(
        symbol=symbol, action=action, shares=shares, price=price,
        date=datetime.strptime(d["date"], "%Y-%m-%d").date() if d.get("date") else date.today(),
        notes=d.get("notes", ""),
        signal_triggered=d.get("signal_triggered", False),
        signal_type=d.get("signal_type"),
        signal_confidence=d.get("signal_confidence"),
        realized_pnl=realized,
    )
    db.session.add(trade)
    db.session.commit()
    return jsonify(trade.to_dict()), 201


@app.route("/api/trades/<int:tid>", methods=["PUT"])
def update_trade(tid):
    trade = TradeLog.query.get_or_404(tid)
    d = request.json or {}
    for f in ("notes", "realized_pnl"):
        if f in d:
            setattr(trade, f, d[f])
    db.session.commit()
    return jsonify(trade.to_dict())


@app.route("/api/trades/<int:tid>", methods=["DELETE"])
def delete_trade(tid):
    trade = TradeLog.query.get_or_404(tid)
    db.session.delete(trade)
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/trades/export/csv")
def export_trades_csv():
    trades = TradeLog.query.order_by(TradeLog.date.desc()).all()
    rows = [t.to_dict() for t in trades]
    if not rows:
        return Response("No trades", mimetype="text/plain")
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=trade_journal.csv"},
    )


# ─── Preferences ─────────────────────────────────────────────────────────────
@app.route("/api/preferences", methods=["GET"])
def get_preferences():
    prefs = {p.key: p.value for p in Preference.query.all()}
    defaults = {"interval": "300", "density": "compact"}
    defaults.update(prefs)
    return jsonify(defaults)


@app.route("/api/preferences", methods=["PUT"])
def update_preferences():
    d = request.json or {}
    for key, value in d.items():
        set_pref(key, value)
    return jsonify({"ok": True})


# ─── Backup / Restore ─────────────────────────────────────────────────────────
@app.route("/api/backup")
def backup():
    data = {
        "version": 2,
        "exported_at": datetime.now().isoformat(),
        "watchlist": [i.to_dict() for i in WatchlistItem.query.all()],
        "profiles": [p.to_dict() for p in AnalysisProfile.query.all()],
        "portfolio": [p.to_dict() for p in PortfolioPosition.query.all()],
        "trades": [t.to_dict() for t in TradeLog.query.all()],
        "preferences": {p.key: p.value for p in Preference.query.all()},
    }
    return Response(
        json.dumps(data, indent=2),
        mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=terminal_backup.json"},
    )


def _restore_float(val, field):
    """Parse val as float; raise ValueError with a meaningful message on failure."""
    try:
        return float(val)
    except (TypeError, ValueError):
        raise ValueError(f"Invalid numeric value for '{field}': {val!r}")


@app.route("/api/restore", methods=["POST"])
def restore():
    d = request.json or {}
    if not isinstance(d, dict):
        return jsonify({"error": "Backup must be a JSON object"}), 400
    try:
        # Watchlist
        for item in d.get("watchlist", []):
            sym = str(item.get("symbol", "")).upper()
            if not sym:
                raise ValueError("Watchlist item missing symbol")
            if not WatchlistItem.query.filter_by(symbol=sym).first():
                db.session.add(WatchlistItem(
                    symbol=sym, name=item.get("name", ""),
                    tier=item.get("tier", "Active Watch"), notes=item.get("notes", ""),
                    alert_direction=item.get("alert_direction"),
                    alert_price=item.get("alert_price"),
                ))
        # Profiles
        for p in d.get("profiles", []):
            db.session.add(AnalysisProfile(
                name=str(p.get("name", "Restored Profile")),
                risk_tolerance=p.get("risk_tolerance", "Moderate"),
                horizon=p.get("horizon", "Swing"),
                ma_weight=_restore_float(p.get("ma_weight", 0.25), "ma_weight"),
                volume_weight=_restore_float(p.get("volume_weight", 0.25), "volume_weight"),
                rsi_weight=_restore_float(p.get("rsi_weight", 0.25), "rsi_weight"),
                momentum_weight=_restore_float(p.get("momentum_weight", 0.25), "momentum_weight"),
                volume_spike_threshold=_restore_float(p.get("volume_spike_threshold", 1.5), "volume_spike_threshold"),
                rsi_overbought=_restore_float(p.get("rsi_overbought", 70), "rsi_overbought"),
                rsi_oversold=_restore_float(p.get("rsi_oversold", 30), "rsi_oversold"),
                momentum_days=int(p.get("momentum_days", 10)),
                max_trades_per_day=int(p.get("max_trades_per_day", 3)),
                is_active=bool(p.get("is_active", False)),
            ))
        # Portfolio
        for pos in d.get("portfolio", []):
            sym = str(pos.get("symbol", "")).upper()
            if not sym:
                raise ValueError("Portfolio position missing symbol")
            db.session.add(PortfolioPosition(
                symbol=sym,
                shares=_restore_float(pos.get("shares"), "shares"),
                cost_basis=_restore_float(pos.get("cost_basis"), "cost_basis"),
                notes=pos.get("notes", ""),
                date_acquired=datetime.strptime(pos["date_acquired"], "%Y-%m-%d").date()
                              if pos.get("date_acquired") else None,
            ))
        # Trades
        for t in d.get("trades", []):
            sym = str(t.get("symbol", "")).upper()
            if not sym:
                raise ValueError("Trade missing symbol")
            db.session.add(TradeLog(
                symbol=sym,
                action=str(t.get("action", "Buy")),
                shares=_restore_float(t.get("shares"), "shares"),
                price=_restore_float(t.get("price"), "price"),
                notes=t.get("notes", ""),
                date=datetime.strptime(t["date"], "%Y-%m-%d").date()
                     if t.get("date") else date.today(),
                realized_pnl=_restore_float(t["realized_pnl"], "realized_pnl")
                             if t.get("realized_pnl") is not None else None,
            ))
        # Preferences
        for key, value in d.get("preferences", {}).items():
            set_pref(key, value)

        db.session.flush()
        # Enforce exactly one active profile after import: keep the most recent
        # active one, deactivate the rest (handles backups with 0 or many active).
        actives = AnalysisProfile.query.filter_by(is_active=True).order_by(
            AnalysisProfile.created_at.desc()
        ).all()
        if actives:
            for p in actives[1:]:
                p.is_active = False
        else:
            first = AnalysisProfile.query.order_by(AnalysisProfile.created_at).first()
            if first:
                first.is_active = True

        db.session.commit()
        return jsonify({"ok": True})
    except (ValueError, KeyError) as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 400


# ─── Ticker Extended Details ──────────────────────────────────────────────────
@app.route("/api/ticker-details", methods=["POST"])
def ticker_details():
    data = request.json or {}
    tickers = data.get("tickers", [])
    results = {}

    for symbol in tickers:
        sym = symbol.upper()
        try:
            t   = yf.Ticker(sym)
            fi  = t.fast_info

            prev_close = getattr(fi, "previous_close", None) or 0

            pre_price  = getattr(fi, "pre_market_price",  None)
            post_price = getattr(fi, "post_market_price", None)

            def ext_pct(p):
                if p and prev_close:
                    return round((float(p) - float(prev_close)) / float(prev_close) * 100, 2)
                return None

            high52 = getattr(fi, "fifty_two_week_high", None)
            low52  = getattr(fi, "fifty_two_week_low",  None)

            # Short interest — read from cache only (never block on t.info here;
            # t.info has no timeout and will stall Flask's single-threaded dev server)
            short_pct = None
            cached_si = _fmp_cache_get(sym, "si_ext", ttl_hours=4)
            if cached_si is not None:
                short_pct = cached_si.get("short_pct")

            # Earnings date
            earn_date    = _fetch_earnings_date(sym)
            days_to_earn = None
            if earn_date:
                days_to_earn = (date.fromisoformat(earn_date) - date.today()).days
                if days_to_earn < 0:
                    days_to_earn = None   # past

            results[sym] = {
                "pre_price":      round(float(pre_price),  2) if pre_price  else None,
                "pre_pct":        ext_pct(pre_price),
                "post_price":     round(float(post_price), 2) if post_price else None,
                "post_pct":       ext_pct(post_price),
                "high_52w":       round(float(high52), 2) if high52 else None,
                "low_52w":        round(float(low52),  2) if low52  else None,
                # short_interest sent as 0-1 decimal (e.g. 0.15 = 15%)
                "short_interest": round(float(short_pct), 4) if short_pct else None,
                "earn_date":      earn_date,
                "days_to_earn":   days_to_earn,
            }
        except Exception:
            results[sym] = {}

    return jsonify(results)


# ─── Sector Heatmap ───────────────────────────────────────────────────────────
@app.route("/api/heatmap")
def sector_heatmap():
    sectors = []
    for sym, name in SECTOR_ETFS.items():
        try:
            hist = yf.Ticker(sym).history(period="2d")
            if len(hist) < 2:
                continue
            closes  = hist["Close"].astype(float)
            prev, cur = float(closes.iloc[-2]), float(closes.iloc[-1])
            pct = round((cur - prev) / prev * 100, 2) if prev else 0.0
            sectors.append({"symbol": sym, "name": name,
                            "pct": pct, "price": round(cur, 2)})
        except Exception:
            pass

    n_up = sum(1 for s in sectors if s["pct"] > 0)
    n_dn = len(sectors) - n_up
    return jsonify({
        "sectors": sectors,
        "n_up": n_up,
        "n_dn": n_dn,
        "generated_at": datetime.now().isoformat(),
    })


# ─── News Feed ────────────────────────────────────────────────────────────────
@app.route("/api/news/<ticker>")
def ticker_news(ticker):
    if not FINNHUB_API_KEY:
        return jsonify({"no_key": True, "articles": []})

    sym = ticker.upper()
    cached = _fmp_cache_get(sym, "finnhub_news", ttl_hours=4)
    if cached is not None:
        # Cached value is the articles list; wrap it
        articles = cached if isinstance(cached, list) else cached.get("articles", [])
        return jsonify({"articles": articles})

    try:
        today     = date.today()
        from_date = (today - timedelta(days=7)).isoformat()
        resp = requests.get(
            f"{FINNHUB_BASE}/company-news",
            params={"symbol": sym, "from": from_date,
                    "to": today.isoformat(), "token": FINNHUB_API_KEY},
            timeout=8,
        )
        if resp.status_code != 200:
            return jsonify({"articles": []})

        articles = resp.json() or []
        now_ts   = int(datetime.utcnow().timestamp())
        results  = []
        for art in articles[:5]:
            headline = art.get("headline", "")
            art_ts   = int(art.get("datetime", 0) or 0)
            delta    = now_ts - art_ts if art_ts else 0
            if delta < 3600:
                time_ago = f"{max(1, delta // 60)}m ago"
            elif delta < 86400:
                time_ago = f"{delta // 3600}h ago"
            else:
                time_ago = f"{delta // 86400}d ago"
            results.append({
                "headline":  headline,
                "source":    art.get("source", ""),
                "url":       art.get("url", ""),
                "time_ago":  time_ago,
                "sentiment": news_sentiment(headline),
            })
        _fmp_cache_set(sym, "finnhub_news", results)
        return jsonify({"articles": results})
    except Exception:
        return jsonify({"articles": []})


# ─── Correlation Matrix ───────────────────────────────────────────────────────
@app.route("/api/correlation", methods=["POST"])
def correlation_matrix():
    data    = request.json or {}
    tickers = [t.upper() for t in data.get("tickers", [])]

    if len(tickers) < 2:
        return jsonify({"error": "Need at least 2 tickers", "tickers": [], "matrix": []})

    price_map = {}
    for sym in tickers:
        try:
            hist = yf.Ticker(sym).history(period="60d")
            if not hist.empty and len(hist) >= 10:
                price_map[sym] = hist["Close"].astype(float)
        except Exception:
            pass

    valid = [k for k in tickers if k in price_map]
    if len(valid) < 2:
        return jsonify({"error": "Insufficient data", "tickers": [], "matrix": []})

    df   = pd.DataFrame({k: price_map[k] for k in valid}).dropna()
    corr = df.corr().round(2)

    return jsonify({
        "tickers":      list(corr.columns),
        "matrix":       corr.values.tolist(),
        "generated_at": datetime.now().isoformat(),
    })


# ─── Options — Regex Validators ──────────────────────────────────────────────
_TICKER_RE = re.compile(r'^[A-Z0-9.\-]{1,10}$')
_EXPIRY_RE  = re.compile(r'^\d{4}-\d{2}-\d{2}$')


# ─── Options — Helper Functions ───────────────────────────────────────────────
def _opt_float(val, default=None):
    """Cast to float; return default on NaN / None / error."""
    try:
        f = float(val)
        return default if (f != f) else f   # NaN check (f != f)
    except (TypeError, ValueError):
        return default


def _opt_int(val, default=0):
    """Cast to non-negative int; return default on error."""
    try:
        i = int(val)
        return 0 if i < 0 else i
    except (TypeError, ValueError):
        return default


def _chain_to_list(df):
    """Convert yfinance option chain DataFrame to JSON-safe list of dicts."""
    rows = []
    for _, row in df.iterrows():
        rows.append({
            "strike":            _opt_float(row.get("strike")),
            "lastPrice":         _opt_float(row.get("lastPrice")),
            "bid":               _opt_float(row.get("bid")),
            "ask":               _opt_float(row.get("ask")),
            "volume":            _opt_int(row.get("volume")),
            "openInterest":      _opt_int(row.get("openInterest")),
            "impliedVolatility": _opt_float(row.get("impliedVolatility")),
            "inTheMoney":        bool(row.get("inTheMoney", False)),
        })
    return rows


def _calc_max_pain(calls_df, puts_df):
    """Return the strike that minimises total intrinsic value of all open options."""
    try:
        strikes = sorted(set(
            calls_df["strike"].dropna().tolist() +
            puts_df["strike"].dropna().tolist()
        ))
        if not strikes:
            return None
        min_pain    = float("inf")
        pain_strike = None
        for s in strikes:
            call_pain = sum(
                max(0.0, s - float(k)) * float(oi)
                for k, oi in zip(calls_df["strike"], calls_df["openInterest"])
                if _opt_float(k) is not None and _opt_int(oi) > 0
            )
            put_pain = sum(
                max(0.0, float(k) - s) * float(oi)
                for k, oi in zip(puts_df["strike"], puts_df["openInterest"])
                if _opt_float(k) is not None and _opt_int(oi) > 0
            )
            total = call_pain + put_pain
            if total < min_pain:
                min_pain    = total
                pain_strike = s
        return round(pain_strike, 2) if pain_strike is not None else None
    except Exception:
        return None


# ─── Options Routes ───────────────────────────────────────────────────────────
@app.route("/api/options/expirations/<ticker>")
def options_expirations(ticker):
    """Return list of available expiration date strings for a ticker."""
    ticker = ticker.upper()
    if not _TICKER_RE.match(ticker):
        return jsonify({"error": "Invalid ticker"}), 400
    try:
        t    = yf.Ticker(ticker)
        exps = list(t.options)
        return jsonify({"ticker": ticker, "expirations": exps})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/options/chain/<ticker>/<expiration>")
def options_chain(ticker, expiration):
    """Return calls + puts for a specific expiration."""
    ticker = ticker.upper()
    if not _TICKER_RE.match(ticker):
        return jsonify({"error": "Invalid ticker"}), 400
    if not _EXPIRY_RE.match(expiration):
        return jsonify({"error": "Invalid expiration date format (YYYY-MM-DD)"}), 400
    try:
        t     = yf.Ticker(ticker)
        chain = t.option_chain(expiration)
        return jsonify({
            "ticker":     ticker,
            "expiration": expiration,
            "calls":      _chain_to_list(chain.calls),
            "puts":       _chain_to_list(chain.puts),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/options/summary/<ticker>")
def options_summary(ticker):
    """Put/Call ratio, max pain, and total call/put OI across nearest 8 expiries."""
    ticker = ticker.upper()
    if not _TICKER_RE.match(ticker):
        return jsonify({"error": "Invalid ticker"}), 400
    try:
        t    = yf.Ticker(ticker)
        exps = list(t.options)
        if not exps:
            return jsonify({"error": "No options data available"}), 404

        total_call_oi = 0
        total_put_oi  = 0
        all_calls     = []
        all_puts      = []

        for exp in exps[:8]:        # limit to nearest 8 expiries for speed
            try:
                chain          = t.option_chain(exp)
                total_call_oi += int(chain.calls["openInterest"].fillna(0).sum())
                total_put_oi  += int(chain.puts["openInterest"].fillna(0).sum())
                all_calls.append(chain.calls)
                all_puts.append(chain.puts)
            except Exception:
                continue

        pc_ratio = (round(total_put_oi / total_call_oi, 3)
                    if total_call_oi > 0 else None)

        # Max pain calculated on the nearest expiry only
        max_pain = None
        if all_calls and all_puts:
            max_pain = _calc_max_pain(all_calls[0], all_puts[0])

        return jsonify({
            "ticker":         ticker,
            "put_call_ratio": pc_ratio,
            "max_pain":       max_pain,
            "total_call_oi":  total_call_oi,
            "total_put_oi":   total_put_oi,
            "expirations":    exps,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── Options Signals Routes ───────────────────────────────────────────────────
from options_signals import recommend_for_strategy, scan_top_n, STRATEGY_CONFIGS


def _attach_optsig_extras(results: list, strategy_id: str) -> list:
    """
    Mutates each result in-place:
      1. Adds plain_english_plan via generate_options_plan()
      2. Adds earnings_risk / earnings_date if earnings fall before expiry
    """
    for rec in results:
        # Plain-English plan
        try:
            plan = generate_options_plan(rec, strategy_id)
            if plan:
                rec["plain_english_plan"] = plan
        except Exception:
            pass

        # Earnings risk
        trade = rec.get("suggested_trade")
        if trade and trade.get("expiry"):
            try:
                earn_str = _fetch_earnings_date(rec["ticker"])
                if earn_str:
                    earn_d   = date.fromisoformat(earn_str)
                    expiry_d = date.fromisoformat(trade["expiry"])
                    if earn_d <= expiry_d:
                        rec["earnings_risk"] = True
                        rec["earnings_date"] = earn_str
                        continue
            except Exception:
                pass
        rec.setdefault("earnings_risk", False)

    return results


def _parse_param_overrides(args):
    """Extract optional scoring param overrides from query string."""
    overrides = {}
    for key in ('ivr_min', 'ivr_max'):
        val = args.get(key)
        if val is not None:
            try:
                overrides[key] = float(val)
            except ValueError:
                pass
    for key in ('rsi_signal', 'iv_regime', 'trend_requirement'):
        val = args.get(key)
        if val:
            overrides[key] = val
    return overrides or None


@app.route("/api/options/signals/strategies")
def optsig_strategies():
    """Return strategy_id → display metadata for the frontend picker."""
    out = []
    for sid, cfg in STRATEGY_CONFIGS.items():
        out.append({'id': sid, 'ivr_min': cfg.get('ivr_min'), 'ivr_max': cfg.get('ivr_max'),
                    'rsi_signal': cfg.get('rsi_signal'), 'iv_regime': cfg.get('iv_regime'),
                    'trend_requirement': cfg.get('trend_requirement'), 'dte_entry': cfg.get('dte_entry')})
    return jsonify(out)


@app.route("/api/options/signals/recommend/<strategy_id>")
def optsig_recommend(strategy_id):
    """
    Score tickers against a strategy and return recommendations.

    Query params:
        tickers=AAPL,SPY,QQQ   (comma-separated; falls back to watchlist)
        ivr_min, ivr_max, rsi_signal, iv_regime, trend_requirement  (optional overrides)
    """
    strategy_id = strategy_id.lower()

    tickers_param = request.args.get('tickers', '')
    if tickers_param:
        tickers = [t.strip().upper() for t in tickers_param.split(',') if t.strip()]
    else:
        # Fall back to the user's watchlist
        tickers = [w.symbol for w in WatchlistItem.query.all()]

    if not tickers:
        return jsonify([])

    overrides = _parse_param_overrides(request.args)

    try:
        results = recommend_for_strategy(strategy_id, tickers, overrides)
        _attach_optsig_extras(results, strategy_id)
        return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/options/signals/scan")
def optsig_scan():
    """
    Auto-scan watchlist + any extra tickers, return top-N for active strategy.

    Query params:
        strategy_id   (required)
        top_n=10      (default 10)
        tickers=...   (additional tickers beyond watchlist)
        + same override params as /recommend
    """
    strategy_id = request.args.get('strategy_id', 'iron_condor').lower()
    top_n_str   = request.args.get('top_n', '10')
    try:
        top_n = max(1, min(25, int(top_n_str)))
    except ValueError:
        top_n = 10

    extra_param = request.args.get('tickers', '')
    extra = [t.strip().upper() for t in extra_param.split(',') if t.strip()]
    watchlist = [w.symbol for w in WatchlistItem.query.all()]

    tickers = list(dict.fromkeys(watchlist + extra))   # deduplicate, preserve order
    if not tickers:
        return jsonify([])

    overrides = _parse_param_overrides(request.args)

    try:
        results = scan_top_n(strategy_id, tickers, top_n, overrides)
        _attach_optsig_extras(results, strategy_id)
        return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── Surge / Crash Detection ─────────────────────────────────────────────────
@app.route("/api/surge-crash/<ticker>")
def surge_crash(ticker):
    """
    Return the N largest single-day price moves for a ticker.

    Query params
    ------------
    type      : 'surge' (default) | 'crash'
    n         : int 1–50, default 10
    period    : yfinance history period string, default '1y'
                (e.g. '3mo', '6mo', '1y', '2y', '5y', 'max')

    Response (200)
    --------------
    {
      "ticker":  "AAPL",
      "type":    "surge",
      "period":  "1y",
      "results": [
        {"date": "2024-02-16", "change": 4.82},
        ...
      ]
    }
    """
    symbol = ticker.upper()

    detect_type = request.args.get("type", "surge").lower()
    if detect_type not in ("surge", "crash"):
        return jsonify({"error": "type must be 'surge' or 'crash'"}), 400

    try:
        n = max(1, min(50, int(request.args.get("n", 10))))
    except (TypeError, ValueError):
        n = 10

    period = request.args.get("period", "1y")
    # Whitelist recognised period strings to avoid arbitrary yfinance calls
    valid_periods = {"1mo", "3mo", "6mo", "1y", "2y", "3y", "5y", "10y", "max"}
    if period not in valid_periods:
        period = "1y"

    try:
        t    = yf.Ticker(symbol)
        hist = t.history(period=period)
        if hist is None or hist.empty:
            return jsonify({"error": f"No data for {symbol}"}), 404

        dates, changes = detect_surge_crash(hist, detect_type=detect_type, num_output=n)

        results = [{"date": d, "change": c} for d, c in zip(dates, changes)]
        return jsonify({
            "ticker":  symbol,
            "type":    detect_type,
            "period":  period,
            "results": results,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── Walk-forward Backtest (Phase 2e) ─────────────────────────────────────────
@app.route("/api/backtest", methods=["POST"])
def backtest():
    """
    Replay the signal engine over history for one symbol.

    Body: {symbol, profile_id?|preset_key?, start?, end?, cost_bps?}
    Capped at 1 symbol × 3 years to stay polite to yfinance. Results are
    hypothetical and never persisted.
    """
    d = request.json or {}
    symbol = str(d.get("symbol", "")).upper().strip()
    if not symbol:
        return jsonify({"error": "A symbol is required."}), 400

    # Resolve the profile: explicit id, preset, or the active profile.
    profile = None
    if d.get("preset_key"):
        profile = preset_to_profile(d["preset_key"])
        if not profile:
            return jsonify({"error": f"Unknown preset '{d['preset_key']}'"}), 400
    elif d.get("profile_id"):
        p = AnalysisProfile.query.get(d["profile_id"])
        if not p:
            return jsonify({"error": "Profile not found."}), 404
        profile = p.to_dict()
    else:
        profile = get_active_profile_dict()

    try:
        cost_bps = max(0.0, float(d.get("cost_bps", 0) or 0))
    except (TypeError, ValueError):
        cost_bps = 0.0

    # Fetch up to 3y once (warm-up + test window both come from this).
    hist = fetch_history(symbol, period="3y")
    if hist is None or len(hist) < 60:
        return jsonify({"error": f"Not enough history for {symbol}."}), 404

    try:
        result = run_backtest(symbol, hist, profile,
                              start=d.get("start"), end=d.get("end"), cost_bps=cost_bps)
        if "error" in result:
            return jsonify(result), 400
        result["profile_name"] = profile.get("name", "Active profile")
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── Signal Scoreboard + Time Machine (Phase 6) ───────────────────────────────
def _fetch_history_both(symbol: str, period: str = "2y"):
    """
    Adjusted OHLCV (for all math) with a 'CloseRaw' column (as-traded close for
    display). yfinance auto_adjust=False gives raw OHLC + 'Adj Close'; we scale
    OHLC by Adj/Close so ratios stay continuous across splits/dividends.
    """
    try:
        t = yf.Ticker(symbol.upper())
        df = t.history(period=period, auto_adjust=False)
        if df is None or df.empty or "Adj Close" not in df.columns:
            return None
        df = df.dropna(subset=["Close", "Adj Close"])
        factor = df["Adj Close"] / df["Close"]
        out = pd.DataFrame({
            "Open": df["Open"] * factor, "High": df["High"] * factor,
            "Low": df["Low"] * factor, "Close": df["Adj Close"],
            "Volume": df["Volume"], "CloseRaw": df["Close"],
        }, index=df.index)
        return out
    except Exception:
        return None


def _spy_prices_list(period: str = "2y"):
    """SPY adjusted price list for benchmark comparison (cached in prices_daily)."""
    df = _fetch_history_both("SPY", period)
    if df is None:
        return snap.prices_list("SPY")
    snap.upsert_prices("SPY", df)
    return snap.prices_list("SPY")


def _resolve_scoreboard_profile(profile_id=None, preset_key=None):
    """Return (profile_dict, profile_id, preset_key) for a scoreboard request."""
    if preset_key:
        p = preset_to_profile(preset_key)
        return (p, None, preset_key) if p else (None, None, None)
    if profile_id:
        row = AnalysisProfile.query.get(profile_id)
        return (row.to_dict(), row.id, None) if row else (None, None, None)
    active = AnalysisProfile.query.filter_by(is_active=True).first()
    if active:
        return active.to_dict(), active.id, None
    return DEFAULT_PROFILE.copy(), None, None


def capture_live_snapshots():
    """One live snapshot per watchlist symbol under the active profile (daemon job)."""
    profile, pid, pkey_preset = _resolve_scoreboard_profile()
    if not profile:
        return 0
    pkey = snap.profile_key(pid, pkey_preset)
    today = date.today()
    written = 0
    for w in WatchlistItem.query.all():
        df = _fetch_history_both(w.symbol, "1y")
        if df is None:
            continue
        snap.upsert_prices(w.symbol, df)
        recs = replay_signals(w.symbol, df, profile, as_of=today)
        if not recs or recs[0]["analysis"] is None:
            continue
        r = recs[0]
        sd = date.fromisoformat(r["snapshot_date"])
        existing = SignalSnapshot.query.filter_by(symbol=w.symbol, snapshot_date=sd, profile_key=pkey).first()
        if existing:
            continue
        db.session.add(SignalSnapshot(
            symbol=w.symbol, snapshot_date=sd, signal=r["signal"], confidence=r["confidence"],
            signed_score=r["signed_score"], regime=r["regime"], price_close_adj=r["price_close_adj"],
            price_close_raw=r["price_close_raw"], price_next_open_adj=r["price_next_open_adj"],
            had_ma200=r["had_ma200"], profile_id=pid, preset_key=pkey_preset, profile_key=pkey,
            source="live", matured_through=0))
        written += 1
    db.session.commit()
    for w in WatchlistItem.query.all():
        snap.mature_symbol(w.symbol)
    return written


@app.route("/api/scoreboard/backfill", methods=["POST"])
def scoreboard_backfill():
    d = request.json or {}
    lookback = max(30, min(365, int(d.get("lookback_days", 180))))
    profile, pid, pkey_preset = _resolve_scoreboard_profile(d.get("profile_id"), d.get("preset_key"))
    if not profile:
        return jsonify({"error": "No profile available."}), 400
    symbols = [s.upper() for s in d.get("symbols", [])] or [w.symbol for w in WatchlistItem.query.all()]
    if not symbols:
        return jsonify({"error": "No symbols to backfill."}), 400

    fetch_period = "2y" if lookback > 300 else "1y"
    results, total_created = [], 0
    for symbol in symbols:
        df = _fetch_history_both(symbol, fetch_period)
        if df is None or len(df) < 60:
            results.append({"symbol": symbol, "snapshots": 0, "error": "no history"})
            continue
        try:
            created = snap.backfill_symbol(symbol, df, profile, pid, pkey_preset, lookback)
            total_created += created
            results.append({"symbol": symbol, "snapshots": created})
        except Exception as e:
            db.session.rollback()
            results.append({"symbol": symbol, "error": str(e)})
    return jsonify({"created": total_created, "symbols": results,
                    "lookback_days": lookback,
                    "profile_key": snap.profile_key(pid, pkey_preset)})


@app.route("/api/scoreboard")
def scoreboard():
    horizon = int(request.args.get("horizon", 20))
    if horizon not in sb.HORIZONS:
        horizon = 20
    source = request.args.get("source", "all")
    use_bench = request.args.get("benchmark", "spy") == "spy"
    profile, pid, pkey_preset = _resolve_scoreboard_profile(
        request.args.get("profile_id"), request.args.get("preset_key"))
    pkey = snap.profile_key(pid, pkey_preset)

    symbols = sorted({s.symbol for s in SignalSnapshot.query.filter_by(profile_key=pkey).all()})
    if not symbols:
        return jsonify({"empty": True, "per_ticker": [], "cross_sectional_ic": {},
                        "disclaimer": DISCLAIMER})

    benchmark_rets = None
    if use_bench:
        spy = _spy_prices_list("2y")
        d2i = {p["date"]: i for i, p in enumerate(spy)}
        benchmark_rets = {}
        for p in spy:
            i = d2i[p["date"]]
            benchmark_rets[p["date"]] = {H: (round(spy[i + H]["close"] / p["close"] - 1, 6)
                                             if i + H < len(spy) and p["close"] else None)
                                         for H in sb.HORIZONS}

    data = snap.build_scoreboard(symbols, pkey, horizon, benchmark_rets, source)
    data["empty"] = False
    data["benchmark"] = "spy" if use_bench else "none"
    data["survivorship_note"] = ("This is your self-curated watchlist — names you chose and "
                                 "kept — an inherently flattering sample.")
    return jsonify(data)


@app.route("/api/scoreboard/<symbol>")
def scoreboard_symbol(symbol):
    sym = symbol.upper()
    snap.mature_symbol(sym)
    rows = SignalSnapshot.query.filter_by(symbol=sym).order_by(SignalSnapshot.snapshot_date).all()
    return jsonify({"symbol": sym, "timeline": [r.to_dict() for r in rows],
                    "disclaimer": DISCLAIMER})


@app.route("/api/timemachine/<symbol>")
def timemachine(symbol):
    sym = symbol.upper()
    as_of = request.args.get("as_of")
    if not as_of:
        return jsonify({"error": "as_of date required (YYYY-MM-DD)"}), 400
    try:
        as_of_d = date.fromisoformat(as_of[:10])
    except ValueError:
        return jsonify({"error": "invalid as_of date"}), 400
    profile, pid, pkey_preset = _resolve_scoreboard_profile(
        request.args.get("profile_id"), request.args.get("preset_key"))

    df = _fetch_history_both(sym, "2y")
    if df is None or df.empty:
        return jsonify({"error": f"Unknown or unavailable symbol {sym}"}), 404
    spy = _spy_prices_list("2y")
    result = snap.build_timemachine(sym, df, profile, as_of_d, spy_prices=spy)
    # Earliest fully-supported date (need ~200 prior bars for the full panel)
    if len(df) > 200:
        result["earliest_full_date"] = str(df.index[200].date() if hasattr(df.index[200], "date") else df.index[200])
    return jsonify(result)


# ─── Server-side alert engine + webhook (Phase 4) ─────────────────────────────
def _current_price(symbol: str):
    """Latest price from the quote cache, else a single fetch. None on failure."""
    row = _quote_cache_get(symbol)
    if row and row.payload:
        try:
            p = json.loads(row.payload).get("price")
            if p is not None:
                return float(p)
        except Exception:
            pass
    try:
        hist = fetch_history(symbol, period="5d")
        if hist is not None and not hist.empty:
            return float(hist["Close"].astype(float).iloc[-1])
    except Exception:
        pass
    return None


def _fire_webhook(payload: dict):
    url = get_pref("alert_webhook", "")
    if not url:
        return
    try:
        requests.post(url, json=payload, timeout=6)
    except Exception as e:
        app.logger.warning("alert webhook failed: %s", e)


def check_alerts_once() -> int:
    """
    Check every watchlist alert against the current price. Records an AlertEvent
    (and fires the webhook) when a threshold is crossed, deduped to once per
    symbol+direction per hour so multiple gunicorn workers don't double-fire.
    Returns the number of new events recorded.
    """
    fired = 0
    items = [w for w in WatchlistItem.query.all() if w.alert_direction and w.alert_price]
    for w in items:
        price = _current_price(w.symbol)
        if price is None:
            continue
        crossed = (w.alert_direction == "above" and price >= w.alert_price) or \
                  (w.alert_direction == "below" and price <= w.alert_price)
        if not crossed:
            continue
        cutoff = datetime.utcnow() - timedelta(hours=1)
        recent = AlertEvent.query.filter(
            AlertEvent.symbol == w.symbol,
            AlertEvent.direction == w.alert_direction,
            AlertEvent.created_at > cutoff,
        ).first()
        if recent:
            continue
        ev = AlertEvent(symbol=w.symbol, direction=w.alert_direction,
                        threshold=w.alert_price, price=round(price, 2))
        db.session.add(ev)
        db.session.commit()
        fired += 1
        app.logger.info("alert fired: %s %s %.2f (price %.2f)",
                        w.symbol, w.alert_direction, w.alert_price, price)
        _fire_webhook({"symbol": w.symbol, "direction": w.alert_direction,
                       "threshold": w.alert_price, "price": round(price, 2),
                       "at": ev.created_at.isoformat()})
    return fired


_alert_thread_started = False


def _maybe_capture_daily_snapshot():
    """After the close on a weekday, write one live snapshot per symbol (once/day)."""
    try:
        from zoneinfo import ZoneInfo
        now_et = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        now_et = datetime.utcnow() - timedelta(hours=4)   # rough ET fallback
    if now_et.weekday() >= 5 or (now_et.hour, now_et.minute) < (16, 5):
        return
    today = now_et.date()
    already = SignalSnapshot.query.filter_by(snapshot_date=today, source="live").first()
    if already:
        return
    try:
        written = capture_live_snapshots()
        if written:
            app.logger.info("captured %d live snapshots for %s", written, today)
    except Exception as e:
        app.logger.warning("daily snapshot error: %s", e)


def _alert_loop():
    while True:
        try:
            interval = max(60, int(get_pref("interval", "300")))
        except Exception:
            interval = 300
        _time.sleep(interval)
        try:
            with app.app_context():
                check_alerts_once()
                _maybe_capture_daily_snapshot()
        except Exception as e:
            app.logger.warning("alert loop error: %s", e)


def start_alert_engine():
    global _alert_thread_started
    if _alert_thread_started or os.environ.get("DISABLE_ALERT_ENGINE"):
        return
    _alert_thread_started = True
    threading.Thread(target=_alert_loop, daemon=True, name="alert-engine").start()


@app.route("/api/alerts/events")
def alert_events():
    """Unseen alert events for the bell icon (most recent first)."""
    rows = AlertEvent.query.filter_by(seen=False).order_by(AlertEvent.created_at.desc()).limit(50).all()
    return jsonify([e.to_dict() for e in rows])


@app.route("/api/alerts/events/seen", methods=["POST"])
def alert_events_seen():
    AlertEvent.query.filter_by(seen=False).update({"seen": True})
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/alerts/check", methods=["POST"])
def alert_check_now():
    """Manually trigger an alert sweep (used by tests / a 'check now' button)."""
    return jsonify({"fired": check_alerts_once()})


# ─── Health endpoint (Phase 5) ────────────────────────────────────────────────
APP_VERSION = "2.0"


@app.route("/api/health")
def health():
    """Lightweight health probe for uptime monitors."""
    db_ok = True
    try:
        db.session.execute(db.text("SELECT 1"))
    except Exception:
        db_ok = False
    # Age of the freshest cached quote (proxy for yfinance freshness)
    yf_age = None
    try:
        newest = QuoteCache.query.order_by(QuoteCache.fetched_at.desc()).first()
        if newest:
            yf_age = round((datetime.utcnow() - newest.fetched_at).total_seconds())
    except Exception:
        pass
    return jsonify({
        "ok": db_ok, "db": db_ok, "yfinance_age_s": yf_age,
        "version": APP_VERSION, "ai_enabled": ai.ai_enabled(),
    })


# ─── Claude Fable 5 intelligence layer (Phase 3) ──────────────────────────────
def _earnings_within(symbol: str, days: int = 7):
    """Return the next earnings date string if it falls within `days`, else None."""
    try:
        e = _fetch_earnings_date(symbol)
        if e:
            d = date.fromisoformat(e)
            if 0 <= (d - date.today()).days <= days:
                return e
    except Exception:
        pass
    return None


@app.route("/api/ai/status")
def ai_status():
    """Tell the frontend whether AI features should be shown."""
    return jsonify({"enabled": ai.ai_enabled(), "model": ai.MODEL})


@app.route("/api/ai/briefing")
def ai_briefing():
    """Morning briefing — replaces the template summary when a key is present."""
    if not ai.ai_enabled():
        return jsonify({"enabled": False})
    force = request.args.get("force") == "1"
    if not force:
        cached = _fmp_cache_get("__ai__", "briefing_resp", ttl_hours=0.5)
        if cached:
            return jsonify(cached)

    profile = get_active_profile_dict()
    watch = []
    for item in WatchlistItem.query.limit(12).all():
        try:
            a = analyze_ticker(item.symbol, fetch_history(item.symbol, period="1y"), profile)
            if not a:
                continue
            entry = {"symbol": item.symbol, "signal": a["signal"],
                     "confidence": a["confidence"],
                     "regime": (a.get("regime") or {}).get("label")}
            earn = _earnings_within(item.symbol, 7)
            if earn:
                entry["earnings_in_7d"] = earn
            watch.append(entry)
        except Exception:
            continue

    payload = {"indices": fetch_index_data(), "watchlist": watch}
    summary = ai.morning_briefing(payload, force=force)
    if not summary:
        return jsonify({"enabled": True, "summary": None})
    resp = {"enabled": True, "summary": summary, "source": "claude",
            "generated_at": datetime.now().isoformat()}
    try:
        _fmp_cache_set("__ai__", "briefing_resp", resp)
    except Exception:
        pass
    return jsonify(resp)


@app.route("/api/ai/signal-explanation/<symbol>")
def ai_signal_explanation(symbol):
    """Plain-English explanation of one ticker's current signal."""
    if not ai.ai_enabled():
        return jsonify({"enabled": False})
    force = request.args.get("force") == "1"
    a = analyze_ticker(symbol.upper(), fetch_history(symbol, period="1y"), get_active_profile_dict())
    if not a:
        return jsonify({"enabled": True, "explanation": None})
    text = ai.explain_signal(a, force=force)
    return jsonify({"enabled": True, "explanation": text, "source": "claude" if text else None})


@app.route("/api/ai/journal-review", methods=["POST"])
def ai_journal_review():
    """On-demand pattern review of the user's trade log."""
    if not ai.ai_enabled():
        return jsonify({"enabled": False})
    trades = [{
        "symbol": t.symbol, "action": t.action, "date": t.date.isoformat() if t.date else None,
        "shares": t.shares, "price": t.price, "realized_pnl": t.realized_pnl,
        "signal_triggered": t.signal_triggered, "signal_type": t.signal_type,
    } for t in TradeLog.query.order_by(TradeLog.date).all()]
    if not trades:
        return jsonify({"enabled": True, "result": None, "message": "No trades to review yet."})
    force = (request.json or {}).get("force", False)
    result = ai.journal_review(trades, force=bool(force))
    return jsonify({"enabled": True, "result": result})


@app.route("/api/ai/watchlist-query", methods=["POST"])
def ai_watchlist_query():
    """Answer a natural-language question over the user's watchlist analysis."""
    if not ai.ai_enabled():
        return jsonify({"enabled": False})
    query = str((request.json or {}).get("query", "")).strip()
    if not query:
        return jsonify({"enabled": True, "error": "Empty query."}), 400

    profile = get_active_profile_dict()
    candidates = []
    for item in WatchlistItem.query.limit(30).all():
        try:
            a = analyze_ticker(item.symbol, fetch_history(item.symbol, period="1y"), profile)
            if not a:
                continue
            rsi = next((s.get("rsi") for s in a.get("signals", []) if s.get("name") == "RSI"), None)
            candidates.append({
                "symbol": item.symbol, "signal": a["signal"], "confidence": a["confidence"],
                "regime": (a.get("regime") or {}).get("label"),
                "trend": (a.get("regime") or {}).get("direction"),
                "rsi": rsi, "weighted_score": a.get("weighted_score"),
            })
        except Exception:
            continue
    if not candidates:
        return jsonify({"enabled": True, "result": {"matches": []}})
    result = ai.watchlist_query(query, candidates)
    return jsonify({"enabled": True, "result": result})


@app.route("/api/ai/backtest-postmortem", methods=["POST"])
def ai_backtest_postmortem():
    """Explain a backtest the user just ran (stats + trades posted from the client)."""
    if not ai.ai_enabled():
        return jsonify({"enabled": False})
    d = request.json or {}
    stats = d.get("stats")
    trades = d.get("trades", [])
    if not stats:
        return jsonify({"enabled": True, "error": "No backtest stats provided."}), 400
    text = ai.backtest_postmortem(stats, trades, force=bool(d.get("force", False)))
    return jsonify({"enabled": True, "explanation": text})


@app.route("/api/ai/scoreboard-postmortem", methods=["POST"])
def ai_scoreboard_postmortem():
    """Explain the signal-accuracy scoreboard (aggregate stats posted by the client)."""
    if not ai.ai_enabled():
        return jsonify({"enabled": False})
    d = request.json or {}
    stats = d.get("stats")
    if not stats:
        return jsonify({"enabled": True, "error": "No stats provided."}), 400
    text = ai.scoreboard_postmortem(stats, force=bool(d.get("force", False)))
    return jsonify({"enabled": True, "explanation": text})


@app.route("/api/ai/timemachine-explain/<symbol>")
def ai_timemachine_explain(symbol):
    """Explain a single reconstructed signal + its outcome."""
    if not ai.ai_enabled():
        return jsonify({"enabled": False})
    as_of = request.args.get("as_of")
    if not as_of:
        return jsonify({"enabled": True, "error": "as_of required"}), 400
    try:
        as_of_d = date.fromisoformat(as_of[:10])
    except ValueError:
        return jsonify({"enabled": True, "error": "invalid as_of"}), 400
    profile, pid, pkey_preset = _resolve_scoreboard_profile(request.args.get("profile_id"))
    df = _fetch_history_both(symbol.upper(), "2y")
    if df is None:
        return jsonify({"enabled": True, "explanation": None})
    tm = snap.build_timemachine(symbol.upper(), df, profile, as_of_d, spy_prices=_spy_prices_list("2y"))
    payload = {"analysis": {k: tm["analysis"].get(k) for k in
                            ("signal", "confidence", "regime", "signals", "weighted_score", "suggested_stop")}
               if tm.get("analysis") else None,
               "outcome": tm.get("outcome"), "availability": tm.get("availability")}
    text = ai.timemachine_explain(payload, symbol.upper(), as_of,
                                  force=request.args.get("force") == "1")
    return jsonify({"enabled": True, "explanation": text})


# Start the background alert engine (runs under gunicorn import and dev run alike;
# cross-worker dedup in check_alerts_once keeps multiple workers from double-firing).
start_alert_engine()


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
        threaded=True,   # allow concurrent requests; prevents t.info blocking /api/quote
    )