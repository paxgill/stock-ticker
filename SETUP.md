# TERMINAL — Setup Guide

## Quick Start (Browser)

```bash
pip install flask flask-cors yfinance pandas flask-sqlalchemy
python app.py
# Open http://localhost:5000
```

## Electron Desktop App

```bash
# Install Node deps
npm install

# Run (starts Flask automatically)
npm run electron

# Build distributable
npm run dist:win   # Windows .exe
npm run dist:mac   # macOS .dmg
```

**Note:** Python + dependencies must be installed on the machine running Electron.
For a fully self-contained build, use PyInstaller to bundle app.py first.

## Cloud Deployment (Render/Railway)

Set environment variable:
```
DATABASE_URL=postgresql://user:pass@host/dbname
```

Flask auto-detects this and switches from SQLite to PostgreSQL.
Render offers a free PostgreSQL instance — connect it to your Flask service.

## Optional API keys (environment variables)

All keys are optional. With none set, the app runs fully on its rule-based
engine and free yfinance data — every key-gated feature degrades gracefully.

| Variable | Enables | Notes |
|---|---|---|
| `FMP_API_KEY` | Fundamentals (revenue/EPS growth, FCF, short interest) | Financial Modeling Prep free tier |
| `FINNHUB_API_KEY` | Per-ticker news feed with sentiment | Finnhub free tier |
| `ANTHROPIC_API_KEY` | Claude Fable 5 layer: morning briefing, signal explanations, journal review, natural-language watchlist search, backtest postmortems | See cost note below |

### Claude Fable 5 (`ANTHROPIC_API_KEY`) — cost note

The AI layer calls the Anthropic Messages API with model `claude-fable-5`. When
the key is unset, none of these features appear and the app falls back to its
templated/rule-based behavior (the dashboard never blocks on AI).

Cost is usage-based and intentionally small: every response is short (≤300
tokens out) and **cached** in the database — the morning briefing for 30 min,
signal explanations for 4 h per (symbol, signal, confidence-bucket). Journal
review, watchlist search, and backtest postmortems run only when you click them.
Token usage is logged to stdout per feature (`[fable] feature=… in=… out=…`) so
you can watch spend. A "↻ Regenerate" button bypasses the cache on demand.

```bash
# local
export ANTHROPIC_API_KEY=sk-ant-...
python app.py
```

## Electron Keyboard Shortcuts
- `Ctrl+Shift+T` — Show/Hide window from anywhere
- Tray icon click — Toggle window

## Packaging Notes
- App icon: place `icon.icns` (macOS) or `icon.ico` (Windows) in `electron/assets/`
- Tray icon: place `tray-icon.png` (16x16 or 32x32) in `electron/assets/`
