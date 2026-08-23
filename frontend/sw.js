// Mutemo Desk service worker.
//
// Two jobs, deliberately kept separate:
// 1. App-shell caching so the page itself (and its icons) still loads
//    when offline or on a flaky connection -- network-first for the
//    shell (see the fetch handler below for why cache-first was wrong),
//    network-only for everything under /api/ (data must always be
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
//
// v2: was cache-first for "/" -- correct for offline access, but it meant
// a browser that had this worker installed never saw a newer index.html
// after a deploy, ever, because a service worker only re-fetches its own
// script (and re-runs install) when that script's bytes change; this
// file hadn't changed since the shell was first cached, so "/" stayed
// frozen at whatever was cached back then even after later deploys
// shipped real UI changes (confirmed live: the AML/KYC compliance UI was
// fully deployed and being served correctly, but browsers already
// running the old worker kept rendering the pre-compliance page). The
// cache name bump below forces every existing installation to discard
// that stale entry the moment this file updates.
const SHELL_CACHE = "mutemo-shell-v2";
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

  // Network-first, cache as the offline/flaky-connection fallback only --
  // deliberately not cache-first (see the v2 note above for the outage
  // that caused). A successful network response also refreshes the
  // cached copy, so the offline fallback doesn't itself go stale forever.
  event.respondWith(
    fetch(event.request)
      .then((response) => {
        const copy = response.clone();
        caches.open(SHELL_CACHE).then((cache) => cache.put(event.request, copy));
        return response;
      })
      .catch(() => caches.match(event.request))
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
