from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()


class WatchlistItem(db.Model):
    __tablename__ = "watchlist"
    id = db.Column(db.Integer, primary_key=True)
    symbol = db.Column(db.String(16), unique=True, nullable=False, index=True)
    name = db.Column(db.String(100), default="")
    tier = db.Column(db.String(20), default="Active Watch")
    notes = db.Column(db.Text, default="")
    alert_direction = db.Column(db.String(10))
    alert_price = db.Column(db.Float)
    tags = db.Column(db.String(255))   # comma-separated free-form tags
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "symbol": self.symbol,
            "name": self.name,
            "tier": self.tier,
            "notes": self.notes,
            "tags": [t.strip() for t in (self.tags or "").split(",") if t.strip()],
            "alert_direction": self.alert_direction,
            "alert_price": self.alert_price,
            "created_at": self.created_at.isoformat(),
        }


class AnalysisProfile(db.Model):
    __tablename__ = "analysis_profiles"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    risk_tolerance = db.Column(db.String(20), default="Moderate")
    horizon = db.Column(db.String(20), default="Swing")
    ma_weight = db.Column(db.Float, default=0.25)
    volume_weight = db.Column(db.Float, default=0.25)
    rsi_weight = db.Column(db.Float, default=0.25)
    momentum_weight = db.Column(db.Float, default=0.25)
    volume_spike_threshold = db.Column(db.Float, default=1.5)
    rsi_overbought = db.Column(db.Float, default=70.0)
    rsi_oversold = db.Column(db.Float, default=30.0)
    momentum_days = db.Column(db.Integer, default=10)
    max_trades_per_day = db.Column(db.Integer, default=3)
    is_active = db.Column(db.Boolean, default=False)
    preset_key = db.Column(db.String(40))  # set when created from a research preset
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "risk_tolerance": self.risk_tolerance,
            "horizon": self.horizon,
            "ma_weight": self.ma_weight,
            "volume_weight": self.volume_weight,
            "rsi_weight": self.rsi_weight,
            "momentum_weight": self.momentum_weight,
            "volume_spike_threshold": self.volume_spike_threshold,
            "rsi_overbought": self.rsi_overbought,
            "rsi_oversold": self.rsi_oversold,
            "momentum_days": self.momentum_days,
            "max_trades_per_day": self.max_trades_per_day,
            "is_active": self.is_active,
            "preset_key": self.preset_key,
            "created_at": self.created_at.isoformat(),
        }


class PortfolioPosition(db.Model):
    __tablename__ = "portfolio"
    id = db.Column(db.Integer, primary_key=True)
    symbol = db.Column(db.String(10), nullable=False, index=True)
    shares = db.Column(db.Float, nullable=False)
    cost_basis = db.Column(db.Float, nullable=False)
    date_acquired = db.Column(db.Date)
    notes = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "symbol": self.symbol,
            "shares": self.shares,
            "cost_basis": self.cost_basis,
            "date_acquired": self.date_acquired.isoformat() if self.date_acquired else None,
            "notes": self.notes,
            "created_at": self.created_at.isoformat(),
        }


class TradeLog(db.Model):
    __tablename__ = "trades"
    id = db.Column(db.Integer, primary_key=True)
    symbol = db.Column(db.String(10), nullable=False, index=True)
    action = db.Column(db.String(10), nullable=False)
    shares = db.Column(db.Float, nullable=False)
    price = db.Column(db.Float, nullable=False)
    date = db.Column(db.Date, nullable=False)
    notes = db.Column(db.Text, default="")
    signal_triggered = db.Column(db.Boolean, default=False)
    signal_type = db.Column(db.String(10))
    signal_confidence = db.Column(db.Float)
    realized_pnl = db.Column(db.Float)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "symbol": self.symbol,
            "action": self.action,
            "shares": self.shares,
            "price": self.price,
            "total": round(self.shares * self.price, 2),
            "date": self.date.isoformat() if self.date else None,
            "notes": self.notes,
            "signal_triggered": self.signal_triggered,
            "signal_type": self.signal_type,
            "signal_confidence": self.signal_confidence,
            "realized_pnl": self.realized_pnl,
            "created_at": self.created_at.isoformat(),
        }


class Preference(db.Model):
    __tablename__ = "preferences"
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(50), unique=True, nullable=False)
    value = db.Column(db.Text)


