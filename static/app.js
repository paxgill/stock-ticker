// ════════════════════════════════════════════════════════════════
// STATE
// ════════════════════════════════════════════════════════════════
const S = {
  watchlist: [],       // [{id, symbol, name, tier, notes, alert_direction, alert_price}]
  profiles: [],
  activeProfile: null,
  portfolio: [],
  trades: [],
  quotes: {},
  suggestions: [],
  preferences: { interval: '300', density: 'compact' },
  countdownVal: 0,
  countdownTimer: null,
  notifGranted: false,
  triggeredAlerts: new Set(),
};

// ════════════════════════════════════════════════════════════════
// API CLIENT
// ════════════════════════════════════════════════════════════════
const api = {
  get: (url) => fetch(url).then(r => r.json()),
  post: (url, body) => fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then(r => r.json()),
  put: (url, body) => fetch(url, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then(r => r.json()),
  del: (url) => fetch(url, { method: 'DELETE' }).then(r => r.json()),
};

// ════════════════════════════════════════════════════════════════
// INIT
// ════════════════════════════════════════════════════════════════
window.addEventListener('DOMContentLoaded', async () => {
  document.getElementById('ticker-input').addEventListener('keydown', e => { if (e.key === 'Enter') addTicker(); });

  // Load persistent state from backend
  await Promise.all([
    loadWatchlist(),
    loadProfiles(),
    loadPreferences(),
  ]);

  // Migrate old localStorage state if DB is empty
  await migrateLocalStorage();

  if (S.watchlist.length > 0) {
    showDashboard();
  } else {
    showOnboarding();
  }

  if ('Notification' in window && Notification.permission === 'granted') {
    S.notifGranted = true;
    document.getElementById('notif-btn').classList.add('active');
  }

  initElectronControls();
});

async function loadWatchlist() {
  try { S.watchlist = await api.get('/api/watchlist'); } catch (e) { S.watchlist = []; }
}
async function loadProfiles() {
  try {
    S.profiles = await api.get('/api/profiles');
    S.activeProfile = S.profiles.find(p => p.is_active) || null;
  } catch (e) { S.profiles = []; }
}
async function loadPreferences() {
  try { S.preferences = await api.get('/api/preferences'); } catch (e) {}
}
async function loadPortfolio() {
  try { S.portfolio = await api.get('/api/portfolio'); } catch (e) { S.portfolio = []; }
}
async function loadTrades(filters = {}) {
  try {
    const q = new URLSearchParams(filters).toString();
    S.trades = await api.get('/api/trades' + (q ? '?' + q : ''));
  } catch (e) { S.trades = []; }
}

async function migrateLocalStorage() {
  if (S.watchlist.length > 0) return;
  try {
    const old = JSON.parse(localStorage.getItem('terminal_state') || '{}');
    if (old.tickers && old.tickers.length > 0) {
      for (const t of old.tickers) {
        await api.post('/api/watchlist', { symbol: t.symbol, name: t.name || '' });
      }
      if (old.alerts) {
        for (const [sym, alert] of Object.entries(old.alerts)) {
          await api.put(`/api/watchlist/${sym}`, { alert_direction: alert.direction, alert_price: alert.price });
        }
      }
      await api.put('/api/preferences', { interval: String(old.interval || 300), density: old.density || 'compact' });
      localStorage.removeItem('terminal_state');
      await loadWatchlist();
      showToast('Migrated your existing watchlist to the database.', 'success');
    }
  } catch (e) {}
}

// ════════════════════════════════════════════════════════════════
// SCREENS
// ════════════════════════════════════════════════════════════════
function showOnboarding() {
  document.getElementById('onboarding').classList.add('active');
  document.getElementById('dashboard').classList.remove('active');
  goStep(1);
  renderChips();
}

function showDashboard() {
  document.getElementById('onboarding').classList.remove('active');
  document.getElementById('dashboard').classList.add('active');
  applyPreferences();
  updateMarketStatus();
  fetchAllQuotes();
  fetchMarketSummary();
  startAutoRefresh();
}

// ════════════════════════════════════════════════════════════════
// ONBOARDING FLOW
// ════════════════════════════════════════════════════════════════
let currentStep = 1;

function goStep(n) {
  currentStep = n;
  document.querySelectorAll('.ob-step').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.step').forEach(el => {
    const s = parseInt(el.dataset.step);
    el.classList.toggle('active', s === n);
    el.classList.toggle('done', s < n);
  });
  document.getElementById(`step-${n}`).classList.add('active');
  if (n === 2) renderAlertInputs();
}

// Onboarding weight sync
function updateWeight(key, val) {
  document.getElementById(`w-${key}-val`).textContent = val + '%';
}

// Step 1 — tickers
let addingTicker = false;
async function addTicker() {
  const input = document.getElementById('ticker-input');
  const sym = input.value.trim().toUpperCase().replace(/[^A-Z0-9.\-]/g, '');
  if (!sym || addingTicker) return;
  if (S.watchlist.find(t => t.symbol === sym)) { setStatus(`${sym} already added.`, 'err'); return; }
  addingTicker = true;
  setStatus(`Validating ${sym}...`, 'loading');
  try {
    const data = await api.get(`/api/validate/${sym}`);
    if (data.valid) {
      const item = await api.post('/api/watchlist', { symbol: sym, name: data.name });
      S.watchlist.push(item);
      input.value = '';
      setStatus(`✓ ${sym} — ${data.name} ($${data.price})`, 'ok');
      renderChips();
      document.getElementById('step1-next').disabled = false;
    } else {
      setStatus(`✗ "${sym}" not found. Check the symbol.`, 'err');
    }
  } catch (e) { setStatus('Server error. Is the backend running?', 'err'); }
  addingTicker = false;
}

async function quickAdd(sym) {
  document.getElementById('ticker-input').value = sym;
  await addTicker();
}

function setStatus(msg, type) {
  const el = document.getElementById('ticker-status');
  el.textContent = msg;
  el.className = 'ticker-status ' + type;
}

function renderChips() {
  const el = document.getElementById('ticker-chips');
  el.innerHTML = S.watchlist.map(t => `
    <div class="chip">
      <span class="chip-sym">${t.symbol}</span>
      <span class="chip-name">${t.name}</span>
      <span class="chip-tier tier-badge ${tierClass(t.tier)}" onclick="cycleTier('${t.symbol}')">${t.tier}</span>
      <button class="chip-remove" onclick="removeTicker('${t.symbol}')">✕</button>
    </div>`).join('');
}

function tierClass(tier) {
  return tier === 'Core Hold' ? 'tier-core' : tier === 'Speculative' ? 'tier-spec' : 'tier-watch';
}

async function cycleTier(sym) {
  const tiers = ['Active Watch', 'Core Hold', 'Speculative'];
  const item = S.watchlist.find(t => t.symbol === sym);
  if (!item) return;
  const next = tiers[(tiers.indexOf(item.tier) + 1) % tiers.length];
  await api.put(`/api/watchlist/${sym}`, { tier: next });
  item.tier = next;
  renderChips();
}

async function removeTicker(sym) {
  await api.del(`/api/watchlist/${sym}`);
  S.watchlist = S.watchlist.filter(t => t.symbol !== sym);
  delete S.quotes[sym];
  renderChips();
  if (document.getElementById('dashboard').classList.contains('active')) {
    renderDashboard();
  }
  document.getElementById('step1-next').disabled = S.watchlist.length === 0;
}

