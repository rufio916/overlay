// Network first so you always get the latest refresh; falls back to the last copy when offline.
// Pages are re-checked with the server every time (GitHub otherwise lets phones reuse a copy for 10 minutes).
const C = "overlay-v2";
self.addEventListener("install", e => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== C).map(k => caches.delete(k)))).then(() => self.clients.claim())));
self.addEventListener("fetch", e => {
  const u = new URL(e.request.url);
  if (e.request.method !== "GET" || u.origin !== location.origin) return;
  const page = e.request.mode === "navigate" || u.pathname.endsWith(".html") || u.pathname.endsWith("/");
  e.respondWith(fetch(e.request, page ? {cache: "no-cache"} : {}).then(r => { const copy = r.clone(); caches.open(C).then(c => c.put(e.request, copy)); return r; })
    .catch(() => caches.match(e.request, {ignoreSearch: true})));
});
