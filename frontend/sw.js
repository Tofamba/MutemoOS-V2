// Mutemo Desk service worker.
//
// Two jobs, deliberately kept separate:
// 1. App-shell caching so the page itself (and its icons) still loads
//    when offline or on a flaky connection -- cache-first for the static
//    shell, network-only for everything under /api/ (data must always be
//    fresh; a stale cached search result or document list would be worse
//    than an error).
// 2. Registering for Background Sync so a queued capture (see the
//    IndexedDB queue in index.html's flushCaptureQueue()) gets a retry
//    nudge from the browser/OS even if the tab isn't in the foreground
//    when connectivity returns. This is a Chromium-only API (Chrome,
//    Edge, Android WebView) -- Safari/iOS has no Background Sync support
//    at all as of this writing. For iOS, index.html's own 'online' event
//    listener is the real retry mechanism (fires whenever the tab is
//    foregrounded with connectivity restored); the sync event here is a
//    strict bonus on top of that for browsers that support it, not the
//    only path. Known limitation, not silently pretending otherwise.

const SHELL_CACHE = "mutemo-shell-v1";
const SHELL_ASSETS = [
  "/",
  "/manifest.json",
  "/icons/icon-192.png",
  "/icons/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE).then((cache) => cache.addAll(SHELL_ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== SHELL_CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (url.pathname.startsWith("/api/")) return; // never intercept API calls

  event.respondWith(
    caches.match(event.request).then((cached) => {
      if (cached) return cached;
      return fetch(event.request).catch(() => cached);
    })
  );
});

// Fired by the browser when connectivity returns, IF the page registered
// a sync tag while offline (see registerCaptureSync() in index.html) AND
// the browser supports Background Sync. Just tells every open client to
// run its own flush -- the actual upload logic and IndexedDB access stays
// in page JS (a service worker has no access to the page's `API` base
// URL/auth state without duplicating it here).
self.addEventListener("sync", (event) => {
  if (event.tag === "capture-queue-flush") {
    event.waitUntil(
      self.clients.matchAll().then((clients) => {
        clients.forEach((c) => c.postMessage({ type: "flush-capture-queue" }));
      })
    );
  }
});