class FMPCache(db.Model):
    __tablename__ = "fmp_cache"
    id = db.Column(db.Integer, primary_key=True)
    symbol = db.Column(db.String(16), nullable=False)
    endpoint = db.Column(db.String(50), nullable=False)
    data = db.Column(db.Text)
    cached_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('symbol', 'endpoint', name='uix_fmp_symbol_endpoint'),
        db.Index('ix_fmp_symbol_endpoint', 'symbol', 'endpoint'),
    )


class QuoteCache(db.Model):
    """Last fetched quote payload per symbol — serves stale data on yfinance outage."""
    __tablename__ = "quote_cache"
    id = db.Column(db.Integer, primary_key=True)
    symbol = db.Column(db.String(16), unique=True, nullable=False, index=True)
    payload = db.Column(db.Text)
    fetched_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        import json
        d = json.loads(self.payload) if self.payload else {}
        d["_cached_at"] = self.fetched_at.isoformat() if self.fetched_at else None
        return d


class PricesDaily(db.Model):
    """Adjusted + raw daily OHLC cache so forward-return reads never hit the network."""
    __tablename__ = "prices_daily"
    id = db.Column(db.Integer, primary_key=True)
    symbol = db.Column(db.String(16), nullable=False, index=True)
    d = db.Column(db.Date, nullable=False, index=True)
    open_adj = db.Column(db.Float)
    high_adj = db.Column(db.Float)
    low_adj = db.Column(db.Float)
    close_adj = db.Column(db.Float)
    close_raw = db.Column(db.Float)
    __table_args__ = (db.UniqueConstraint("symbol", "d", name="uix_prices_symbol_date"),)


class SignalSnapshot(db.Model):
    """A point-in-time signal record (backfilled or live-captured) for accuracy scoring."""
    __tablename__ = "signal_snapshots"
    id = db.Column(db.Integer, primary_key=True)
    symbol = db.Column(db.String(16), nullable=False, index=True)
    snapshot_date = db.Column(db.Date, nullable=False, index=True)
    signal = db.Column(db.String(4))
    confidence = db.Column(db.Float)
    signed_score = db.Column(db.Float)
    regime = db.Column(db.String(24))
    price_close_adj = db.Column(db.Float)
    price_close_raw = db.Column(db.Float)
    price_next_open_adj = db.Column(db.Float)
    had_ma200 = db.Column(db.Boolean, default=True)
    profile_id = db.Column(db.Integer)
    preset_key = db.Column(db.String(40))
    profile_key = db.Column(db.String(48), nullable=False)   # coalesce(profile_id, preset_key, 'active')
    source = db.Column(db.String(8))                          # 'backfill' | 'live'
    ret_1d = db.Column(db.Float)
    ret_5d = db.Column(db.Float)
    ret_10d = db.Column(db.Float)
    ret_20d = db.Column(db.Float)
    ret_1d_no = db.Column(db.Float)
    ret_5d_no = db.Column(db.Float)
    ret_10d_no = db.Column(db.Float)
    ret_20d_no = db.Column(db.Float)
    matured_through = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint("symbol", "snapshot_date", "profile_key", name="uix_snap_sym_date_profile"),
        db.Index("ix_snap_symbol_date", "symbol", "snapshot_date"),
        db.Index("ix_snap_date", "snapshot_date"),
    )

    def to_dict(self):
        return {
            "id": self.id, "symbol": self.symbol,
            "snapshot_date": self.snapshot_date.isoformat() if self.snapshot_date else None,
            "signal": self.signal, "confidence": self.confidence, "signed_score": self.signed_score,
            "regime": self.regime, "price_close_adj": self.price_close_adj,
            "price_close_raw": self.price_close_raw, "price_next_open_adj": self.price_next_open_adj,
            "had_ma200": self.had_ma200, "source": self.source,
            "ret_1d": self.ret_1d, "ret_5d": self.ret_5d, "ret_10d": self.ret_10d, "ret_20d": self.ret_20d,
            "ret_1d_no": self.ret_1d_no, "ret_5d_no": self.ret_5d_no,
            "ret_10d_no": self.ret_10d_no, "ret_20d_no": self.ret_20d_no,
            "matured_through": self.matured_through,
        }


class AlertEvent(db.Model):
    """A recorded alert trigger (from the server-side alert engine or client)."""
    __tablename__ = "alert_events"
    id = db.Column(db.Integer, primary_key=True)
    symbol = db.Column(db.String(16), nullable=False, index=True)
    direction = db.Column(db.String(10))
    threshold = db.Column(db.Float)
    price = db.Column(db.Float)
    seen = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id, "symbol": self.symbol, "direction": self.direction,
            "threshold": self.threshold, "price": self.price, "seen": self.seen,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
