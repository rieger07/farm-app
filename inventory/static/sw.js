// Minimal service worker: lets /quick-sale load offline.
const CACHE = 'il-v6';
const SHELL = ['/shared-static/theme.css', '/shared-static/qrcode.min.js', '/shared-static/pay_qr.js', '/static/style.css', '/static/quicksale.js'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)));
  self.skipWaiting();
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))));
  self.clients.claim();
});
self.addEventListener('fetch', e => {
  const req = e.request, url = new URL(req.url);
  if (req.method !== 'GET' || url.origin !== location.origin) return;
  // Network-first for the quick-sale page, falling back to the last copy.
  if (url.pathname === '/quick-sale') {
    e.respondWith(fetch(req).then(r => {
      if (r.ok) { const copy = r.clone(); caches.open(CACHE).then(c => c.put(req, copy)); }
      return r;
    }).catch(() => caches.match(req)));
    return;
  }
  if (url.pathname.startsWith('/static/') || url.pathname.startsWith('/shared-static/')) {
    e.respondWith(fetch(req).then(r => {
      const copy = r.clone(); caches.open(CACHE).then(c => c.put(req, copy)); return r;
    }).catch(() => caches.match(req, { ignoreSearch: true })));
  }
});
