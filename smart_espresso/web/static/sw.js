// The dashboard's shell, cached so the page opens instantly on the LAN and
// still opens (slightly stale, clearly marked so by the page itself) when
// the machine's server hiccups. Sensor data is never cached: /api/* always
// goes to the network.
const CACHE = 'espresso-shell-v10';
const SHELL = [
  '/',
  '/dashboard_logic.js',
  '/manifest.webmanifest',
  '/icon.svg',
  '/icon-192.png',
  '/icon-512.png',
  '/apple-touch-icon.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches
      .open(CACHE)
      .then((cache) =>
        // addAll fails as a batch if one request fails; cache the shell
        // one file at a time so a missing icon cannot break the page.
        Promise.allSettled(SHELL.map((url) => cache.add(url)))
      )
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key)))
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);

  // Live readings only: a cached status response is a lie.
  if (url.origin === self.location.origin && url.pathname.startsWith('/api/')) return;

  // Webfonts: the machine's network may have no internet, so once a font
  // is in the cache it is served from there and never re-fetched — which
  // is exactly why a bad response must never enter the cache.
  if (url.hostname === 'fonts.googleapis.com' || url.hostname === 'fonts.gstatic.com') {
    event.respondWith(
      caches.match(request).then(
        (hit) =>
          hit ||
          fetch(request).then((response) => {
            if (!response.ok) return response;
            const copy = response.clone();
            // Anchor the write to the response we hand back, so the worker
            // cannot be terminated before the font is in the cache.
            return caches
              .open(CACHE)
              .then((cache) => cache.put(request, copy))
              .catch(() => {}) // a failed put must not surface as a rejection
              .then(() => response);
          })
      )
    );
    return;
  }

  // Page loads: try the network so deploys show up, fall back to the
  // cached shell when the machine cannot answer.
  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request)
        .then((response) => {
          if (!response.ok) {
            // A reachable-but-erroring server must not put an error page
            // on screen while the last good shell sits in the cache.
            return caches.match('/').then((shell) => shell || response);
          }
          // Only a good response for the shell may live under '/': a
          // direct hit on another file must not masquerade as the page.
          if (new URL(request.url).pathname !== '/') return response;
          const copy = response.clone();
          // Anchor the write to the response we hand back, so the worker
          // cannot be terminated before the shell is in the cache.
          return caches
            .open(CACHE)
            .then((cache) => cache.put('/', copy))
            .catch(() => {})
            .then(() => response);
        })
        .catch(() => caches.match('/').then((hit) => hit || Response.error()))
    );
    return;
  }

  // Everything else from this origin: cached first, refreshed in the
  // background so the next load is current.
  if (url.origin === self.location.origin) {
    event.respondWith(
      caches.match(request).then((hit) => {
        const refresh = fetch(request)
          .then((response) => {
            if (!response.ok) return response;
            const copy = response.clone();
            // The put rides inside refresh itself, so on a cache miss the
            // response handed to respondWith covers the write too.
            return caches
              .open(CACHE)
              .then((cache) => cache.put(request, copy))
              .catch(() => {})
              .then(() => response);
          })
          .catch(() => hit);
        // The cached response goes back now, but the revalidation must
        // still finish: keep the worker alive until the put has landed.
        if (hit) event.waitUntil(refresh.then(() => {}, () => {}));
        return hit || refresh;
      })
    );
  }
});
