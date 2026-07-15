/* LeyLineBook PWA Service Worker — 网络优先，离线回退缓存 */
const CACHE = "leylinebook-shell-v2";
const SHELL = [
  ".",
  "index.html",
  "app.js",
  "local-backend.js",
  "styles.css",
  "manifest.webmanifest",
  "icon.svg",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  // /api/* 不缓存（由 IndexedDB 本地后端处理，不会真的发到网络）
  if (new URL(req.url).pathname.includes("/api/")) return;
  // 应用外壳：网络优先，成功则顺带刷新缓存；离线时回退缓存
  event.respondWith(
    fetch(req).then((res) => {
      if (res && res.status === 200 && res.type === "basic") {
        const clone = res.clone();
        caches.open(CACHE).then((c) => c.put(req, clone));
      }
      return res;
    }).catch(() => caches.match(req).then((cached) => cached || caches.match("index.html")))
  );
});
