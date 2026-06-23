"""
Claude Fable 5 intelligence layer for PG Stock Analysis.

Thin server-side wrapper around the Anthropic Messages API. Every feature here
is OPTIONAL: when ANTHROPIC_API_KEY is unset (or any call fails / times out),
the helpers return None and callers fall back to the existing rule-based
behavior. Nothing here may crash a request or block page load.

Design:
  • One model only: claude-fable-5.
  • Uses `requests` directly (no extra dependency).
  • 10s timeout, wrapped in try/except.
  • Optional response caching via hooks injected by app.py (the existing
    FMPCache table); a `force` flag bypasses the cache for "regenerate".
  • Token usage is logged to stdout per feature so cost is observable.
  • Every rendered output ends with the one-line disclaimer (callers append it).
"""
from __future__ import annotations

import os
import re
import json
import sys

import requests

try:
    from analysis import DISCLAIMER
except Exception:  # pragma: no cover - analysis always importable in practice
    DISCLAIMER = ("Algorithmic output, not financial advice. "
                  "Do your own research before trading.")

MODEL = "claude-fable-5"
_API_URL = "https://api.anthropic.com/v1/messages"
_API_VERSION = "2023-06-01"
_TIMEOUT = 10
_AI_SYMBOL = "__ai__"          # FMPCache.symbol bucket for AI responses

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Cache hooks (set by app.py). Signatures:
#   get_fn(symbol, endpoint, ttl_hours) -> data | None
#   set_fn(symbol, endpoint, data) -> None
_cache_get = None
_cache_set = None


def set_cache_backend(get_fn, set_fn):
    global _cache_get, _cache_set
    _cache_get, _cache_set = get_fn, set_fn


def ai_enabled() -> bool:
    return bool(ANTHROPIC_API_KEY)


def with_disclaimer(text: str) -> str:
    """Ensure a one-line disclaimer terminates any AI-rendered output."""
    if not text:
        return text
    if "not financial advice" in text.lower():
        return text
    return f"{text.rstrip()}\n\n{DISCLAIMER}"


# ─── Low-level call ───────────────────────────────────────────────────────────
def _strip_fences(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z0-9]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    return t.strip()


def _request(system: str, user_text: str, max_tokens: int, feature: str) -> str | None:
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": _API_VERSION,
        "content-type": "application/json",
    }
    body = {
        "model": MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user_text}],
    }
    try:
        resp = requests.post(_API_URL, headers=headers, json=body, timeout=_TIMEOUT)
        if resp.status_code != 200:
            print(f"[fable] feature={feature} HTTP {resp.status_code} — falling back", file=sys.stdout, flush=True)
            return None
        data = resp.json()
        usage = data.get("usage", {})
        print(f"[fable] feature={feature} model={MODEL} "
              f"in={usage.get('input_tokens')} out={usage.get('output_tokens')} tokens",
              file=sys.stdout, flush=True)
        parts = data.get("content", [])
        text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
        return text.strip() or None
    except Exception as e:
        print(f"[fable] feature={feature} error={e} — falling back", file=sys.stdout, flush=True)
        return None


def call_fable(system: str, user_text: str, max_tokens: int = 512,
               cache_key: str | None = None, ttl_hours: float = 1.0,
               feature: str = "fable", force: bool = False) -> str | None:
    """
    Core helper. Returns the model's text, or None when AI is unavailable / fails.
    Caches by `cache_key` (in the FMPCache table) unless `force` is set.
    """
    if not ai_enabled():
        return None
    ep = (cache_key or "")[:50]
    if cache_key and not force and _cache_get:
        try:
            cached = _cache_get(_AI_SYMBOL, ep, ttl_hours)
            if isinstance(cached, dict) and "text" in cached:
                return cached["text"]
        except Exception:
            pass
    text = _request(system, user_text, max_tokens, feature)
    if text is None:
        return None
    if cache_key and _cache_set:
        try:
            _cache_set(_AI_SYMBOL, ep, {"text": text})
        except Exception:
            pass
    return text


def _json_call(system: str, user_text: str, max_tokens: int, feature: str,
               cache_key=None, ttl_hours=1.0, force=False):
    sys_json = system + ("\n\nRespond with ONLY valid JSON — no prose, no "
                         "markdown, no code fences.")
    raw = call_fable(sys_json, user_text, max_tokens, cache_key, ttl_hours, feature, force)
    if raw is None:
        return None
    try:
        return json.loads(_strip_fences(raw))
    except Exception:
        print(f"[fable] feature={feature} bad JSON — falling back", file=sys.stdout, flush=True)
        return None


# ─── Feature 1: Morning briefing ──────────────────────────────────────────────
def morning_briefing(payload: dict, force: bool = False) -> str | None:
    system = (
        "You are a markets desk assistant writing a brief morning note for one "
        "retail investor's personal watchlist. Use ONLY the data provided in the "
        "user message. Do not invent numbers, news, or outside facts. Plain prose, "
        "at most 120 words, no headers or bullet lists. State what changed and what "
        "is worth watching today. No price predictions, no buy/sell advice."
    )
    user = json.dumps(payload, separators=(",", ":"))
    text = call_fable(system, user, max_tokens=300, cache_key="briefing",
                      ttl_hours=0.5, feature="briefing", force=force)
    return with_disclaimer(text) if text else None


