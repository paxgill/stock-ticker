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
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "symbol": self.symbol,
            "name": self.name,
            "tier": self.tier,
            "notes": self.notes,
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
