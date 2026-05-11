import os
import csv
import json
import io
from datetime import datetime, date

from flask import Flask, jsonify, request, Response
from flask_cors import CORS
import yfinance as yf
import pandas as pd

from models import db, WatchlistItem, AnalysisProfile, PortfolioPosition, TradeLog, Preference
from analysis import analyze_ticker, compute_rsi, DEFAULT_PROFILE
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
        info = t.fast_info
        price = info.last_price
        if not price or price == 0:
            return jsonify({"valid": False})
        try:
            long_name = t.info.get("longName") or t.info.get("shortName") or ticker.upper()
        except Exception:
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
            avg_vol30 = float(volumes.tail(30).mean()) if len(volumes) >= 30 else vol
            vol_ratio = round(vol / avg_vol30, 2) if avg_vol30 > 0 else 1.0

            n = 10
            mom_pct = None
            if len(closes) > n:
                mom_pct = round((current / float(closes.iloc[-n - 1]) - 1) * 100, 2)

            results[symbol.upper()] = {
                "symbol": symbol.upper(),
                "price": round(current, 2),
                "change": round(change, 2),
                "pct_change": round(pct, 2),
                "volume": fmt_vol(int(vol)),
                "volume_raw": int(vol),
                "vol_ratio": vol_ratio,
                "ma20": round(ma20, 2) if ma20 else None,
                "ma50": round(ma50, 2) if ma50 else None,
                "above_ma20": current > ma20 if ma20 else None,
                "above_ma50": current > ma50 if ma50 else None,
                "rsi": round(rsi, 1) if rsi else None,
                "momentum_pct": mom_pct,
                "timestamp": datetime.now().isoformat(),
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
    profile = AnalysisProfile(
        name=d.get("name", "New Profile"),
        risk_tolerance=d.get("risk_tolerance", "Moderate"),
        horizon=d.get("horizon", "Swing"),
        ma_weight=d.get("ma_weight", 0.25),
        volume_weight=d.get("volume_weight", 0.25),
        rsi_weight=d.get("rsi_weight", 0.25),
        momentum_weight=d.get("momentum_weight", 0.25),
        volume_spike_threshold=d.get("volume_spike_threshold", 1.5),
        rsi_overbought=d.get("rsi_overbought", 70.0),
        rsi_oversold=d.get("rsi_oversold", 30.0),
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
        hist = fetch_history(item.symbol)
        analysis = analyze_ticker(item.symbol, hist, profile)
        if analysis:
            analysis["name"] = item.name
            analysis["tier"] = item.tier
            analysis["description"] = generate_trade_description(analysis)
            results.append(analysis)

    # Sort: BUY first (desc confidence), then HOLD, then SELL
    order = {"BUY": 0, "HOLD": 1, "SELL": 2}
    results.sort(key=lambda x: (order.get(x["signal"], 1), -x["confidence"]))
    return jsonify(results)


@app.route("/api/suggestions/export/csv")
def export_suggestions_csv():
    watchlist = WatchlistItem.query.all()
    profile = get_active_profile_dict()
    rows = []

    for item in watchlist:
        hist = fetch_history(item.symbol)
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

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=rows[0].keys() if rows else [])
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
        "worst_performer": worst,
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


@app.route("/api/restore", methods=["POST"])
def restore():
    d = request.json or {}
    try:
        # Watchlist
        for item in d.get("watchlist", []):
            if not WatchlistItem.query.filter_by(symbol=item["symbol"]).first():
                db.session.add(WatchlistItem(
                    symbol=item["symbol"], name=item.get("name", ""),
                    tier=item.get("tier", "Active Watch"), notes=item.get("notes", ""),
                    alert_direction=item.get("alert_direction"),
                    alert_price=item.get("alert_price"),
                ))
        # Profiles
        for p in d.get("profiles", []):
            db.session.add(AnalysisProfile(
                name=p["name"], risk_tolerance=p.get("risk_tolerance", "Moderate"),
                horizon=p.get("horizon", "Swing"), ma_weight=p.get("ma_weight", 0.25),
                volume_weight=p.get("volume_weight", 0.25), rsi_weight=p.get("rsi_weight", 0.25),
                momentum_weight=p.get("momentum_weight", 0.25),
                volume_spike_threshold=p.get("volume_spike_threshold", 1.5),
                rsi_overbought=p.get("rsi_overbought", 70), rsi_oversold=p.get("rsi_oversold", 30),
                momentum_days=p.get("momentum_days", 10),
                max_trades_per_day=p.get("max_trades_per_day", 3),
                is_active=p.get("is_active", False),
            ))
        # Portfolio
        for pos in d.get("portfolio", []):
            db.session.add(PortfolioPosition(
                symbol=pos["symbol"], shares=pos["shares"],
                cost_basis=pos["cost_basis"], notes=pos.get("notes", ""),
                date_acquired=datetime.strptime(pos["date_acquired"], "%Y-%m-%d").date() if pos.get("date_acquired") else None,
            ))
        # Trades
        for t in d.get("trades", []):
            db.session.add(TradeLog(
                symbol=t["symbol"], action=t["action"], shares=t["shares"],
                price=t["price"], notes=t.get("notes", ""),
                date=datetime.strptime(t["date"], "%Y-%m-%d").date() if t.get("date") else date.today(),
                realized_pnl=t.get("realized_pnl"),
            ))
        # Preferences
        for key, value in d.get("preferences", {}).items():
            set_pref(key, value)

        db.session.commit()
        return jsonify({"ok": True})
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 400


if __name__ == "__main__":
	app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))