/* PG Stock Analysis service worker — caches the static shell so the app opens
   instantly, and stores the last /api/quote response for an offline view. */
const SHELL = 'pg-shell-v1';
const DATA = 'pg-data-v1';
const SHELL_ASSETS = ['/', '/index.html', '/style.css', '/app.js', '/icon.svg', '/manifest.webmanifest'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(SHELL).then(c => c.addAll(SHELL_ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then(keys => Promise.all(
      keys.filter(k => k !== SHELL && k !== DATA).map(k => caches.delete(k))
    )).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' && url.pathname !== '/api/quote') {
    // Only the last quote POST is cached; everything else network-only
    if (url.pathname === '/api/quote') {
      e.respondWith(networkThenCacheQuote(e.request));
    }
    return;
  }
  if (url.pathname === '/api/quote') {
    e.respondWith(networkThenCacheQuote(e.request));
    return;
  }
  // Static shell: cache-first, fall back to network
  if (SHELL_ASSETS.includes(url.pathname) || url.pathname === '/') {
    e.respondWith(caches.match(e.request).then(r => r || fetch(e.request)));
  }
});

async function networkThenCacheQuote(request) {
  try {
    const res = await fetch(request.clone());
    const cache = await caches.open(DATA);
    cache.put('last-quote', res.clone());
    return res;
  } catch (err) {
    const cached = await caches.open(DATA).then(c => c.match('last-quote'));
    if (cached) {
      const body = await cached.json();
      // Flag every quote stale so the UI shows the offline banner
      Object.keys(body).forEach(k => { if (body[k]) body[k].stale = true; });
      return new Response(JSON.stringify(body), { headers: { 'Content-Type': 'application/json' } });
    }
    throw err;
  }
}
