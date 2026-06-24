// Wolf Scanner service worker — installability only.
// IMPORTANT: never caches HTML pages or API/data responses, so the
// trading dashboard is always live. Only the app icons are cached.
const CACHE = 'wolf-static-v1';

self.addEventListener('install', (e) => { self.skipWaiting(); });
self.addEventListener('activate', (e) => { e.waitUntil(self.clients.claim()); });

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  // Cache-first ONLY for static icons/manifest. Everything else = live network.
  if (url.pathname.startsWith('/static/')) {
    e.respondWith(
      caches.open(CACHE).then((c) =>
        c.match(e.request).then((hit) =>
          hit || fetch(e.request).then((resp) => { c.put(e.request, resp.clone()); return resp; })
        )
      )
    );
  }
  // All other requests fall through to the network (no stale data).
});
