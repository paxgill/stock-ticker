"""
Rule-based natural language generation for market summaries and trade descriptions.
No AI calls — pure string templates driven by yfinance data.
"""
import yfinance as yf


# ─── Market Summary ───────────────────────────────────────────────────────────

INDICES = {
    "^GSPC": "S&P 500",
    "^IXIC": "Nasdaq",
    "^DJI":  "Dow Jones",
}


def fetch_index_data() -> dict:
    """Returns {symbol: {name, price, pct, change}} for the three major indices."""
    results = {}
    for sym, name in INDICES.items():
        try:
            hist = yf.Ticker(sym).history(period="2d")
            if len(hist) < 2:
                continue
            closes = hist["Close"].astype(float)
            prev, cur = float(closes.iloc[-2]), float(closes.iloc[-1])
            change = cur - prev
            pct = (change / prev * 100) if prev else 0.0
            results[sym] = {"name": name, "price": cur, "pct": round(pct, 2), "change": round(change, 2)}
        except Exception:
            pass
    return results


def _magnitude_verb(pct: float) -> str:
    a = abs(pct)
    if a >= 2.0:
        return "surged" if pct > 0 else "plunged"
    if a >= 1.0:
        return "gained" if pct > 0 else "fell"
    if a >= 0.3:
        return "edged higher" if pct > 0 else "slipped"
    return "was little changed"


def _sign_str(pct: float) -> str:
    return f"+{pct:.2f}%" if pct >= 0 else f"{pct:.2f}%"


def _index_phrase(name: str, pct: float) -> str:
    """Build 'the S&P 500 gained +0.72%' without mangling mixed-case names."""
    verb = _magnitude_verb(pct)
    if verb == "was little changed":
        return f"the {name} was little changed ({_sign_str(pct)})"
    return f"the {name} {verb} {_sign_str(pct)}"


def _capitalize_first(s: str) -> str:
    """Uppercase only the very first character, preserving the rest."""
    return s[0].upper() + s[1:] if s else s


def generate_market_summary(indices: dict) -> str:
    """
    Generates a 3–4 sentence plain-English market summary.
    `indices` is the dict returned by fetch_index_data().
    """
    if not indices:
        return "Market index data is unavailable at this time."

    sp  = indices.get("^GSPC")
    nq  = indices.get("^IXIC")
    dj  = indices.get("^DJI")

    valid = [v for v in [sp, nq, dj] if v is not None]
    if not valid:
        return "Market index data could not be loaded."

    pcts = {v["name"]: v["pct"] for v in valid}
    avg_pct = sum(pcts.values()) / len(pcts)
    n_up = sum(1 for p in pcts.values() if p > 0)
    n_dn = len(pcts) - n_up

    # ── Sentence 1: overall direction ────────────────────────────────────────
    if n_up == len(valid):
        if avg_pct >= 1.5:
            opener = "Markets closed broadly and strongly higher today."
        elif avg_pct >= 0.5:
            opener = "Markets closed broadly higher today."
        else:
            opener = "Markets finished with modest gains across the board today."
    elif n_dn == len(valid):
        if avg_pct <= -1.5:
            opener = "Markets fell sharply today with broad-based selling."
        elif avg_pct <= -0.5:
            opener = "Markets closed broadly lower today."
        else:
            opener = "Markets finished with modest losses across the board today."
    elif n_up > n_dn:
        opener = "Markets posted a mixed session, leaning higher today."
    else:
        opener = "Markets posted a mixed session, leaning lower today."

    # ── Sentence 2: index rundown ────────────────────────────────────────────
    parts = []
    if sp:
        parts.append(_index_phrase("S&P 500", sp["pct"]))
    if nq:
        parts.append(_index_phrase("Nasdaq", nq["pct"]))
    if dj:
        parts.append(_index_phrase("Dow", dj["pct"]))

    if len(parts) == 3:
        rundown = f"{_capitalize_first(parts[0])}, {parts[1]}, and {parts[2]}."
    elif len(parts) == 2:
        rundown = f"{_capitalize_first(parts[0])} and {parts[1]}."
    else:
        rundown = f"{_capitalize_first(parts[0])}."

    # ── Sentence 3: leader callout ────────────────────────────────────────────
    if len(valid) > 1:
        leader = max(valid, key=lambda v: v["pct"])
        laggard = min(valid, key=lambda v: v["pct"])
        leader_str = leader["name"]
        if leader["pct"] > 0 and abs(leader["pct"]) > abs(laggard["pct"]) + 0.3:
            leader_note = f"{leader_str} led the session with the strongest gain."
        elif leader["pct"] < 0 and abs(laggard["pct"]) > abs(leader["pct"]) + 0.3:
            leader_note = f"{laggard['name']} bore the brunt of today's selling."
        else:
            leader_note = "Moves were broadly uniform across the major averages."
    else:
        leader_note = ""

    # ── Sentence 4: sentiment tag ────────────────────────────────────────────
    if avg_pct > 1.5:
        sentiment = "Risk-on sentiment dominated the session."
    elif avg_pct > 0.5:
        sentiment = "Sentiment was broadly positive."
    elif avg_pct > 0.1:
        sentiment = "Sentiment was cautiously optimistic."
    elif avg_pct > -0.1:
        sentiment = "Investors appeared largely neutral heading into the close."
    elif avg_pct > -0.5:
        sentiment = "Caution crept in as sellers outweighed buyers late in the day."
    elif avg_pct > -1.5:
        sentiment = "Selling pressure weighed on the broader market through the session."
    else:
        sentiment = "Risk-off sentiment prevailed, with heavy selling across sectors."

    sentences = [opener, rundown]
    if leader_note:
        sentences.append(leader_note)
    sentences.append(sentiment)
    return " ".join(sentences)


