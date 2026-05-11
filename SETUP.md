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

## Electron Keyboard Shortcuts
- `Ctrl+Shift+T` — Show/Hide window from anywhere
- Tray icon click — Toggle window

## Packaging Notes
- App icon: place `icon.icns` (macOS) or `icon.ico` (Windows) in `electron/assets/`
- Tray icon: place `tray-icon.png` (16x16 or 32x32) in `electron/assets/`
