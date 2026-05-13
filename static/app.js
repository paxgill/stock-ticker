// ════════════════════════════════════════════════════════════════
// STATE
// ════════════════════════════════════════════════════════════════
const S = {
  watchlist: [],
  profiles: [],
  activeProfile: null,
  portfolio: [],
  trades: [],
  quotes: {},
  tickerDetails: {},   // extended: earnings, 52W, short interest, pre/post market
  suggestions: [],
  preferences: { interval: '300', density: 'compact' },
  countdownVal: 0,
  countdownTimer: null,
  notifGranted: false,
  triggeredAlerts: new Set(),
  theme: localStorage.getItem('pg_theme') || 'dark',
  strategyPresets: [],
  presetLocked: { ob: false, pe: false },
  quizAnswers: [null, null, null, null, null],
};

// ════════════════════════════════════════════════════════════════
// XSS ESCAPE HELPER (BUG-008, BUG-009)
// ════════════════════════════════════════════════════════════════
function esc(str) {
  if (str == null) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// ════════════════════════════════════════════════════════════════
// API CLIENT (BUG-010 — check response.ok before parsing JSON)
// ════════════════════════════════════════════════════════════════
async function _apiFetch(url, opts = {}) {
  const r = await fetch(url, opts);
  if (!r.ok) {
    let msg = `${r.status} ${r.statusText}`;
    try { const j = await r.json(); if (j && j.error) msg = j.error; } catch {}
    throw new Error(msg);
  }
  return r.json();
}
const api = {
  get:  (url)        => _apiFetch(url),
  post: (url, body)  => _apiFetch(url, { method: 'POST',   headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
  put:  (url, body)  => _apiFetch(url, { method: 'PUT',    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
  del:  (url)        => _apiFetch(url, { method: 'DELETE' }),
};

// ════════════════════════════════════════════════════════════════
// INIT
// ════════════════════════════════════════════════════════════════
window.addEventListener('DOMContentLoaded', async () => {
  applyTheme(S.theme);
  document.getElementById('ticker-input').addEventListener('keydown', e => { if (e.key === 'Enter') addTicker(); });

  // Load persistent state from backend
  await Promise.all([
    loadWatchlist(),
    loadProfiles(),
    loadPreferences(),
    loadStrategyPresets(),
  ]);

  // Migrate old localStorage state if DB is empty
  await migrateLocalStorage();

  if (S.watchlist.length > 0) {
    showDashboard();
  } else {
    showOnboarding();
  }

  // BUG-034: only mark as granted if already granted; 'default' means not yet asked
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
  } catch (e) {
    // BUG-030: log migration failures so they're diagnosable
    console.warn('migrateLocalStorage failed:', e);
  }
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

// ── Weight slider rebalancing (BUG-005) ─────────────────────────────────────
// Keeps all 4 sliders summed to exactly 100% by redistributing the remainder
// proportionally among the unchanged sliders whenever one is moved.
function _rebalanceSliders(keys, changedKey, newVal, sliderIdFn, labelIdFn) {
  const others = keys.filter(k => k !== changedKey);
  const cv = Math.max(0, Math.min(100, parseInt(newVal) || 0));
  const remaining = Math.max(0, 100 - cv);
  const otherVals = others.map(k => Math.max(0, parseInt(document.getElementById(sliderIdFn(k))?.value || 0)));
  const otherSum  = otherVals.reduce((a, b) => a + b, 0);
  let given = 0;
  others.forEach((k, i) => {
    const isLast = i === others.length - 1;
    let v = isLast
      ? Math.max(0, remaining - given)
      : otherSum > 0
        ? Math.round(otherVals[i] / otherSum * remaining)
        : Math.floor(remaining / others.length);
    v = Math.max(0, Math.min(100, v));
    given += v;
    const sl = document.getElementById(sliderIdFn(k));
    const lb = document.getElementById(labelIdFn(k));
    if (sl) sl.value = v;
    if (lb) lb.textContent = v + '%';
  });
  const lb = document.getElementById(labelIdFn(changedKey));
  if (lb) lb.textContent = cv + '%';
}

// Onboarding sliders (IDs: w-ma / w-ma-val)
function rebalanceObWeight(key, val) {
  _rebalanceSliders(
    ['ma', 'vol', 'rsi', 'mom'], key, val,
    k => `w-${k}`,
    k => `w-${k}-val`
  );
}

// Profile-editor sliders (IDs: pe-w-ma / pe-w-ma-v)
function rebalancePeWeight(key, val) {
  _rebalanceSliders(
    ['ma', 'vol', 'rsi', 'mom'], key, val,
    k => `pe-w-${k}`,
    k => `pe-w-${k}-v`
  );
}

// Legacy single-slider display update (kept for any remaining callers)
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
  // BUG-015: disable all quick-add buttons during validation so clicks don't queue
  const qaButtons = document.querySelectorAll('.qa-btn');
  qaButtons.forEach(b => { b.disabled = true; });
  document.getElementById('ticker-input').value = sym;
  await addTicker();
  qaButtons.forEach(b => { b.disabled = false; });
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
      <span class="chip-sym">${esc(t.symbol)}</span>
      <span class="chip-name">${esc(t.name)}</span>
      <span class="chip-tier tier-badge ${tierClass(t.tier)}" onclick="cycleTier('${esc(t.symbol)}')">${esc(t.tier)}</span>
      <button class="chip-remove" onclick="removeTicker('${esc(t.symbol)}')">✕</button>
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

  // BUG-004: always save the onboarding profile (even when re-onboarding after reset)
  try {
    await api.post('/api/profiles', {
      name: profileName, risk_tolerance: risk, horizon,
      ma_weight: maW, volume_weight: volW, rsi_weight: rsiW, momentum_weight: momW,
      rsi_overbought: parseFloat(document.getElementById('ob-rsi-ob')?.value || 70),
      rsi_oversold: parseFloat(document.getElementById('ob-rsi-os')?.value || 30),
      volume_spike_threshold: parseFloat(document.getElementById('ob-vol-thresh')?.value || 1.5),
      max_trades_per_day: parseInt(document.getElementById('ob-max-trades')?.value || 3),
      momentum_days: parseInt(document.getElementById('ob-mom-days')?.value || 10),
      is_active: true,
    });
    await loadProfiles();  // sync S.profiles + S.activeProfile from server
  } catch (e) {
    // RSI validation error or server error — surface it and abort launch
    showToast(e.message || 'Failed to save profile.', 'error');
    return;
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
    if (document.getElementById('tab-portfolio').classList.contains('active')) {
      renderPortfolio();
    }
    // Fire extended details async — re-renders cards when done
    fetchTickerDetails();
  } catch (e) { showToast('Failed to fetch quotes. Is Flask running?', 'error'); }
}

async function fetchTickerDetails() {
  if (S.watchlist.length === 0) return;
  try {
    const data = await api.post('/api/ticker-details', { tickers: S.watchlist.map(t => t.symbol) });
    S.tickerDetails = data;
    if (document.getElementById('tab-dashboard').classList.contains('active')) {
      renderDashboard();
    }
  } catch (e) {}
}

// ── Market helpers (BUG-028: always use US/Eastern, regardless of browser locale) ──
function _etTime() {
  // Parse current time in US Eastern so market-hours checks are correct worldwide
  const etStr = new Date().toLocaleString('en-US', { timeZone: 'America/New_York' });
  const et = new Date(etStr);
  return { day: et.getDay(), h: et.getHours() + et.getMinutes() / 60 };
}
function isMarketOpen()  { const { day, h } = _etTime(); return day >= 1 && day <= 5 && h >= 9.5 && h < 16; }
function isPreMarket()   { const { day, h } = _etTime(); return day >= 1 && day <= 5 && h >= 4   && h < 9.5; }
function isAfterHours()  { const { day, h } = _etTime(); return day >= 1 && day <= 5 && h >= 16  && h < 20; }

function renderDashboard() {
  const el = document.getElementById('dashboard-content');
  if (S.watchlist.length === 0) {
    el.innerHTML = `<div class="empty-state"><h3>No tickers</h3><p>Add ticker symbols using the input above.</p></div>`;
    return;
  }
  S.preferences.density === 'expanded' ? renderExpanded(el) : renderCompact(el);
}

function renderCompact(el) {
  const mktOpen = isMarketOpen();
  const showPre  = isPreMarket();
  const showPost = isAfterHours();

  const cards = S.watchlist.map(t => {
    const q   = S.quotes[t.symbol];
    const ext = S.tickerDetails[t.symbol] || {};
    const sug = S.suggestions.find(s => s.symbol === t.symbol);
    if (!q || q.error) return `<div class="stock-card"><div class="card-sym">${esc(t.symbol)}</div><div style="color:var(--red);font-size:11px">${esc((q?.error || 'Loading...').slice(0, 50))}</div><button class="card-remove" onclick="removeTicker('${esc(t.symbol)}')">✕</button></div>`;
    const dir = q.pct_change >= 0 ? 'up' : 'down';
    const alertOn = S.triggeredAlerts.has(t.symbol);

    // ── Extended-hours price ──────────────────────────────────────
    let extPriceHtml = '';
    if (!mktOpen && showPre && ext.pre_price) {
      const pc = ext.pre_pct;
      extPriceHtml = `<div class="ext-price">Pre-Market: <strong>$${ext.pre_price.toFixed(2)}</strong> <span class="${pc >= 0 ? 'up' : 'down'}">${pc >= 0 ? '+' : ''}${pc?.toFixed(2)}%</span></div>`;
    } else if (!mktOpen && showPost && ext.post_price) {
      const pc = ext.post_pct;
      extPriceHtml = `<div class="ext-price">After-Hours: <strong>$${ext.post_price.toFixed(2)}</strong> <span class="${pc >= 0 ? 'up' : 'down'}">${pc >= 0 ? '+' : ''}${pc?.toFixed(2)}%</span></div>`;
    }

    // ── 52W range bar ─────────────────────────────────────────────
    let w52Html = '';
    if (ext.high_52w && ext.low_52w && ext.high_52w > ext.low_52w) {
      const pct52 = Math.max(0, Math.min(100,
        ((q.price - ext.low_52w) / (ext.high_52w - ext.low_52w)) * 100
      ));
      w52Html = `
        <div class="w52-wrap">
          <div class="w52-labels">
            <span class="w52-end">52W Low $${ext.low_52w.toFixed(2)}</span>
            <span class="w52-pct">${pct52.toFixed(0)}% of range</span>
            <span class="w52-end">$${ext.high_52w.toFixed(2)} 52W High</span>
          </div>
          <div class="w52-track"><div class="w52-fill" style="width:${pct52.toFixed(1)}%"></div></div>
        </div>`;
    }

    // ── Earnings badge ────────────────────────────────────────────
    let earnBadge = '';
    if (ext.days_to_earn != null && ext.days_to_earn >= 0 && ext.days_to_earn <= 30) {
      const d = ext.days_to_earn;
      const label = d === 0 ? 'Earnings today' : d === 1 ? 'Earnings tomorrow' : `Earnings in ${d}d`;
      const cls   = d <= 2 ? 'earn-red' : 'earn-yellow';
      earnBadge   = `<span class="earn-badge ${cls}">📅 ${label}</span>`;
    }

    return `
      <div class="stock-card ${alertOn ? 'alert-triggered' : ''}">
        <div class="card-accent-bar ${q.above_ma50 ? 'bullish' : 'bearish'}"></div>
        <button class="card-edit" onclick="openTickerDetail('${t.symbol}')">✎</button>
        <button class="card-remove" onclick="removeTicker('${t.symbol}')">✕</button>
        <button class="card-chart-btn" onclick="openChart('${t.symbol}')">📈 CHART</button>
        <div class="card-top">
          <div>
            <div class="card-sym" style="cursor:pointer" onclick="openChart('${t.symbol}')" title="Open chart">${t.symbol}</div>
            <div class="card-meta">
              <span class="card-name">${esc(t.name)}</span>
              <span class="tier-badge ${tierClass(t.tier)}">${esc(t.tier)}</span>
            </div>
          </div>
          <div class="card-price-block">
            <div class="card-price ${dir}">$${q.price.toFixed(2)}</div>
            <div class="card-change ${dir}">${q.change >= 0 ? '+' : ''}$${q.change.toFixed(2)} (${q.pct_change >= 0 ? '+' : ''}${q.pct_change.toFixed(2)}%)</div>
            ${extPriceHtml}
          </div>
        </div>
        <div class="card-row">
          <div class="card-stat"><div class="stat-label">VOLUME</div><div class="stat-val">${q.volume}</div></div>
          <div class="card-stat"><div class="stat-label">RSI</div><div class="stat-val ${q.rsi < 30 ? 'bullish' : q.rsi > 70 ? 'bearish' : ''}">${q.rsi ? q.rsi.toFixed(0) : '—'}</div></div>
          <div class="card-stat"><div class="stat-label">MA20</div><div class="stat-val ${q.above_ma20 ? 'bullish' : 'bearish'}">${q.ma20 ? '$' + q.ma20.toFixed(2) : '—'}</div></div>
          <div class="card-stat"><div class="stat-label">MA50</div><div class="stat-val ${q.above_ma50 ? 'bullish' : 'bearish'}">${q.ma50 ? '$' + q.ma50.toFixed(2) : '—'}</div></div>
        </div>
        ${q.rvol != null ? `<div class="card-rvol-row"><span class="rvol-badge rvol-${(q.rvol_tier||'normal').toLowerCase().replace(' ','-')}">RVOL ${q.rvol.toFixed(2)}× ${q.rvol_tier||''}</span></div>` : ''}
        ${w52Html}
        <div class="card-bottom">
          <div class="card-signals">
            ${q.ma20 ? `<span class="signal-badge ${q.above_ma20 ? 'signal-bull' : 'signal-bear'}">${q.above_ma20 ? '▲' : '▼'} MA20</span>` : ''}
            ${q.ma50 ? `<span class="signal-badge ${q.above_ma50 ? 'signal-bull' : 'signal-bear'}">${q.above_ma50 ? '▲' : '▼'} MA50</span>` : ''}
            ${earnBadge}
            ${t.notes ? `<span class="signal-badge signal-neutral" title="${esc(t.notes)}">📝</span>` : ''}
          </div>
          ${sug ? `<span class="suggestion-badge ${sug.signal === 'BUY' ? 'sug-buy' : sug.signal === 'SELL' ? 'sug-sell' : 'sug-hold'}">${sug.signal === 'BUY' ? '🟢' : sug.signal === 'SELL' ? '🔴' : '🟡'} ${sug.signal} ${sug.confidence}%</span>` : ''}
        </div>
      </div>`;
  }).join('');
  el.innerHTML = `<div class="compact-grid">${cards}</div>`;
}

function renderExpanded(el) {
  const rows = S.watchlist.map(t => {
    const q   = S.quotes[t.symbol];
    const ext = S.tickerDetails[t.symbol] || {};
    const sug = S.suggestions.find(s => s.symbol === t.symbol);
    if (!q || q.error) return `<tr><td class="td-accent"></td><td><div class="td-sym">${esc(t.symbol)}</div></td><td colspan="8" style="color:var(--red)">${esc((q?.error || 'Loading...').slice(0, 50))}</td><td><button onclick="removeTicker('${esc(t.symbol)}')" style="background:none;border:none;color:var(--red);cursor:pointer;font-family:var(--font)">✕</button></td></tr>`;
    const dir = q.pct_change >= 0 ? 'up' : 'down';

    // Short interest badge
    let siBadge = '';
    if (ext.short_interest != null) {
      const siPct = (ext.short_interest * 100).toFixed(1);
      if (ext.short_interest >= 0.30) {
        siBadge = `<span class="si-badge si-squeeze" title="Short Interest: ${siPct}% of float">🔥 Squeeze Watch</span>`;
      } else if (ext.short_interest >= 0.15) {
        siBadge = `<span class="si-badge si-high" title="Short Interest: ${siPct}% of float">▲ High Short</span>`;
      } else {
        siBadge = `<span class="si-normal" title="Short Interest: ${siPct}% of float">${siPct}% short</span>`;
      }
    }

    // Earnings badge (compact for table)
    let earnCell = '—';
    if (ext.days_to_earn != null && ext.days_to_earn >= 0 && ext.days_to_earn <= 30) {
      const d = ext.days_to_earn;
      const label = d === 0 ? 'Today' : d === 1 ? 'Tomorrow' : `${d}d`;
      const cls   = d <= 2 ? 'earn-red' : 'earn-yellow';
      earnCell = `<span class="earn-badge ${cls}">📅 ${label}</span>`;
    }

    return `
      <tr class="${S.triggeredAlerts.has(t.symbol) ? 'alert-triggered' : ''}">
        <td class="td-accent"><div class="td-accent-inner" style="background:${q.above_ma50 ? 'var(--green)' : 'var(--red)'}"></div></td>
        <td>
          <div class="td-sym" style="cursor:pointer" onclick="openChart('${t.symbol}')" title="Open chart">${t.symbol} <span class="tier-badge ${tierClass(t.tier)}" style="font-size:9px;padding:1px 4px">${t.tier}</span></div>
          <div class="td-name">${esc(t.name)}</div>
        </td>
        <td class="${dir}" style="font-weight:700;font-size:14px">$${q.price.toFixed(2)}</td>
        <td class="${dir}">${q.change >= 0 ? '+' : ''}$${q.change.toFixed(2)}</td>
        <td class="${dir}">${q.pct_change >= 0 ? '+' : ''}${q.pct_change.toFixed(2)}%</td>
        <td style="color:var(--text2)">${q.volume}</td>
        <td>${q.rvol != null ? `<span class="rvol-badge rvol-${(q.rvol_tier||'normal').toLowerCase().replace(' ','-')}">${q.rvol.toFixed(2)}×</span>` : '—'}</td>
        <td style="color:${q.rsi < 30 ? 'var(--green)' : q.rsi > 70 ? 'var(--red)' : 'var(--text2)'}">${q.rsi ? q.rsi.toFixed(0) : '—'}</td>
        <td class="${q.above_ma20 ? 'up' : 'down'}">${q.ma20 ? '$' + q.ma20.toFixed(2) : '—'}</td>
        <td class="${q.above_ma50 ? 'up' : 'down'}">${q.ma50 ? '$' + q.ma50.toFixed(2) : '—'}</td>
        <td>${earnCell}</td>
        <td>
          <div class="td-signals">
            ${sug ? `<span class="suggestion-badge ${sug.signal === 'BUY' ? 'sug-buy' : sug.signal === 'SELL' ? 'sug-sell' : 'sug-hold'}">${sug.signal} ${sug.confidence}%</span>` : ''}
            ${q.ma50 ? `<span class="signal-badge ${q.above_ma50 ? 'signal-bull' : 'signal-bear'}">${q.above_ma50 ? '▲' : '▼'} MA50</span>` : ''}
            ${siBadge}
          </div>
        </td>
        <td>
          <button class="td-edit" onclick="openTickerDetail('${t.symbol}')">✎</button>
          <button class="td-remove" onclick="removeTicker('${t.symbol}')">✕</button>
        </td>
      </tr>`;
  }).join('');
  el.innerHTML = `<table class="expanded-table"><thead><tr><th></th><th>TICKER</th><th>PRICE</th><th>CHG</th><th>% CHG</th><th>VOLUME</th><th>RVOL</th><th>RSI</th><th>MA20</th><th>MA50</th><th>EARN</th><th>SIGNALS</th><th></th></tr></thead><tbody>${rows}</tbody></table>`;
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

  if (name === 'dashboard')   fetchMarketSummary();
  if (name === 'signals')     renderSignalsTab();
  if (name === 'heatmap')     renderHeatmapTab();
  if (name === 'correlation') renderCorrelationTab();
  if (name === 'portfolio')   { loadPortfolio().then(renderPortfolio); }
  if (name === 'journal')     { loadTrades().then(renderJournal); }
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
          <div style="display:flex;gap:6px;flex-wrap:wrap">
            <button class="log-trade-btn" onclick="openChart('${s.symbol}')">📈 Chart</button>
            <button class="log-trade-btn" onclick="openLogTradeFromSignal('${s.symbol}', '${s.signal}', ${s.confidence})">
              ${s.signal === 'BUY' ? '+ Log Buy' : s.signal === 'SELL' ? '+ Log Sell' : '+ Log Trade'}
            </button>
          </div>
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
        <td class="pos-notes" title="${esc(pos.notes)}">${esc(pos.notes) || '—'}</td>
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
        <td style="color:var(--text3);font-size:11px">${esc(t.notes) || ''}</td>
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

let _filterJournalTimer = null;
function filterJournal() {
  // BUG-017: debounce so keystroke-by-keystroke typing doesn't fire a query per character
  clearTimeout(_filterJournalTimer);
  _filterJournalTimer = setTimeout(() => {
    const sym    = document.getElementById('journal-sym-filter').value.toUpperCase();
    const action = document.getElementById('journal-action-filter').value;
    const start  = document.getElementById('journal-start').value;
    const end    = document.getElementById('journal-end').value;
    const filters = {};
    if (sym)    filters.symbol = sym;
    if (action) filters.action = action;
    if (start)  filters.start  = start;
    if (end)    filters.end    = end;
    loadTrades(filters).then(renderJournal);
  }, 275);
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
        <div class="profile-item-name">${esc(p.name)} ${p.is_active ? '<span class="profile-active-badge">ACTIVE</span>' : ''}</div>
        <div class="profile-item-meta">${esc(p.risk_tolerance)} · ${esc(p.horizon)} · Max ${p.max_trades_per_day} trades/day</div>
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
        <option value="below" ${t.alert_direction === 'below' ? 'selected' : ''}>BELOW</option>
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
// STRATEGY PRESETS
// ════════════════════════════════════════════════════════════════
async function loadStrategyPresets() {
  try {
    const data = await api.get('/api/strategies/presets');
    S.strategyPresets = data.strategies || [];
    _populatePresetDropdown('ob-preset-select', S.strategyPresets);
    _populatePresetDropdown('pe-preset-select', S.strategyPresets);
  } catch (e) {
    console.warn('Could not load strategy presets:', e);
  }
}

function _populatePresetDropdown(selectId, presets) {
  const sel = document.getElementById(selectId);
  if (!sel) return;
  // Keep only the first "— Custom / Manual —" option
  while (sel.options.length > 1) sel.remove(1);
  const types = ['Trend', 'Mean Reversion', 'Calendar', 'TAA', 'Volatility'];
  const byType = {};
  presets.forEach(p => { (byType[p.type] = byType[p.type] || []).push(p); });
  types.forEach(t => {
    if (!byType[t]?.length) return;
    const og = document.createElement('optgroup');
    og.label = t;
    byType[t].forEach(p => {
      const opt = document.createElement('option');
      opt.value = p.name;
      opt.textContent = p.name;
      og.appendChild(opt);
    });
    sel.appendChild(og);
  });
}

const PRESET_TYPE_COLORS = {
  'Trend':          '#22c55e',
  'Mean Reversion': '#3b82f6',
  'Calendar':       '#eab308',
  'TAA':            '#a855f7',
  'Volatility':     '#f97316',
};
const PRESET_TYPE_SLUG = {
  'Trend':          'trend',
  'Mean Reversion': 'mean-reversion',
  'Calendar':       'calendar',
  'TAA':            'taa',
  'Volatility':     'volatility',
};

// ── Quiz data ────────────────────────────────────────────────────
const QUIZ_QUESTIONS = [
  {
    q: 'How often do you want to trade?',
    hint: 'Think about how many buy or sell decisions you want to make per month.',
    options: [
      { id: 'a', text: 'Daily or several times per week' },
      { id: 'b', text: 'Every 1–2 weeks' },
      { id: 'c', text: 'Once a month or less' },
      { id: 'd', text: 'Set it and forget it (quarterly rebalance)' },
    ],
  },
  {
    q: "What's your comfort level with losses?",
    hint: 'A "drawdown" is a temporary drop from a portfolio\'s peak — it always happens in investing.',
    options: [
      { id: 'a', text: 'I can handle 30%+ drops — bigger risk, bigger reward' },
      { id: 'b', text: "I'm OK with 15–25% dips" },
      { id: 'c', text: 'I prefer small, steady gains with less than 15% max loss' },
      { id: 'd', text: 'I want near-bond-like stability, minimal volatility' },
    ],
  },
  {
    q: 'What trading style appeals to you most?',
    hint: "There's no right answer — just pick what excites you.",
    options: [
      { id: 'a', text: 'Riding big, multi-month uptrends' },
      { id: 'b', text: 'Buying dips and selling bounces (mean reversion)' },
      { id: 'c', text: 'Exploiting predictable patterns like earnings or month-end effects' },
      { id: 'd', text: 'Diversifying across assets and letting math manage risk' },
    ],
  },
  {
    q: 'How much time can you dedicate to monitoring your trades?',
    hint: "Be honest — many strategies fail because traders can't follow their own rules.",
    options: [
      { id: 'a', text: 'I check the market every day' },
      { id: 'b', text: 'A few times per week' },
      { id: 'c', text: 'About once a week' },
      { id: 'd', text: 'Monthly or less' },
    ],
  },
  {
    q: 'How familiar are you with technical indicators?',
    hint: 'Examples include RSI, moving averages, ATR, and Bollinger Bands.',
    options: [
      { id: 'a', text: "Expert — I'm comfortable reading charts and indicators" },
      { id: 'b', text: 'Intermediate — I understand the basics' },
      { id: 'c', text: 'Beginner — not much, keep it simple' },
      { id: 'd', text: "None — I just want clear buy/sell signals" },
    ],
  },
];

// Scoring: QUIZ_SCORING[questionIndex][answerId] = { strategyName: points }
const QUIZ_SCORING = [
  // Q1 — trading frequency
  {
    a: { 'RSI Mean Reversion': 3, 'High Frequency Scalp': 3, 'Oversold Bounce': 2, 'Volatility Filter': 2, 'Earnings Season': 1 },
    b: { 'Momentum Surfer': 3, 'Swing Trader': 3, 'Volume Breakout': 2, 'Golden Cross Hunter': 2, 'Trend Following': 1 },
    c: { 'Sector Rotation': 3, 'Conservative Long-Term': 3, 'Tactical Asset Allocation': 2, 'Golden Cross Hunter': 1 },
    d: { 'Tactical Asset Allocation': 3, 'Conservative Long-Term': 3, 'Classic Balanced': 2, 'Sector Rotation': 1 },
  },
  // Q2 — loss comfort
  {
    a: { 'High Frequency Scalp': 3, 'Volume Breakout': 2, 'Volatility Filter': 2, 'Momentum Surfer': 2, 'Earnings Season': 1 },
    b: { 'Momentum Surfer': 3, 'Swing Trader': 3, 'Trend Following': 2, 'Golden Cross Hunter': 2, 'Volume Breakout': 1 },
    c: { 'Oversold Bounce': 3, 'RSI Mean Reversion': 3, 'Earnings Season': 2, 'Classic Balanced': 1, 'Tactical Asset Allocation': 1 },
    d: { 'Conservative Long-Term': 3, 'Tactical Asset Allocation': 3, 'Sector Rotation': 1, 'Classic Balanced': 1 },
  },
  // Q3 — trading style
  {
    a: { 'Volume Breakout': 3, 'Momentum Surfer': 3, 'Trend Following': 3, 'Golden Cross Hunter': 2, 'Swing Trader': 1 },
    b: { 'RSI Mean Reversion': 3, 'Oversold Bounce': 3, 'High Frequency Scalp': 2, 'Volatility Filter': 1 },
    c: { 'Earnings Season': 3, 'Sector Rotation': 3, 'Classic Balanced': 1 },
    d: { 'Conservative Long-Term': 3, 'Tactical Asset Allocation': 3, 'Sector Rotation': 2 },
  },
  // Q4 — time commitment
  {
    a: { 'RSI Mean Reversion': 3, 'High Frequency Scalp': 3, 'Volatility Filter': 2, 'Oversold Bounce': 2, 'Earnings Season': 1 },
    b: { 'Momentum Surfer': 2, 'Swing Trader': 2, 'Golden Cross Hunter': 2, 'Volume Breakout': 2, 'Trend Following': 1 },
    c: { 'Classic Balanced': 3, 'Swing Trader': 1, 'Trend Following': 1, 'Golden Cross Hunter': 1 },
    d: { 'Sector Rotation': 3, 'Conservative Long-Term': 3, 'Tactical Asset Allocation': 3, 'Classic Balanced': 1 },
  },
  // Q5 — technical knowledge
  {
    a: { 'RSI Mean Reversion': 2, 'High Frequency Scalp': 2, 'Volatility Filter': 2, 'Golden Cross Hunter': 1, 'Earnings Season': 1 },
    b: { 'Momentum Surfer': 1, 'Swing Trader': 1, 'Trend Following': 1, 'Volume Breakout': 1, 'Oversold Bounce': 1 },
    c: { 'Classic Balanced': 2, 'Sector Rotation': 1, 'Oversold Bounce': 1, 'Conservative Long-Term': 1, 'Tactical Asset Allocation': 1 },
    d: { 'Conservative Long-Term': 2, 'Tactical Asset Allocation': 2, 'Classic Balanced': 2, 'Sector Rotation': 1 },
  },
];

// Pre-computed theoretical max score for each strategy (sum of max per question)
const QUIZ_MAX_SCORES = {
  'Classic Balanced':        9,
  'Golden Cross Hunter':     9,
  'Swing Trader':           10,
  'Trend Following':         8,
  'Momentum Surfer':        12,
  'Volume Breakout':        10,
  'RSI Mean Reversion':     14,
  'Oversold Bounce':        11,
  'Earnings Season':         8,
  'Sector Rotation':        11,
  'Conservative Long-Term': 14,
  'Tactical Asset Allocation': 14,
  'High Frequency Scalp':   13,
  'Volatility Filter':       9,
};

// Module-level quiz state (UI only — not user data)
let _quizStep = 0;
let _quizContext = 'ob';
let _quizKeyBound = false;

function openStrategyQuiz(ctx) {
  _quizContext = ctx || 'ob';
  _quizStep = 0;
  S.quizAnswers = [null, null, null, null, null];
  _renderQuizQuestion(0);
  openModal('strategy-quiz-modal');
  if (!_quizKeyBound) {
    document.addEventListener('keydown', _quizKeyHandler);
    _quizKeyBound = true;
  }
}

function closeStrategyQuiz() {
  closeModal('strategy-quiz-modal');
  document.removeEventListener('keydown', _quizKeyHandler);
  _quizKeyBound = false;
}

function _quizKeyHandler(e) {
  if (!document.getElementById('strategy-quiz-modal')?.classList.contains('open')) return;
  if (_quizStep >= QUIZ_QUESTIONS.length) return;   // on results screen — no keyboard nav

  const opts = [...document.querySelectorAll('.quiz-option input[type="radio"]')];
  const currentIdx = opts.findIndex(r => r.checked);

  if (e.key === 'ArrowDown' || e.key === 'ArrowRight') {
    e.preventDefault();
    const next = opts[Math.min(currentIdx + 1, opts.length - 1)];
    if (next) { next.click(); next.closest('.quiz-option').classList.add('selected'); }
  } else if (e.key === 'ArrowUp' || e.key === 'ArrowLeft') {
    e.preventDefault();
    const prev = opts[Math.max(currentIdx - 1, 0)];
    if (prev) { prev.click(); prev.closest('.quiz-option').classList.add('selected'); }
  } else if (e.key === 'Enter') {
    e.preventDefault();
    nextQuizQuestion();
  }
}

function _renderQuizQuestion(n) {
  const total = QUIZ_QUESTIONS.length;
  const q = QUIZ_QUESTIONS[n];
  const pct = Math.round((n / total) * 100);
  const saved = S.quizAnswers[n];
  document.getElementById('quiz-body').innerHTML = `
    <div>
      <div class="quiz-progress-label">QUESTION ${n + 1} OF ${total}</div>
      <div class="quiz-progress-track"><div class="quiz-progress-fill" style="width:${pct}%"></div></div>
    </div>
    <div>
      <p class="quiz-q-text">${esc(q.q)}</p>
      ${q.hint ? `<p class="quiz-q-hint">${esc(q.hint)}</p>` : ''}
    </div>
    <div class="quiz-options">
      ${q.options.map(opt => `
        <label class="quiz-option${saved === opt.id ? ' selected' : ''}">
          <input type="radio" name="quiz-q${n}" value="${opt.id}" ${saved === opt.id ? 'checked' : ''}
            onchange="S.quizAnswers[${n}]='${opt.id}';document.querySelectorAll('.quiz-option').forEach(el=>el.classList.toggle('selected',el.querySelector('input').checked))">
          <div class="quiz-option-inner">
            <div class="quiz-option-dot"></div>
            <span class="quiz-option-text">${esc(opt.text)}</span>
          </div>
        </label>`).join('')}
    </div>
    <div class="quiz-nav">
      ${n > 0 ? `<button class="btn-ghost" onclick="prevQuizQuestion()">← BACK</button>` : '<div></div>'}
      <button class="btn-primary" onclick="nextQuizQuestion()">
        ${n < total - 1 ? 'NEXT →' : 'SHOW RESULTS →'}
      </button>
    </div>
  `;
}

function nextQuizQuestion() {
  const n = _quizStep;
  const selected = document.querySelector(`[name="quiz-q${n}"]:checked`);
  if (!selected) { showToast('Please select an answer to continue.', 'error'); return; }
  S.quizAnswers[n] = selected.value;
  if (n < QUIZ_QUESTIONS.length - 1) {
    _quizStep = n + 1;
    _renderQuizQuestion(_quizStep);
  } else {
    _quizStep = QUIZ_QUESTIONS.length;  // signals results screen
    _renderQuizResults();
  }
}

function prevQuizQuestion() {
  if (_quizStep > 0) { _quizStep--; _renderQuizQuestion(_quizStep); }
}

function calculateQuizResults() {
  const scores = {};
  S.strategyPresets.forEach(p => { scores[p.name] = 0; });
  S.quizAnswers.forEach((answer, qIdx) => {
    if (!answer || !QUIZ_SCORING[qIdx]) return;
    Object.entries(QUIZ_SCORING[qIdx][answer] || {}).forEach(([name, pts]) => {
      if (scores[name] !== undefined) scores[name] += pts;
    });
  });
  return Object.entries(scores)
    .map(([name, score]) => {
      const maxS = QUIZ_MAX_SCORES[name] || 1;
      const matchPct = Math.min(99, Math.round(score / maxS * 100));
      return { name, score, matchPct };
    })
    .sort((a, b) => b.matchPct - a.matchPct || b.score - a.score);
}

function _renderQuizResults() {
  if (!S.strategyPresets.length) {
    document.getElementById('quiz-body').innerHTML =
      '<p style="padding:20px;color:var(--text2)">Strategies could not be loaded. Please refresh and try again.</p>';
    return;
  }
  const top3 = calculateQuizResults().slice(0, 3);
  document.getElementById('quiz-body').innerHTML = `
    <div class="quiz-results-header">
      <p class="quiz-results-subtitle">Based on your answers, here are your best strategy matches.</p>
    </div>
    <div class="quiz-results">
      ${top3.map((r, i) => {
        const preset  = S.strategyPresets.find(p => p.name === r.name);
        const pctColor = r.matchPct >= 85 ? 'var(--green)' : r.matchPct >= 65 ? 'var(--yellow)' : 'var(--text2)';
        const typeColor = PRESET_TYPE_COLORS[preset?.type] || '#888';
        return `
          <div class="result-card ${i === 0 ? 'result-card-top' : ''}">
            <div class="result-card-header">
              <span class="result-match-pct" style="color:${pctColor}">${r.matchPct}% match</span>
              <span class="pic-type-badge" style="background:${typeColor}22;color:${typeColor};border-color:${typeColor}55">${esc(preset?.type || '')}</span>
              <span class="pic-meta">⚙ ${esc(preset?.complexity || '')}</span>
            </div>
            <div class="result-name">${esc(r.name)}</div>
            <p class="result-desc">${esc(preset?.description || '')}</p>
            <button class="btn-primary result-btn"
              data-strategy-name="${esc(r.name)}"
              onclick="loadQuizStrategy(this.getAttribute('data-strategy-name'))">
              Load This Strategy
            </button>
          </div>`;
      }).join('')}
    </div>
    <div class="quiz-results-footer">
      <button class="btn-ghost" onclick="openStrategyQuiz(_quizContext)">↺ Take Quiz Again</button>
      <button class="btn-ghost" onclick="closeStrategyQuiz()">CLOSE</button>
    </div>
  `;
}

function loadQuizStrategy(name) {
  const ctx = _quizContext;
  closeStrategyQuiz();
  if (ctx === 'pe') {
    // Open the profile editor for a new profile, then apply the preset
    openProfileEditor(null);
    setTimeout(() => {
      const sel = document.getElementById('pe-preset-select');
      if (sel) sel.value = name;
      loadStrategyPreset(name, 'pe');
    }, 50);
  } else {
    // Onboarding — set dropdown and apply
    const sel = document.getElementById('ob-preset-select');
    if (sel) sel.value = name;
    loadStrategyPreset(name, 'ob');
  }
  showToast(`"${name}" preset loaded.`, 'success');
}

function loadStrategyPreset(name, ctx) {
  const infoId = ctx === 'ob' ? 'ob-preset-info' : 'pe-preset-info';
  const custId = ctx === 'ob' ? 'ob-preset-customize' : 'pe-preset-customize';
  const infoEl = document.getElementById(infoId);
  const custEl = document.getElementById(custId);

  if (!name) {
    // User picked "Custom / Manual" — unlock sliders, hide card
    _setPresetLock(ctx, false);
    if (infoEl) infoEl.style.display = 'none';
    if (custEl) custEl.style.display = 'none';
    return;
  }

  const preset = S.strategyPresets.find(p => p.name === name);
  if (!preset) return;

  // ── Populate all fields ──────────────────────────────────────
  if (ctx === 'ob') {
    _setSliderPct('w-ma',  'w-ma-val',  preset.ma_weight);
    _setSliderPct('w-vol', 'w-vol-val', preset.volume_weight);
    _setSliderPct('w-rsi', 'w-rsi-val', preset.rsi_weight);
    _setSliderPct('w-mom', 'w-mom-val', preset.momentum_weight);
    document.querySelectorAll('[name="ob-risk"]').forEach(r => { r.checked = r.value === preset.risk_tolerance; });
    document.querySelectorAll('[name="ob-horizon"]').forEach(r => { r.checked = r.value === preset.horizon; });
    _setInputVal('ob-rsi-ob',     preset.rsi_overbought);
    _setInputVal('ob-rsi-os',     preset.rsi_oversold);
    _setInputVal('ob-max-trades', preset.max_trades_per_day);
    _setInputVal('ob-vol-thresh', preset.volume_spike_threshold);
    _setInputVal('ob-mom-days',   preset.momentum_days);
  } else {
    _setSliderPct('pe-w-ma',  'pe-w-ma-v',  preset.ma_weight);
    _setSliderPct('pe-w-vol', 'pe-w-vol-v', preset.volume_weight);
    _setSliderPct('pe-w-rsi', 'pe-w-rsi-v', preset.rsi_weight);
    _setSliderPct('pe-w-mom', 'pe-w-mom-v', preset.momentum_weight);
    document.querySelectorAll('[name="pe-risk"]').forEach(r => { r.checked = r.value === preset.risk_tolerance; });
    document.querySelectorAll('[name="pe-horizon"]').forEach(r => { r.checked = r.value === preset.horizon; });
    _setInputVal('pe-rsi-ob',     preset.rsi_overbought);
    _setInputVal('pe-rsi-os',     preset.rsi_oversold);
    _setInputVal('pe-max-trades', preset.max_trades_per_day);
    _setInputVal('pe-vol-thresh', preset.volume_spike_threshold);
    _setInputVal('pe-mom-days',   preset.momentum_days);
  }

  // ── Info card ────────────────────────────────────────────────
  _renderPresetInfoCard(preset, infoId);

  // ── Lock sliders ─────────────────────────────────────────────
  _setPresetLock(ctx, true);
  if (custEl) custEl.style.display = 'flex';
}

function _setSliderPct(sliderId, labelId, frac) {
  const pct = Math.round(frac * 100);
  const sl = document.getElementById(sliderId);
  const lb = document.getElementById(labelId);
  if (sl) sl.value = pct;
  if (lb) lb.textContent = pct + '%';
}

function _setInputVal(id, val) {
  const el = document.getElementById(id);
  if (el) el.value = val;
}

function _renderPresetInfoCard(preset, cardId) {
  const el = document.getElementById(cardId);
  if (!el) return;
  const color = PRESET_TYPE_COLORS[preset.type] || '#888';
  const slug  = PRESET_TYPE_SLUG[preset.type] || 'trend';
  el.className = `preset-info-card preset-type-${slug}`;
  el.innerHTML = `
    <div class="pic-header">
      <span class="pic-type-badge" style="background:${color}22;color:${color};border-color:${color}55">${esc(preset.type)}</span>
      <span class="pic-meta">⚙ ${esc(preset.complexity)}</span>
      <span class="pic-meta">↗ ${esc(preset.expected_return)}</span>
    </div>
    <p class="pic-desc">${esc(preset.description)}</p>
    <div class="pic-weights">
      <span>MA <strong>${Math.round(preset.ma_weight * 100)}%</strong></span>
      <span>Vol <strong>${Math.round(preset.volume_weight * 100)}%</strong></span>
      <span>RSI <strong>${Math.round(preset.rsi_weight * 100)}%</strong></span>
      <span>Mom <strong>${Math.round(preset.momentum_weight * 100)}%</strong></span>
    </div>
  `;
  el.style.display = 'block';
}

function customizePreset(ctx) {
  const selectId = ctx === 'ob' ? 'ob-preset-select' : 'pe-preset-select';
  const custId   = ctx === 'ob' ? 'ob-preset-customize' : 'pe-preset-customize';
  const infoId   = ctx === 'ob' ? 'ob-preset-info' : 'pe-preset-info';
  const sel = document.getElementById(selectId);
  if (sel) sel.value = '';
  _setPresetLock(ctx, false);
  const custEl = document.getElementById(custId);
  if (custEl) custEl.style.display = 'none';
  const infoEl = document.getElementById(infoId);
  if (infoEl) infoEl.style.display = 'none';
}

function _setPresetLock(ctx, locked) {
  S.presetLocked[ctx] = locked;
  const containerId = ctx === 'ob' ? 'ob-sliders' : 'pe-sliders';
  const container = document.getElementById(containerId);
  if (!container) return;
  container.classList.toggle('preset-locked', locked);
  container.querySelectorAll('input[type="range"]').forEach(sl => { sl.disabled = locked; });
}

// ════════════════════════════════════════════════════════════════
// PROFILE EDITOR
// ════════════════════════════════════════════════════════════════
function openProfileEditor(id) {
  const profile = id ? S.profiles.find(p => p.id === id) : null;
  document.getElementById('pe-id').value = id || '';
  document.getElementById('profile-modal-title').textContent = id ? 'EDIT PROFILE' : 'NEW PROFILE';
  // Reset preset picker state
  const peSel = document.getElementById('pe-preset-select');
  if (peSel) peSel.value = '';
  document.getElementById('pe-preset-info')?.style.setProperty('display', 'none');
  document.getElementById('pe-preset-customize')?.style.setProperty('display', 'none');
  _setPresetLock('pe', false);
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
  } catch (e) { showToast(e.message || 'Failed to save profile.', 'error'); }
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
  // BUG-034: treat 'default' as not-yet-asked; only show denied toast if actually denied
  if (Notification.permission === 'granted') {
    S.notifGranted = true;
    document.getElementById('notif-btn').classList.add('active');
    showToast('Notifications already enabled.', 'success');
    return;
  }
  const perm = await Notification.requestPermission();
  S.notifGranted = perm === 'granted';
  document.getElementById('notif-btn').classList.toggle('active', S.notifGranted);
  if (S.notifGranted) {
    showToast('Desktop notifications enabled.', 'success');
    new Notification('▣ PG Stock Analysis', { body: 'Price alerts are now active.' });
  } else if (perm === 'denied') {
    showToast('Notification permission denied — enable it in browser settings.', 'error');
  } else {
    showToast('Notification request dismissed.', 'error');
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
  // BUG-029: don't waste cycles refreshing while watchlist is empty
  if (S.watchlist.length === 0) return;
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
  const open = isMarketOpen();   // already uses US/Eastern via _etTime()
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
  // BUG-006: use window.location.origin so protocol and port are always correct
  const baseUrl = window.location.origin;

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
// ════════════════════════════════════════════════════════════════
// INTERACTIVE CHART
// ════════════════════════════════════════════════════════════════
let currentChartSymbol = null;
let currentChartRange  = '1Y';
let chartData          = null;

async function openChart(symbol) {
  currentChartSymbol = symbol;
  currentChartRange  = '1Y';

  document.getElementById('chart-modal-title').textContent = `${symbol} — CHART`;
  document.getElementById('chart-loading').style.display   = 'flex';
  document.getElementById('chart-loading').innerHTML =
    '<div class="loading-spinner"></div><span>Loading chart data...</span>';
  document.getElementById('chart-container').style.display = 'none';

  // Clear previous news
  const newsEl = document.getElementById('chart-news');
  if (newsEl) newsEl.innerHTML = '';

  // Default range buttons — reset to 1Y
  document.querySelectorAll('.chart-range-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.range === '1Y');
  });

  // Always open on chart tab; prepare options tab in background
  switchChartTab('chart');

  openModal('chart-modal');
  await loadChartData(symbol, '1Y');
  loadChartNews(symbol);      // fire-and-forget
  initOptions(symbol);        // fire-and-forget (pre-load expirations + summary)
}

async function loadChartData(symbol, range) {
  try {
    chartData = await api.get(`/api/chart/${symbol}?range=${range}`);
    if (chartData.error) throw new Error(chartData.error);
    document.getElementById('chart-loading').style.display   = 'none';
    document.getElementById('chart-container').style.display = 'block';
    renderPlotlyChart(chartData);
    renderFundamentals(chartData);
  } catch (e) {
    document.getElementById('chart-loading').innerHTML =
      `<span style="color:var(--red)">Failed to load chart: ${e.message}</span>`;
  }
}

async function setChartRange(range, btn) {
  currentChartRange = range;
  document.querySelectorAll('.chart-range-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById('chart-loading').style.display   = 'flex';
  document.getElementById('chart-loading').innerHTML =
    '<div class="loading-spinner"></div><span>Loading chart data...</span>';
  document.getElementById('chart-container').style.display = 'none';
  await loadChartData(currentChartSymbol, range);
}

function renderPlotlyChart(data) {
  const traces = [];

  // ── Candlestick ───────────────────────────────────────────────
  traces.push({
    type: 'candlestick',
    x: data.dates,
    open:  data.open,
    high:  data.high,
    low:   data.low,
    close: data.close,
    name:  data.symbol,
    increasing: { line: { color: '#00e676' }, fillcolor: 'rgba(0,230,118,0.7)' },
    decreasing: { line: { color: '#ff5252' }, fillcolor: 'rgba(255,82,82,0.7)' },
    showlegend: false,
    xaxis: 'x', yaxis: 'y',
    whiskerwidth: 0,
  });

  // ── MA overlays ───────────────────────────────────────────────
  const maConfigs = [
    { key: 'ma20',  name: 'MA20',  color: '#ff9f0a', checkId: 'ma20-toggle'  },
    { key: 'ma50',  name: 'MA50',  color: '#30d158', checkId: 'ma50-toggle'  },
    { key: 'ma200', name: 'MA200', color: '#0a84ff', checkId: 'ma200-toggle' },
  ];
  maConfigs.forEach(({ key, name, color, checkId }) => {
    if (!data[key]) return;
    const checked = document.getElementById(checkId)?.checked !== false;
    traces.push({
      type: 'scatter',
      mode: 'lines',
      x: data.dates,
      y: data[key],
      name,
      line: { color, width: 1.5 },
      xaxis: 'x', yaxis: 'y',
      visible: checked ? true : 'legendonly',
      connectgaps: false,
    });
  });

  // ── Volume bars ───────────────────────────────────────────────
  const avgVol = data.avg_vol30 || 1;
  const volColors = data.volume.map((v, i) => {
    const up   = i === 0 || data.close[i] >= data.close[i - 1];
    const high = v > avgVol;
    if (high) return up ? 'rgba(0,230,118,0.65)' : 'rgba(255,82,82,0.65)';
    return       up ? 'rgba(0,230,118,0.22)'  : 'rgba(255,82,82,0.22)';
  });
  traces.push({
    type: 'bar',
    x: data.dates,
    y: data.volume,
    name: 'Volume',
    marker: { color: volColors },
    xaxis: 'x', yaxis: 'y2',
    showlegend: false,
  });

  // ── MA50−MA200 Oscillator panel ───────────────────────────────
  const hasMa200 = data.ma200 && data.ma200.some(v => v !== null);
  if (hasMa200) {
    const osc = data.dates.map((_, i) => {
      const v50 = data.ma50[i], v200 = data.ma200[i];
      if (v50 == null || v200 == null) return null;
      return parseFloat((v50 - v200).toFixed(4));
    });
    const oscColors = osc.map((v, i) => {
      if (v == null) return 'rgba(0,0,0,0)';
      const prev = i > 0 ? osc[i - 1] : v;
      return (prev == null || v >= prev)
        ? 'rgba(0,230,118,0.60)'    // rising → converging (bullish)
        : 'rgba(255,82,82,0.60)';   // falling → diverging (bearish)
    });
    traces.push({
      type: 'bar',
      x: data.dates,
      y: osc,
      name: 'MA50−MA200',
      marker: { color: oscColors },
      xaxis: 'x', yaxis: 'y3',
      showlegend: false,
      hovertemplate: 'MA50−MA200: %{y:.2f}<extra></extra>',
    });
    // Zero crossover scatter markers
    const cxDates = [], cxVals = [];
    for (let i = 1; i < osc.length; i++) {
      if (osc[i - 1] != null && osc[i] != null) {
        if ((osc[i - 1] < 0 && osc[i] >= 0) || (osc[i - 1] > 0 && osc[i] <= 0)) {
          cxDates.push(data.dates[i]);
          cxVals.push(0);
        }
      }
    }
    if (cxDates.length > 0) {
      traces.push({
        type: 'scatter', mode: 'markers',
        x: cxDates, y: cxVals,
        name: 'Crossover',
        marker: { color: '#ffd740', size: 7, symbol: 'diamond' },
        xaxis: 'x', yaxis: 'y3',
        showlegend: false,
        hovertemplate: 'MA Crossover<extra></extra>',
      });
    }
  }

  // ── Crossover annotations ─────────────────────────────────────
  const plotAnnotations = (data.annotations || []).map(a => ({
    x: a.date, y: a.price,
    xref: 'x', yref: 'y',
    text: a.label,
    showarrow: true,
    arrowhead: 2,
    arrowsize: 0.8,
    arrowcolor: a.type === 'golden_cross' ? '#ffd740' : '#9898a8',
    font: { color: a.type === 'golden_cross' ? '#ffd740' : '#9898a8', size: 10,
            family: 'JetBrains Mono, monospace' },
    bgcolor: 'rgba(10,10,11,0.85)',
    bordercolor: a.type === 'golden_cross' ? '#ffd740' : '#9898a8',
    borderwidth: 1, borderpad: 3,
    ay: a.type === 'golden_cross' ? -36 : 36,
  }));

  const layout = {
    paper_bgcolor: '#0a0a0b',
    plot_bgcolor:  '#111114',
    font: { family: 'JetBrains Mono, monospace', color: '#9898a8', size: 10 },
    margin: { l: 10, r: 70, t: 8, b: 30 },
    dragmode: 'zoom',
    xaxis: {
      type: 'date',
      rangebreaks: [{ bounds: ['sat', 'mon'] }],
      rangeslider: { visible: false },
      rangeselector: {
        buttons: [
          { count: 1,  label: '1M', step: 'month', stepmode: 'backward' },
          { count: 3,  label: '3M', step: 'month', stepmode: 'backward' },
          { count: 6,  label: '6M', step: 'month', stepmode: 'backward' },
          { count: 1,  label: 'YTD', step: 'year', stepmode: 'todate'   },
          { count: 1,  label: '1Y', step: 'year',  stepmode: 'backward' },
          { step: 'all', label: 'All' },
        ],
        bgcolor:      '#18181d',
        activecolor:  '#00d4aa',
        bordercolor:  '#2a2a35',
        borderwidth:  1,
        font: { family: 'JetBrains Mono, monospace', color: '#9898a8', size: 10 },
        x: 0, xanchor: 'left', y: 1.04,
      },
      gridcolor: '#1e1e26',
      color: '#5a5a6e',
      showgrid: true,
    },
    yaxis: {
      domain: hasMa200 ? [0.38, 1.0] : [0.22, 1.0],
      gridcolor: '#1e1e26',
      color: '#5a5a6e',
      side: 'right',
      tickprefix: '$',
      showgrid: true,
    },
    yaxis2: {
      domain: hasMa200 ? [0.20, 0.35] : [0, 0.17],
      gridcolor: '#1e1e26',
      color: '#5a5a6e',
      side: 'right',
      showgrid: false,
    },
    yaxis3: {
      domain: [0, 0.17],
      gridcolor: '#1e1e26',
      color: '#5a5a6e',
      side: 'right',
      zeroline: true,
      zerolinecolor: '#363645',
      zerolinewidth: 1,
      showgrid: false,
      title: { text: 'OSC', font: { size: 9, color: '#5a5a6e' } },
    },
    legend: {
      x: 0.01, y: 0.97,
      bgcolor: 'rgba(10,10,11,0.75)',
      bordercolor: '#2a2a35',
      borderwidth: 1,
      font: { size: 10 },
    },
    annotations: plotAnnotations,
    hovermode: 'x unified',
    hoverlabel: { bgcolor: '#18181d', bordercolor: '#363645',
                  font: { family: 'JetBrains Mono, monospace', size: 11 } },
    selectdirection: 'h',
  };

  Plotly.newPlot('plotly-chart', traces, layout, {
    responsive: true,
    displayModeBar: false,
    scrollZoom: true,
  });
}

function toggleMA(maKey, checkbox) {
  if (!chartData) return;
  const nameMap = { ma20: 'MA20', ma50: 'MA50', ma200: 'MA200' };
  const name = nameMap[maKey];
  const div = document.getElementById('plotly-chart');
  if (!div || !div.data) return;
  const idx = div.data.findIndex(t => t.name === name);
  if (idx < 0) return;
  Plotly.restyle('plotly-chart', { visible: checkbox.checked ? true : 'legendonly' }, [idx]);
}

function resetChartZoom() {
  Plotly.relayout('plotly-chart', {
    'xaxis.autorange': true,
    'yaxis.autorange': true,
    'yaxis2.autorange': true,
    'yaxis3.autorange': true,
  });
}

function renderFundamentals(data) {
  const el = document.getElementById('fundamentals-card');
  if (!el) return;

  const f   = data.fundamentals || {};
  const cvg = data.convergence  || [];

  const fmtPct = v => v == null ? '—' : (v * 100).toFixed(1) + '%';
  const fmtPE  = v => v == null ? '—' : v.toFixed(1) + 'x';
  const fmtCap = v => {
    if (v == null) return '—';
    if (v >= 1e12) return '$' + (v / 1e12).toFixed(2) + 'T';
    if (v >= 1e9)  return '$' + (v / 1e9).toFixed(1)  + 'B';
    if (v >= 1e6)  return '$' + (v / 1e6).toFixed(1)  + 'M';
    return '$' + v.toLocaleString();
  };
  const colorCls = (v, invert = false) => {
    if (v == null) return '';
    return (invert ? v < 0 : v > 0) ? 'up' : 'down';
  };
  const passFail = (v, unknownText = '—') => {
    if (v === true)  return '<span class="fmp-pass">✓</span>';
    if (v === false) return '<span class="fmp-fail">✗</span>';
    return `<span style="color:var(--text3)">${unknownText}</span>`;
  };

  const isFmp = f.data_source === 'fmp';

  const stats = [
    { label: 'REVENUE GROWTH',  val: fmtPct(f.revenue_growth),  cls: colorCls(f.revenue_growth)  },
    { label: 'EARNINGS GROWTH', val: fmtPct(f.earnings_growth), cls: colorCls(f.earnings_growth) },
    { label: 'PROFIT MARGIN',   val: fmtPct(f.profit_margins),  cls: colorCls(f.profit_margins)  },
    { label: 'FORWARD P/E',     val: fmtPE(f.forward_pe),       cls: '' },
    { label: 'MARKET CAP',      val: fmtCap(f.market_cap),      cls: '' },
    { label: 'BETA',            val: f.beta != null ? f.beta.toFixed(2) : '—', cls: '' },
    { label: '52W HIGH',        val: f['52w_high'] != null ? '$' + f['52w_high'].toFixed(2) : '—', cls: '' },
    { label: '52W LOW',         val: f['52w_low']  != null ? '$' + f['52w_low'].toFixed(2)  : '—', cls: '' },
    { label: 'DIV YIELD',       val: fmtPct(f.dividend_yield),  cls: '' },
    { label: 'SECTOR',          val: f.sector || '—',           cls: '' },
  ];

  const fmpScreenHtml = isFmp ? `
    <div class="fmp-screen-row">
      <div class="fmp-screen-label">FMP FUNDAMENTAL SCREEN</div>
      <div class="fmp-screen-checks">
        <span class="fmp-check-item">${passFail(f.revenue_3q_growth)} Rev 3Q growth</span>
        <span class="fmp-check-item">${passFail(f.earnings_3q_growth)} EPS 3Q growth</span>
        <span class="fmp-check-item">${passFail(f.fcf_positive)} FCF positive</span>
      </div>
    </div>` : '';

  const strongBuyHtml = data.strong_buy
    ? `<div class="strong-buy-flag">★ STRONG BUY — Fundamental screen passed with MA convergence</div>`
    : '';

  const cvgHtml = cvg.length > 0
    ? `<div class="convergence-row">${cvg.map(c => `<span class="convergence-badge">⟳ ${c.label}</span>`).join('')}</div>`
    : '';

  const srcBadge = isFmp
    ? `<span class="data-src-badge src-fmp">FMP</span>`
    : `<span class="data-src-badge src-yf">yfinance</span>`;

  el.innerHTML = `
    ${strongBuyHtml}
    ${fmpScreenHtml}
    ${cvgHtml}
    <div class="fundamentals-header" onclick="toggleFundamentals()">
      <div class="fundamentals-title">FUNDAMENTALS ${srcBadge}</div>
      <div class="fundamentals-chevron" id="fund-chevron">▼</div>
    </div>
    <div class="fundamentals-body" id="fundamentals-body">
      ${stats.map(s => `
        <div class="fund-stat">
          <div class="stat-label">${s.label}</div>
          <div class="stat-val ${s.cls}">${s.val}</div>
        </div>`).join('')}
    </div>`;
}

function toggleFundamentals() {
  const body    = document.getElementById('fundamentals-body');
  const chevron = document.getElementById('fund-chevron');
  if (!body) return;
  const open = body.classList.toggle('open');
  if (chevron) { chevron.textContent = open ? '▲' : '▼'; chevron.classList.toggle('open', open); }
}

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

// ════════════════════════════════════════════════════════════════
// THEME TOGGLE
// ════════════════════════════════════════════════════════════════
function toggleTheme() {
  S.theme = S.theme === 'dark' ? 'light' : 'dark';
  localStorage.setItem('pg_theme', S.theme);
  applyTheme(S.theme);
}

function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  const btn = document.getElementById('theme-toggle-btn');
  if (btn) btn.textContent = theme === 'dark' ? '🌙' : '☀️';
}

// ════════════════════════════════════════════════════════════════
// HEATMAP TAB
// ════════════════════════════════════════════════════════════════
let heatmapRefreshTimer = null;

// BUG-018: force=true skips the auto-refresh timer so the button actually forces a fresh fetch
async function renderHeatmapTab(force = false) {
  const plotEl    = document.getElementById('heatmap-plot');
  const loadingEl = document.getElementById('heatmap-loading');
  const summaryEl = document.getElementById('heatmap-summary');
  if (!plotEl) return;

  // Show loading state
  if (loadingEl) { loadingEl.style.display = 'flex'; }
  plotEl.style.display = 'none';
  if (summaryEl) summaryEl.textContent = '—';

  try {
    const data = await api.get('/api/heatmap');
    if (data.error) throw new Error(data.error);

    // Backend returns: { sectors: [{symbol, name, pct, price}, ...], n_up, n_dn, generated_at }
    const sectors = data.sectors || [];
    if (sectors.length === 0) throw new Error('No sector data returned');

    const etfs   = sectors.map(s => s.symbol);
    const pcts   = sectors.map(s => s.pct   ?? 0);
    const prices = sectors.map(s => s.price ?? 0);
    const names  = sectors.map(s => s.name  || s.symbol);

    // Helper: format a % value that may be 0 (defaulted) or a real number
    const fmtPct = (p) => `${p >= 0 ? '+' : ''}${p.toFixed(2)}%`;

    const maxAbs = Math.max(...pcts.map(p => Math.abs(p)), 0.01);

    const bgColors = pcts.map(p => {
      const norm = p / maxAbs;
      return norm >= 0
        ? `rgba(0,230,118,${Math.min(0.95, 0.18 + 0.55 * norm).toFixed(2)})`
        : `rgba(255,82,82,${Math.min(0.95, 0.18 + 0.55 * Math.abs(norm)).toFixed(2)})`;
    });

    const trace = {
      type: 'treemap',
      labels: etfs,
      parents: etfs.map(() => ''),
      values: etfs.map(() => 1),
      text: etfs.map((sym, i) =>
        `${sym}<br>${names[i]}<br><b>${fmtPct(pcts[i])}</b>`
      ),
      textinfo: 'text',
      hovertemplate: '<b>%{label}</b> — %{customdata[1]}<br>Price: $%{customdata[0]:.2f}<br>Change: %{customdata[2]}<extra></extra>',
      customdata: etfs.map((sym, i) => [
        prices[i],
        names[i],
        fmtPct(pcts[i]),
      ]),
      marker: {
        colors: bgColors,
        line: { color: '#0a0a0b', width: 2 },
      },
      pathbar: { visible: false },
      textfont: { family: 'JetBrains Mono, monospace', size: 11, color: '#e2e2e8' },
    };

    const layout = {
      paper_bgcolor: '#0a0a0b',
      font: { family: 'JetBrains Mono, monospace', color: '#e2e2e8', size: 11 },
      margin: { l: 0, r: 0, t: 4, b: 0 },
    };

    if (loadingEl) loadingEl.style.display = 'none';
    plotEl.style.display = '';
    Plotly.newPlot(plotEl, [trace], layout, { responsive: true, displayModeBar: false });

    // Summary line — guard bestI/worstI in case all pcts are 0
    const up     = pcts.filter(p => p > 0).length;
    const dn     = pcts.filter(p => p < 0).length;
    const avg    = pcts.length ? (pcts.reduce((a, b) => a + b, 0) / pcts.length) : 0;
    const bestI  = pcts.indexOf(Math.max(...pcts));
    const worstI = pcts.indexOf(Math.min(...pcts));
    const now    = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    if (summaryEl) {
      summaryEl.innerHTML =
        `${up} up, ${dn} down &nbsp;·&nbsp; Avg ${fmtPct(avg)} &nbsp;·&nbsp; ` +
        `<span class="up">Best: ${etfs[bestI] ?? '—'} ${fmtPct(pcts[bestI] ?? 0)}</span> &nbsp;·&nbsp; ` +
        `<span class="down">Worst: ${etfs[worstI] ?? '—'} ${fmtPct(pcts[worstI] ?? 0)}</span> &nbsp;·&nbsp; ` +
        `<span style="color:var(--text3)">Updated ${now}</span>`;
    }
    const updEl = document.getElementById('heatmap-updated');
    if (updEl) updEl.textContent = now;

    // Auto-refresh every 5 min while tab is active (skip scheduling when forced)
    if (!force) {
      if (heatmapRefreshTimer) clearTimeout(heatmapRefreshTimer);
      heatmapRefreshTimer = setTimeout(() => {
        if (document.getElementById('tab-heatmap')?.classList.contains('active')) renderHeatmapTab();
      }, 300_000);
    }

  } catch (e) {
    if (loadingEl) loadingEl.style.display = 'none';
    plotEl.style.display = '';
    plotEl.innerHTML = `<div class="no-profile-msg">Failed to load sector data: ${e.message}</div>`;
  }
}

// ════════════════════════════════════════════════════════════════
// CORRELATION TAB
// ════════════════════════════════════════════════════════════════
// BUG-018: force parameter accepted for consistency with refresh button
async function renderCorrelationTab(force = false) {
  const plotEl    = document.getElementById('corr-plot');
  const loadingEl = document.getElementById('corr-loading');
  if (!plotEl) return;

  if (S.watchlist.length < 2) {
    if (loadingEl) loadingEl.style.display = 'none';
    plotEl.innerHTML = '<div class="no-profile-msg">Add at least 2 tickers to see a correlation matrix.</div>';
    return;
  }

  if (loadingEl) loadingEl.style.display = 'flex';
  plotEl.innerHTML = '';

  try {
    const data = await api.post('/api/correlation', { tickers: S.watchlist.map(t => t.symbol) });
    if (data.error) throw new Error(data.error);

    // Backend returns: { tickers: [...], matrix: [[...], ...], generated_at }
    const symbols = data.tickers;   // was data.symbols — wrong key
    const matrix  = data.matrix;
    if (!symbols || !symbols.length) throw new Error('No correlation data returned');
    const n = symbols.length;

    // Text labels for each cell — guard against null/undefined from API
    const textMatrix = matrix.map((row, ri) =>
      row.map((v, ci) => (ri === ci ? '—' : v != null ? v.toFixed(2) : '—'))
    );

    const trace = {
      type: 'heatmap',
      z: matrix,
      x: symbols,
      y: symbols,
      zmin: -1,
      zmax: 1,
      colorscale: [
        [0,    '#ff5252'],
        [0.25, '#7a1a1a'],
        [0.5,  '#1e1e26'],
        [0.75, '#0d3a1e'],
        [1.0,  '#00e676'],
      ],
      showscale: true,
      colorbar: {
        thickness: 12,
        len: 0.75,
        tickvals: [-1, -0.5, 0, 0.5, 1],
        ticktext: ['−1.0', '−0.5', '0', '+0.5', '+1.0'],
        tickfont: { family: 'JetBrains Mono, monospace', size: 9, color: '#9898a8' },
        outlinecolor: '#2a2a35',
        outlinewidth: 1,
      },
      hovertemplate: '<b>%{y} × %{x}</b><br>r = %{z:.3f}<extra></extra>',
      text: textMatrix,
      texttemplate: '%{text}',
      textfont: { family: 'JetBrains Mono, monospace', size: n > 8 ? 9 : 11, color: '#e2e2e8' },
    };

    const layout = {
      paper_bgcolor: '#0a0a0b',
      plot_bgcolor:  '#111114',
      font: { family: 'JetBrains Mono, monospace', color: '#9898a8', size: 10 },
      margin: { l: 60, r: 70, t: 20, b: 60 },
      xaxis: { color: '#9898a8', gridcolor: '#1e1e26', side: 'bottom', tickangle: n > 6 ? -45 : 0 },
      yaxis: { color: '#9898a8', gridcolor: '#1e1e26', autorange: 'reversed' },
    };

    if (loadingEl) loadingEl.style.display = 'none';
    Plotly.newPlot(plotEl, [trace], layout, { responsive: true, displayModeBar: false });

  } catch (e) {
    if (loadingEl) loadingEl.style.display = 'none';
    plotEl.innerHTML = `<div class="no-profile-msg">Failed to compute correlation: ${e.message}</div>`;
  }
}

// ════════════════════════════════════════════════════════════════
// CHART NEWS (Finnhub)
// ════════════════════════════════════════════════════════════════
async function loadChartNews(symbol) {
  const el = document.getElementById('chart-news');
  if (!el) return;

  try {
    const data = await api.get(`/api/news/${symbol}`);

    // Backend returns: { articles: [...] }  OR  { no_key: true, articles: [] }
    if (!data || data.no_key) return;
    const rawArticles = data.articles || [];
    if (rawArticles.length === 0) return;

    const articleHtml = rawArticles.map(a => {
      const sentClass = a.sentiment === 'bull' ? 'news-bull'
                      : a.sentiment === 'bear' ? 'news-bear'
                      : 'news-neutral';
      const sentLabel = a.sentiment === 'bull' ? 'BULLISH'
                      : a.sentiment === 'bear' ? 'BEARISH'
                      : 'NEUTRAL';
      // Escape any quotes in the URL to avoid HTML attribute issues
      const safeUrl = (a.url || '').replace(/"/g, '%22');
      return `
        <div class="news-item">
          <a href="${safeUrl}" target="_blank" rel="noopener noreferrer">
            <div class="news-headline">${esc(a.headline)}</div>
            <div class="news-meta">
              <span class="news-source">${esc(a.source)}</span>
              <span>${esc(a.time_ago || '')}</span>
              <span class="news-sentiment ${sentClass}">${sentLabel}</span>
            </div>
          </a>
        </div>`;
    }).join('');

    el.innerHTML = `
      <div class="news-section-header">
        <span>RECENT NEWS</span>
        <span>via Finnhub</span>
      </div>
      <div class="news-articles">${articleHtml}</div>`;

  } catch (e) {
    // Silently fail — news is supplementary
  }
}

// ════════════════════════════════════════════════════════════════
// OPTIONS CHAIN
// ════════════════════════════════════════════════════════════════
let _optCache       = {};   // key: `${sym}|${expiry}` → { ts, data }
let _optSide        = 'calls';
let _optSortCol     = 'strike';
let _optSortDir     = 'asc';
let _optCurrentExpiry = null;
let _optCurrentChain  = null;   // { calls:[], puts:[] }

const OPT_CACHE_MS  = 2 * 60 * 1000;   // 2-minute TTL

// ── Tab switcher ─────────────────────────────────────────────────────────────
function switchChartTab(tab) {
  const isChart   = tab === 'chart';
  document.getElementById('chart-tab-chart').style.display   = isChart ? '' : 'none';
  document.getElementById('chart-tab-options').style.display = isChart ? 'none' : '';

  document.getElementById('chart-tab-btn-chart').classList.toggle('active', isChart);
  document.getElementById('chart-tab-btn-options').classList.toggle('active', !isChart);

  // Show/hide chart-specific header controls (range, MA toggles, reset)
  const ctrl = document.getElementById('chart-header-controls');
  if (ctrl) ctrl.style.display = isChart ? '' : 'none';
}

// ── Bootstrap: load expirations + summary in background ──────────────────────
async function initOptions(symbol) {
  // Reset UI
  const sel = document.getElementById('opt-expiry-select');
  if (sel) { sel.innerHTML = '<option>Loading…</option>'; sel.disabled = true; }
  const sumBar = document.getElementById('opt-summary-bar');
  if (sumBar) sumBar.innerHTML = '<span class="opt-summary-item opt-summary-loading">Loading options data…</span>';
  _optCurrentChain  = null;
  _optCurrentExpiry = null;

  // Parallel: fetch expirations + summary
  const [expirations, summary] = await Promise.all([
    _fetchExpirations(symbol),
    _fetchOptionsSummary(symbol),
  ]);

  // Populate expiry dropdown
  if (sel) {
    sel.innerHTML = '';
    if (!expirations || !expirations.length) {
      sel.innerHTML = '<option>No options available</option>';
      sel.disabled = true;
    } else {
      expirations.forEach(exp => {
        const opt = document.createElement('option');
        opt.value       = exp;
        opt.textContent = exp;
        sel.appendChild(opt);
      });
      sel.disabled = false;
      _optCurrentExpiry = expirations[0];
    }
  }

  // Render summary bar
  _renderSummaryBar(summary);

  // Pre-load the first expiry chain in background
  if (_optCurrentExpiry) {
    _fetchOptionsChain(symbol, _optCurrentExpiry).then(chain => {
      _optCurrentChain = chain;
      // Only render if OPTIONS tab is currently active
      if (document.getElementById('chart-tab-options').style.display !== 'none') {
        _renderChainTable(chain ? chain[_optSide] : null);
        if (chain) _renderIVSmile(chain.calls, chain.puts, _optCurrentExpiry);
      }
    });
  }
}

// ── Fetch helpers ─────────────────────────────────────────────────────────────
async function _fetchExpirations(symbol) {
  try {
    const data = await api.get(`/api/options/expirations/${encodeURIComponent(symbol)}`);
    return data.expirations || [];
  } catch (e) {
    return [];
  }
}

async function _fetchOptionsSummary(symbol) {
  try {
    return await api.get(`/api/options/summary/${encodeURIComponent(symbol)}`);
  } catch (e) {
    return null;
  }
}

async function _fetchOptionsChain(symbol, expiry) {
  const cacheKey = `${symbol}|${expiry}`;
  const cached   = _optCache[cacheKey];
  if (cached && (Date.now() - cached.ts) < OPT_CACHE_MS) {
    return cached.data;
  }
  try {
    const data = await api.get(
      `/api/options/chain/${encodeURIComponent(symbol)}/${encodeURIComponent(expiry)}`
    );
    _optCache[cacheKey] = { ts: Date.now(), data };
    return data;
  } catch (e) {
    return null;
  }
}

// ── Summary bar renderer ──────────────────────────────────────────────────────
function _renderSummaryBar(summary) {
  const bar = document.getElementById('opt-summary-bar');
  if (!bar) return;
  if (!summary || summary.error) {
    bar.innerHTML = '<span class="opt-summary-item opt-summary-na">Options data unavailable</span>';
    return;
  }

  const fmtOI = v => {
    if (v == null) return '—';
    if (v >= 1e6) return (v / 1e6).toFixed(1) + 'M';
    if (v >= 1e3) return (v / 1e3).toFixed(0) + 'K';
    return v.toString();
  };
  const pcr = summary.put_call_ratio;
  const pcrColor = pcr == null ? '' :
    pcr > 1.2 ? 'opt-val-red' : pcr < 0.8 ? 'opt-val-green' : '';

  bar.innerHTML = `
    <span class="opt-summary-item">
      <span class="opt-summary-label">P/C Ratio</span>
      <span class="opt-summary-val ${pcrColor}">${pcr != null ? pcr.toFixed(2) : '—'}</span>
    </span>
    <span class="opt-summary-sep">|</span>
    <span class="opt-summary-item">
      <span class="opt-summary-label">Max Pain</span>
      <span class="opt-summary-val">${summary.max_pain != null ? '$' + summary.max_pain.toFixed(2) : '—'}</span>
    </span>
    <span class="opt-summary-sep">|</span>
    <span class="opt-summary-item">
      <span class="opt-summary-label">Call OI</span>
      <span class="opt-summary-val opt-val-green">${fmtOI(summary.total_call_oi)}</span>
    </span>
    <span class="opt-summary-sep">|</span>
    <span class="opt-summary-item">
      <span class="opt-summary-label">Put OI</span>
      <span class="opt-summary-val opt-val-red">${fmtOI(summary.total_put_oi)}</span>
    </span>`;
}

// ── Expiry change handler ────────────────────────────────────────────────────
async function onExpiryChange(expiry) {
  _optCurrentExpiry = expiry;
  const wrap = document.getElementById('opt-chain-wrap');
  if (wrap) wrap.innerHTML = '<div class="chart-loading"><div class="loading-spinner"></div><span>Loading chain…</span></div>';

  const chain = await _fetchOptionsChain(currentChartSymbol, expiry);
  _optCurrentChain = chain;
  _renderChainTable(chain ? chain[_optSide] : null);
  if (chain) _renderIVSmile(chain.calls, chain.puts, expiry);
}

// ── Side toggle ──────────────────────────────────────────────────────────────
function _setSide(side) {
  _optSide = side;
  document.getElementById('opt-btn-calls').classList.toggle('active', side === 'calls');
  document.getElementById('opt-btn-puts').classList.toggle('active', side === 'puts');
  if (_optCurrentChain) {
    _renderChainTable(_optCurrentChain[side]);
  }
}

// ── Sort helpers ─────────────────────────────────────────────────────────────
function _setSort(col) {
  if (_optSortCol === col) {
    _optSortDir = _optSortDir === 'asc' ? 'desc' : 'asc';
  } else {
    _optSortCol = col;
    _optSortDir = col === 'strike' ? 'asc' : 'desc';
  }
  if (_optCurrentChain) {
    _renderChainTable(_optCurrentChain[_optSide]);
  }
}

function _sortChain(rows, col, dir) {
  return [...rows].sort((a, b) => {
    const av = a[col] ?? (col === 'strike' ? Infinity : -Infinity);
    const bv = b[col] ?? (col === 'strike' ? Infinity : -Infinity);
    return dir === 'asc' ? av - bv : bv - av;
  });
}

// ── Chain table renderer ─────────────────────────────────────────────────────
function _renderChainTable(rows) {
  const wrap = document.getElementById('opt-chain-wrap');
  if (!wrap) return;

  if (!rows || !rows.length) {
    wrap.innerHTML = '<div class="opt-empty">No data for this expiry / side.</div>';
    return;
  }

  const sorted = _sortChain(rows, _optSortCol, _optSortDir);
  const f2  = v => (v != null ? v.toFixed(2) : '—');
  const fOI = v => (v != null && v > 0 ? v.toLocaleString() : '—');
  const fIV = v => (v != null ? (v * 100).toFixed(1) + '%' : '—');

  const sortIcon = col =>
    _optSortCol === col
      ? (_optSortDir === 'asc' ? ' ▲' : ' ▼')
      : ' ⇅';

  const cols = [
    { key: 'strike',            label: 'Strike'  },
    { key: 'lastPrice',         label: 'Last'    },
    { key: 'bid',               label: 'Bid'     },
    { key: 'ask',               label: 'Ask'     },
    { key: 'volume',            label: 'Volume'  },
    { key: 'openInterest',      label: 'OI'      },
    { key: 'impliedVolatility', label: 'IV'      },
  ];

  const headerHtml = cols.map(c =>
    `<th class="opt-th opt-th-sort" onclick="_setSort('${c.key}')">${esc(c.label)}${sortIcon(c.key)}</th>`
  ).join('');

  const rowsHtml = sorted.map(r => {
    const itm   = r.inTheMoney ? ' opt-itm' : '';
    const ivPct = r.impliedVolatility != null ? r.impliedVolatility * 100 : null;
    const ivCls = ivPct == null ? '' : ivPct > 60 ? ' opt-iv-hot' : ivPct > 30 ? ' opt-iv-warm' : '';
    return `<tr class="opt-row${itm}">
      <td class="opt-td opt-strike${itm}">${f2(r.strike)}</td>
      <td class="opt-td">${f2(r.lastPrice)}</td>
      <td class="opt-td">${f2(r.bid)}</td>
      <td class="opt-td">${f2(r.ask)}</td>
      <td class="opt-td">${fOI(r.volume)}</td>
      <td class="opt-td">${fOI(r.openInterest)}</td>
      <td class="opt-td${ivCls}">${fIV(r.impliedVolatility)}</td>
    </tr>`;
  }).join('');

  wrap.innerHTML = `
    <div class="opt-itm-legend">
      <span class="opt-itm-dot"></span><span>In-the-money</span>
    </div>
    <div class="opt-table-scroll">
      <table class="opt-table">
        <thead><tr>${headerHtml}</tr></thead>
        <tbody>${rowsHtml}</tbody>
      </table>
    </div>`;
}

// ── IV Smile chart (Plotly) ──────────────────────────────────────────────────
function _renderIVSmile(calls, puts, expiry) {
  const el = document.getElementById('opt-iv-chart');
  if (!el) return;

  const isDark = document.documentElement.getAttribute('data-theme') !== 'light';
  const bg     = isDark ? '#0a0a0b' : '#f5f5f7';
  const plotBg = isDark ? '#111114' : '#ffffff';
  const gridC  = isDark ? '#1e1e26' : '#e4e4ea';
  const fontC  = isDark ? '#9898a8' : '#48484f';

  const validCalls = (calls || []).filter(r => r.strike != null && r.impliedVolatility != null);
  const validPuts  = (puts  || []).filter(r => r.strike != null && r.impliedVolatility != null);

  if (!validCalls.length && !validPuts.length) {
    el.style.display = 'none';
    return;
  }
  el.style.display = '';

  const mkTrace = (rows, name, color) => ({
    type: 'scatter',
    mode: 'lines+markers',
    x: rows.map(r => r.strike),
    y: rows.map(r => parseFloat((r.impliedVolatility * 100).toFixed(2))),
    name,
    line:   { color, width: 2 },
    marker: { color, size: 4 },
    hovertemplate: `Strike: $%{x}<br>IV: %{y:.1f}%<extra>${esc(name)}</extra>`,
  });

  const traces = [];
  if (validCalls.length) traces.push(mkTrace(validCalls, 'Calls IV', '#00e676'));
  if (validPuts.length)  traces.push(mkTrace(validPuts,  'Puts IV',  '#ff5252'));

  const layout = {
    paper_bgcolor: bg,
    plot_bgcolor:  plotBg,
    font:   { family: 'JetBrains Mono, monospace', color: fontC, size: 10 },
    margin: { l: 10, r: 60, t: 28, b: 36 },
    height: 200,
    title:  { text: `IV Smile — ${esc(expiry)}`, font: { size: 11, color: fontC }, x: 0.01 },
    xaxis:  { title: { text: 'Strike', font: { size: 10 } }, gridcolor: gridC, color: fontC, tickprefix: '$' },
    yaxis:  { title: { text: 'IV %', font: { size: 10 } }, gridcolor: gridC, color: fontC, side: 'right', ticksuffix: '%' },
    legend: { x: 0.01, y: 0.99, bgcolor: 'rgba(0,0,0,0)', font: { size: 10 } },
    hovermode: 'x unified',
    hoverlabel: { bgcolor: isDark ? '#18181d' : '#ffffff', bordercolor: gridC,
                  font: { family: 'JetBrains Mono, monospace', size: 11 } },
  };

  Plotly.newPlot('opt-iv-chart', traces, layout, {
    responsive: true,
    displayModeBar: false,
    scrollZoom: false,
  });
}

// ── Handle chart tab click when options tab is active (lazy render) ───────────
// Ensures the chain table shows if user switches back to options tab manually
document.addEventListener('DOMContentLoaded', () => {
  const optBtn = document.getElementById('chart-tab-btn-options');
  if (optBtn) {
    optBtn.addEventListener('click', () => {
      // Only re-render chain if the CHAIN subtab is currently active
      const chainTab = document.getElementById('opt-subtab-chain');
      if (chainTab && chainTab.style.display !== 'none' && _optCurrentChain) {
        _renderChainTable(_optCurrentChain[_optSide]);
        _renderIVSmile(_optCurrentChain.calls, _optCurrentChain.puts, _optCurrentExpiry);
      }
    });
  }

  // Register options quiz keyboard handler (always active; checked by display state)
  document.addEventListener('keydown', _optQuizKeyHandler);
});

// ════════════════════════════════════════════════════════════════
// OPTIONS STRATEGIES — browser + questionnaire engine
// ════════════════════════════════════════════════════════════════

// Type → hex color (Income=blue, Directional=green, Volatility=orange, Hedging=purple, Arbitrage=yellow)
const OPT_STRATEGY_TYPE_COLORS = {
  'Income':      '#3b82f6',
  'Directional': '#22c55e',
  'Volatility':  '#f97316',
  'Hedging':     '#a855f7',
  'Arbitrage':   '#eab308',
};

let _osFilterActive = 'All';

// ── Options subtab switcher ───────────────────────────────────────────────────
function switchOptSubtab(tab) {
  ['chain', 'strategies', 'quiz'].forEach(t => {
    const el  = document.getElementById(`opt-subtab-${t}`);
    const btn = document.getElementById(`opt-subtab-btn-${t}`);
    if (el)  el.style.display = t === tab ? 'flex' : 'none';
    if (btn) btn.classList.toggle('active', t === tab);
  });
  if (tab === 'strategies') renderOptStrategyGrid(_osFilterActive);
  if (tab === 'quiz') {
    if (_optQuizStep < OPT_QUIZ_QUESTIONS.length) _renderOptQuizQuestion(_optQuizStep);
    else _renderOptQuizResults();
  }
}

// ── Strategy grid ─────────────────────────────────────────────────────────────
function filterOptStrategies(type) {
  _osFilterActive = type;
  document.querySelectorAll('.os-filter-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.type === type)
  );
  renderOptStrategyGrid(type);
}

function renderOptStrategyGrid(type) {
  const grid = document.getElementById('os-strategy-grid');
  if (!grid) return;
  const list = (type === 'All')
    ? OPTIONS_STRATEGIES
    : OPTIONS_STRATEGIES.filter(s => s.type === type);

  if (!list.length) {
    grid.innerHTML = '<div class="opt-empty" style="padding:32px;text-align:center;color:var(--text3)">No strategies in this category.</div>';
    return;
  }
  grid.innerHTML = list.map(s => _buildStrategyCard(s)).join('');
}

function _buildStrategyCard(s) {
  const color = OPT_STRATEGY_TYPE_COLORS[s.type] || '#888';
  const riskBadge = s.risk_type === 'defined'
    ? '<span class="os-risk-badge os-risk-defined">✓ DEFINED RISK</span>'
    : '<span class="os-risk-badge os-risk-undefined">⚠ UNDEFINED RISK</span>';
  const biasIcon = { positive: '↑', negative: '↓', neutral: '→' };
  const ivReq = s.ivr_min != null
    ? `IVR ≥ ${s.ivr_min}` : s.ivr_max != null
    ? `IVR ≤ ${s.ivr_max}` : 'Any IVR';

  return `
    <div class="os-card" id="os-card-${esc(s.id)}">
      <div class="os-card-header" onclick="toggleOptStrategyCard('${esc(s.id)}')">
        <div class="os-card-title-row">
          <span class="os-card-name">${esc(s.name)}</span>
          <div class="os-card-badges">
            <span class="os-type-badge" style="background:${color}22;color:${color};border:1px solid ${color}55">${esc(s.type)}</span>
            ${riskBadge}
            <span class="os-legs-badge">${s.legs} leg${s.legs !== 1 ? 's' : ''}</span>
          </div>
        </div>
        <p class="os-card-desc">${esc(s.description)}</p>
        <div class="os-greeks-row">
          <span class="os-greek" title="Delta target">Δ ${esc(s.delta_target)}</span>
          <span class="os-greek-sep"></span>
          <span class="os-greek" title="Theta bias">Θ ${biasIcon[s.theta_bias] || '→'} ${esc(s.theta_bias)}</span>
          <span class="os-greek-sep"></span>
          <span class="os-greek" title="Vega bias">ν ${biasIcon[s.vega_bias] || '→'} ${esc(s.vega_bias)}</span>
          <span class="os-greek-sep"></span>
          <span class="os-greek" title="Gamma bias">Γ ${biasIcon[s.gamma_bias] || '→'} ${esc(s.gamma_bias)}</span>
        </div>
        <div class="os-card-meta-row">
          <span class="os-meta-chip"><span class="os-meta-label">DTE Entry</span>${s.dte_entry}</span>
          <span class="os-meta-chip"><span class="os-meta-label">Exit at</span>${s.dte_exit} DTE</span>
          <span class="os-meta-chip"><span class="os-meta-label">Target</span>${s.profit_target_pct}%</span>
          <span class="os-meta-chip"><span class="os-meta-label">IV</span>${esc(ivReq)}</span>
        </div>
        <div class="os-toggle-hint">▾ expand for full detail</div>
      </div>

      <div class="os-card-detail" id="os-detail-${esc(s.id)}" style="display:none">
        <div class="os-detail-grid">
          <div class="os-detail-section">
            <div class="os-detail-title">Entry Conditions</div>
            <div class="os-detail-row"><span class="os-dl">Signal</span><span>${esc(s.entry_signal)}</span></div>
            <div class="os-detail-row"><span class="os-dl">RSI Signal</span><span>${esc(s.rsi_signal)}</span></div>
            <div class="os-detail-row"><span class="os-dl">Trend</span><span>${esc(s.trend_requirement.replace(/_/g,' '))}</span></div>
            <div class="os-detail-row"><span class="os-dl">IV Regime</span><span>${esc(s.iv_regime)}</span></div>
          </div>
          <div class="os-detail-section">
            <div class="os-detail-title">Risk Profile</div>
            <div class="os-detail-row"><span class="os-dl">Max Gain</span><span>${esc(s.max_gain)}</span></div>
            <div class="os-detail-row"><span class="os-dl">Max Loss</span><span>${esc(s.max_loss)}</span></div>
            <div class="os-detail-row"><span class="os-dl">Breakeven</span><span>${esc(s.breakeven)}</span></div>
            <div class="os-detail-row"><span class="os-dl">Stop Loss</span><span>${esc(s.stop_loss_rule)}</span></div>
          </div>
          <div class="os-detail-section">
            <div class="os-detail-title">Trade Management</div>
            <div class="os-detail-row"><span class="os-dl">Rolling Rule</span><span>${esc(s.rolling_rule)}</span></div>
            <div class="os-detail-row"><span class="os-dl">Market Condition</span><span>${esc(s.market_condition)}</span></div>
            <div class="os-detail-row"><span class="os-dl">Earnings Play</span><span>${s.earnings_play ? '✓ Yes' : 'No'}</span></div>
          </div>
          <div class="os-detail-section">
            <div class="os-detail-title">Position Sizing</div>
            <div class="os-detail-row"><span class="os-dl">Max % Account</span><span>${s.capital_pct_max}%</span></div>
            <div class="os-detail-row"><span class="os-dl">Capital Tier</span><span>${esc(s.capital_tier)}</span></div>
            <div class="os-detail-row"><span class="os-dl">Margin Required</span><span>${s.margin_required ? '✓ Yes' : 'No'}</span></div>
          </div>
        </div>
      </div>
    </div>`;
}

function toggleOptStrategyCard(id) {
  const detail = document.getElementById(`os-detail-${id}`);
  const card   = document.getElementById(`os-card-${id}`);
  if (!detail || !card) return;
  const isOpen = detail.style.display !== 'none';
  detail.style.display = isOpen ? 'none' : 'block';
  card.classList.toggle('os-card-open', !isOpen);
  const hint = card.querySelector('.os-toggle-hint');
  if (hint) hint.textContent = isOpen ? '▾ expand for full detail' : '▴ collapse';
}

// ════════════════════════════════════════════════════════════════
// OPTIONS QUESTIONNAIRE — 7 questions, weighted scoring
// ════════════════════════════════════════════════════════════════

const OPT_QUIZ_QUESTIONS = [
  {
    q: 'What is your directional view on the underlying?',
    hint: 'The most important factor — your bias directly determines which strategies qualify.',
    options: [
      { id: 'a', text: 'Strongly Bullish — expecting a significant upward move' },
      { id: 'b', text: 'Moderately Bullish — slight bullish lean; OK if it goes sideways' },
      { id: 'c', text: 'Neutral / Sideways — expecting little to no price movement' },
      { id: 'd', text: 'Moderately Bearish — slight bearish lean; OK if it stays flat' },
      { id: 'e', text: 'Strongly Bearish — expecting a significant downward move' },
    ],
  },
  {
    q: 'How much capital are you allocating to this trade?',
    hint: 'Some strategies require more capital or margin approval to execute properly.',
    options: [
      { id: 'a', text: 'Under $500' },
      { id: 'b', text: '$500 – $2,500' },
      { id: 'c', text: '$2,500 – $10,000' },
      { id: 'd', text: 'Over $10,000' },
    ],
  },
  {
    q: 'What is your risk tolerance for this trade?',
    hint: '"Undefined" risk means losses can exceed the premium collected — e.g., selling naked options.',
    options: [
      { id: 'a', text: 'Strictly defined — only strategies with a hard cap on maximum loss' },
      { id: 'b', text: 'Defined but wide — accept ratio spreads with limited additional downside' },
      { id: 'c', text: 'Undefined — willing to accept assignment or margin calls if managed actively' },
    ],
  },
  {
    q: 'How would you describe the current volatility environment?',
    hint: 'IVR (IV Rank) compares current IV to its 52-week range. High IVR means options are expensive.',
    options: [
      { id: 'a', text: 'Low / Normal IV — options are cheap, IVR below 30' },
      { id: 'b', text: 'Elevated IV — approaching a catalyst event (earnings, Fed meeting, data release)' },
      { id: 'c', text: 'Post-event crush — IV spike just occurred; expecting rapid contraction' },
    ],
  },
  {
    q: 'What is your preferred time horizon and monitoring frequency?',
    hint: 'Longer DTE requires less monitoring but ties up capital longer.',
    options: [
      { id: 'a', text: 'Intraday / Short-term — 0–7 DTE, monitor daily' },
      { id: 'b', text: 'Swing — 30–45 DTE, check a few times per week' },
      { id: 'c', text: 'Long-term — 90+ DTE (LEAPS), monthly monitoring is fine' },
    ],
  },
  {
    q: 'What is your primary trading objective?',
    hint: "Different objectives fundamentally change which strategy fits best.",
    options: [
      { id: 'a', text: 'Consistent premium income — generate steady cash flow from my account' },
      { id: 'b', text: 'Leveraged capital growth — maximize gains on a high-conviction directional move' },
      { id: 'c', text: 'Hedging — reduce risk on an existing stock or portfolio position' },
      { id: 'd', text: 'Acquiring stock at a discount — use The Wheel to buy shares below market' },
    ],
  },
  {
    q: 'Which Greeks characteristic do you most want to trade?',
    hint: 'This reveals whether you profit from time passing, direction, or volatility changing.',
    options: [
      { id: 'a', text: 'Time Decay (Theta) — want time passing to work in my favor' },
      { id: 'b', text: 'Directional Momentum (Delta) — want to profit from a price move' },
      { id: 'c', text: 'Volatility Expansion (Vega) — want to profit from an IV spike' },
      { id: 'd', text: 'Volatility Contraction (−Vega) — want to profit from IV collapsing' },
    ],
  },
];

let _optQuizStep     = 0;
let _optQuizAnswers  = Array(OPT_QUIZ_QUESTIONS.length).fill(null);
let _optQuizKeyBound = false;   // keyboard handler is registered once at DOMContentLoaded

function openOptQuiz() {
  _optQuizStep    = 0;
  _optQuizAnswers = Array(OPT_QUIZ_QUESTIONS.length).fill(null);
  switchOptSubtab('quiz');
}

// ── Key handler (always registered; checks subtab visibility) ────────────────
function _optQuizKeyHandler(e) {
  const quizEl = document.getElementById('opt-subtab-quiz');
  if (!quizEl || quizEl.style.display === 'none') return;
  if (_optQuizStep >= OPT_QUIZ_QUESTIONS.length) return;   // results screen

  const opts = [...document.querySelectorAll('.opt-quiz-option input[type="radio"]')];
  const cur  = opts.findIndex(r => r.checked);

  if (e.key === 'ArrowDown' || e.key === 'ArrowRight') {
    e.preventDefault();
    const nxt = opts[Math.min(cur + 1, opts.length - 1)];
    if (nxt) { nxt.click(); nxt.closest('.opt-quiz-option').classList.add('selected'); }
  } else if (e.key === 'ArrowUp' || e.key === 'ArrowLeft') {
    e.preventDefault();
    const prv = opts[Math.max(cur - 1, 0)];
    if (prv) { prv.click(); prv.closest('.opt-quiz-option').classList.add('selected'); }
  } else if (e.key === 'Enter') {
    e.preventDefault();
    nextOptQuizQuestion();
  }
}

// ── Render question ──────────────────────────────────────────────────────────
function _renderOptQuizQuestion(n) {
  const body = document.getElementById('opt-quiz-body');
  if (!body) return;
  const total = OPT_QUIZ_QUESTIONS.length;
  const q     = OPT_QUIZ_QUESTIONS[n];
  const pct   = Math.round((n / total) * 100);
  const saved = _optQuizAnswers[n];

  body.innerHTML = `
    <div class="opt-quiz-inner">
      <div>
        <div class="quiz-progress-label">QUESTION ${n + 1} OF ${total}</div>
        <div class="quiz-progress-track"><div class="quiz-progress-fill" style="width:${pct}%"></div></div>
      </div>
      <div>
        <p class="quiz-q-text">${esc(q.q)}</p>
        ${q.hint ? `<p class="quiz-q-hint">${esc(q.hint)}</p>` : ''}
      </div>
      <div class="quiz-options">
        ${q.options.map(opt => `
          <label class="opt-quiz-option quiz-option${saved === opt.id ? ' selected' : ''}">
            <input type="radio" name="opt-quiz-q${n}" value="${opt.id}" ${saved === opt.id ? 'checked' : ''}
              onchange="_optQuizAnswers[${n}]='${opt.id}';document.querySelectorAll('.opt-quiz-option').forEach(el=>el.classList.toggle('selected',el.querySelector('input').checked))">
            <div class="quiz-option-inner">
              <div class="quiz-option-dot"></div>
              <span class="quiz-option-text">${esc(opt.text)}</span>
            </div>
          </label>`).join('')}
      </div>
      <div class="quiz-nav">
        ${n > 0 ? `<button class="btn-ghost" onclick="prevOptQuizQuestion()">← BACK</button>` : '<div></div>'}
        <button class="btn-primary" onclick="nextOptQuizQuestion()">
          ${n < total - 1 ? 'NEXT →' : 'SHOW RESULTS →'}
        </button>
      </div>
    </div>`;
}

function nextOptQuizQuestion() {
  const n        = _optQuizStep;
  const selected = document.querySelector(`[name="opt-quiz-q${n}"]:checked`);
  if (!selected) { showToast('Please select an answer to continue.', 'error'); return; }
  _optQuizAnswers[n] = selected.value;
  if (n < OPT_QUIZ_QUESTIONS.length - 1) {
    _optQuizStep = n + 1;
    _renderOptQuizQuestion(_optQuizStep);
  } else {
    _optQuizStep = OPT_QUIZ_QUESTIONS.length;   // signals results screen
    _renderOptQuizResults();
  }
}

function prevOptQuizQuestion() {
  if (_optQuizStep > 0) { _optQuizStep--; _renderOptQuizQuestion(_optQuizStep); }
}

// ── Scoring engine ────────────────────────────────────────────────────────────
// weights: directional 0.35 | iv_environment 0.30 | capital 0.20 | obj+greek 0.15
// Hard filters applied first (result = 0 if any fail).
function calculateOptQuizResults() {
  const [q1, q2, q3, q4, q5, q6, q7] = _optQuizAnswers;

  // Map answers to semantic labels
  const userView   = { a:'bullish', b:'bullish', c:'neutral', d:'bearish', e:'bearish' }[q1] || 'neutral';
  const strongDir  = q1 === 'a' || q1 === 'e';
  const capTier    = { a:'cap_low', b:'cap_low_med', c:'cap_med', d:'cap_high' }[q2] || 'cap_med';
  const riskPref   = { a:'defined_only', b:'defined_wide', c:'undefined' }[q3] || 'defined_only';
  const ivEnv      = { a:'low', b:'high', c:'crush' }[q4] || 'low';
  const horizon    = { a:'intraday', b:'swing', c:'long_term' }[q5] || 'swing';
  const objective  = { a:'income', b:'growth', c:'hedging', d:'acquiring_stock' }[q6] || 'income';
  const greekPref  = { a:'theta', b:'delta', c:'vega', d:'negative_vega' }[q7] || 'theta';

  return OPTIONS_STRATEGIES.map(s => {
    // ── Hard filters ─────────────────────────────────────────────────────────
    // 1. Undefined risk + user wants defined_only → eliminate
    if (s.risk_type === 'undefined' && riskPref === 'defined_only') {
      return { ...s, score: 0, matchPct: 0 };
    }
    // 2. High-capital strategy + tiny account → eliminate
    if (s.capital_tier === 'high' && capTier === 'cap_low') {
      return { ...s, score: 0, matchPct: 0 };
    }
    // 3. Margin strategy + strictly defined-only user → eliminate
    if (s.margin_required && riskPref === 'defined_only') {
      return { ...s, score: 0, matchPct: 0 };
    }

    // ── Soft scores ───────────────────────────────────────────────────────────
    // 1. Directional alignment (weight 0.35)
    let directional = 0;
    if (s.compatible_views.includes(userView)) {
      directional = strongDir ? 1.0 : 0.88;
    } else if (s.compatible_views.length >= 3) {
      directional = 0.50;   // wide/flexible strategy
    } else {
      directional = 0.08;   // misaligned but not eliminated
    }
    if (strongDir && s.type === 'Directional') directional = Math.min(1.0, directional + 0.10);

    // 2. IV environment match (weight 0.30)
    let ivMatch = 0;
    if (ivEnv === 'low') {
      ivMatch = s.iv_regime === 'low' ? 1.0 : s.iv_regime === 'any' ? 0.60 : 0.15;
    } else if (ivEnv === 'high') {
      if   (s.iv_regime === 'high' && s.ivr_min != null) ivMatch = 1.0;
      else if (s.iv_regime === 'high')                   ivMatch = 0.85;
      else if (s.iv_regime === 'any')                    ivMatch = 0.55;
      else                                               ivMatch = 0.15;
    } else {  // crush
      if   (s.earnings_play && s.iv_regime === 'high')  ivMatch = 1.0;
      else if (s.iv_regime === 'high')                  ivMatch = 0.80;
      else if (s.iv_regime === 'any')                   ivMatch = 0.45;
      else                                              ivMatch = 0.10;
    }

    // 3. Capital efficiency (weight 0.20)
    const capMatrix = {
      cap_low:     { low: 1.0, medium: 0.45, high: 0.0  },
      cap_low_med: { low: 1.0, medium: 0.90, high: 0.25 },
      cap_med:     { low: 1.0, medium: 1.0,  high: 0.60 },
      cap_high:    { low: 1.0, medium: 1.0,  high: 1.0  },
    };
    const capital = (capMatrix[capTier] || capMatrix['cap_med'])[s.capital_tier] ?? 0.5;

    // 4. Objective + Greek match combined (weight 0.15)
    const objMatch   = s.compatible_objectives.includes(objective)  ? 1.0 : 0.08;
    const greekMatch = s.compatible_greeks_pref.includes(greekPref) ? 1.0 : 0.12;
    const objGreek   = (objMatch + greekMatch) / 2;

    // Horizon bonus: multiply score by 0.82–1.00
    const horizonBonus = s.compatible_horizons.includes(horizon) ? 1.0 : 0.82;

    const rawScore = (directional * 0.35 + ivMatch * 0.30 + capital * 0.20 + objGreek * 0.15) * horizonBonus;
    const matchPct = Math.min(99, Math.round(rawScore * 100));

    return { ...s, score: rawScore, matchPct };
  }).sort((a, b) => b.matchPct - a.matchPct || b.score - a.score);
}

// ── Results screen ────────────────────────────────────────────────────────────
function _renderOptQuizResults() {
  const body = document.getElementById('opt-quiz-body');
  if (!body) return;

  const top5 = calculateOptQuizResults().slice(0, 5);

  body.innerHTML = `
    <div class="opt-quiz-inner">
      <div class="quiz-results-header">
        <p class="quiz-results-subtitle">Top 5 options strategies matched to your market view, risk tolerance, and objectives.</p>
      </div>
      <div class="quiz-results">
        ${top5.map((r, i) => {
          const color    = OPT_STRATEGY_TYPE_COLORS[r.type] || '#888';
          const pctColor = r.matchPct >= 75 ? 'var(--green)' : r.matchPct >= 50 ? 'var(--yellow)' : 'var(--text2)';
          const riskBadge = r.risk_type === 'defined'
            ? '<span class="os-risk-badge os-risk-defined">✓ DEFINED</span>'
            : '<span class="os-risk-badge os-risk-undefined">⚠ UNDEFINED</span>';
          return `
            <div class="result-card ${i === 0 ? 'result-card-top' : ''}">
              <div class="result-card-header">
                <span class="result-match-pct" style="color:${pctColor}">${r.matchPct}% match</span>
                <span class="os-type-badge" style="background:${color}22;color:${color};border:1px solid ${color}55">${esc(r.type)}</span>
                ${riskBadge}
              </div>
              <div class="result-name">${esc(r.name)}</div>
              <p class="result-desc">${esc(r.description)}</p>
              <div class="os-result-bar-wrap">
                <div class="os-result-bar-fill" style="width:${r.matchPct}%;background:${color}88"></div>
              </div>
              <button class="btn-primary result-btn" onclick="viewOptStrategyCard('${esc(r.id)}')">
                View Strategy Details →
              </button>
            </div>`;
        }).join('')}
      </div>
      <div class="quiz-results-footer">
        <button class="btn-ghost" onclick="openOptQuiz()">↺ Start Over</button>
      </div>
    </div>`;
}

// Navigate to the matching strategy card in STRATEGIES subtab
function viewOptStrategyCard(id) {
  filterOptStrategies('All');
  switchOptSubtab('strategies');
  setTimeout(() => {
    const card = document.getElementById(`os-card-${id}`);
    if (!card) return;
    card.scrollIntoView({ behavior: 'smooth', block: 'center' });
    const detail = document.getElementById(`os-detail-${id}`);
    if (detail && detail.style.display === 'none') toggleOptStrategyCard(id);
  }, 120);
}