# ─── Trade Description ────────────────────────────────────────────────────────

def generate_trade_description(analysis: dict) -> str:
    """
    Generates a 2–3 sentence plain-English explanation of why a signal fired.
    `analysis` is a single entry from /api/suggestions.
    """
    symbol     = analysis.get("symbol", "?")
    signal     = analysis.get("signal", "HOLD")
    confidence = analysis.get("confidence", 50)
    signals    = analysis.get("signals", [])
    price      = analysis.get("price")

    # ── Sentence 1: headline ──────────────────────────────────────────────────
    conf_word = (
        "strong" if confidence >= 75 else
        "moderate" if confidence >= 55 else
        "weak"
    )
    if signal == "BUY":
        headline = (
            f"{symbol} is showing a {conf_word} BUY signal at {confidence}% confidence."
        )
    elif signal == "SELL":
        headline = (
            f"{symbol} is flashing a {conf_word} SELL signal at {confidence}% confidence."
        )
    else:
        headline = (
            f"{symbol} is rated HOLD at {confidence}% confidence — "
            f"{'conditions are mixed' if confidence >= 45 else 'conditions lean bearish'}."
        )

    # ── Sentences 2–3: per-signal detail ─────────────────────────────────────
    detail_parts = []

    for s in signals:
        name = s.get("name")
        direction = s.get("direction", "neutral")
        rsi_val = s.get("rsi")
        vol_ratio = s.get("vol_ratio")
        mom_pct = s.get("pct")

        if name == "MA Crossover":
            # Parse values out of the reason string as fallback
            reason = s.get("reason", "")
            if direction == "bull":
                detail_parts.append(
                    "Price is trading above both its 20-day and 50-day moving averages, "
                    "forming a bullish Golden Cross."
                )
            else:
                detail_parts.append(
                    "The 20-day moving average has crossed below the 50-day, "
                    "forming a bearish Death Cross."
                )

        elif name == "Volume":
            if vol_ratio is not None:
                if direction == "bull":
                    detail_parts.append(
                        f"Volume is running {vol_ratio:.1f}× its 30-day average on an up day, "
                        "confirming buying conviction behind the move."
                    )
                elif direction == "bear":
                    detail_parts.append(
                        f"Volume is elevated at {vol_ratio:.1f}× the 30-day average on a down day, "
                        "a sign of distribution pressure."
                    )
                else:
                    detail_parts.append(
                        f"Volume is near its 30-day average ({vol_ratio:.1f}×), "
                        "offering no strong directional confirmation."
                    )

        elif name == "RSI":
            if rsi_val is not None:
                if direction == "bull":
                    detail_parts.append(
                        f"RSI has dipped to {rsi_val:.0f}, entering oversold territory "
                        "and suggesting the stock may be due for a mean-reversion bounce."
                    )
                elif direction == "bear":
                    detail_parts.append(
                        f"RSI has climbed to {rsi_val:.0f}, pushing into overbought territory "
                        "and raising the risk of a near-term pullback."
                    )
                else:
                    detail_parts.append(
                        f"RSI sits at {rsi_val:.0f}, comfortably in the neutral zone with "
                        "room to run before hitting overbought levels."
                    )

        elif name == "MA Convergence":
            if direction in ("bull", "neutral"):
                detail_parts.append(
                    "Moving averages are converging, suggesting a potential trend change is building."
                )

        elif name == "Anticipatory Crossover":
            days = s.get("days_to_cross")
            cross_type = s.get("cross_type", "golden")
            if cross_type == "golden" and days:
                detail_parts.append(
                    f"The 50-day and 200-day moving averages are converging rapidly, "
                    f"with a potential Golden Cross estimated in approximately {days} trading days."
                )
            elif cross_type == "death" and days:
                detail_parts.append(
                    f"The 50-day moving average is falling toward the 200-day, "
                    f"with a potential Death Cross estimated in approximately {days} trading days."
                )

        elif name == "Momentum":
            if mom_pct is not None:
                if direction == "bull":
                    detail_parts.append(
                        f"Price momentum is positive, up {mom_pct:+.1f}% over the lookback window, "
                        "indicating sustained buying interest."
                    )
                elif direction == "bear":
                    detail_parts.append(
                        f"Price has declined {abs(mom_pct):.1f}% over the lookback window, "
                        "reflecting persistent selling pressure."
                    )
                else:
                    detail_parts.append(
                        f"Price momentum is essentially flat ({mom_pct:+.1f}% over the lookback window), "
                        "suggesting a consolidation phase."
                    )

    # Combine: headline + up to 2 detail sentences
    combined = [headline] + detail_parts[:2]
    return " ".join(combined)


