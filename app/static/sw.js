// Wolf Scanner service worker — installability only.
//
// IMPORTANT: never caches HTML pages or API/data responses, so the trading
// dashboard is always live.
//
// 2026-08-19: the code did NOT match its own comment. It claimed to cache
// "only the app icons" while the condition was startsWith('/static/') —
// which is every stylesheet and every script — cache-first with no expiry
// and no cleanup. Versioned URLs (?v=ASSET_VER) still busted it, so it was
// not the cause of the stale chart that day, but it meant:
//   · any /static/ asset linked WITHOUT ?v was frozen on a device forever
//   · every past ASSET_VER stayed in the cache permanently, so the store grew
//     with each deploy and nothing ever removed it
// A phone that cannot be talked out of an old build is the worst possible
// failure mode for a dashboard someone trades from, so the rule is now what
// the comment always said: icons and the manifest, nothing else.
const CACHE = 'wolf-static-v2';

// Precisely the installability assets. Extensions, not a path prefix — a
// prefix is what quietly swallowed the app's code.
const CACHEABLE = /\.(png|ico|svg|webmanifest)$/i;

self.addEventListener('install', () => { self.skipWaiting(); });

self.addEventListener('activate', (e) => {
  // Drop v1, which is holding old app.css / wolf_chart.js builds.
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (url.pathname.startsWith('/static/') && CACHEABLE.test(url.pathname)) {
    e.respondWith(
      caches.open(CACHE).then((c) =>
        c.match(e.request).then((hit) =>
          hit || fetch(e.request).then((resp) => { c.put(e.request, resp.clone()); return resp; })
        )
      )
    );
  }
  // Everything else — HTML, APIs, CSS, JS — goes to the network, always.
});
