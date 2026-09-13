"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const http = require("node:http");
const { once } = require("node:events");
const { chromium } = require("playwright");

const root = path.join(__dirname, "..");
const source = fs.readFileSync(path.join(root, "static/sw.js"), "utf8");
const revision = /const CACHE = `\$\{CACHE_PREFIX\}v(\d+)`;/.exec(source);
assert.ok(revision, "SW must declare an explicit shell revision");
const types = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".webmanifest": "application/manifest+json" };
const files = new Map(["index.html", "app.js", "time-contract.js", "local-backend.js", "styles.css", "manifest.webmanifest", "icon.svg"].map((name) => [name, fs.readFileSync(path.join(root, "static", name), "utf8")]));

function shellBody(name, version) {
  let body = files.get(name);
  if (name === "index.html") body = body.replace("<html ", `<html data-test-shell="${version}" `);
  if (name === "app.js") body = `window.__testShell = ${version};\n` + body;
  return body;
}

async function fixture(t) {
  const state = { version: 0, missing: "", htmlScript: false, offline: false };
  const requests = [];
  t.after(() => t.diagnostic(`Fixture requests: ${JSON.stringify(requests)}`));
  const server = http.createServer((request, response) => {
    const record = { url: request.url, finished: false };
    requests.push(record);
    response.on("finish", () => { record.finished = true; });
    if (state.offline) { request.socket.destroy(); return; }
    const pathname = new URL(request.url, "http://localhost").pathname;
    const name = pathname.replace(/^\/book\//, "") || "index.html";
    response.setHeader("Cache-Control", "no-store");
    if (!pathname.startsWith("/book/")) { response.writeHead(404).end("Not found"); return; }
    if (name === "sw.js") {
      response.setHeader("Content-Type", "text/javascript");
      response.end(source.replace(revision[0], `const CACHE = \`\${CACHE_PREFIX}v${Number(revision[1]) + state.version}\`;`) + `\n// Fixture worker build: ${state.workerBuild || 0}\n`);
      return;
    }
    if (name === state.missing) { response.writeHead(503).end("Fixture unavailable"); return; }
    if (state.htmlScript && name === "app.js") { response.setHeader("Content-Type", "text/html"); response.end("<!doctype html><title>Hosting fallback</title>"); return; }
    if (!files.has(name)) { response.writeHead(404).end("Not found"); return; }
    const body = shellBody(name, state.shellVersion ?? state.version);
    response.setHeader("Content-Type", `${types[path.extname(name)]}; charset=utf-8`);
    response.end(body);
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  t.after(async () => { server.closeAllConnections(); await new Promise((resolve) => server.close(resolve)); });
  return { state, base: `http://127.0.0.1:${server.address().port}/book/` };
}

async function ready(page, url) {
  await page.goto(url);
  await page.waitForFunction(() => typeof state !== "undefined" && state.data && !state.loadingDate);
}

async function waitRegistration(page, predicate, scope) {
  const deadline = Date.now() + 10000;
  let registration;
  do {
    registration = await page.evaluate(async (value) => {
      const current = await navigator.serviceWorker.getRegistration(value);
      return current ? { active: current.active?.state, waiting: current.waiting?.state, installing: current.installing?.state } : null;
    }, scope);
    if (predicate(registration)) return;
    await new Promise((resolve) => setTimeout(resolve, 50));
  } while (Date.now() < deadline);
  throw new Error(`Registration did not reach expected state: ${JSON.stringify(registration)}`);
}

async function installed(page) {
  await waitRegistration(page, (registration) => registration?.active === "activated");
}

async function controlled(page) {
  try {
    await page.waitForFunction(() => navigator.serviceWorker.controller && state.data && !state.loadingDate);
  } catch (error) {
    const details = await page.evaluate(async () => ({
      url: location.href, controller: navigator.serviceWorker.controller?.scriptURL,
      registrations: (await navigator.serviceWorker.getRegistrations()).map((registration) => ({ scope: registration.scope, active: registration.active?.state })),
      caches: await caches.keys(), pwa: Boolean(window.LOCAL_BACKEND),
      data: Boolean(state.data), loading: state.loadingDate, toast: document.querySelector("#toast")?.textContent,
    }));
    throw new Error(`${error.message}\nLifecycle evidence: ${JSON.stringify(details)}`);
  }
}

async function update(page) {
  return page.evaluate(async () => {
    const registration = await navigator.serviceWorker.getRegistration();
    const result = new Promise((resolve) => {
      registration.addEventListener("updatefound", () => {
        const worker = registration.installing;
        const changed = () => {
          if (["installed", "redundant"].includes(worker.state)) resolve(worker.state);
        };
        worker.addEventListener("statechange", changed);
        changed();
      }, { once: true });
    });
    await registration.update();
    return Promise.race([result, new Promise((_, reject) => setTimeout(() => reject(new Error("SW update did not reach a terminal install state")), 10000))]);
  });
}

async function registerInstall(page, script) {
  return page.evaluate(async (url) => {
    const registration = await navigator.serviceWorker.register(url);
    const worker = registration.installing || registration.waiting || registration.active;
    if (!worker) throw new Error("Registration returned no worker to observe");
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("Install did not finish")), 10000);
      const changed = () => {
        if (["installed", "redundant", "activated"].includes(worker.state)) {
          clearTimeout(timer);
          resolve(worker.state);
        }
      };
      worker.addEventListener("statechange", changed);
      changed();
    });
  }, script);
}