# ─── Feature 2: Signal explanation ────────────────────────────────────────────
def explain_signal(analysis: dict, force: bool = False) -> str | None:
    symbol = analysis.get("symbol", "?")
    signal = analysis.get("signal", "HOLD")
    bucket = int(round((analysis.get("confidence", 50)) / 10.0) * 10)
    cache_key = f"sig:{symbol}:{signal}:{bucket}"
    system = (
        "You explain a transparent rule-based trading signal to a beginner. Use "
        "ONLY the computation in the user message (signals, families, regime, "
        "confidence, suggested stop). Do NOT introduce any outside claim about the "
        "company, its products, news, or fundamentals. No price predictions. In 3-4 "
        "sentences, explain plainly why the engine reached this signal and name the "
        "single strongest counter-signal pulling the other way."
    )
    user = json.dumps({
        "symbol": symbol, "signal": signal, "confidence": analysis.get("confidence"),
        "weighted_score": analysis.get("weighted_score"),
        "regime": analysis.get("regime", {}).get("label"),
        "families": analysis.get("families"),
        "signals": [{"name": s.get("name"), "direction": s.get("direction"),
                     "score": s.get("score"), "reason": s.get("reason")}
                    for s in analysis.get("signals", [])],
        "confirmations": analysis.get("confirmations"),
        "suggested_stop": analysis.get("suggested_stop"),
    }, separators=(",", ":"))
    text = call_fable(system, user, max_tokens=260, cache_key=cache_key,
                      ttl_hours=4, feature="signal_explain", force=force)
    return with_disclaimer(text) if text else None


# ─── Feature 3: Journal review ────────────────────────────────────────────────
def journal_review(trades: list, force: bool = False) -> dict | None:
    system = (
        "You are reviewing one trader's own trade log to surface patterns for "
        "self-reflection. Use ONLY the trades provided. Return 4-6 short "
        "observations, each phrased as a question to consider (not an instruction "
        "or recommendation). Look at: win rate when a signal was followed vs not, "
        "hold-time tendencies, and concentration. No predictions, no advice. "
        'Return JSON: {"observations": ["...", "..."]}.'
    )
    user = json.dumps({"trades": trades}, separators=(",", ":"))
    data = _json_call(system, user, max_tokens=500, feature="journal_review",
                      cache_key=None, force=force)
    if not data or "observations" not in data:
        return None
    obs = [str(o) for o in data["observations"]][:6]
    return {"observations": obs, "disclaimer": DISCLAIMER}


# ─── Feature 4: Natural-language watchlist query ──────────────────────────────
def watchlist_query(query: str, candidates: list) -> dict | None:
    system = (
        "You filter a provided list of stocks to match a user's natural-language "
        "question. Use ONLY the per-stock data given (signal, confidence, regime, "
        "RSI, momentum, etc.). You have NO outside knowledge of these tickers; never "
        "use any. Select the symbols that match and give a one-line reason for each "
        'drawn only from the provided fields. Return JSON: '
        '{"matches":[{"symbol":"X","reason":"..."}]}. If none match, return an empty list.'
    )
    user = json.dumps({"question": query, "stocks": candidates}, separators=(",", ":"))
    data = _json_call(system, user, max_tokens=600, feature="watchlist_query")
    if not data or "matches" not in data:
        return None
    valid = {c.get("symbol") for c in candidates}
    matches = [m for m in data["matches"]
               if isinstance(m, dict) and m.get("symbol") in valid]
    return {"matches": matches, "disclaimer": DISCLAIMER}


# ─── Feature 6: Scoreboard postmortem (Phase 6) ───────────────────────────────
def scoreboard_postmortem(stats: dict, force: bool = False) -> str | None:
    system = (
        "You interpret a signal-accuracy scoreboard for one trader's watchlist. Use "
        "ONLY the numbers provided (cross-sectional rank IC by horizon, hit rates, "
        "BUY/SELL/HOLD forward-return table, by-regime breakdown, sample sizes). Lead "
        "with whether the signals show real predictive power or look like noise, citing "
        "the IC and its n. Name the strongest and weakest regime or signal type. Close "
        "with one caveat about sample size, survivorship, or overfitting. 4-6 sentences. "
        "No outside claims, no predictions."
    )
    user = json.dumps(stats, separators=(",", ":"))
    key = f"sbpm:{abs(hash(user)) % (10 ** 10)}"
    text = call_fable(system, user, max_tokens=320, cache_key=key, ttl_hours=6,
                      feature="scoreboard_postmortem", force=force)
    return with_disclaimer(text) if text else None


def timemachine_explain(payload: dict, symbol: str, as_of: str, force: bool = False) -> str | None:
    system = (
        "You explain a single reconstructed trading signal and its realized outcome. "
        "Use ONLY the provided computation (the indicator panel, regime, confidence, "
        "suggested stop) and the realized forward returns. No outside knowledge of the "
        "company, no predictions. Explicitly note this is one example, not evidence of "
        "whether the signal works. 3-4 sentences."
    )
    user = json.dumps(payload, separators=(",", ":"))
    key = f"tmx:{symbol}:{as_of}"
    text = call_fable(system, user, max_tokens=240, cache_key=key, ttl_hours=4,
                      feature="timemachine_explain", force=force)
    return with_disclaimer(text) if text else None


# ─── Feature 5: Backtest postmortem ───────────────────────────────────────────
def backtest_postmortem(stats: dict, trades: list, force: bool = False) -> str | None:
    system = (
        "You explain the results of a hypothetical strategy backtest. Use ONLY the "
        "stats and trades provided. In 3-4 sentences, describe where the strategy "
        "made or lost its money (winning vs losing stretches, win/loss asymmetry) "
        "and end with one caveat about overfitting or small sample size. No "
        "predictions, no advice."
    )
    user = json.dumps({"stats": stats, "trades": trades[:40]}, separators=(",", ":"))
    text = call_fable(system, user, max_tokens=260, feature="backtest_postmortem", force=force)
    return with_disclaimer(text) if text else None
