"""
options_signals.py — Options signal engine
Black-Scholes Greeks, IVR calculator, ticker scorer, trade builder.
MIT License — https://pg-stock-ticker.up.railway.app
"""
import math
from datetime import date, datetime
import yfinance as yf
import pandas as pd

# ─── Constants ────────────────────────────────────────────────────────────────
RISK_FREE_RATE = 0.05   # approximate risk-free rate
IV_FALLBACK    = 0.25   # 25% IV when options data unavailable

# ─── Strategy configs (mirrors options_strategies.js scoring fields) ──────────
STRATEGY_CONFIGS = {
    'iron_condor':      {'ivr_min': 50,   'ivr_max': None, 'rsi_signal': 'neutral',    'iv_regime': 'high', 'trend_requirement': 'consolidating',  'dte_entry': 45,  'profit_target_pct': 50},
    'iron_butterfly':   {'ivr_min': 60,   'ivr_max': None, 'rsi_signal': 'neutral',    'iv_regime': 'high', 'trend_requirement': 'consolidating',  'dte_entry': 30,  'profit_target_pct': 25},
    'bull_put_spread':  {'ivr_min': 30,   'ivr_max': None, 'rsi_signal': 'oversold',   'iv_regime': 'any',  'trend_requirement': 'bullish',         'dte_entry': 30,  'profit_target_pct': 50},
    'bear_call_spread': {'ivr_min': 30,   'ivr_max': None, 'rsi_signal': 'overbought', 'iv_regime': 'any',  'trend_requirement': 'bearish',         'dte_entry': 30,  'profit_target_pct': 50},
    'jade_lizard':      {'ivr_min': 50,   'ivr_max': None, 'rsi_signal': 'neutral',    'iv_regime': 'high', 'trend_requirement': 'any',             'dte_entry': 45,  'profit_target_pct': 50},
    'bwb_call':         {'ivr_min': 30,   'ivr_max': None, 'rsi_signal': 'neutral',    'iv_regime': 'any',  'trend_requirement': 'consolidating',   'dte_entry': 30,  'profit_target_pct': 50},
    'bwb_put':          {'ivr_min': 30,   'ivr_max': None, 'rsi_signal': 'neutral',    'iv_regime': 'any',  'trend_requirement': 'consolidating',   'dte_entry': 30,  'profit_target_pct': 50},
    'put_ratio_spread': {'ivr_min': 40,   'ivr_max': None, 'rsi_signal': 'overbought', 'iv_regime': 'high', 'trend_requirement': 'bearish',         'dte_entry': 45,  'profit_target_pct': 50},
    'calendar_spread':  {'ivr_min': None, 'ivr_max': 40,   'rsi_signal': 'neutral',    'iv_regime': 'low',  'trend_requirement': 'consolidating',   'dte_entry': 45,  'profit_target_pct': 25},
    'diagonal_spread':  {'ivr_min': None, 'ivr_max': 50,   'rsi_signal': 'neutral',    'iv_regime': 'any',  'trend_requirement': 'bullish',         'dte_entry': 60,  'profit_target_pct': 25},
    'pmcc':             {'ivr_min': None, 'ivr_max': 50,   'rsi_signal': 'oversold',   'iv_regime': 'any',  'trend_requirement': 'bullish',         'dte_entry': 180, 'profit_target_pct': 50},
    'long_call_put':    {'ivr_min': None, 'ivr_max': 35,   'rsi_signal': 'any',        'iv_regime': 'low',  'trend_requirement': 'any',             'dte_entry': 45,  'profit_target_pct': 100},
    'short_strangle':   {'ivr_min': 60,   'ivr_max': None, 'rsi_signal': 'neutral',    'iv_regime': 'high', 'trend_requirement': 'consolidating',   'dte_entry': 45,  'profit_target_pct': 50},
    'short_straddle':   {'ivr_min': 70,   'ivr_max': None, 'rsi_signal': 'neutral',    'iv_regime': 'high', 'trend_requirement': 'consolidating',   'dte_entry': 30,  'profit_target_pct': 25},
    'the_wheel':        {'ivr_min': 30,   'ivr_max': None, 'rsi_signal': 'oversold',   'iv_regime': 'any',  'trend_requirement': 'bullish',         'dte_entry': 30,  'profit_target_pct': 50},
}


