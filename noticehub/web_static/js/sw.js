/* Only the page shell belongs here. Personal data stays out of Cache Storage. */
const CACHE = 'noticehub-pwa-shell';
const SHELL = ['/', '/manifest.webmanifest', '/icon.svg', '/icon-192.png',
  '/icon-512.png', '/icon-maskable-512.png', '/apple-touch-icon.png',
  '/js/pwa.js', '/js/notice.js', '/js/announce.js', '/js/pair.js',
  '/js/lab-rules.js', '/js/settings.js', '/js/trip.js', '/js/guide.js',
  '/js/map3d.js', '/js/three.module.min.js'];

self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', event => {
  event.waitUntil(caches.keys().then(names => Promise.all(names
    .filter(name => name === 'noticehub-shell-1')
    .map(name => caches.delete(name)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', event => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== 'GET' || url.origin !== self.location.origin ||
      url.pathname === '/api' || url.pathname.startsWith('/api/') ||
      !SHELL.includes(url.pathname)) return;
  const key = url.pathname;
  event.respondWith((async () => {
    const cache = await caches.open(CACHE);
    try {
      const response = await fetch(request, { cache: 'no-store' });
      if (response.ok && !response.redirected) {
        try { await cache.put(key, response.clone()); } catch (error) { /* Storage may be full; keep the network response. */ }
      }
      return response;
    } catch (error) {
      const saved = await cache.match(key);
      if (saved) return saved;
      throw error;
    }
  })());
});
