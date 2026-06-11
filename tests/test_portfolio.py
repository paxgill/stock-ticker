"""Phase 4: FIFO lot accounting + product endpoints."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DISABLE_ALERT_ENGINE"] = "1"

from portfolio import build_open_lots, fifo_realized, blended_cost, fifo_preview

# ── FIFO basics ──
trades = [
    {"action": "Buy",  "shares": 10, "price": 100, "date": "2024-01-01", "id": 1},
    {"action": "Buy",  "shares": 10, "price": 120, "date": "2024-02-01", "id": 2},
]
lots = build_open_lots(trades)
shares, cost = blended_cost(lots)
assert shares == 20 and cost == 110.0, (shares, cost)
print(f"PASS: blended cost {cost} over {shares} shares")

# Sell 15 @ 130: 10@100 (+300) + 5@120 (+50) = 350
assert fifo_realized(lots, 15, 130) == 350.0
print("PASS: FIFO realized P&L = 350 on partial multi-lot sell")

# After a recorded sell of 12, open lots should be 8 @ 120
trades2 = trades + [{"action": "Sell", "shares": 12, "price": 130, "date": "2024-03-01", "id": 3}]
lots2 = build_open_lots(trades2)
s2, c2 = blended_cost(lots2)
assert abs(s2 - 8) < 1e-6 and abs(c2 - 120) < 1e-6, (s2, c2)
print(f"PASS: open lots after sell = {s2} @ {c2}")

# fifo_preview against prior trades
prev = fifo_preview(trades, 5, 130)
assert prev["realized_pnl"] == 150.0 and prev["sufficient_lots"] is True
over = fifo_preview(trades, 25, 130)
assert over["sufficient_lots"] is False
print("PASS: fifo_preview computes realized + sufficiency")

# Oversell with no lots realizes nothing
assert fifo_realized(build_open_lots([]), 5, 100) == 0.0
print("PASS: oversell with no lots = 0")

# ── Endpoint smoke (Flask test client, offline) ──
import app as appmod
client = appmod.app.test_client()

with appmod.app.app_context():
    appmod.TradeLog.query.delete(); appmod.WatchlistItem.query.delete()
    appmod.PortfolioPosition.query.delete(); appmod.AlertEvent.query.delete()
    appmod.db.session.commit()

# Log Buy trades, then a Sell — realized P&L should auto-compute FIFO
client.post("/api/trades", json={"symbol": "AAPL", "action": "Buy", "shares": 10, "price": 100, "date": "2024-01-01"})
client.post("/api/trades", json={"symbol": "AAPL", "action": "Buy", "shares": 10, "price": 120, "date": "2024-02-01"})
r = client.post("/api/trades", json={"symbol": "AAPL", "action": "Sell", "shares": 15, "price": 130, "date": "2024-03-01"})
assert r.get_json()["realized_pnl"] == 350.0, r.get_json()
print("PASS: POST /api/trades auto-fills FIFO realized P&L = 350")

# fifo-preview endpoint
r = client.post("/api/portfolio/fifo-preview", json={"symbol": "AAPL", "shares": 5, "price": 130})
assert "realized_pnl" in r.get_json()
print("PASS: /api/portfolio/fifo-preview")

# lots endpoint (5 shares left @ 120 after selling 15 of 20)
r = client.get("/api/portfolio/lots")
lots_resp = r.get_json()
aapl = next((x for x in lots_resp if x["symbol"] == "AAPL"), None)
assert aapl and abs(aapl["open_shares"] - 5) < 1e-6
print(f"PASS: /api/portfolio/lots -> AAPL open {aapl['open_shares']} @ {aapl['blended_cost']}")

# Tags via PUT
client.post("/api/watchlist", json={"symbol": "MSFT", "name": "Microsoft"})
r = client.put("/api/watchlist/MSFT", json={"tags": ["core", "tech"]})
assert r.get_json()["tags"] == ["core", "tech"]
print("PASS: watchlist tags update + serialize as list")

# Portfolio CSV import
r = client.post("/api/portfolio/import", json={"text": "symbol,shares,cost_basis,date\nTSLA,5,200,2024-05-01\nBAD\n"})
j = r.get_json()
assert j["added"] == 1 and any(x["status"] == "bad format" for x in j["results"])
print("PASS: portfolio CSV import (1 added, 1 bad row flagged)")

# Portfolio CSV export
r = client.get("/api/portfolio/export/csv")
assert r.mimetype == "text/csv" and "TSLA" in r.get_data(as_text=True)
print("PASS: portfolio CSV export")

# Health
r = client.get("/api/health")
h = r.get_json()
assert h["ok"] is True and h["version"] and "yfinance_age_s" in h
print(f"PASS: /api/health ok, version {h['version']}")

# Alert engine: set an alert that is already crossed, force a check
client.put("/api/watchlist/MSFT", json={"alert_direction": "above", "alert_price": 0.01})
with appmod.app.app_context():
    appmod._quote_cache_put("MSFT", {"symbol": "MSFT", "price": 500.0})
    fired = appmod.check_alerts_once()
assert fired >= 1
r = client.get("/api/alerts/events")
assert any(e["symbol"] == "MSFT" for e in r.get_json())
# dedupe: second check within the hour fires nothing new
with appmod.app.app_context():
    assert appmod.check_alerts_once() == 0
print("PASS: alert engine fires once and dedupes within the hour")

# cleanup
with appmod.app.app_context():
    appmod.TradeLog.query.delete(); appmod.WatchlistItem.query.delete()
    appmod.PortfolioPosition.query.delete(); appmod.AlertEvent.query.delete()
    appmod.QuoteCache.query.delete(); appmod.db.session.commit()

print("\nAll portfolio/Phase-4 tests passed.")
