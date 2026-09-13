/* Keep a complete shell version until all of its pages have closed. */
const CACHE_PREFIX = `leylinebook-shell:${self.registration.scope}:`;
// Bump this revision whenever any SHELL resource changes.
const CACHE = `${CACHE_PREFIX}v7`;
const COMPLETE = "__shell_complete__";
const SHELL = [
  "index.html",
  "app.js",
  "time-contract.js",
  "local-backend.js",
  "styles.css",
  "manifest.webmanifest",
  "icon.svg",
];

function shellUrl(name) {
  return new URL(name, self.registration.scope).href;
}

function isShellResponse(name, response, network = true) {
  if (!response || !response.ok || response.redirected || (network && response.type !== "basic")) return false;
  const type = (response.headers.get("Content-Type") || "").split(";", 1)[0].trim();
  if (name.endsWith(".js")) return /^(text|application)\/(javascript|ecmascript)$/.test(type);
  if (name.endsWith(".css")) return type === "text/css";
  if (name.endsWith(".html")) return type === "text/html";
  return type !== "text/html";
}

self.addEventListener("install", (event) => {
  event.waitUntil((async () => {
    if (await caches.has(CACHE)) {
      const existing = await caches.open(CACHE);
      const complete = await Promise.all(SHELL.map(async (name) =>
        isShellResponse(name, await existing.match(shellUrl(name)), false)));
      // Complete caches may belong to an active/waiting worker, including older
      // workers without our marker. Reuse them without any write or deletion.
      if (complete.every(Boolean)) return;
      if (await existing.match(shellUrl(COMPLETE))) throw new Error("Published shell is incomplete; refusing to overwrite it");
      // Termination can bypass catch after open/put. Only unfinished candidates
      // can be rebuilt; a complete or previously published shell stays untouched.
      await caches.delete(CACHE);
    }
    const responses = await Promise.all(SHELL.map(async (name) => {
      const response = await fetch(new Request(shellUrl(name), { cache: "reload" }));
      if (!isShellResponse(name, response)) throw new Error(`Invalid shell response: ${name}`);
      // Consume each body before waiting for other headers, so large scripts do
      // not hold every HTTP connection while the remaining requests are queued.
      const body = await response.arrayBuffer();
      return new Response(body, { status: response.status, statusText: response.statusText, headers: response.headers });
    }));
    try {
      const cache = await caches.open(CACHE);
      for (let i = 0; i < SHELL.length; i++) await cache.put(shellUrl(SHELL[i]), responses[i]);
      await cache.put(shellUrl(COMPLETE), new Response("complete"));
    } catch (error) {
      await caches.delete(CACHE);
      throw error;
    }
    // No skipWaiting: an old page must keep its matching worker and shell.
  })());
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys
      .filter((key) => key !== CACHE && (key.startsWith(CACHE_PREFIX) || /^leylinebook-shell-v\d+$/.test(key)))
      .map((key) => caches.delete(key))))
  );
  // No clients.claim: first-load pages may have loaded a different shell.
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const scope = new URL(self.registration.scope);
  const url = new URL(request.url);
  if (url.origin !== scope.origin || !url.pathname.startsWith(scope.pathname)) return;
  const name = url.pathname.slice(scope.pathname.length);
  if (name.startsWith("api/")) return;

  const shellName = name === "" ? "index.html" : name;
  if (SHELL.includes(shellName)) {
    event.respondWith((async () => {
      const cache = await caches.open(CACHE);
      // Missing cache entries fail closed, never mix in another deployed version.
      return (await cache.match(shellUrl(shellName))) || Response.error();
    })());
    return;
  }
  // Unknown resources are network-only, with no HTML navigation fallback.
  event.respondWith(fetch(request));
});