// Step 2 — alerts
function renderAlertInputs() {
  const container = document.getElementById('alert-inputs');
  container.innerHTML = S.watchlist.map(t => `
    <div style="display:flex;align-items:center;gap:10px;background:var(--bg3);border:1px solid var(--border);border-radius:4px;padding:10px 14px;margin-bottom:8px">
      <span style="color:var(--accent);font-weight:700;width:56px;font-size:13px">${t.symbol}</span>
      <select id="adir-${t.symbol}" style="background:var(--bg2);border:1px solid var(--border2);border-radius:3px;color:var(--text2);font-family:var(--font);font-size:12px;padding:5px 8px">
        <option value="above" ${t.alert_direction === 'above' ? 'selected' : ''}>ABOVE</option>
        <option value="below" ${t.alert_direction === 'below' ? 'selected' : ''}>BELOW</option>
      </select>
      <input type="number" step="0.01" min="0" id="aprice-${t.symbol}"
        value="${t.alert_price || ''}" placeholder="price (optional)"
        style="background:var(--bg2);border:1px solid var(--border2);border-radius:3px;color:var(--text);font-family:var(--font);font-size:13px;padding:6px 10px;width:130px">
    </div>`).join('');
}

async function collectAlerts() {
  for (const t of S.watchlist) {
    const priceEl = document.getElementById(`aprice-${t.symbol}`);
    const dirEl = document.getElementById(`adir-${t.symbol}`);
    if (!priceEl) continue;
    const price = parseFloat(priceEl.value);
    if (price > 0) {
      await api.put(`/api/watchlist/${t.symbol}`, { alert_direction: dirEl.value, alert_price: price });
      t.alert_direction = dirEl.value;
      t.alert_price = price;
    }
  }
}

// Step 3 — analysis profile
function selectDensity(d) {
  document.querySelectorAll('[name="density"]').forEach(r => { r.checked = r.value === d; });
}

async function launchDashboard() {
  await collectAlerts();

  // Save analysis profile (step 3)
  const profileName = document.getElementById('ob-profile-name')?.value || 'My Profile';
  const risk = document.querySelector('[name="ob-risk"]:checked')?.value || 'Moderate';
  const horizon = document.querySelector('[name="ob-horizon"]:checked')?.value || 'Swing';
  const maW = parseInt(document.getElementById('w-ma')?.value || 25) / 100;
  const volW = parseInt(document.getElementById('w-vol')?.value || 25) / 100;
  const rsiW = parseInt(document.getElementById('w-rsi')?.value || 25) / 100;
  const momW = parseInt(document.getElementById('w-mom')?.value || 25) / 100;

  // Save profile only if no profiles exist yet
  if (S.profiles.length === 0) {
    const profile = await api.post('/api/profiles', {
      name: profileName, risk_tolerance: risk, horizon,
      ma_weight: maW, volume_weight: volW, rsi_weight: rsiW, momentum_weight: momW,
      rsi_overbought: parseFloat(document.getElementById('ob-rsi-ob')?.value || 70),
      rsi_oversold: parseFloat(document.getElementById('ob-rsi-os')?.value || 30),
      volume_spike_threshold: parseFloat(document.getElementById('ob-vol-thresh')?.value || 1.5),
      max_trades_per_day: parseInt(document.getElementById('ob-max-trades')?.value || 3),
      is_active: true,
    });
    S.profiles = [profile];
    S.activeProfile = profile;
  }

  const intervalEl = document.querySelector('[name="interval"]:checked');
  const densityEl = document.querySelector('[name="density"]:checked');
  const interval = intervalEl?.value || '300';
  const density = densityEl?.value || 'compact';
  await api.put('/api/preferences', { interval, density });
  S.preferences = { interval, density };

  showDashboard();
}

// ════════════════════════════════════════════════════════════════
// MARKET SUMMARY CARD
// ════════════════════════════════════════════════════════════════
let marketSummaryCache = null;
let marketSummaryFetchedAt = 0;
const SUMMARY_TTL_MS = 5 * 60 * 1000; // re-fetch at most every 5 min

async function fetchMarketSummary() {
  const wrap = document.getElementById('market-summary-wrap');
  if (!wrap) return;

  // Show skeleton while loading (only on first load)
  if (!marketSummaryCache) {
    wrap.innerHTML = `<div class="market-summary-loading"><div class="loading-spinner" style="width:14px;height:14px;border-width:1px"></div>Fetching market indices...</div>`;
  }

  const now = Date.now();
  if (marketSummaryCache && (now - marketSummaryFetchedAt) < SUMMARY_TTL_MS) {
    renderMarketSummary(marketSummaryCache);
    return;
  }

  try {
    const data = await api.get('/api/market-summary');
    marketSummaryCache = data;
    marketSummaryFetchedAt = now;
    renderMarketSummary(data);
  } catch (e) {
    wrap.innerHTML = ''; // fail silently — dashboard still loads
  }
}