# ─── Black-Scholes helpers ────────────────────────────────────────────────────
def _norm_cdf(x):
    """Cumulative standard normal distribution (Horner's method approximation)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2)))


def _norm_pdf(x):
    """Standard normal probability density function."""
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def bs_greeks(S, K, T, r, sigma, option_type='call'):
    """
    Compute Black-Scholes option price and Greeks.

    S      : current underlying price
    K      : strike price
    T      : time to expiry in years  (e.g. 45/365)
    r      : risk-free rate (e.g. 0.05)
    sigma  : implied volatility (e.g. 0.25 for 25%)
    option_type : 'call' | 'put'

    Returns dict: price, delta, gamma, theta (per day), vega (per 1% IV move).
    """
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return {'price': 0.0, 'delta': 0.0, 'gamma': 0.0, 'theta': 0.0, 'vega': 0.0}

    sqrt_T = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T

    n_d1 = _norm_pdf(d1)
    discount = math.exp(-r * T)

    if option_type.lower() == 'call':
        price = S * _norm_cdf(d1) - K * discount * _norm_cdf(d2)
        delta = _norm_cdf(d1)
        theta = (-(S * n_d1 * sigma) / (2 * sqrt_T)
                 - r * K * discount * _norm_cdf(d2)) / 365
    else:
        price = K * discount * _norm_cdf(-d2) - S * _norm_cdf(-d1)
        delta = _norm_cdf(d1) - 1.0
        theta = (-(S * n_d1 * sigma) / (2 * sqrt_T)
                 + r * K * discount * _norm_cdf(-d2)) / 365

    gamma = n_d1 / (S * sigma * sqrt_T)
    vega  = S * n_d1 * sqrt_T / 100   # per 1% IV change

    return {
        'price': round(max(price, 0.0), 4),
        'delta': round(delta, 4),
        'gamma': round(gamma, 6),
        'theta': round(theta, 4),
        'vega':  round(vega,  4),
    }


# ─── IVR Calculator ───────────────────────────────────────────────────────────
def compute_ivr(ticker_obj):
    """
    Compute IV Rank (0-100) and current ATM IV.

    Uses the nearest expiry's ATM implied volatility as "current IV".
    Approximates the 52-week IV range from rolling 20-day realised vol.

    Returns (ivr: float|None, current_iv: float).
    """
    try:
        exps = list(ticker_obj.options)
        if not exps:
            return None, IV_FALLBACK

        chain = ticker_obj.option_chain(exps[0])

        info = ticker_obj.fast_info
        spot = float(info.last_price or 0)
        if spot <= 0:
            return None, IV_FALLBACK

        # ATM IV — average the 4 nearest strikes across calls + puts
        combined = pd.concat([chain.calls, chain.puts])
        combined['dist'] = (combined['strike'] - spot).abs()
        atm_rows = combined.sort_values('dist').head(4)
        ivs = atm_rows['impliedVolatility'].dropna().tolist()
        current_iv = float(pd.Series(ivs).mean()) if ivs else IV_FALLBACK

        # 52-week historical vol range as proxy for IV range
        hist = ticker_obj.history(period='1y')
        if len(hist) < 30:
            return None, current_iv

        closes  = hist['Close'].astype(float)
        returns = closes.pct_change().dropna()
        rolling_vol = returns.rolling(20).std() * math.sqrt(252)

        iv_low  = float(rolling_vol.quantile(0.10))
        iv_high = float(rolling_vol.quantile(0.90))

        if iv_high <= iv_low:
            return 50.0, current_iv

        ivr = (current_iv - iv_low) / (iv_high - iv_low) * 100
        ivr = max(0.0, min(100.0, ivr))
        return round(ivr, 1), round(current_iv, 4)

    except Exception:
        return None, IV_FALLBACK


# ─── DTE Helpers ─────────────────────────────────────────────────────────────
def _dte(exp_str):
    """Return integer days to expiry from a YYYY-MM-DD string."""
    try:
        exp = datetime.strptime(exp_str, '%Y-%m-%d').date()
        return max(0, (exp - date.today()).days)
    except Exception:
        return 0


def _nearest_expiry(exps, target_dte=45):
    """Return the expiry string closest to target_dte."""
    if not exps:
        return None
    return min(exps, key=lambda e: abs(_dte(e) - target_dte))


# ─── Trade Builder ────────────────────────────────────────────────────────────
def build_trade(symbol, ticker_obj, strategy, spot, iv):
    """
    Construct a concrete option trade for a given strategy.

    Returns a dict:
        expiry, dte, legs, credit_debit (+ = credit, - = debit),
        max_gain, max_loss, profit_target_pct.
    """
    try:
        exps = list(ticker_obj.options)
        if not exps:
            return None

        target_dte = strategy.get('dte_entry', 45)
        expiry = _nearest_expiry(exps, target_dte)
        if not expiry:
            return None

        dte_val = _dte(expiry)
        T = max(dte_val / 365.0, 0.001)
        r = RISK_FREE_RATE
        sid = strategy.get('id', strategy.get('_id', ''))

        legs = []
        credit_debit = 0.0
        max_loss = None
        max_gain = None

        def c(K): return bs_greeks(spot, K, T, r, iv, 'call')
        def p(K): return bs_greeks(spot, K, T, r, iv, 'put')
        def rnd(v, pct): return round(spot * pct / (spot * 0.01)) * (spot * 0.01) if spot > 0 else round(spot * pct)

        # Helper: round to nearest clean strike
        def strike(pct):
            raw = spot * pct
            # Round to sensible granularity
            if spot < 20:   return round(raw, 1)
            if spot < 100:  return round(raw / 0.5) * 0.5
            if spot < 500:  return round(raw)
            return round(raw / 5) * 5

        if sid == 'iron_condor':
            cs_k, cl_k = strike(1.05), strike(1.10)
            ps_k, pl_k = strike(0.95), strike(0.90)
            cs, cl_g = c(cs_k), c(cl_k)
            ps, pl_g = p(ps_k), p(pl_k)
            credit = (cs['price'] - cl_g['price']) + (ps['price'] - pl_g['price'])
            legs = [
                {'action': 'SELL', 'type': 'CALL', 'strike': cs_k, 'qty': 1, 'price': cs['price']},
                {'action': 'BUY',  'type': 'CALL', 'strike': cl_k, 'qty': 1, 'price': cl_g['price']},
                {'action': 'SELL', 'type': 'PUT',  'strike': ps_k, 'qty': 1, 'price': ps['price']},
                {'action': 'BUY',  'type': 'PUT',  'strike': pl_k, 'qty': 1, 'price': pl_g['price']},
            ]
            credit_debit = round(credit * 100, 2)
            max_gain = round(credit * 100, 2)
            max_loss = round((cl_k - cs_k - credit) * 100, 2)

        elif sid == 'iron_butterfly':
            atm = strike(1.0)
            wing = max(1.0, round(spot * 0.05))
            cl_k, pl_k = atm + wing, atm - wing
            atm_c, atm_p = c(atm), p(atm)
            cl_g, pl_g   = c(cl_k), p(pl_k)
            credit = (atm_c['price'] + atm_p['price']) - (cl_g['price'] + pl_g['price'])
            legs = [
                {'action': 'SELL', 'type': 'CALL', 'strike': atm,  'qty': 1, 'price': atm_c['price']},
                {'action': 'SELL', 'type': 'PUT',  'strike': atm,  'qty': 1, 'price': atm_p['price']},
                {'action': 'BUY',  'type': 'CALL', 'strike': cl_k, 'qty': 1, 'price': cl_g['price']},
                {'action': 'BUY',  'type': 'PUT',  'strike': pl_k, 'qty': 1, 'price': pl_g['price']},
            ]
            credit_debit = round(credit * 100, 2)
            max_gain = round(credit * 100, 2)
            max_loss = round((wing - credit) * 100, 2)

        elif sid in ('the_wheel', 'bull_put_spread'):
            ps_k = strike(0.97)
            pl_k = strike(0.92) if sid == 'bull_put_spread' else None
            ps_g = p(ps_k)
            if pl_k:
                pl_g = p(pl_k)
                credit = ps_g['price'] - pl_g['price']
                legs = [
                    {'action': 'SELL', 'type': 'PUT', 'strike': ps_k, 'qty': 1, 'price': ps_g['price']},
                    {'action': 'BUY',  'type': 'PUT', 'strike': pl_k, 'qty': 1, 'price': pl_g['price']},
                ]
                max_loss = round((ps_k - pl_k - credit) * 100, 2)
            else:
                credit = ps_g['price']
                legs = [
                    {'action': 'SELL', 'type': 'PUT', 'strike': ps_k, 'qty': 1, 'price': ps_g['price']},
                ]
                max_loss = round((ps_k - credit) * 100, 2)
            credit_debit = round(credit * 100, 2)
            max_gain = round(credit * 100, 2)

        elif sid == 'bear_call_spread':
            cs_k = strike(1.03)
            cl_k = strike(1.08)
            cs_g, cl_g = c(cs_k), c(cl_k)
            credit = cs_g['price'] - cl_g['price']
            legs = [
                {'action': 'SELL', 'type': 'CALL', 'strike': cs_k, 'qty': 1, 'price': cs_g['price']},
                {'action': 'BUY',  'type': 'CALL', 'strike': cl_k, 'qty': 1, 'price': cl_g['price']},
            ]
            credit_debit = round(credit * 100, 2)
            max_gain = round(credit * 100, 2)
            max_loss = round((cl_k - cs_k - credit) * 100, 2)

        elif sid == 'jade_lizard':
            put_k  = strike(0.97)
            cs_k   = strike(1.03)
            cl_k   = strike(1.08)
            p_g    = p(put_k)
            cs_g, cl_g = c(cs_k), c(cl_k)
            credit = p_g['price'] + cs_g['price'] - cl_g['price']
            legs = [
                {'action': 'SELL', 'type': 'PUT',  'strike': put_k, 'qty': 1, 'price': p_g['price']},
                {'action': 'SELL', 'type': 'CALL', 'strike': cs_k,  'qty': 1, 'price': cs_g['price']},
                {'action': 'BUY',  'type': 'CALL', 'strike': cl_k,  'qty': 1, 'price': cl_g['price']},
            ]
            credit_debit = round(credit * 100, 2)
            max_gain = round(credit * 100, 2)
            max_loss = round((put_k - credit) * 100, 2)

        elif sid in ('bwb_call', 'bwb_put'):
            if sid == 'bwb_call':
                k1, k2, k3 = strike(1.00), strike(1.05), strike(1.12)
                g1, g2, g3 = c(k1), c(k2), c(k3)
                credit = -g1['price'] + 2 * g2['price'] - g3['price']
                legs = [
                    {'action': 'BUY',  'type': 'CALL', 'strike': k1, 'qty': 1, 'price': g1['price']},
                    {'action': 'SELL', 'type': 'CALL', 'strike': k2, 'qty': 2, 'price': g2['price']},
                    {'action': 'BUY',  'type': 'CALL', 'strike': k3, 'qty': 1, 'price': g3['price']},
                ]
            else:
                k1, k2, k3 = strike(0.88), strike(0.95), strike(1.00)
                g1, g2, g3 = p(k1), p(k2), p(k3)
                credit = -g1['price'] + 2 * g2['price'] - g3['price']
                legs = [
                    {'action': 'BUY',  'type': 'PUT', 'strike': k1, 'qty': 1, 'price': g1['price']},
                    {'action': 'SELL', 'type': 'PUT', 'strike': k2, 'qty': 2, 'price': g2['price']},
                    {'action': 'BUY',  'type': 'PUT', 'strike': k3, 'qty': 1, 'price': g3['price']},
                ]
            credit_debit = round(credit * 100, 2)
            max_gain = round(abs(k2 - k1) * 100, 2)
            max_loss = round(abs(credit) * 100, 2) if credit < 0 else 0

        elif sid == 'put_ratio_spread':
            ps_k = strike(0.97)
            pl_k = strike(0.92)
            ps_g, pl_g = p(ps_k), p(pl_k)
            credit = 2 * ps_g['price'] - pl_g['price']
            legs = [
                {'action': 'BUY',  'type': 'PUT', 'strike': pl_k, 'qty': 1, 'price': pl_g['price']},
                {'action': 'SELL', 'type': 'PUT', 'strike': ps_k, 'qty': 2, 'price': ps_g['price']},
            ]
            credit_debit = round(credit * 100, 2)
            max_gain = round((ps_k - pl_k + credit) * 100, 2)
            max_loss = round((ps_k - credit) * 100, 2)

        elif sid in ('calendar_spread', 'diagonal_spread'):
            # Near leg (short) + far leg (long)
            far_exps = [e for e in exps if _dte(e) >= target_dte]
            near_exps = [e for e in exps if _dte(e) < target_dte]
            near_exp = _nearest_expiry(near_exps or exps, 30)
            far_exp  = _nearest_expiry(far_exps or exps, target_dte)
            atm = strike(1.0)
            near_T = max(_dte(near_exp) / 365.0, 0.001) if near_exp else T
            far_T  = max(_dte(far_exp)  / 365.0, 0.001) if far_exp  else T * 2
            near_g = bs_greeks(spot, atm, near_T, r, iv, 'call')
            far_g  = bs_greeks(spot, atm, far_T,  r, iv, 'call')
            debit  = far_g['price'] - near_g['price']
            legs = [
                {'action': 'SELL', 'type': 'CALL', 'strike': atm, 'qty': 1, 'price': near_g['price'], 'expiry': near_exp or expiry},
                {'action': 'BUY',  'type': 'CALL', 'strike': atm, 'qty': 1, 'price': far_g['price'],  'expiry': far_exp  or expiry},
            ]
            credit_debit = -round(debit * 100, 2)
            max_loss = round(debit * 100, 2)
            max_gain = None

        elif sid == 'pmcc':
            # Poor Man's Covered Call: Buy deep ITM LEAP + Sell near OTM call
            leap_k   = strike(0.75)
            short_k  = strike(1.03)
            leap_exps = [e for e in exps if _dte(e) >= 180]
            leap_exp  = _nearest_expiry(leap_exps or exps, 180)
            leap_T    = max(_dte(leap_exp) / 365.0, 0.001) if leap_exp else 0.5
            leap_g    = bs_greeks(spot, leap_k,  leap_T, r, iv, 'call')
            near_g    = bs_greeks(spot, short_k, T,      r, iv, 'call')
            debit     = leap_g['price'] - near_g['price']
            legs = [
                {'action': 'BUY',  'type': 'CALL', 'strike': leap_k,  'qty': 1, 'price': leap_g['price'], 'expiry': leap_exp or expiry},
                {'action': 'SELL', 'type': 'CALL', 'strike': short_k, 'qty': 1, 'price': near_g['price']},
            ]
            credit_debit = -round(debit * 100, 2)
            max_loss = round(debit * 100, 2)
            max_gain = round((short_k - leap_k + near_g['price']) * 100, 2)

        elif sid == 'long_call_put':
            atm = strike(1.0)
            c_g = c(atm)
            p_g = p(atm)
            legs = [
                {'action': 'BUY', 'type': 'CALL', 'strike': atm, 'qty': 1, 'price': c_g['price']},
                {'action': 'BUY', 'type': 'PUT',  'strike': atm, 'qty': 1, 'price': p_g['price']},
            ]
            debit = c_g['price'] + p_g['price']
            credit_debit = -round(debit * 100, 2)
            max_loss = round(debit * 100, 2)
            max_gain = None

        elif sid == 'short_strangle':
            call_k = strike(1.05)
            put_k  = strike(0.95)
            cs_g, ps_g = c(call_k), p(put_k)
            credit = cs_g['price'] + ps_g['price']
            legs = [
                {'action': 'SELL', 'type': 'CALL', 'strike': call_k, 'qty': 1, 'price': cs_g['price']},
                {'action': 'SELL', 'type': 'PUT',  'strike': put_k,  'qty': 1, 'price': ps_g['price']},
            ]
            credit_debit = round(credit * 100, 2)
            max_gain = round(credit * 100, 2)
            max_loss = None   # unlimited

        elif sid == 'short_straddle':
            atm = strike(1.0)
            cs_g, ps_g = c(atm), p(atm)
            credit = cs_g['price'] + ps_g['price']
            legs = [
                {'action': 'SELL', 'type': 'CALL', 'strike': atm, 'qty': 1, 'price': cs_g['price']},
                {'action': 'SELL', 'type': 'PUT',  'strike': atm, 'qty': 1, 'price': ps_g['price']},
            ]
            credit_debit = round(credit * 100, 2)
            max_gain = round(credit * 100, 2)
            max_loss = None   # unlimited

        else:
            # Generic ATM call for unrecognised strategies
            atm = strike(1.0)
            c_g = c(atm)
            legs = [{'action': 'BUY', 'type': 'CALL', 'strike': atm, 'qty': 1, 'price': c_g['price']}]
            credit_debit = -round(c_g['price'] * 100, 2)
            max_loss = round(c_g['price'] * 100, 2)

        return {
            'expiry':            expiry,
            'dte':               dte_val,
            'legs':              legs,
            'credit_debit':      credit_debit,
            'max_gain':          max_gain,
            'max_loss':          max_loss,
            'profit_target_pct': strategy.get('profit_target_pct', 50),
        }

    except Exception:
        return None


# ─── Ticker Scorer ────────────────────────────────────────────────────────────
def score_ticker(symbol, strategy_id, param_overrides=None):
    """
    Score a single ticker against a strategy.

    param_overrides (dict) may contain:
        ivr_min, ivr_max, rsi_signal, iv_regime, trend_requirement

    Returns None on data error, else:
        {ticker, signal, confidence, reasons, spot, current_iv, ivr, rsi, trend, suggested_trade}
    """
    from analysis import compute_rsi

    strategy = dict(STRATEGY_CONFIGS.get(strategy_id, {}))
    if not strategy:
        strategy = {'ivr_min': None, 'ivr_max': None, 'rsi_signal': 'any',
                    'iv_regime': 'any', 'trend_requirement': 'any',
                    'dte_entry': 45, 'profit_target_pct': 50}
    strategy['id'] = strategy_id

    if param_overrides:
        strategy.update(param_overrides)

    reasons   = []
    score_pts = 0   # accumulates to 100

    try:
        t    = yf.Ticker(symbol)
        info = t.fast_info
        spot = float(info.last_price or 0)
        if spot <= 0:
            return None

        # ── IVR / IV ──────────────────────────────────────────────────────
        ivr, current_iv = compute_ivr(t)

        # ── Price history + technicals ────────────────────────────────────
        hist = t.history(period='6mo', interval='1d')
        if len(hist) < 20:
            return None

        closes = hist['Close'].astype(float)
        rsi    = compute_rsi(closes)

        ma20 = float(closes.tail(20).mean())
        ma50 = float(closes.tail(50).mean()) if len(closes) >= 50 else ma20
        ma200 = float(closes.tail(200).mean()) if len(closes) >= 200 else None

        if spot > ma20 and spot > ma50:
            trend = 'bullish'
        elif spot < ma20 and spot < ma50:
            trend = 'bearish'
        else:
            trend = 'consolidating'

        # ── Score: IVR (35 pts) ──────────────────────────────────────────
        ivr_min = strategy.get('ivr_min')
        ivr_max = strategy.get('ivr_max')

        if ivr is not None:
            if ivr_min is not None and ivr < ivr_min:
                reasons.append(f'IVR {ivr:.0f} below required minimum ({ivr_min})')
            elif ivr_max is not None and ivr > ivr_max:
                reasons.append(f'IVR {ivr:.0f} above required maximum ({ivr_max})')
            else:
                score_pts += 35
                reasons.append(f'IVR {ivr:.0f} — meets strategy criteria')
        else:
            score_pts += 18   # partial credit
            reasons.append('IV data limited — scoring on technicals only')

        # ── Score: RSI (25 pts) ───────────────────────────────────────────
        rsi_signal = strategy.get('rsi_signal', 'neutral')
        if rsi_signal == 'oversold':
            if rsi < 35:
                score_pts += 25
                reasons.append(f'RSI {rsi:.1f} oversold — favorable for strategy')
            elif rsi < 45:
                score_pts += 13
                reasons.append(f'RSI {rsi:.1f} slightly oversold')
            else:
                reasons.append(f'RSI {rsi:.1f} — strategy wants RSI < 35')
        elif rsi_signal == 'overbought':
            if rsi > 65:
                score_pts += 25
                reasons.append(f'RSI {rsi:.1f} overbought — favorable for strategy')
            elif rsi > 55:
                score_pts += 13
                reasons.append(f'RSI {rsi:.1f} slightly overbought')
            else:
                reasons.append(f'RSI {rsi:.1f} — strategy wants RSI > 65')
        elif rsi_signal == 'any':
            score_pts += 20
        else:  # neutral
            if 35 <= rsi <= 65:
                score_pts += 25
                reasons.append(f'RSI {rsi:.1f} — neutral range favors strategy')
            else:
                score_pts += 12
                reasons.append(f'RSI {rsi:.1f} — strategy prefers neutral RSI 35–65')

        # ── Score: Trend (20 pts) ─────────────────────────────────────────
        trend_req = strategy.get('trend_requirement', 'any')
        if trend_req in ('any', None):
            score_pts += 20
        elif trend_req in ('above_200sma', 'bullish'):
            # both 'above_200sma' and 'bullish' mean we want uptrend
            if ma200 is not None and spot > ma200:
                score_pts += 20
                reasons.append('Price above MA200 — uptrend confirmed')
            elif trend == 'bullish':
                score_pts += 15
                reasons.append('Price above MA20/MA50 — short-term bullish')
            else:
                reasons.append(f'Trend is {trend} — strategy wants bullish')
        elif trend_req in ('below_200sma', 'bearish'):
            if ma200 is not None and spot < ma200:
                score_pts += 20
                reasons.append('Price below MA200 — downtrend confirmed')
            elif trend == 'bearish':
                score_pts += 15
                reasons.append('Price below MA20/MA50 — short-term bearish')
            else:
                reasons.append(f'Trend is {trend} — strategy wants bearish')
        elif trend_req == 'consolidating':
            if trend == 'consolidating':
                score_pts += 20
                reasons.append('Price consolidating — ideal for neutral strategies')
            else:
                score_pts += 8
                reasons.append(f'Trend is {trend} — strategy prefers consolidation')

        # ── Score: IV Regime (20 pts) ─────────────────────────────────────
        iv_regime = strategy.get('iv_regime', 'any')
        if iv_regime == 'any':
            score_pts += 20
        elif ivr is not None:
            if iv_regime == 'high' and ivr >= 50:
                score_pts += 20
                reasons.append('High IV confirmed — premium selling conditions')
            elif iv_regime == 'low' and ivr < 40:
                score_pts += 20
                reasons.append('Low IV — premium buying conditions')
            elif iv_regime == 'high' and ivr < 50:
                reasons.append(f'IV too low (IVR {ivr:.0f}) — strategy wants elevated IV')
            elif iv_regime == 'low' and ivr >= 40:
                reasons.append(f'IV elevated (IVR {ivr:.0f}) — strategy wants low IV')

        # ── Clamp and classify ────────────────────────────────────────────
        confidence = min(100, max(0, score_pts))
        if confidence >= 65:
            signal = 'OPEN'
        elif confidence >= 38:
            signal = 'MONITOR'
        else:
            signal = 'AVOID'

        suggested_trade = build_trade(symbol, t, strategy, spot, current_iv)

        return {
            'ticker':          symbol,
            'signal':          signal,
            'confidence':      confidence,
            'reasons':         reasons,
            'spot':            round(spot, 2),
            'current_iv':      round(current_iv * 100, 1),
            'ivr':             round(ivr, 1) if ivr is not None else None,
            'rsi':             round(rsi, 1),
            'trend':           trend,
            'suggested_trade': suggested_trade,
        }

    except Exception:
        return None


# ─── Public API functions ─────────────────────────────────────────────────────
def recommend_for_strategy(strategy_id, tickers, param_overrides=None):
    """
    Score every ticker in `tickers` against `strategy_id`.
    Returns list sorted by descending confidence, AVOID entries last.
    """
    results = []
    for sym in tickers:
        r = score_ticker(sym.upper().strip(), strategy_id, param_overrides)
        if r:
            results.append(r)

    signal_order = {'OPEN': 0, 'MONITOR': 1, 'AVOID': 2}
    results.sort(key=lambda x: (signal_order.get(x['signal'], 9), -x['confidence']))
    return results


def scan_top_n(strategy_id, tickers, n=10, param_overrides=None):
    """
    Scan tickers and return top-N by confidence for the given strategy.
    Only returns OPEN and MONITOR signals.
    """
    all_results = recommend_for_strategy(strategy_id, tickers, param_overrides)
    filtered = [r for r in all_results if r['signal'] in ('OPEN', 'MONITOR')]
    return filtered[:n]
