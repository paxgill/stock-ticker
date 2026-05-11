const { app, BrowserWindow, Tray, Menu, shell, ipcMain, nativeImage, globalShortcut } = require('electron');
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');
const http = require('http');

// ─── Config ──────────────────────────────────────────────────────────────────
const FLASK_PORT = 5000;
const FLASK_URL = `http://localhost:${FLASK_PORT}`;
const IS_DEV = process.argv.includes('--dev');
const ALWAYS_ON_TOP_KEY = 'always-on-top';
const COMPACT_WIDTH = 320;
const FULL_WIDTH = 1280;
const WINDOW_HEIGHT = 800;

let mainWindow = null;
let tray = null;
let flaskProcess = null;
let alwaysOnTop = false;
let trayQuotes = {}; // cache for tray menu top movers

// ─── Flask Lifecycle ─────────────────────────────────────────────────────────
function startFlask() {
  const pythonCmd = process.platform === 'win32' ? 'python' : 'python3';
  const scriptPath = IS_DEV
    ? path.join(__dirname, '..', 'app.py')
    : path.join(process.resourcesPath, 'app.py');

  flaskProcess = spawn(pythonCmd, [scriptPath], {
    cwd: IS_DEV ? path.join(__dirname, '..') : process.resourcesPath,
    env: { ...process.env, FLASK_ENV: 'production' },
    stdio: IS_DEV ? 'inherit' : 'ignore',
  });

  flaskProcess.on('error', err => {
    console.error('Flask failed to start:', err.message);
    showFlaskError();
  });

  flaskProcess.on('exit', code => {
    if (code !== 0 && code !== null) console.error('Flask exited with code', code);
  });
}

function stopFlask() {
  if (flaskProcess) {
    flaskProcess.kill();
    flaskProcess = null;
  }
}

function waitForFlask(maxRetries = 30, delay = 500) {
  return new Promise((resolve, reject) => {
    let attempts = 0;
    const check = () => {
      http.get(`${FLASK_URL}/api/validate/AAPL`, res => {
        if (res.statusCode < 500) resolve();
        else retry();
      }).on('error', () => retry());
    };
    const retry = () => {
      attempts++;
      if (attempts >= maxRetries) reject(new Error('Flask did not start in time'));
      else setTimeout(check, delay);
    };
    check();
  });
}

function showFlaskError() {
  if (mainWindow) {
    mainWindow.loadURL(`data:text/html,<html style="background:#0a0a0b;color:#ff5252;font-family:monospace;padding:40px">
      <h2>⚠ Flask Backend Not Running</h2>
      <p>Make sure Python and dependencies are installed:</p>
      <pre style="background:#111;padding:12px;border-radius:4px">pip install flask flask-cors yfinance pandas flask-sqlalchemy</pre>
      <p>Then restart the app.</p>
    </html>`);
  }
}

// ─── Window ──────────────────────────────────────────────────────────────────
function createWindow() {
  mainWindow = new BrowserWindow({
    width: FULL_WIDTH,
    height: WINDOW_HEIGHT,
    minWidth: COMPACT_WIDTH,
    minHeight: 400,
    backgroundColor: '#0a0a0b',
    titleBarStyle: process.platform === 'darwin' ? 'hiddenInset' : 'default',
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
    title: '▣ PG Stock Analysis',
    show: false,
  });

  mainWindow.once('ready-to-show', () => mainWindow.show());

  mainWindow.on('closed', () => { mainWindow = null; });

  // Keep tray visible when window is closed (don't quit)
  mainWindow.on('close', e => {
    if (tray && !app.isQuitting) {
      e.preventDefault();
      mainWindow.hide();
    }
  });

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });

  return mainWindow;
}

// ─── Tray ─────────────────────────────────────────────────────────────────────
function createTray() {
  let icon;
  const iconPath = path.join(__dirname, 'assets', 'tray-icon.png');
  if (fs.existsSync(iconPath)) {
    icon = nativeImage.createFromPath(iconPath);
  } else {
    // Programmatic 16x16 teal icon fallback
    icon = createProgrammaticIcon();
  }
  if (process.platform === 'darwin') icon = icon.resize({ width: 16, height: 16 });

  tray = new Tray(icon);
  tray.setToolTip('▣ PG Stock Analysis — Stock Dashboard');
  updateTrayMenu();

  tray.on('click', () => toggleWindow());
}