async function cacheSnapshot(page, name) {
  return page.evaluate(async (key) => {
    const cache = await caches.open(key);
    const requests = await cache.keys();
    const entries = await Promise.all(requests.map(async (request) => {
      const response = await cache.match(request);
      return { url: request.url, body: await response.text() };
    }));
    return entries.sort((a, b) => a.url.localeCompare(b.url));
  }, name);
}

test("interrupted same-revision shell install can retry without changing an active shell", { timeout: 60000 }, async (t) => {
  for (const withActive of [false, true]) {
    await t.test(withActive ? "previous worker remains active" : "first install has no active worker", async (t) => {
      const { state: server, base } = await fixture(t);
      const browser = await chromium.launch({ channel: process.env.PLAYWRIGHT_CHANNEL || undefined });
      t.after(() => browser.close());
      const context = await browser.newContext();
      const observer = await context.newPage();
      await observer.goto(new URL("../observer", base).href);
      const oldCache = `leylinebook-shell:${base}:v${revision[1]}`;
      let oldPage;
      let oldSnapshot;
      if (withActive) {
        oldPage = await context.newPage();
        oldPage.setDefaultTimeout(10000);
        await ready(oldPage, `${base}?local=1`);
        await installed(oldPage);
        await oldPage.reload();
        await controlled(oldPage);
        await oldPage.evaluate(() => { window.__beforeRetry = navigator.serviceWorker.controller; });
        oldSnapshot = await cacheSnapshot(observer, oldCache);
        server.version = 1;
      }
      const candidateCache = `leylinebook-shell:${base}:v${Number(revision[1]) + server.version}`;
      server.missing = "time-contract.js";
      const failed = withActive ? await update(oldPage) : await registerInstall(observer, `${base}sw.js`);
      assert.equal(failed, "redundant", "a real failed install must precede the retry");

      // Reproduce the durable state of termination after some puts, without
      // relying on a timing-sensitive browser kill or the install catch handler.
      await observer.evaluate(async ({ key, base }) => {
        const cache = await caches.open(key);
        await cache.put(new URL("index.html", base), new Response("incomplete install", { headers: { "Content-Type": "text/html" } }));
        await cache.put(new URL("app.js", base), new Response("incomplete script", { headers: { "Content-Type": "text/javascript" } }));
      }, { key: candidateCache, base });
      assert.equal((await cacheSnapshot(observer, candidateCache)).length, 2);
      server.missing = "";
      const retried = withActive ? await update(oldPage) : await registerInstall(observer, `${base}sw.js`);
      assert.ok((withActive ? ["installed"] : ["installed", "activated"]).includes(retried), `same bytes/revision must recover the partial cache, got ${retried}`);
      if (withActive) {
        assert.deepEqual(await cacheSnapshot(observer, oldCache), oldSnapshot);
        assert.equal(await oldPage.evaluate(() => navigator.serviceWorker.controller === __beforeRetry && __testShell === 0), true);
        await waitRegistration(observer, (registration) => registration?.waiting === "installed", base);
        await oldPage.close();
      }
      await waitRegistration(observer, (registration) => registration?.active === "activated" && !registration.waiting, base);
      const complete = await cacheSnapshot(observer, candidateCache);
      for (const name of files.keys()) {
        assert.equal(complete.find((entry) => entry.url === new URL(name, base).href)?.body, shellBody(name, server.version), `${name} must be complete after retry`);
      }
      await context.setOffline(true);
      const fresh = await context.newPage();
      fresh.setDefaultTimeout(10000);
      await ready(fresh, `${base}?local=1`);
      assert.equal(await fresh.evaluate(() => __testShell), server.version);
      assert.equal(await fresh.getAttribute("html", "data-test-shell"), String(server.version));
      await context.close();
    });
  }
});