# ─── Options Plan Generator ───────────────────────────────────────────────────

def _first_leg_strike(legs: list, leg_type: str, leg_action: str):
    """Return the strike of the first matching leg, or None."""
    for lg in legs:
        if lg.get('type') == leg_type and lg.get('action') == leg_action:
            s = lg.get('strike')
            return float(s) if s is not None else None
    return None


def generate_options_plan(rec: dict, strategy_id: str) -> dict | None:
    """
    Generate a plain-English trade plan for an options recommendation.

    rec         : one entry from recommend_for_strategy()
    strategy_id : e.g. 'iron_condor'

    Returns a dict:
        summary        — one sentence: what the trade bets on
        order_lines    — list of broker-ticket strings, one per leg
        max_loss_note  — dollar + plain reason
        max_gain_note  — dollar or "Unlimited" + plain reason
        breakeven      — price the stock must be at expiry to break even
        exit_plan      — specific take-profit / stop-loss / DTE rules
        is_credit      — bool: True if you receive cash up front
        net_str        — e.g. "+$145 credit" or "-$80 debit"
    """
    ticker = rec.get("ticker", "?")
    spot   = float(rec.get("spot") or 0)
    trade  = rec.get("suggested_trade")
    if not trade:
        return None

    expiry   = trade.get("expiry", "—")
    dte      = int(trade.get("dte") or 0)
    legs     = trade.get("legs") or []
    cd       = float(trade.get("credit_debit") or 0)   # + credit, - debit
    max_gain = trade.get("max_gain")
    max_loss = trade.get("max_loss")
    pt_pct   = int(trade.get("profit_target_pct") or 50)

    is_credit = cd >= 0
    net_abs   = abs(cd)
    net_str   = f"+${net_abs:.0f} credit" if is_credit else f"-${net_abs:.0f} debit"

    # ── Strategy-specific plain-English summary ───────────────────────────────
    cs_k = _first_leg_strike(legs, "CALL", "SELL")
    cl_k = _first_leg_strike(legs, "CALL", "BUY")
    ps_k = _first_leg_strike(legs, "PUT",  "SELL")
    pl_k = _first_leg_strike(legs, "PUT",  "BUY")

    def fmtk(v): return f"${v:.0f}" if v is not None else "—"

    _SUMMARIES = {
        "iron_condor": (
            f"Collect ${net_abs:.0f} cash now, then profit if {ticker} stays "
            f"between {fmtk(ps_k)} and {fmtk(cs_k)} through {expiry}."
        ),
        "iron_butterfly": (
            f"Collect ${net_abs:.0f} cash now; earn the most if {ticker} closes "
            f"right at {fmtk(cs_k)} on {expiry}."
        ),
        "bull_put_spread": (
            f"Collect ${net_abs:.0f} cash now by betting {ticker} stays above "
            f"{fmtk(ps_k)} through {expiry}."
        ),
        "bear_call_spread": (
            f"Collect ${net_abs:.0f} cash now by betting {ticker} stays below "
            f"{fmtk(cs_k)} through {expiry}."
        ),
        "jade_lizard": (
            f"Collect ${net_abs:.0f} cash now; profit if {ticker} stays above "
            f"{fmtk(ps_k)} through {expiry} — with no upside risk."
        ),
        "the_wheel": (
            f"Agree to buy {ticker} at {fmtk(ps_k)} if it falls there — and "
            f"collect ${net_abs:.0f} cash now that you keep no matter what."
        ),
        "short_strangle": (
            f"Collect ${net_abs:.0f} cash now; profit if {ticker} stays "
            f"between {fmtk(ps_k)} and {fmtk(cs_k)} through {expiry}."
        ),
        "short_straddle": (
            f"Collect ${net_abs:.0f} cash now; profit if {ticker} barely "
            f"moves from ${spot:.0f} through {expiry}."
        ),
        "long_call_put": (
            f"Pay ${net_abs:.0f} now; profit if {ticker} makes a big move "
            f"up or down by {expiry}."
        ),
        "calendar_spread": (
            f"Pay ${net_abs:.0f} now; profit if {ticker} stays near ${spot:.0f} "
            f"while the short option decays faster than the long."
        ),
        "diagonal_spread": (
            f"Pay ${net_abs:.0f} now and collect rent by selling short-dated "
            f"calls against a longer-term long call."
        ),
        "pmcc": (
            f"Pay ${net_abs:.0f} now for a deep-in-the-money long call, then "
            f"sell short calls against it to reduce cost — like owning stock cheaply."
        ),
        "bwb_call": (
            f"A low-cost spread; pay ${net_abs:.0f} and profit if {ticker} "
            f"rises moderately over {dte} days."
        ),
        "bwb_put": (
            f"A low-cost spread; pay ${net_abs:.0f} and profit if {ticker} "
            f"falls moderately over {dte} days."
        ),
        "put_ratio_spread": (
            f"Collect ${net_abs:.0f} cash now; profit if {ticker} falls to near "
            f"{fmtk(ps_k)} by {expiry} — be careful of sharp drops past that level."
        ),
    }
    summary = _SUMMARIES.get(
        strategy_id,
        f"Enter a {len(legs)}-leg options trade on {ticker} expiring {expiry}.",
    )

    # ── Broker-ticket order lines ─────────────────────────────────────────────
    order_lines = []
    for lg in legs:
        action = lg.get("action", "")
        ltype  = lg.get("type", "")
        strike = lg.get("strike")
        qty    = lg.get("qty", 1)
        price  = float(lg.get("price") or 0)
        leg_exp = lg.get("expiry", expiry)

        if ltype == "STOCK":
            order_lines.append(f"{action} {qty * 100} shares {ticker} at market")
        else:
            order_lines.append(
                f"{action} {qty} × {ticker} {leg_exp} ${strike} {ltype}"
                f"  @ ${price:.2f} limit"
            )

    # ── Max loss note ─────────────────────────────────────────────────────────
    if max_loss is None:
        ml_note = (
            "Unlimited — this trade has no hard cap on losses. "
            "Only use if you are experienced with margin and assignment risk."
        )
    else:
        ml_abs = abs(max_loss)
        if is_credit:
            ml_note = (
                f"${ml_abs:.0f} — the worst case if the stock moves fully against you. "
                f"Your broker will require approximately this much as collateral."
            )
        else:
            ml_note = (
                f"${ml_abs:.0f} — the premium you paid, which you lose entirely "
                f"if {ticker} doesn't move enough by {expiry}."
            )

    # ── Max gain note ─────────────────────────────────────────────────────────
    if max_gain is None:
        mg_note = (
            "Unlimited (directional long position) — "
            "your profit grows as the stock moves in your favor."
        )
    else:
        mg_abs = abs(max_gain)
        if is_credit:
            mg_note = (
                f"${mg_abs:.0f} — the full cash you collected at entry, "
                f"which you keep when the options expire worthless."
            )
        else:
            mg_note = (
                f"${mg_abs:.0f} — your best-case profit "
                f"if {ticker} makes the expected move by {expiry}."
            )

    # ── Break-even price ─────────────────────────────────────────────────────
    if is_credit:
        cpershare = net_abs / 100.0
        if cs_k and ps_k:
            be_up  = f"${cs_k + cpershare:.2f}"
            be_dn  = f"${ps_k - cpershare:.2f}"
            breakeven = (
                f"Upper break-even: {be_up}. Lower break-even: {be_dn}. "
                f"{ticker} must close between these on {expiry} for you to profit."
            )
        elif cs_k:
            breakeven = f"${cs_k + cpershare:.2f} — {ticker} must close below this on {expiry}."
        elif ps_k:
            breakeven = f"${ps_k - cpershare:.2f} — {ticker} must close above this on {expiry}."
        else:
            breakeven = f"Near ${spot:.2f} (current price)."
    else:
        dpershare = net_abs / 100.0
        if cl_k:
            breakeven = (
                f"${cl_k + dpershare:.2f} for the call — {ticker} needs to rise past this. "
            )
        elif pl_k:
            breakeven = (
                f"${pl_k - dpershare:.2f} for the put — {ticker} needs to fall past this. "
            )
        else:
            breakeven = f"Stock needs to move ${dpershare:.2f} per share to cover your cost."

    # ── Exit plan ─────────────────────────────────────────────────────────────
    close_dte = max(0, dte - 14)
    if is_credit and max_gain is not None:
        tp_dollars = round(abs(max_gain) * pt_pct / 100)
        sl_dollars = round(abs(max_gain) * 2)
        exit_plan = (
            f"Take profit when the position gains ${tp_dollars} ({pt_pct}% of max). "
            f"Cut your loss if the position reaches a ${sl_dollars} loss (2× premium rule). "
            f"Close any remaining contracts at {close_dte} DTE to avoid expiration risk."
        )
    elif not is_credit and max_loss is not None:
        sl_pct = 50
        sl_dollars = round(abs(max_loss) * sl_pct / 100)
        exit_plan = (
            f"Take profit at {pt_pct}% gain on your investment. "
            f"Close if the trade loses ${sl_dollars} ({sl_pct}% of premium paid). "
            f"Close no later than {close_dte} days before {expiry} to avoid assignment."
        )
    else:
        exit_plan = (
            f"Set a take-profit and stop-loss with your broker before entering. "
            f"Close all contracts before {expiry} to avoid expiration complications."
        )

    return {
        "summary":      summary,
        "order_lines":  order_lines,
        "max_loss_note": ml_note,
        "max_gain_note": mg_note,
        "breakeven":    breakeven,
        "exit_plan":    exit_plan,
        "is_credit":    is_credit,
        "net_str":      net_str,
    }