function createProgrammaticIcon() {
  // 16x16 PNG with teal color (#00d4aa), generated as a minimal data URI
  const base64 = 'iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAiklEQVQ4T2NkIAIw' +
    'EqmHgYGBgZEYNaMGUMsAqjuBYMsJqnsBoQag2gEIe6jiAIIt' +
    'IKoHoIEHqB6geg+oHqB6D6geoHoHqh6gegeoHqB6D6geoHoHqB6B6geoHoHqB6' +
    'D6geoHoHqB6D6geoPoBqB6gegeoHoHAAAA//8DAE7uBrEAAAAASUVORK5CYII=';
  try {
    return nativeImage.createFromDataURL('data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAA' +
      'QAAAAQCAYAAAA/Od+lAAAABmJLR0QA/wD/AP+gvaeTAAAAKElEQVQoz2NgGAWk' +
      'gv///w8DA8N/KBsHGBUYMGoAqQygugMGRg0AAHoABo3xqp4AAAAASUVORK5CYII=');
  } catch(e) {
    return nativeImage.createEmpty();
  }
}

function updateTrayMenu() {
  const topMovers = buildTopMovers();
  const onTopLabel = alwaysOnTop ? '✓ Always on Top' : 'Always on Top';

  const template = [
    { label: '▣ PG Stock Analysis', enabled: false },
    { type: 'separator' },
    ...(topMovers.length > 0 ? [
      { label: 'TOP MOVERS', enabled: false },
      ...topMovers,
      { type: 'separator' },
    ] : []),
    { label: 'Show / Hide Window', click: () => toggleWindow() },
    { label: onTopLabel, click: () => toggleAlwaysOnTop() },
    {
      label: 'Window Size', submenu: [
        { label: 'Compact (sidebar)', click: () => setCompactMode() },
        { label: 'Full Dashboard', click: () => setFullMode() },
      ]
    },
    { type: 'separator' },
    { label: 'Open Dashboard in Browser', click: () => shell.openExternal(FLASK_URL) },
    { type: 'separator' },
    { label: 'Quit PG Stock Analysis', click: () => { app.isQuitting = true; app.quit(); } },
  ];

  tray.setContextMenu(Menu.buildFromTemplate(template));
}

function buildTopMovers() {
  const entries = Object.values(trayQuotes)
    .filter(q => q && !q.error && q.pct_change != null)
    .sort((a, b) => Math.abs(b.pct_change) - Math.abs(a.pct_change))
    .slice(0, 5);

  return entries.map(q => ({
    label: `${q.symbol.padEnd(6)} $${q.price.toFixed(2)}  ${q.pct_change >= 0 ? '+' : ''}${q.pct_change.toFixed(2)}%`,
    enabled: false,
  }));
}

// ─── Window Controls ─────────────────────────────────────────────────────────
function toggleWindow() {
  if (!mainWindow) return;
  if (mainWindow.isVisible()) mainWindow.hide();
  else { mainWindow.show(); mainWindow.focus(); }
}

function toggleAlwaysOnTop() {
  alwaysOnTop = !alwaysOnTop;
  if (mainWindow) mainWindow.setAlwaysOnTop(alwaysOnTop, 'floating');
  updateTrayMenu();
}

function setCompactMode() {
  if (mainWindow) {
    mainWindow.setSize(COMPACT_WIDTH, WINDOW_HEIGHT);
    mainWindow.show();
  }
}

function setFullMode() {
  if (mainWindow) {
    mainWindow.setSize(FULL_WIDTH, WINDOW_HEIGHT);
    mainWindow.center();
    mainWindow.show();
  }
}

// ─── IPC ─────────────────────────────────────────────────────────────────────
ipcMain.handle('toggle-always-on-top', () => { toggleAlwaysOnTop(); return alwaysOnTop; });
ipcMain.handle('set-compact', () => setCompactMode());
ipcMain.handle('set-full', () => setFullMode());
ipcMain.handle('get-always-on-top', () => alwaysOnTop);

// Receive quotes from renderer to update tray menu
ipcMain.on('quotes-update', (_, quotes) => {
  trayQuotes = quotes;
  updateTrayMenu();
});

// ─── App Lifecycle ───────────────────────────────────────────────────────────
app.whenReady().then(async () => {
  startFlask();
  const win = createWindow();

  try {
    await waitForFlask();
    win.loadURL(FLASK_URL);
  } catch (e) {
    showFlaskError();
  }

  createTray();

  // Global shortcut: Cmd/Ctrl+Shift+T to show/hide
  globalShortcut.register('CommandOrControl+Shift+T', toggleWindow);

  app.on('activate', () => { if (!mainWindow?.isVisible()) toggleWindow(); });
});

app.on('window-all-closed', () => {
  // On macOS, keep running even with no windows (lives in tray)
  if (process.platform !== 'darwin') {
    // Still keep alive for tray on Windows/Linux
  }
});

app.on('before-quit', () => {
  app.isQuitting = true;
  globalShortcut.unregisterAll();
  stopFlask();
});

app.on('will-quit', () => stopFlask());