test("same-revision replacement reuses a complete active legacy cache without writing it", { timeout: 30000 }, async (t) => {
  const { state: server, base } = await fixture(t);
  const browser = await chromium.launch({ channel: process.env.PLAYWRIGHT_CHANNEL || undefined });
  t.after(() => browser.close());
  const context = await browser.newContext();
  const page = await context.newPage();
  page.setDefaultTimeout(10000);
  await ready(page, `${base}?local=1`);
  await installed(page);
  await page.reload();
  await controlled(page);
  const key = `leylinebook-shell:${base}:v${revision[1]}`;
  await page.evaluate(async ({ key, base }) => {
    window.__originalController = navigator.serviceWorker.controller;
    const cache = await caches.open(key);
    await cache.delete(new URL("__shell_complete__", base));
  }, { key, base });
  const before = await cacheSnapshot(page, key);
  assert.equal(before.length, files.size, "legacy cache has all resources but no completion marker");
  server.workerBuild = 1;
  server.shellVersion = 99;
  assert.equal(await update(page), "installed");
  assert.deepEqual(await cacheSnapshot(page, key), before, "an active complete cache must remain byte-for-byte unchanged");
  assert.equal(await page.evaluate(() => navigator.serviceWorker.controller === __originalController), true);
  await context.setOffline(true);
  await page.reload();
  await controlled(page);
  assert.equal(await page.evaluate(() => __testShell), 0);
  assert.equal(await page.getAttribute("html", "data-test-shell"), "0");
  await context.close();
});

test("warm PWA reopens offline after a real browser restart; first install does not claim", { timeout: 60000 }, async (t) => {
  const { state: server, base } = await fixture(t);
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "leylinebook-sw-profile-"));
  const options = { channel: process.env.PLAYWRIGHT_CHANNEL || undefined, viewport: { width: 390, height: 844 } };
  let context;
  try {
    context = await chromium.launchPersistentContext(profile, options);
    context.on("serviceworker", (worker) => worker.on("console", (message) => t.diagnostic(`SW: ${message.text()}`)));
    const page = await context.newPage();
    page.on("console", (message) => { if (message.type() === "error") t.diagnostic(message.text()); });
    page.on("requestfailed", (request) => t.diagnostic(`Request failed: ${request.url()} ${request.failure()?.errorText}`));
    page.setDefaultTimeout(10000);
    await ready(page, `${base}?local=1`);
    await installed(page);
    assert.equal(await page.evaluate(() => navigator.serviceWorker.controller), null);
    await page.reload();
    await controlled(page);
    await page.evaluate(async () => {
      await api("/api/accounts", { method: "POST", body: JSON.stringify({ name: "Isolated offline account", proxyUntil: "2099-12-31" }) });
      await loadState();
    });
    await context.close();
    context = undefined;
    server.offline = true;
    context = await chromium.launchPersistentContext(profile, { ...options, offline: true });
    const reopened = await context.newPage();
    reopened.setDefaultTimeout(10000);
    const errors = [];
    reopened.on("pageerror", (error) => errors.push(error.message));
    await ready(reopened, `${base}?local=1`);
    assert.equal(await reopened.evaluate(() => Boolean(navigator.serviceWorker.controller)), true);
    assert.equal(await reopened.evaluate(() => state.data.accounts[0].name), "Isolated offline account");
    assert.equal(await reopened.evaluate(() => __testShell), 0);
    const missing = await reopened.evaluate(async () => {
      try { const response = await fetch("missing-script.js"); return { status: response.status, text: await response.text() }; }
      catch { return { failed: true }; }
    });
    assert.deepEqual(missing, { failed: true }, "script fetch must fail, never return the HTML shell");
    const output = path.join(root, "output/playwright");
    fs.mkdirSync(output, { recursive: true });
    await reopened.screenshot({ path: path.join(output, "sw-offline-reopen.png"), fullPage: true });
    assert.deepEqual(errors, []);
  } finally {
    if (context) await context.close();
    const resolved = path.resolve(profile);
    assert.ok(resolved.startsWith(path.resolve(os.tmpdir()) + path.sep) && path.basename(resolved).startsWith("leylinebook-sw-profile-"));
    fs.rmSync(resolved, { recursive: true, force: true });
  }
});

