"use strict";

// Only the Python ownership gate launches this observer. No browser is launched.
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const { chromium } = require("playwright");

async function main() {
  const [cdpText, apiText, directory, deadlineText] = process.argv.slice(2);
  const cdp = Number(cdpText), api = Number(apiText), deadline = Number(deadlineText);
  assert(Number.isInteger(cdp) && Number.isInteger(api) && cdp !== api);
  assert(![8765, 18765, 9222].some(port => port === cdp || port === api));
  assert(path.isAbsolute(directory) && fs.realpathSync(directory) === directory);
  const origin = `http://127.0.0.1:${api}`;
  const report = { passed: false, launchedBrowser: false, fakeReady: false,
    backgroundUpdateCheckObserved: false, requests: [], errors: [] };
  const timeline = path.join(directory, "cdp-events.jsonl");
  const event = value => fs.appendFileSync(timeline, JSON.stringify({ epochMs: Date.now(), ...value }) + "\n");
  const remaining = () => {
    assert(Date.now() < deadline, "Original outer observation deadline expired");
    return deadline - Date.now();
  };
  const safeUrl = value => {
    try { const url = new URL(value); return url.origin === origin ? url.pathname : url.origin; }
    catch { return "non-url"; }
  };
  const redact = error => String(error).replace(/session=[A-Za-z0-9_-]+/g, "session=[redacted]");
  const timer = setTimeout(() => finish(new Error("Observer deadline expired")), remaining());
  function finish(error) {
    clearTimeout(timer);
    if (error) report.error = redact(error);
    fs.writeFileSync(path.join(directory, "observer.json"), JSON.stringify(report, null, 2));
    // Disconnect sockets by ending this observer, never Browser.close/Page.close.
    process.exit(report.passed ? 0 : 1);
  }
  try {
    const response = await fetch(`http://127.0.0.1:${cdp}/json/list`, {
      redirect: "error", signal: AbortSignal.timeout(remaining()),
    });
    assert(response.ok, "CDP target inventory failed");
    const targets = (await response.json()).filter(target => target.type === "page");
    assert.equal(targets.length, 1, "Expected one isolated WebView2 page");
    const target = targets[0];
    assert(target.url === "about:blank" || new URL(target.url).origin === origin, "Unexpected native page origin");
    const endpoint = new URL(target.webSocketDebuggerUrl);
    assert(endpoint.protocol === "ws:" && ["127.0.0.1", "localhost"].includes(endpoint.hostname)
      && Number(endpoint.port) === cdp && endpoint.pathname.startsWith("/devtools/page/"), "Foreign CDP target");
    endpoint.hostname = "127.0.0.1";
    const socket = new WebSocket(endpoint.href);
    const pending = new Map();
    let sequence = 0, readyId = null, readyResolve;
    const ready = new Promise(resolve => { readyResolve = resolve; });
    await new Promise((resolve, reject) => {
      socket.addEventListener("open", resolve, { once: true });
      socket.addEventListener("error", reject, { once: true });
    });
    socket.addEventListener("message", message => {
      const item = JSON.parse(message.data);
      if (item.id) {
        const promise = pending.get(item.id);
        if (promise) { pending.delete(item.id); item.error ? promise.reject(new Error(item.error.message)) : promise.resolve(item.result); }
        return;
      }
      const p = item.params;
      if (item.method === "Network.requestWillBeSent") {
        const url = new URL(p.request.url);
        if (url.origin === origin && url.pathname === "/api/update/check") {
          report.backgroundUpdateCheckObserved = true;
          event({ type: "normal-background-update-check", method: p.request.method, path: url.pathname });
        }
        if (url.origin === origin && url.pathname === "/api/ready") {
          const frames = p.initiator.stack?.callFrames || [];
          const entry = { method: p.request.method, path: url.pathname, initiator: p.initiator.type,
            scripts: frames.map(frame => safeUrl(frame.url)), timestamp: p.timestamp };
          report.requests.push(entry);
          event({ type: "natural-ready-request", ...entry });
          if (entry.method === "POST" && entry.initiator === "script" && entry.scripts.includes("/app.js")) readyId = p.requestId;
        }
      } else if (item.method === "Network.responseReceived" && p.requestId === readyId) {
        report.readyResponse = { status: p.response.status, path: safeUrl(p.response.url), timestamp: p.timestamp };
        event({ type: "natural-ready-response", ...report.readyResponse });
        readyResolve();
      } else if (item.method === "Runtime.exceptionThrown") {
        report.errors.push(redact(p.exceptionDetails.text));
      }
    });
    const send = (method, params = {}) => new Promise((resolve, reject) => {
      const id = ++sequence;
      pending.set(id, { resolve, reject });
      socket.send(JSON.stringify({ id, method, params }));
    });
    await send("Network.enable");
    await send("Runtime.enable");
    event({ type: "network-observer-armed" });
    const browser = await chromium.connectOverCDP(`http://127.0.0.1:${cdp}`, { timeout: remaining() });
    event({ type: "playwright-connected" });
    await send("Runtime.runIfWaitingForDebugger");
    event({ type: "native-debugger-wait-released" });
    await ready;
    assert.equal(report.readyResponse.status, 200, "Natural ready was not accepted");
    assert.equal(report.requests.length, 1, "First natural ready was absent or duplicated");
    assert.equal(browser.contexts().length, 1);
    const pages = browser.contexts()[0].pages();
    assert.equal(pages.length, 1);
    const page = pages[0];
    assert.equal(new URL(page.url()).origin, origin);
    report.observed = await page.evaluate(() => ({
      webview2: Boolean(window.chrome && window.chrome.webview),
      pwa: Boolean(window.LOCAL_BACKEND),
      loaded: typeof state !== "undefined" && Boolean(state.data && !state.loadingDate),
      selectedDate: document.querySelector("#selectedDate")?.value,
      title: document.title, readyState: document.readyState,
      width: innerWidth, height: innerHeight, bodyCharacters: document.body.innerText.length,
    }));
    assert(report.observed.webview2 && !report.observed.pwa && report.observed.loaded
      && report.observed.selectedDate && report.observed.bodyCharacters > 100
      && report.observed.width >= 800 && report.observed.height >= 400, "Native frontend is not usable");
    assert.equal(report.errors.length, 0, "Native JS exception observed");
    await page.screenshot({ path: path.join(directory, "native-content.png"), timeout: remaining() });
    report.passed = true;
    finish();
  } catch (error) {
    finish(error);
  }
}

main().catch(error => { console.error(String(error).replace(/session=[A-Za-z0-9_-]+/g, "session=[redacted]")); process.exit(1); });