function renderMarketSummary(data) {
  const wrap = document.getElementById('market-summary-wrap');
  if (!wrap) return;

  const indices = data.indices || {};
  const indexOrder = ['^GSPC', '^IXIC', '^DJI'];
  const names = { '^GSPC': 'S&P 500', '^IXIC': 'NASDAQ', '^DJI': 'DOW' };

  const indexBlocks = indexOrder
    .filter(sym => indices[sym])
    .map(sym => {
      const idx = indices[sym];
      const up = idx.pct >= 0;
      const pctStr = (up ? '+' : '') + idx.pct.toFixed(2) + '%';
      const priceStr = idx.price >= 10000
        ? idx.price.toLocaleString('en-US', { maximumFractionDigits: 0 })
        : idx.price.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
      return `
        <div class="ms-index">
          <div class="ms-index-name">${names[sym]}</div>
          <div class="ms-index-pct ${up ? 'up' : 'down'}">${pctStr}</div>
          <div class="ms-index-price">${priceStr}</div>
        </div>`;
    }).join('');

  const time = new Date(data.generated_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

  wrap.innerHTML = `
    <div class="market-summary-card">
      <div class="market-summary-indices">${indexBlocks}</div>
      <div class="ms-divider"></div>
      <div class="ms-text-wrap">
        <div class="ms-label">DAILY MARKET SUMMARY</div>
        <div class="ms-paragraph">${data.summary}</div>
        <div class="ms-timestamp">Generated ${time}</div>
      </div>
    </div>`;
}

// ════════════════════════════════════════════════════════════════
// DASHBOARD — QUOTES
// ════════════════════════════════════════════════════════════════
async function fetchAllQuotes() {
  if (S.watchlist.length === 0) { renderDashboard(); return; }
  try {
    const data = await api.post('/api/quote', { tickers: S.watchlist.map(t => t.symbol) });
    S.quotes = data;
    document.getElementById('last-updated').textContent = 'Updated ' + new Date().toLocaleTimeString();
    sendQuotesToTray(data);
    renderDashboard();
    checkAlerts();
    updateMarketStatus();
    // Refresh portfolio display if on that tab
    if (document.getElementById('tab-portfolio').classList.contains('active')) {
      renderPortfolio();
    }
  } catch (e) { showToast('Failed to fetch quotes. Is Flask running?', 'error'); }
}

function renderDashboard() {
  const el = document.getElementById('dashboard-content');
  if (S.watchlist.length === 0) {
    el.innerHTML = `<div class="empty-state"><h3>No tickers</h3><p>Add ticker symbols using the input above.</p></div>`;
    return;
  }
  S.preferences.density === 'expanded' ? renderExpanded(el) : renderCompact(el);
}

function renderCompact(el) {
  const cards = S.watchlist.map(t => {
    const q = S.quotes[t.symbol];
    const sug = S.suggestions.find(s => s.symbol === t.symbol);
    if (!q || q.error) return `<div class="stock-card"><div class="card-sym">${t.symbol}</div><div style="color:var(--red);font-size:11px">${q?.error || 'Loading...'}</div><button class="card-remove" onclick="removeTicker('${t.symbol}')">✕</button></div>`;
    const dir = q.pct_change >= 0 ? 'up' : 'down';
    const alertOn = S.triggeredAlerts.has(t.symbol);
    return `
      <div class="stock-card ${alertOn ? 'alert-triggered' : ''}">
        <div class="card-accent-bar ${q.above_ma50 ? 'bullish' : 'bearish'}"></div>
        <button class="card-edit" onclick="openTickerDetail('${t.symbol}')">✎</button>
        <button class="card-remove" onclick="removeTicker('${t.symbol}')">✕</button>
        <div class="card-top">
          <div>
            <div class="card-sym">${t.symbol}</div>
            <div class="card-meta">
              <span class="card-name">${t.name}</span>
              <span class="tier-badge ${tierClass(t.tier)}">${t.tier}</span>
            </div>
          </div>
          <div class="card-price-block">
            <div class="card-price ${dir}">$${q.price.toFixed(2)}</div>
            <div class="card-change ${dir}">${q.change >= 0 ? '+' : ''}$${q.change.toFixed(2)} (${q.pct_change >= 0 ? '+' : ''}${q.pct_change.toFixed(2)}%)</div>
          </div>
        </div>
        <div class="card-row">
          <div class="card-stat"><div class="stat-label">VOLUME</div><div class="stat-val">${q.volume}</div></div>
          <div class="card-stat"><div class="stat-label">RSI</div><div class="stat-val ${q.rsi < 30 ? 'bullish' : q.rsi > 70 ? 'bearish' : ''}">${q.rsi ? q.rsi.toFixed(0) : '—'}</div></div>
          <div class="card-stat"><div class="stat-label">MA20</div><div class="stat-val ${q.above_ma20 ? 'bullish' : 'bearish'}">${q.ma20 ? '$' + q.ma20.toFixed(2) : '—'}</div></div>
          <div class="card-stat"><div class="stat-label">MA50</div><div class="stat-val ${q.above_ma50 ? 'bullish' : 'bearish'}">${q.ma50 ? '$' + q.ma50.toFixed(2) : '—'}</div></div>
        </div>
        <div class="card-bottom">
          <div class="card-signals">
            ${q.ma20 ? `<span class="signal-badge ${q.above_ma20 ? 'signal-bull' : 'signal-bear'}">${q.above_ma20 ? '▲' : '▼'} MA20</span>` : ''}
            ${q.ma50 ? `<span class="signal-badge ${q.above_ma50 ? 'signal-bull' : 'signal-bear'}">${q.above_ma50 ? '▲' : '▼'} MA50</span>` : ''}
            ${t.notes ? `<span class="signal-badge signal-neutral" title="${t.notes}">📝</span>` : ''}
          </div>
          ${sug ? `<span class="suggestion-badge ${sug.signal === 'BUY' ? 'sug-buy' : sug.signal === 'SELL' ? 'sug-sell' : 'sug-hold'}">${sug.signal === 'BUY' ? '🟢' : sug.signal === 'SELL' ? '🔴' : '🟡'} ${sug.signal} ${sug.confidence}%</span>` : ''}
        </div>
      </div>`;
  }).join('');
  el.innerHTML = `<div class="compact-grid">${cards}</div>`;
}

function renderExpanded(el) {
  const rows = S.watchlist.map(t => {
    const q = S.quotes[t.symbol];
    const sug = S.suggestions.find(s => s.symbol === t.symbol);
    if (!q || q.error) return `<tr><td class="td-accent"></td><td><div class="td-sym">${t.symbol}</div></td><td colspan="7" style="color:var(--red)">${q?.error || 'Loading...'}</td><td><button onclick="removeTicker('${t.symbol}')" style="background:none;border:none;color:var(--red);cursor:pointer;font-family:var(--font)">✕</button></td></tr>`;
    const dir = q.pct_change >= 0 ? 'up' : 'down';
    return `
      <tr class="${S.triggeredAlerts.has(t.symbol) ? 'alert-triggered' : ''}">
        <td class="td-accent"><div class="td-accent-inner" style="background:${q.above_ma50 ? 'var(--green)' : 'var(--red)'}"></div></td>
        <td>
          <div class="td-sym">${t.symbol} <span class="tier-badge ${tierClass(t.tier)}" style="font-size:9px;padding:1px 4px">${t.tier}</span></div>
          <div class="td-name">${t.name}</div>
        </td>
        <td class="${dir}" style="font-weight:700;font-size:14px">$${q.price.toFixed(2)}</td>
        <td class="${dir}">${q.change >= 0 ? '+' : ''}$${q.change.toFixed(2)}</td>
        <td class="${dir}">${q.pct_change >= 0 ? '+' : ''}${q.pct_change.toFixed(2)}%</td>
        <td style="color:var(--text2)">${q.volume}</td>
        <td style="color:${q.rsi < 30 ? 'var(--green)' : q.rsi > 70 ? 'var(--red)' : 'var(--text2)'}">${q.rsi ? q.rsi.toFixed(0) : '—'}</td>
        <td class="${q.above_ma20 ? 'up' : 'down'}">${q.ma20 ? '$' + q.ma20.toFixed(2) : '—'}</td>
        <td class="${q.above_ma50 ? 'up' : 'down'}">${q.ma50 ? '$' + q.ma50.toFixed(2) : '—'}</td>
        <td>
          <div class="td-signals">
            ${sug ? `<span class="suggestion-badge ${sug.signal === 'BUY' ? 'sug-buy' : sug.signal === 'SELL' ? 'sug-sell' : 'sug-hold'}">${sug.signal} ${sug.confidence}%</span>` : ''}
            ${q.ma50 ? `<span class="signal-badge ${q.above_ma50 ? 'signal-bull' : 'signal-bear'}">${q.above_ma50 ? '▲' : '▼'} MA50</span>` : ''}
          </div>
        </td>
        <td>
          <button class="td-edit" onclick="openTickerDetail('${t.symbol}')">✎</button>
          <button class="td-remove" onclick="removeTicker('${t.symbol}')">✕</button>
        </td>
      </tr>`;
  }).join('');
  el.innerHTML = `<table class="expanded-table"><thead><tr><th></th><th>TICKER</th><th>PRICE</th><th>CHG</th><th>% CHG</th><th>VOLUME</th><th>RSI</th><th>MA20</th><th>MA50</th><th>SIGNALS</th><th></th></tr></thead><tbody>${rows}</tbody></table>`;
}

// ════════════════════════════════════════════════════════════════
// TABS
// ════════════════════════════════════════════════════════════════
function switchTab(name, btn) {
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById(`tab-${name}`).classList.add('active');
  document.getElementById('dash-toolbar').style.display = name === 'dashboard' ? '' : 'none';

  if (name === 'dashboard') fetchMarketSummary();
  if (name === 'signals') renderSignalsTab();
  if (name === 'portfolio') { loadPortfolio().then(renderPortfolio); }
  if (name === 'journal') { loadTrades().then(renderJournal); }
}

// ════════════════════════════════════════════════════════════════
// SIGNALS TAB
// ════════════════════════════════════════════════════════════════
async function fetchSuggestions() {
  const el = document.getElementById('signals-content');
  el.innerHTML = `<div class="loading-screen"><div class="loading-spinner"></div><div>Running analysis...</div></div>`;
  try {
    S.suggestions = await api.get('/api/suggestions');
    renderSignalsContent();
    // Also re-render dashboard cards to show updated badges
    renderDashboard();
  } catch (e) { el.innerHTML = `<div class="no-profile-msg">Failed to load signals.</div>`; }
}

function renderSignalsTab() {
  const profileNameEl = document.getElementById('signals-profile-name');
  if (S.activeProfile) {
    profileNameEl.textContent = S.activeProfile.name;
  } else {
    profileNameEl.textContent = 'Default';
  }
  if (S.suggestions.length === 0) {
    fetchSuggestions();
  } else {
    renderSignalsContent();
  }
}

function renderSignalsContent() {
  const el = document.getElementById('signals-content');
  if (S.suggestions.length === 0) {
    el.innerHTML = S.watchlist.length === 0
      ? `<div class="no-profile-msg">Add tickers to your watchlist first.</div>`
      : `<div class="no-profile-msg">No signals available. <a onclick="fetchSuggestions()">Run analysis</a> or add tickers.</div>`;
    return;
  }
  el.innerHTML = S.suggestions.map(s => {
    const sigClass = s.signal === 'BUY' ? 'sug-buy' : s.signal === 'SELL' ? 'sug-sell' : 'sug-hold';
    const fillClass = s.signal === 'BUY' ? 'buy' : s.signal === 'SELL' ? 'sell' : 'hold';
    const reasons = (s.signals || []).map(r => `
      <div class="signal-reason-row">
        <div class="reason-icon ${r.direction === 'bull' ? 'reason-bull' : r.direction === 'bear' ? 'reason-bear' : 'reason-neutral'}"></div>
        <div class="reason-text"><strong>${r.name}</strong> — ${r.reason}</div>
      </div>`).join('');
    return `
      <div class="signal-card">
        <div class="card-accent-bar ${s.signal === 'BUY' ? 'bullish' : s.signal === 'SELL' ? 'bearish' : ''}" style="position:absolute;left:0;top:0;width:3px;height:100%"></div>
        <div class="signal-card-header">
          <div>
            <div class="signal-sym">${s.symbol}</div>
            <div class="signal-name">${s.name || ''} ${s.tier ? `<span class="tier-badge ${tierClass(s.tier)}">${s.tier}</span>` : ''}</div>
          </div>
          <span class="suggestion-badge ${sigClass}">${s.signal === 'BUY' ? '🟢' : s.signal === 'SELL' ? '🔴' : '🟡'} ${s.signal}</span>
          <div class="confidence-bar-wrap">
            <div class="confidence-label">Confidence: <span class="confidence-pct" style="color:${s.signal === 'BUY' ? 'var(--green)' : s.signal === 'SELL' ? 'var(--red)' : 'var(--yellow)'}">${s.confidence}%</span></div>
            <div class="confidence-bar"><div class="confidence-fill ${fillClass}" style="width:${s.confidence}%"></div></div>
          </div>
          <div class="signal-price">$${s.price ? s.price.toFixed(2) : '—'}</div>
        </div>
        <div class="signal-reasons">${reasons}</div>
        ${s.description ? `<div class="signal-description">${s.description}</div>` : ''}
        <div class="signal-card-footer">
          <div class="tier-info">Risk: ${s.risk_tolerance || '—'}</div>
          <button class="log-trade-btn" onclick="openLogTradeFromSignal('${s.symbol}', '${s.signal}', ${s.confidence})">
            ${s.signal === 'BUY' ? '+ Log Buy' : s.signal === 'SELL' ? '+ Log Sell' : '+ Log Trade'}
          </button>
        </div>
      </div>`;
  }).join('');
}

function exportSuggestionsCSV() {
  window.location.href = '/api/suggestions/export/csv';
}

function printSuggestions() {
  window.print();
}

// ════════════════════════════════════════════════════════════════
// PORTFOLIO TAB
// ════════════════════════════════════════════════════════════════
async function renderPortfolio() {
  const summaryEl = document.getElementById('portfolio-summary');
  const contentEl = document.getElementById('portfolio-content');

  if (S.portfolio.length === 0) {
    summaryEl.innerHTML = '';
    contentEl.innerHTML = `<div class="empty-state"><h3>No positions</h3><p>Click "ADD POSITION" to track your holdings.</p></div>`;
    return;
  }

  // Request summary with current quotes
  let summary = null;
  try {
    summary = await api.post('/api/portfolio/summary', { quotes: S.quotes });
  } catch (e) {}

  if (summary) {
    const pnlClass = summary.total_pnl >= 0 ? 'up' : 'down';
    const dayClass = summary.day_gain >= 0 ? 'up' : 'down';
    summaryEl.innerHTML = `
      <div class="summary-card">
        <div class="summary-stat"><div class="stat-label">TOTAL VALUE</div><div class="stat-big">$${summary.total_value.toLocaleString('en-US', {minimumFractionDigits:2,maximumFractionDigits:2})}</div></div>
        <div class="summary-stat"><div class="stat-label">TOTAL INVESTED</div><div class="stat-big" style="color:var(--text2)">$${summary.total_invested.toLocaleString('en-US', {minimumFractionDigits:2,maximumFractionDigits:2})}</div></div>
        <div class="summary-stat"><div class="stat-label">TOTAL RETURN</div><div class="stat-big ${pnlClass}">${summary.total_pnl >= 0 ? '+' : ''}$${summary.total_pnl.toLocaleString('en-US', {minimumFractionDigits:2,maximumFractionDigits:2})}</div><div class="stat-sub ${pnlClass}">${summary.total_pnl_pct >= 0 ? '+' : ''}${summary.total_pnl_pct.toFixed(2)}%</div></div>
        <div class="summary-stat"><div class="stat-label">TODAY'S GAIN</div><div class="stat-big ${dayClass}">${summary.day_gain >= 0 ? '+' : ''}$${summary.day_gain.toFixed(2)}</div></div>
        ${summary.best_performer ? `<div class="summary-stat"><div class="stat-label">BEST PERFORMER</div><div class="stat-big" style="color:var(--green)">${summary.best_performer.symbol}</div><div class="stat-sub up">+${summary.best_performer.pnl_pct.toFixed(1)}%</div></div>` : ''}
        ${summary.worst_performer && summary.worst_performer.symbol !== summary.best_performer?.symbol ? `<div class="summary-stat"><div class="stat-label">WORST PERFORMER</div><div class="stat-big" style="color:var(--red)">${summary.worst_performer.symbol}</div><div class="stat-sub down">${summary.worst_performer.pnl_pct.toFixed(1)}%</div></div>` : ''}
      </div>`;
  } else {
    summaryEl.innerHTML = '';
  }

  const rows = S.portfolio.map(pos => {
    const q = S.quotes[pos.symbol];
    const price = q?.price || pos.cost_basis;
    const change = q?.change || 0;
    const value = pos.shares * price;
    const invested = pos.shares * pos.cost_basis;
    const pnl = value - invested;
    const pnlPct = invested ? pnl / invested * 100 : 0;
    const dayGain = pos.shares * change;
    const pnlClass = pnl >= 0 ? 'up' : 'down';
    return `
      <tr>
        <td><div class="pos-sym">${pos.symbol}</div><div class="pos-date">${pos.date_acquired || '—'}</div></td>
        <td>${pos.shares.toLocaleString()}</td>
        <td>$${pos.cost_basis.toFixed(2)}</td>
        <td>$${price.toFixed(2)}</td>
        <td>$${value.toLocaleString('en-US', {minimumFractionDigits:2,maximumFractionDigits:2})}</td>
        <td class="${pnlClass}">${pnl >= 0 ? '+' : ''}$${pnl.toFixed(2)}</td>
        <td class="${pnlClass}">${pnlPct >= 0 ? '+' : ''}${pnlPct.toFixed(2)}%</td>
        <td class="${dayGain >= 0 ? 'up' : 'down'}">${dayGain >= 0 ? '+' : ''}$${dayGain.toFixed(2)}</td>
        <td class="pos-notes" title="${pos.notes}">${pos.notes || '—'}</td>
        <td>
          <div class="td-actions">
            <button onclick="openEditPosition(${pos.id})" title="Edit">✎</button>
            <button class="del" onclick="deletePosition(${pos.id})" title="Delete">✕</button>
          </div>
        </td>
      </tr>`;
  }).join('');

  contentEl.innerHTML = `
    <table class="portfolio-table">
      <thead><tr>
        <th>TICKER</th><th>SHARES</th><th>COST BASIS</th><th>PRICE</th><th>VALUE</th><th>GAIN/LOSS</th><th>%</th><th>DAY G/L</th><th>NOTES</th><th></th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function openAddPosition() {
  document.getElementById('pos-id').value = '';
  document.getElementById('pos-symbol').value = '';
  document.getElementById('pos-shares').value = '';
  document.getElementById('pos-cost').value = '';
  document.getElementById('pos-date').value = new Date().toISOString().split('T')[0];
  document.getElementById('pos-notes').value = '';
  document.getElementById('position-modal-title').textContent = 'ADD POSITION';
  openModal('position-modal');
}

function openEditPosition(id) {
  const pos = S.portfolio.find(p => p.id === id);
  if (!pos) return;
  document.getElementById('pos-id').value = id;
  document.getElementById('pos-symbol').value = pos.symbol;
  document.getElementById('pos-shares').value = pos.shares;
  document.getElementById('pos-cost').value = pos.cost_basis;
  document.getElementById('pos-date').value = pos.date_acquired || '';
  document.getElementById('pos-notes').value = pos.notes || '';
  document.getElementById('position-modal-title').textContent = 'EDIT POSITION';
  openModal('position-modal');
}

async function savePosition() {
  const id = document.getElementById('pos-id').value;
  const body = {
    symbol: document.getElementById('pos-symbol').value.toUpperCase(),
    shares: parseFloat(document.getElementById('pos-shares').value),
    cost_basis: parseFloat(document.getElementById('pos-cost').value),
    date_acquired: document.getElementById('pos-date').value,
    notes: document.getElementById('pos-notes').value,
  };
  if (!body.symbol || isNaN(body.shares) || isNaN(body.cost_basis)) { showToast('Fill in symbol, shares, and cost basis.', 'error'); return; }
  try {
    if (id) {
      await api.put(`/api/portfolio/${id}`, body);
    } else {
      await api.post('/api/portfolio', body);
    }
    closeModal('position-modal');
    await loadPortfolio();
    renderPortfolio();
    showToast('Position saved.', 'success');
  } catch (e) { showToast('Failed to save position.', 'error'); }
}

async function deletePosition(id) {
  if (!confirm('Remove this position?')) return;
  await api.del(`/api/portfolio/${id}`);
  S.portfolio = S.portfolio.filter(p => p.id !== id);
  renderPortfolio();
  showToast('Position removed.', 'success');
}

// ════════════════════════════════════════════════════════════════
// JOURNAL TAB
// ════════════════════════════════════════════════════════════════
function renderJournal() {
  const el = document.getElementById('journal-content');
  if (S.trades.length === 0) {
    el.innerHTML = `<div class="empty-state"><h3>No trades logged</h3><p>Click "LOG TRADE" to start tracking your trades.</p></div>`;
    drawPnLChart([]);
    return;
  }

  drawPnLChart(S.trades);

  const rows = S.trades.map(t => {
    const actionClass = `action-${t.action.toLowerCase()}`;
    const pnlStr = t.realized_pnl != null ? `<span class="${t.realized_pnl >= 0 ? 'pnl-pos' : 'pnl-neg'}">${t.realized_pnl >= 0 ? '+' : ''}$${t.realized_pnl.toFixed(2)}</span>` : '—';
    const sigBadge = t.signal_triggered ? `<span class="signal-indicator sug-${t.signal_type?.toLowerCase() || 'hold'}">${t.signal_type || 'SIG'}</span>` : '';
    return `
      <tr>
        <td>${t.date}</td>
        <td style="font-weight:700">${t.symbol}</td>
        <td class="${actionClass}">${t.action}</td>
        <td>${t.shares.toLocaleString()}</td>
        <td>$${t.price.toFixed(2)}</td>
        <td>$${t.total.toLocaleString('en-US', {minimumFractionDigits:2,maximumFractionDigits:2})}</td>
        <td>${pnlStr}</td>
        <td>${sigBadge}</td>
        <td style="color:var(--text3);font-size:11px">${t.notes || ''}</td>
        <td><button class="td-del" onclick="deleteTrade(${t.id})">✕</button></td>
      </tr>`;
  }).join('');

  el.innerHTML = `
    <table class="journal-table">
      <thead><tr><th>DATE</th><th>TICKER</th><th>ACTION</th><th>SHARES</th><th>PRICE</th><th>TOTAL</th><th>P&L</th><th>SIGNAL</th><th>NOTES</th><th></th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function drawPnLChart(trades) {
  const canvas = document.getElementById('pnl-chart');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  canvas.width = canvas.offsetWidth || 800;

  const sells = trades.filter(t => ['Sell', 'Cover'].includes(t.action) && t.realized_pnl != null)
    .sort((a, b) => new Date(a.date) - new Date(b.date));

  if (sells.length < 2) {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = '#5a5a6e';
    ctx.font = '11px JetBrains Mono, monospace';
    ctx.fillText('P&L chart appears after 2+ closed trades with P&L recorded', 20, 45);
    return;
  }

  let running = 0;
  const points = sells.map(t => { running += t.realized_pnl; return running; });
  const labels = sells.map(t => t.date);

  const W = canvas.width, H = canvas.height;
  const pad = { top: 10, right: 10, bottom: 20, left: 55 };
  const chartW = W - pad.left - pad.right;
  const chartH = H - pad.top - pad.bottom;

  const minVal = Math.min(0, ...points);
  const maxVal = Math.max(0, ...points);
  const range = maxVal - minVal || 1;

  ctx.clearRect(0, 0, W, H);

  // Zero line
  const zeroY = pad.top + chartH - ((0 - minVal) / range * chartH);
  ctx.strokeStyle = '#2a2a35';
  ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(pad.left, zeroY); ctx.lineTo(W - pad.right, zeroY); ctx.stroke();

  // Y axis labels
  ctx.fillStyle = '#5a5a6e';
  ctx.font = '9px JetBrains Mono, monospace';
  ctx.textAlign = 'right';
  ctx.fillText('$' + maxVal.toFixed(0), pad.left - 4, pad.top + 8);
  ctx.fillText('$0', pad.left - 4, zeroY + 3);
  ctx.fillText('$' + minVal.toFixed(0), pad.left - 4, H - pad.bottom - 2);

  // Line
  const xStep = chartW / (points.length - 1);
  const getY = v => pad.top + chartH - ((v - minVal) / range * chartH);
  const getX = i => pad.left + i * xStep;

  ctx.beginPath();
  ctx.moveTo(getX(0), getY(points[0]));
  for (let i = 1; i < points.length; i++) ctx.lineTo(getX(i), getY(points[i]));
  ctx.strokeStyle = points[points.length - 1] >= 0 ? '#00e676' : '#ff5252';
  ctx.lineWidth = 2;
  ctx.stroke();

  // Dots
  points.forEach((v, i) => {
    ctx.beginPath();
    ctx.arc(getX(i), getY(v), 3, 0, Math.PI * 2);
    ctx.fillStyle = v >= 0 ? '#00e676' : '#ff5252';
    ctx.fill();
  });
}

function filterJournal() {
  const sym = document.getElementById('journal-sym-filter').value.toUpperCase();
  const action = document.getElementById('journal-action-filter').value;
  const start = document.getElementById('journal-start').value;
  const end = document.getElementById('journal-end').value;
  const filters = {};
  if (sym) filters.symbol = sym;
  if (action) filters.action = action;
  if (start) filters.start = start;
  if (end) filters.end = end;
  loadTrades(filters).then(renderJournal);
}

function openLogTrade() {
  document.getElementById('trade-id').value = '';
  document.getElementById('trade-symbol').value = '';
  document.querySelector('[name="trade-action"][value="Buy"]').checked = true;
  document.getElementById('trade-shares').value = '';
  document.getElementById('trade-price').value = '';
  document.getElementById('trade-date').value = new Date().toISOString().split('T')[0];
  document.getElementById('trade-pnl').value = '';
  document.getElementById('trade-notes').value = '';
  document.getElementById('trade-signal-cb').checked = false;
  openModal('trade-modal');
}

function openLogTradeFromSignal(symbol, signal, confidence) {
  openLogTrade();
  document.getElementById('trade-symbol').value = symbol;
  const action = signal === 'BUY' ? 'Buy' : signal === 'SELL' ? 'Sell' : 'Buy';
  document.querySelector(`[name="trade-action"][value="${action}"]`).checked = true;
  const q = S.quotes[symbol];
  if (q) document.getElementById('trade-price').value = q.price.toFixed(2);
  document.getElementById('trade-signal-cb').checked = true;
  document.getElementById('trade-notes').value = `${signal} signal — ${confidence}% confidence`;
}

async function saveTrade() {
  const body = {
    symbol: document.getElementById('trade-symbol').value.toUpperCase(),
    action: document.querySelector('[name="trade-action"]:checked')?.value || 'Buy',
    shares: parseFloat(document.getElementById('trade-shares').value),
    price: parseFloat(document.getElementById('trade-price').value),
    date: document.getElementById('trade-date').value,
    notes: document.getElementById('trade-notes').value,
    realized_pnl: parseFloat(document.getElementById('trade-pnl').value) || null,
    signal_triggered: document.getElementById('trade-signal-cb').checked,
  };
  if (!body.symbol || isNaN(body.shares) || isNaN(body.price)) { showToast('Fill in symbol, shares, and price.', 'error'); return; }
  try {
    await api.post('/api/trades', body);
    closeModal('trade-modal');
    await loadTrades();
    renderJournal();
    showToast('Trade logged.', 'success');
  } catch (e) { showToast('Failed to log trade.', 'error'); }
}

async function deleteTrade(id) {
  if (!confirm('Delete this trade?')) return;
  await api.del(`/api/trades/${id}`);
  S.trades = S.trades.filter(t => t.id !== id);
  renderJournal();
}

function exportTradesCSV() { window.location.href = '/api/trades/export/csv'; }

// ════════════════════════════════════════════════════════════════
// TICKER DETAIL (tier + notes)
// ════════════════════════════════════════════════════════════════
function openTickerDetail(sym) {
  const item = S.watchlist.find(t => t.symbol === sym);
  if (!item) return;
  document.getElementById('td-symbol').value = sym;
  document.getElementById('td-title').textContent = `${sym} — DETAILS`;
  document.querySelectorAll('[name="td-tier"]').forEach(r => { r.checked = r.value === item.tier; });
  document.getElementById('td-notes').value = item.notes || '';
  openModal('ticker-detail-modal');
}

async function saveTierNotes() {
  const sym = document.getElementById('td-symbol').value;
  const tier = document.querySelector('[name="td-tier"]:checked')?.value || 'Active Watch';
  const notes = document.getElementById('td-notes').value;
  await api.put(`/api/watchlist/${sym}`, { tier, notes });
  const item = S.watchlist.find(t => t.symbol === sym);
  if (item) { item.tier = tier; item.notes = notes; }
  closeModal('ticker-detail-modal');
  renderDashboard();
  showToast('Details saved.', 'success');
}

// ════════════════════════════════════════════════════════════════
// DASHBOARD — INLINE ADD
// ════════════════════════════════════════════════════════════════
async function dashAddTicker() {
  const input = document.getElementById('dash-ticker-input');
  const sym = input.value.trim().toUpperCase().replace(/[^A-Z0-9.\-]/g, '');
  if (!sym) return;
  if (S.watchlist.find(t => t.symbol === sym)) { showToast(`${sym} already on watchlist.`, 'error'); input.value = ''; return; }
  showToast(`Validating ${sym}...`);
  try {
    const data = await api.get(`/api/validate/${sym}`);
    if (data.valid) {
      const item = await api.post('/api/watchlist', { symbol: sym, name: data.name });
      S.watchlist.push(item);
      input.value = '';
      showToast(`✓ ${sym} added.`, 'success');
      fetchAllQuotes();
    } else {
      showToast(`"${sym}" not found.`, 'error');
    }
  } catch (e) { showToast('Server error.', 'error'); }
}

// ════════════════════════════════════════════════════════════════
// SETTINGS
// ════════════════════════════════════════════════════════════════
function openSettings(tab = 'general') {
  openModal('settings-modal');
  switchSettingsTab(tab, document.getElementById(`stab-btn-${tab}`));
  document.getElementById('settings-interval').value = S.preferences.interval || '300';
  document.getElementById('settings-density').value = S.preferences.density || 'compact';
  renderProfilesList();
  renderSettingsAlerts();
}

function switchSettingsTab(name, btn) {
  document.querySelectorAll('.modal-tab').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.stab-content').forEach(c => c.classList.remove('active'));
  if (btn) btn.classList.add('active');
  document.getElementById(`stab-${name}`).classList.add('active');
  if (name === 'profiles') renderProfilesList();
  if (name === 'alerts') renderSettingsAlerts();
}

async function saveGeneralSettings() {
  S.preferences.interval = document.getElementById('settings-interval').value;
  S.preferences.density = document.getElementById('settings-density').value;
  await api.put('/api/preferences', S.preferences);
  applyPreferences();
  renderDashboard();
}

function applyPreferences() {
  if (document.getElementById('settings-interval')) {
    document.getElementById('settings-interval').value = S.preferences.interval || '300';
  }
  if (document.getElementById('settings-density')) {
    document.getElementById('settings-density').value = S.preferences.density || 'compact';
  }
  startAutoRefresh();
}

function renderProfilesList() {
  const el = document.getElementById('profiles-list');
  if (S.profiles.length === 0) {
    el.innerHTML = '<p style="color:var(--text3);font-size:12px">No profiles yet. Create one below.</p>';
    return;
  }
  el.innerHTML = S.profiles.map(p => `
    <div class="profile-item ${p.is_active ? 'is-active' : ''}">
      <div>
        <div class="profile-item-name">${p.name} ${p.is_active ? '<span class="profile-active-badge">ACTIVE</span>' : ''}</div>
        <div class="profile-item-meta">${p.risk_tolerance} · ${p.horizon} · Max ${p.max_trades_per_day} trades/day</div>
      </div>
      <div class="profile-item-actions">
        ${!p.is_active ? `<button class="btn-sm" onclick="activateProfile(${p.id})">SET ACTIVE</button>` : ''}
        <button class="btn-sm" onclick="openProfileEditor(${p.id})">EDIT</button>
        <button class="btn-sm" style="color:var(--red);border-color:rgba(255,82,82,.3)" onclick="deleteProfile(${p.id})">DEL</button>
      </div>
    </div>`).join('');
}

function renderSettingsAlerts() {
  const el = document.getElementById('settings-alerts');
  if (S.watchlist.length === 0) { el.innerHTML = '<p style="color:var(--text3);font-size:12px">No tickers on watchlist.</p>'; return; }
  el.innerHTML = S.watchlist.map(t => `
    <div class="alert-row-settings">
      <span class="sym">${t.symbol}</span>
      <select id="sadir-${t.symbol}" onchange="saveAlertSettings('${t.symbol}')">
        <option value="above" ${t.alert_direction === 'above' ? 'selected' : ''}>ABOVE</option>
        <option value="below" ${t.alert_direction !== 'above' ? 'selected' : ''}>BELOW</option>
      </select>
      <input type="number" step="0.01" min="0" id="saprice-${t.symbol}"
        value="${t.alert_price || ''}" placeholder="price"
        onchange="saveAlertSettings('${t.symbol}')">
    </div>`).join('');
}

async function saveAlertSettings(sym) {
  const price = parseFloat(document.getElementById(`saprice-${sym}`)?.value);
  const dir = document.getElementById(`sadir-${sym}`)?.value || 'above';
  const update = { alert_direction: dir, alert_price: isNaN(price) ? null : price };
  await api.put(`/api/watchlist/${sym}`, update);
  const item = S.watchlist.find(t => t.symbol === sym);
  if (item) { item.alert_direction = update.alert_direction; item.alert_price = update.alert_price; }
}

async function resetApp() {
  if (!confirm('This will delete ALL data (watchlist, portfolio, trades, profiles). Are you sure?')) return;
  // Delete all watchlist items
  for (const t of [...S.watchlist]) await api.del(`/api/watchlist/${t.symbol}`);
  for (const p of [...S.portfolio]) await api.del(`/api/portfolio/${p.id}`);
  for (const t of [...S.trades]) await api.del(`/api/trades/${t.id}`);
  for (const p of [...S.profiles]) await api.del(`/api/profiles/${p.id}`);
  S.watchlist = []; S.portfolio = []; S.trades = []; S.profiles = []; S.quotes = {}; S.suggestions = [];
  stopAutoRefresh();
  closeModal('settings-modal');
  showOnboarding();
}

// ════════════════════════════════════════════════════════════════
// PROFILE EDITOR
// ════════════════════════════════════════════════════════════════
function openProfileEditor(id) {
  const profile = id ? S.profiles.find(p => p.id === id) : null;
  document.getElementById('pe-id').value = id || '';
  document.getElementById('profile-modal-title').textContent = id ? 'EDIT PROFILE' : 'NEW PROFILE';
  document.getElementById('pe-name').value = profile?.name || 'New Profile';
  document.querySelectorAll('[name="pe-risk"]').forEach(r => { r.checked = r.value === (profile?.risk_tolerance || 'Moderate'); });
  document.querySelectorAll('[name="pe-horizon"]').forEach(r => { r.checked = r.value === (profile?.horizon || 'Swing'); });

  const setSlider = (id, val) => {
    const pct = Math.round(val * 100);
    document.getElementById(id).value = pct;
    document.getElementById(id + '-v').textContent = pct + '%';
  };
  setSlider('pe-w-ma', profile?.ma_weight ?? 0.25);
  setSlider('pe-w-vol', profile?.volume_weight ?? 0.25);
  setSlider('pe-w-rsi', profile?.rsi_weight ?? 0.25);
  setSlider('pe-w-mom', profile?.momentum_weight ?? 0.25);

  document.getElementById('pe-rsi-ob').value = profile?.rsi_overbought ?? 70;
  document.getElementById('pe-rsi-os').value = profile?.rsi_oversold ?? 30;
  document.getElementById('pe-max-trades').value = profile?.max_trades_per_day ?? 3;
  document.getElementById('pe-vol-thresh').value = profile?.volume_spike_threshold ?? 1.5;
  document.getElementById('pe-mom-days').value = profile?.momentum_days ?? 10;

  openModal('profile-modal');
}

async function saveProfile() {
  const id = document.getElementById('pe-id').value;
  const body = {
    name: document.getElementById('pe-name').value,
    risk_tolerance: document.querySelector('[name="pe-risk"]:checked')?.value || 'Moderate',
    horizon: document.querySelector('[name="pe-horizon"]:checked')?.value || 'Swing',
    ma_weight: parseInt(document.getElementById('pe-w-ma').value) / 100,
    volume_weight: parseInt(document.getElementById('pe-w-vol').value) / 100,
    rsi_weight: parseInt(document.getElementById('pe-w-rsi').value) / 100,
    momentum_weight: parseInt(document.getElementById('pe-w-mom').value) / 100,
    rsi_overbought: parseFloat(document.getElementById('pe-rsi-ob').value),
    rsi_oversold: parseFloat(document.getElementById('pe-rsi-os').value),
    max_trades_per_day: parseInt(document.getElementById('pe-max-trades').value),
    volume_spike_threshold: parseFloat(document.getElementById('pe-vol-thresh').value),
    momentum_days: parseInt(document.getElementById('pe-mom-days').value),
  };
  try {
    let result;
    if (id) {
      result = await api.put(`/api/profiles/${id}`, body);
      const idx = S.profiles.findIndex(p => p.id === parseInt(id));
      if (idx >= 0) S.profiles[idx] = result;
    } else {
      result = await api.post('/api/profiles', body);
      S.profiles.push(result);
    }
    closeModal('profile-modal');
    renderProfilesList();
    showToast('Profile saved.', 'success');
  } catch (e) { showToast('Failed to save profile.', 'error'); }
}

async function activateProfile(id) {
  const result = await api.post(`/api/profiles/${id}/activate`, {});
  S.profiles.forEach(p => p.is_active = p.id === id);
  S.activeProfile = S.profiles.find(p => p.id === id);
  renderProfilesList();
  S.suggestions = [];
  showToast(`Profile "${result.name}" is now active.`, 'success');
}

async function deleteProfile(id) {
  if (!confirm('Delete this profile?')) return;
  await api.del(`/api/profiles/${id}`);
  S.profiles = S.profiles.filter(p => p.id !== id);
  if (S.activeProfile?.id === id) S.activeProfile = null;
  renderProfilesList();
}

// ════════════════════════════════════════════════════════════════
// ALERTS / NOTIFICATIONS
// ════════════════════════════════════════════════════════════════
async function requestNotifications() {
  if (!('Notification' in window)) { showToast('Notifications not supported.', 'error'); return; }
  const perm = await Notification.requestPermission();
  S.notifGranted = perm === 'granted';
  document.getElementById('notif-btn').classList.toggle('active', S.notifGranted);
  if (S.notifGranted) {
    showToast('Desktop notifications enabled.', 'success');
    new Notification('▣ PG Stock Analysis', { body: 'Price alerts are now active.' });
  } else {
    showToast('Notification permission denied.', 'error');
  }
}

function checkAlerts() {
  S.watchlist.forEach(item => {
    if (!item.alert_price) return;
    const q = S.quotes[item.symbol];
    if (!q || q.error) return;
    const triggered = item.alert_direction === 'above' ? q.price >= item.alert_price : q.price <= item.alert_price;
    if (triggered && !S.triggeredAlerts.has(item.symbol)) {
      S.triggeredAlerts.add(item.symbol);
      if (S.notifGranted) new Notification(`${item.symbol} Alert`, { body: `${item.symbol} is ${item.alert_direction} $${item.alert_price} — now $${q.price.toFixed(2)}` });
      showToast(`🔔 ${item.symbol} crossed $${item.alert_price} → $${q.price.toFixed(2)}`, 'success');
    } else if (!triggered) {
      S.triggeredAlerts.delete(item.symbol);
    }
  });
}

// ════════════════════════════════════════════════════════════════
// AUTO REFRESH
// ════════════════════════════════════════════════════════════════
function startAutoRefresh() {
  stopAutoRefresh();
  S.countdownVal = parseInt(S.preferences.interval || 300);
  updateCountdown();
  S.countdownTimer = setInterval(() => {
    S.countdownVal--;
    updateCountdown();
    if (S.countdownVal <= 0) { fetchAllQuotes(); S.countdownVal = parseInt(S.preferences.interval || 300); }
  }, 1000);
}

function stopAutoRefresh() { if (S.countdownTimer) clearInterval(S.countdownTimer); }

function updateCountdown() {
  const el = document.getElementById('countdown');
  if (!el) return;
  const m = Math.floor(S.countdownVal / 60), s = S.countdownVal % 60;
  el.textContent = m > 0 ? `${m}m${String(s).padStart(2,'0')}s` : `${s}s`;
}

function updateMarketStatus() {
  const el = document.getElementById('market-status');
  if (!el) return;
  const now = new Date(), day = now.getDay(), h = now.getHours() + now.getMinutes() / 60;
  const open = day >= 1 && day <= 5 && h >= 9.5 && h < 16;
  el.textContent = open ? '● MARKET OPEN' : '○ MARKET CLOSED';
  el.className = 'market-status ' + (open ? 'market-open' : 'market-closed');
}

// ════════════════════════════════════════════════════════════════
// BACKUP / RESTORE
// ════════════════════════════════════════════════════════════════
function downloadBackup() { window.location.href = '/api/backup'; }

function importBackup(input) {
  const file = input.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = async e => {
    try {
      const data = JSON.parse(e.target.result);
      await api.post('/api/restore', data);
      showToast('Backup restored. Reloading...', 'success');
      setTimeout(() => location.reload(), 1500);
    } catch (err) { showToast('Invalid backup file.', 'error'); }
  };
  reader.readAsText(file);
  input.value = '';
}

// ════════════════════════════════════════════════════════════════
// WIDGET GENERATOR
// ════════════════════════════════════════════════════════════════
function openWidget() {
  document.getElementById('widget-code').textContent = generateScriptableScript();
  openModal('widget-modal');
}

function switchModalTab(tab, btn) {
  document.querySelectorAll('#widget-modal .modal-tab').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('#widget-modal .modal-tab-content').forEach(c => c.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById(`mtab-${tab}`).classList.add('active');
}

function copyScript() {
  navigator.clipboard.writeText(document.getElementById('widget-code').textContent).then(() => {
    const btn = document.getElementById('copy-btn');
    btn.textContent = '✓ COPIED'; btn.classList.add('copied');
    setTimeout(() => { btn.textContent = '⎘ COPY'; btn.classList.remove('copied'); }, 2000);
  });
}

function generateScriptableScript() {
  const syms = S.watchlist.map(t => t.symbol);
  const host = window.location.hostname || 'localhost';
  const port = window.location.port || '5000';
  const baseUrl = `http://${host}:${port}`;

  return `// ▣ PG Stock Analysis — Scriptable iOS Widget
// Paste into Scriptable app, name it "StockTicker"
// Update BASE_URL to your server's local IP

const BASE_URL = "${baseUrl}";
const TICKERS = ${JSON.stringify(syms)};

async function fetchData() {
  // Fetch quotes
  const quotesReq = new Request(BASE_URL + "/api/quote");
  quotesReq.method = "POST";
  quotesReq.headers = { "Content-Type": "application/json" };
  quotesReq.body = JSON.stringify({ tickers: TICKERS });

  // Fetch suggestions
  const sugReq = new Request(BASE_URL + "/api/suggestions");

  // Fetch portfolio summary
  const portReq = new Request(BASE_URL + "/api/portfolio");

  const [quotes, suggestions, portfolio] = await Promise.all([
    quotesReq.loadJSON().catch(() => ({})),
    sugReq.loadJSON().catch(() => []),
    portReq.loadJSON().catch(() => []),
  ]);

  return { quotes, suggestions, portfolio };
}

const widget = new ListWidget();
widget.backgroundColor = new Color("#0a0a0b");
widget.setPadding(12, 14, 10, 14);
widget.refreshAfterDate = new Date(Date.now() + 15 * 60 * 1000);
widget.url = BASE_URL;

const isSmall = config.widgetFamily === "small";
const maxTickers = isSmall ? 3 : 6;

try {
  const { quotes, suggestions, portfolio } = await fetchData();

  // Header
  const header = widget.addStack();
  header.layoutHorizontally();
  const logo = header.addText("▣ PG Stock Analysis");
  logo.textColor = new Color("#00d4aa");
  logo.font = Font.boldMonospacedSystemFont(10);
  header.addSpacer();
  const ts = header.addText(new Date().toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}));
  ts.textColor = new Color("#5a5a6e");
  ts.font = Font.mediumMonospacedSystemFont(9);

  widget.addSpacer(4);

  // Portfolio summary (medium widget only)
  if (!isSmall && portfolio.length > 0) {
    const portReq2 = new Request(BASE_URL + "/api/portfolio/summary");
    portReq2.method = "POST";
    portReq2.headers = { "Content-Type": "application/json" };
    portReq2.body = JSON.stringify({ quotes });
    const summary = await portReq2.loadJSON().catch(() => null);
    if (summary) {
      const portRow = widget.addStack();
      portRow.layoutHorizontally();
      const valText = portRow.addText("Portfolio $" + summary.total_value.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2}));
      valText.textColor = new Color("#9898a8");
      valText.font = Font.mediumMonospacedSystemFont(9);
      portRow.addSpacer();
      const dayText = portRow.addText((summary.day_gain >= 0 ? "+" : "") + "$" + summary.day_gain.toFixed(2));
      dayText.textColor = summary.day_gain >= 0 ? new Color("#00e676") : new Color("#ff5252");
      dayText.font = Font.mediumMonospacedSystemFont(9);
      widget.addSpacer(3);
    }
  }

  // Top signal
  const topSignal = suggestions.find(s => s.signal === "BUY" || s.signal === "SELL");
  if (topSignal) {
    const sigRow = widget.addStack();
    sigRow.layoutHorizontally();
    const sigIcon = topSignal.signal === "BUY" ? "🟢" : "🔴";
    const sigText = sigRow.addText(sigIcon + " " + topSignal.symbol + " " + topSignal.signal + " " + topSignal.confidence + "%");
    sigText.textColor = topSignal.signal === "BUY" ? new Color("#00e676") : new Color("#ff5252");
    sigText.font = Font.mediumMonospacedSystemFont(9);
    widget.addSpacer(4);
  }

  // Tickers
  const display = TICKERS.slice(0, maxTickers);
  for (const sym of display) {
    const q = quotes[sym];
    const row = widget.addStack();
    row.layoutHorizontally();
    row.centerAlignContent();

    if (!q || q.error) {
      const t = row.addText(sym + " — error");
      t.textColor = Color.red();
      t.font = Font.mediumMonospacedSystemFont(11);
      widget.addSpacer(2);
      continue;
    }

    const isUp = q.pct_change >= 0;
    const color = isUp ? new Color("#00e676") : new Color("#ff5252");

    const symText = row.addText(sym.padEnd(5));
    symText.textColor = new Color("#e2e2e8");
    symText.font = Font.boldMonospacedSystemFont(12);

    row.addSpacer();

    const priceText = row.addText("$" + q.price.toFixed(2));
    priceText.textColor = color;
    priceText.font = Font.mediumMonospacedSystemFont(11);

    row.addSpacer(4);

    const pctText = row.addText((isUp ? "+" : "") + q.pct_change.toFixed(1) + "%");
    pctText.textColor = color;
    pctText.font = Font.mediumMonospacedSystemFont(10);

    widget.addSpacer(2);
  }
} catch(e) {
  const errText = widget.addText("⚠ " + e.message);
  errText.textColor = Color.red();
  errText.font = Font.mediumMonospacedSystemFont(11);
}

Script.setWidget(widget);
Script.complete();
`;
}

// ════════════════════════════════════════════════════════════════
// MODAL HELPERS
// ════════════════════════════════════════════════════════════════
function openModal(id) { document.getElementById(id).classList.add('open'); }
function closeModal(id) { document.getElementById(id).classList.remove('open'); }
function closeModalOverlay(e, id) { if (e.target === document.getElementById(id)) closeModal(id); }

// ════════════════════════════════════════════════════════════════
// ELECTRON INTEGRATION
// ════════════════════════════════════════════════════════════════
function initElectronControls() {
  if (!window.electronAPI) return;

  // Inject compact/full toggle into toolbar
  const toolbar = document.getElementById('toolbar-center');
  if (toolbar) {
    toolbar.innerHTML = `
      <div style="display:flex;gap:6px;align-items:center">
        <button class="btn-sm" onclick="electronCompact()" title="Compact sidebar mode">⊟ COMPACT</button>
        <button class="btn-sm" onclick="electronFull()" title="Full dashboard mode">⊞ FULL</button>
        <button class="btn-sm" id="aot-btn" onclick="electronToggleAOT()" title="Always on top">📌 AOT</button>
      </div>`;
  }
}

async function electronToggleAOT() {
  const result = await window.electronAPI.toggleAlwaysOnTop();
  const btn = document.getElementById('aot-btn');
  if (btn) { btn.style.color = result ? 'var(--accent)' : ''; btn.style.borderColor = result ? 'var(--accent)' : ''; }
}
function electronCompact() { window.electronAPI?.setCompactMode(); }
function electronFull() { window.electronAPI?.setFullMode(); }

// Send quote data to main process for tray menu
function sendQuotesToTray(quotes) {
  window.electronAPI?.sendQuotes(quotes);
}

// ════════════════════════════════════════════════════════════════
// TOAST
// ════════════════════════════════════════════════════════════════
function showToast(msg, type = '') {
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 3500);
}