test("new shell waits for every old page; incomplete installs cannot replace it", { timeout: 60000 }, async (t) => {
  const { state: server, base } = await fixture(t);
  const browser = await chromium.launch({ channel: process.env.PLAYWRIGHT_CHANNEL || undefined });
  t.after(() => browser.close());
  const context = await browser.newContext();
  const page = await context.newPage();
  page.setDefaultTimeout(10000);
  await ready(page, `${base}?local=1`);
  await installed(page);
  await page.reload();
  await controlled(page);
  const second = await context.newPage();
  await ready(second, `${base}?local=1`);
  await page.evaluate(async (otherScope) => {
    window.__originalController = navigator.serviceWorker.controller;
    window.__controllerChanges = 0;
    navigator.serviceWorker.addEventListener("controllerchange", () => window.__controllerChanges++);
    await caches.open("unrelated-project");
    await caches.open(`leylinebook-shell:${otherScope}:v1`);
  }, base.replace("/book/", "/other/"));

  server.version = 1;
  assert.equal(await update(page), "installed");
  assert.equal(await page.evaluate(async () => Boolean((await navigator.serviceWorker.getRegistration()).waiting)), true);
  assert.equal(await page.evaluate(() => navigator.serviceWorker.controller === __originalController && __controllerChanges === 0), true);
  const oldScript = await page.evaluate(() => fetch("app.js?cache-bust=new").then((response) => response.text()));
  assert.ok(oldScript.startsWith("window.__testShell = 0;"));
  await page.reload();
  assert.equal(await page.getAttribute("html", "data-test-shell"), "0");
  assert.equal(await page.evaluate(() => __testShell), 0);
  await page.close();
  assert.equal(await second.evaluate(async () => Boolean((await navigator.serviceWorker.getRegistration()).waiting)), true);
  // Keep the registration handle on an out-of-scope page while closing the last client.
  const observer = await context.newPage();
  await observer.goto(new URL("../observer", base).href);
  await second.close();
  await waitRegistration(observer, (registration) => registration?.active === "activated" && !registration.waiting, base);
  const fresh = await context.newPage();
  fresh.setDefaultTimeout(10000);
  await ready(fresh, `${base}?local=1`);
  assert.equal(await fresh.getAttribute("html", "data-test-shell"), "1");
  assert.equal(await fresh.evaluate(() => __testShell), 1);
  const keys = await fresh.evaluate(() => caches.keys());
  assert.ok(keys.includes("unrelated-project"));
  assert.ok(keys.includes(`leylinebook-shell:${base.replace("/book/", "/other/")}:v1`));
  assert.ok(!keys.includes(`leylinebook-shell:${base}:v${revision[1]}`));

  for (const failure of ["missing", "html"]) {
    server.version++;
    server.missing = failure === "missing" ? "time-contract.js" : "";
    server.htmlScript = failure === "html";
    assert.equal(await update(fresh), "redundant", `install must reject ${failure} shell`);
    assert.equal(await fresh.evaluate(async () => Boolean((await navigator.serviceWorker.getRegistration()).waiting)), false);
    await fresh.reload();
    assert.equal(await fresh.getAttribute("html", "data-test-shell"), "1");
    assert.equal(await fresh.evaluate(() => __testShell), 1);
  }
  await context.setOffline(true);
  await fresh.reload();
  await fresh.waitForFunction(() => state.data && !state.loadingDate);
  assert.equal(await fresh.evaluate(() => __testShell), 1);
  await context.close();
});
