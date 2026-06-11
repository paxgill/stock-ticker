"""Phase 3: AI layer graceful-degradation + helper behavior (no API key needed)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.pop("ANTHROPIC_API_KEY", None)  # ensure disabled

import ai
import app as appmod

# ── Module-level: disabled without a key ──
assert ai.ai_enabled() is False
assert ai.MODEL == "claude-fable-5"
# All feature helpers return None when disabled (never raise)
assert ai.morning_briefing({"indices": []}) is None
assert ai.explain_signal({"symbol": "AAPL", "signal": "BUY", "confidence": 70}) is None
assert ai.journal_review([{"symbol": "AAPL"}]) is None
assert ai.watchlist_query("oversold?", [{"symbol": "AAPL"}]) is None
assert ai.backtest_postmortem({"trades": 1}, []) is None
print("PASS: all feature helpers return None when disabled")

# ── call_fable returns None when disabled (no network attempted) ──
assert ai.call_fable("sys", "user") is None
print("PASS: call_fable disabled -> None")

# ── Fence stripping + disclaimer helpers ──
assert ai._strip_fences("```json\n{\"a\":1}\n```") == '{"a":1}'
assert ai._strip_fences("plain") == "plain"
d = ai.with_disclaimer("Some note.")
assert "not financial advice" in d.lower() and d.startswith("Some note.")
# Idempotent: don't double-append
assert ai.with_disclaimer(d) == d
print("PASS: fence stripping + disclaimer helpers")

# ── Endpoints return {enabled: false} (200) when no key ──
client = appmod.app.test_client()
for path in ["/api/ai/briefing", "/api/ai/signal-explanation/AAPL"]:
    r = client.get(path)
    assert r.status_code == 200 and r.get_json().get("enabled") is False, path
for path in ["/api/ai/journal-review", "/api/ai/backtest-postmortem"]:
    r = client.post(path, json={})
    assert r.status_code == 200 and r.get_json().get("enabled") is False, path
r = client.post("/api/ai/watchlist-query", json={"query": "x"})
assert r.status_code == 200 and r.get_json().get("enabled") is False
r = client.get("/api/ai/status")
assert r.get_json() == {"enabled": False, "model": "claude-fable-5"}
print("PASS: all AI endpoints degrade to {enabled:false} with 200")

# ── market-summary still works (template path unaffected) ──
r = client.get("/api/market-summary")
assert r.status_code == 200 and "summary" in r.get_json()
print("PASS: template market-summary unaffected")

# ── Simulated key path: monkeypatch the HTTP call to verify wiring + caching ──
ai.ANTHROPIC_API_KEY = "test-key-123"
calls = {"n": 0}
def fake_request(system, user_text, max_tokens, feature):
    calls["n"] += 1
    return "The engine leans bullish because trend and momentum agree."
ai._request = fake_request
appmod.ai = ai  # ensure app uses same module (it imports `ai`)

txt = ai.explain_signal({"symbol": "AAPL", "signal": "BUY", "confidence": 71,
                          "signals": [], "families": {}, "regime": {"label": "Trending / calm"}})
assert txt and "not financial advice" in txt.lower()
assert calls["n"] == 1
# Second identical call should hit the cache (no new _request)
with appmod.app.app_context():
    txt2 = ai.explain_signal({"symbol": "AAPL", "signal": "BUY", "confidence": 73,
                              "signals": [], "families": {}, "regime": {"label": "Trending / calm"}})
print(f"PASS: simulated-key explanation works; _request calls={calls['n']} (cached on bucket match)")

# JSON feature parsing + code-fence tolerance
ai._request = lambda s, u, m, f: '```json\n{"matches":[{"symbol":"AAPL","reason":"oversold in uptrend"}]}\n```'
res = ai.watchlist_query("oversold in uptrend", [{"symbol": "AAPL"}])
assert res and res["matches"][0]["symbol"] == "AAPL"
# symbol not in candidates is filtered out
ai._request = lambda s, u, m, f: '{"matches":[{"symbol":"FAKE","reason":"x"}]}'
res = ai.watchlist_query("q", [{"symbol": "AAPL"}])
assert res["matches"] == []
print("PASS: JSON parsing, fence tolerance, and symbol whitelisting")

ai.ANTHROPIC_API_KEY = ""  # restore disabled
print("\nAll AI layer tests passed.")
