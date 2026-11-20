/* Only the page shell belongs here. Personal data stays out of Cache Storage. */
/* Bump SHELL_VERSION whenever SHELL or the shell strategy changes, so clients
   drop the previous cache instead of serving a stale shell forever. */
const SHELL_VERSION = 'v7';
const CACHE = 'noticehub-pwa-shell-' + SHELL_VERSION;
const SHELL = ['/', '/manifest.webmanifest', '/icon.svg', '/icon-192.png',
  '/icon-512.png', '/icon-maskable-512.png', '/apple-touch-icon.png',
  '/js/pwa.js', '/js/notice.js', '/js/announce.js', '/js/pair.js',
  '/js/lab-rules.js', '/js/settings.js', '/js/trip.js', '/js/guide.js',
  '/js/map3d.js', '/js/campus-snapshot.js', '/js/campus-osm.js',
  '/js/three.module.min.js'];
/* 只预缓存默认快照 js/campus-osm.js。campus-geo.json 里 "snapshot" 指向的自定义快照是
   数据目录按校区给的，构建期不知道文件名，不走预缓存——它由 campus-snapshot.js 运行时
   注入，离线时靠下面的 fetch 兜底（不在 SHELL 里就不缓存，联网时每次直连）。 */

self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', event => {
  event.waitUntil(caches.keys().then(names => Promise.all(names
    .filter(name => name !== CACHE)
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
