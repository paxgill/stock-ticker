import os
import csv
import json
import io
from datetime import datetime, date, timedelta

import requests
from flask import Flask, jsonify, request, Response
from flask_cors import CORS
import yfinance as yf
import pandas as pd

from models import db, WatchlistItem, AnalysisProfile, PortfolioPosition, TradeLog, Preference, FMPCache
from analysis import analyze_ticker, compute_rsi, compute_rvol, rvol_tier, DEFAULT_PROFILE
from narratives import fetch_index_data, generate_market_summary, generate_trade_description

# ─── App / DB Setup ──────────────────────────────────────────────────────────
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DATA_DIR, exist_ok=True)

app = Flask(__name__, static_folder="static", static_url_path="")
CORS(app)

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

with app.app_context():
    db.create_all()
    # Prune FMP cache rows older than 90 days to prevent unbounded growth
    try:
        cutoff = datetime.utcnow() - timedelta(days=90)
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


@app.route("/api/quote", methods=["POST"])
def get_quotes():
    data = request.json or {}
    tickers = data.get("tickers", [])
    results = {}

    for symbol in tickers:
        try:
            hist = fetch_history(symbol)
            if hist is None:
                results[symbol] = {"error": "No data", "symbol": symbol.upper()}
                continue

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
            # 20-bar average — matches compute_rvol() baseline in analysis.py
            avg_vol20 = float(volumes.tail(20).mean()) if len(volumes) >= 20 else vol
            vol_ratio = round(vol / avg_vol20, 2) if avg_vol20 > 0 else 1.0

            rvol = round(compute_rvol(volumes), 2)
            rvol_label = rvol_tier(rvol)

            n = 10
            mom_pct = None
            if len(closes) > n:
                mom_pct = round((current / float(closes.iloc[-n - 1]) - 1) * 100, 2)

            results[symbol.upper()] = {
                "symbol":       symbol.upper(),
                "price":        round(current, 2),
                "change":       round(change, 2),
                "pct_change":   round(pct, 2),
                "volume":       fmt_vol(int(vol)),
                "volume_raw":   int(vol),
                "vol_ratio":    vol_ratio,
                "rvol":         rvol,
                "rvol_tier":    rvol_label,
                "ma20":         round(ma20, 2) if ma20 else None,
                "ma50":         round(ma50, 2) if ma50 else None,
                "above_ma20":   current > ma20 if ma20 else None,
                "above_ma50":   current > ma50 if ma50 else None,
                "rsi":          round(rsi, 1) if rsi else None,
                "momentum_pct": mom_pct,
                "timestamp":    datetime.now().isoformat(),
            }
        except Exception as e:
            results[symbol.upper()] = {"error": str(e), "symbol": symbol.upper()}

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
    db.session.commit()
    return jsonify(item.to_dict())


@app.route("/api/watchlist/<symbol>", methods=["DELETE"])
def delete_watchlist(symbol):
    item = WatchlistItem.query.filter_by(symbol=symbol.upper()).first_or_404()
    db.session.delete(item)
    db.session.commit()
    return jsonify({"ok": True})


# ─── Analysis Profiles ────────────────────────────────────────────────────────
@app.route("/api/profiles", methods=["GET"])
def get_profiles():
    profiles = AnalysisProfile.query.order_by(AnalysisProfile.created_at).all()
    return jsonify([p.to_dict() for p in profiles])


@app.route("/api/profiles", methods=["POST"])
def create_profile():
    d = request.json or {}
    rsi_ob = float(d.get("rsi_overbought", 70.0))
    rsi_os = float(d.get("rsi_oversold",  30.0))
    if rsi_os >= rsi_ob:
        return jsonify({"error": "RSI Oversold must be less than RSI Overbought"}), 400
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


@app.route("/api/trades", methods=["POST"])
def log_trade():
    d = request.json or {}
    trade = TradeLog(
        symbol=d.get("symbol", "").upper(),
        action=d.get("action", "Buy"),
        shares=float(d.get("shares", 0)),
        price=float(d.get("price", 0)),
        date=datetime.strptime(d["date"], "%Y-%m-%d").date() if d.get("date") else date.today(),
        notes=d.get("notes", ""),
        signal_triggered=d.get("signal_triggered", False),
        signal_type=d.get("signal_type"),
        signal_confidence=d.get("signal_confidence"),
        realized_pnl=d.get("realized_pnl"),
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


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
        threaded=True,   # allow concurrent requests; prevents t.info blocking /api/quote
    )