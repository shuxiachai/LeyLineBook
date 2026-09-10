"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const { chromium } = require("playwright");
const { spawn } = require("node:child_process");
const { once } = require("node:events");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { randomBytes } = require("node:crypto");

test("desktop and PWA browser workflows", { timeout: 120000 }, async (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "leylinebook-browser-tests-"));
  const token = randomBytes(32).toString("hex");
  const server = spawn(process.env.PYTHON || "python", ["-B", "-X", "utf8", "scripts/browser_test_server.py"], {
    cwd: path.join(__dirname, ".."), windowsHide: true,
    env: { ...process.env, LEYLINEBOOK_DATA_DIR: directory, LEYLINEBOOK_SESSION_TOKEN: token },
  });
  let stderr = "";
  server.stderr.on("data", (data) => { stderr += data.toString(); });
  let browser;
  try {
    const port = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error(`Test server timed out: ${stderr}`)), 15000);
      let output = "";
      server.stdout.on("data", (chunk) => {
        output += chunk.toString();
        const match = /TEST_SERVER=(\{[^\r\n]+\})/.exec(output);
        if (match) { clearTimeout(timer); resolve(JSON.parse(match[1]).port); }
      });
      server.once("error", (error) => { clearTimeout(timer); reject(error); });
      server.once("exit", () => { clearTimeout(timer); reject(new Error(stderr)); });
    });
    const base = `http://127.0.0.1:${port}`;
    browser = await chromium.launch({ channel: process.env.PLAYWRIGHT_CHANNEL || undefined });
    fs.mkdirSync(path.join(__dirname, "../output/playwright"), { recursive: true });

    for (const pwa of process.env.LEYLINEBOOK_PWA_ONLY ? [true] : [false, true]) {
      await t.test(pwa ? "PWA mobile" : "desktop browser", async () => {
        const context = await browser.newContext({ viewport: pwa ? { width: 390, height: 844 } : { width: 1440, height: 920 }, timezoneId: "Australia/Sydney" });
        const page = await context.newPage();
        page.setDefaultTimeout(10000);
        const errors = [];
        page.on("pageerror", (error) => errors.push(error.message));
        await page.route("**/api/update/check", (route) => route.fulfill({ json: { success: true, data: { hasUpdate: false, current: "test", latest: "test" } } }));
        await page.clock.install({ time: new Date("2026-09-10T12:00:00+10:00") });
        await page.clock.pauseAt(new Date("2026-09-10T12:00:00+10:00"));
        await page.goto(pwa ? `${base}/?local=1` : `${base}/#session=${token}`);
        await page.waitForFunction(() => state.data && !state.loadingDate);
        assert.equal(await page.evaluate(() => location.hash), "");

        await page.locator('[data-view="accounts"]').click();
        await page.locator("#managePlans").click();
        await page.locator("#carePlanName").fill("Browser plan");
        await page.locator('[data-plan-task="体力"]').click();
        await page.locator("#carePlanSaveBtn").click();
        await page.waitForFunction(() => state.data.carePlans.length === 1);
        await page.locator('#carePlanDialog [data-close-dialog="carePlanDialog"]').last().click();
        await page.locator("#addAccount").click();
        await page.locator("#accountName").fill("Browser account");
        await page.locator("#accountProxyUntil").fill("2099-12-31");
        await page.locator("#accountPlan").selectOption({ label: "Browser plan" });
        await page.locator("#saveAccount").click();
        await page.waitForFunction(() => state.data.accounts.length === 1 && state.data.tasks.some((task) => task.name === "体力"));
        await page.reload();
        await page.waitForFunction(() => state.data?.accounts.length === 1);
        assert.ok(await page.evaluate(() => state.data.tasks.some((task) => task.name === "体力")));

        if (!pwa) {
          const ids = await page.evaluate(async () => {
            const a = state.data.accounts[0].id;
            const b = (await api("/api/accounts", { method: "POST", body: JSON.stringify({ name: "Second", proxyUntil: "2099-12-31" }) })).id;
            for (const [id, username] of [[a, "synthetic-A"], [b, "synthetic-B"]]) await api(`/api/accounts/${id}/credentials`, { method: "PUT", body: JSON.stringify({ username, password: "synthetic-only" }) });
            await loadState();
            return { a, b };
          });
          let releaseA, requestedA;
          const gate = new Promise((resolve) => { releaseA = resolve; });
          const requested = new Promise((resolve) => { requestedA = resolve; });
          await page.route(`**/api/accounts/${ids.a}/credentials`, async (route) => {
            if (route.request().method() === "GET") { requestedA(); await gate; }
            await route.continue();
          });
          await page.locator('[data-view="accounts"]').click();
          await page.locator(`[data-credentials-account="${ids.a}"]`).click();
          await requested;
          await page.locator(`[data-credentials-account="${ids.b}"]`).click();
          await page.waitForFunction(() => document.querySelector("#credentialsUsername").value === "synthetic-B");
          const responseA = page.waitForResponse((response) => response.url().endsWith(`/api/accounts/${ids.a}/credentials`));
          releaseA();
          await responseA;
          await page.locator('#credentialsForm button[type="submit"]').click();
          assert.equal(await page.evaluate(async (id) => (await api(`/api/accounts/${id}/credentials`)).username, ids.b), "synthetic-B");
          // The open flag changes before the queued close listener clears credentials.
          await page.waitForFunction(() => !document.querySelector("#credentialsDialog").open && state.editingCredentialsAccountId === null);
          for (const field of ["#credentialsAccountId", "#credentialsUsername", "#credentialsPassword", "#credentialsNote"]) {
            assert.equal(await page.locator(field).inputValue(), "");
          }

          await page.locator('[data-view="today"]').click();
          let releaseDate, requestedDate;
          const dateGate = new Promise((resolve) => { releaseDate = resolve; });
          const dateRequested = new Promise((resolve) => { requestedDate = resolve; });
          await page.route("**/api/state?date=2026-06-13", async (route) => { requestedDate(); await dateGate; await route.continue(); });
          await page.locator("#selectedDate").fill("2026-06-13");
          await page.locator("#selectedDate").dispatchEvent("change");
          await dateRequested;
          await page.locator("[data-toggle-task]").first().click();
          assert.equal(await page.evaluate(async () => (await api("/api/export")).data.records.length), 0);
          releaseDate();
          await page.waitForFunction(() => state.data.date === "2026-06-13" && !state.loadingDate);
          await page.clock.fastForward(61000);
          assert.equal(await page.locator("#selectedDate").inputValue(), "2026-06-13");
        } else {
          await page.evaluate(() => {
            const handle = LOCAL_BACKEND.handle;
            let release;
            const gate = new Promise((resolve) => { release = resolve; });
            window.releaseStory = release;
            window.storyWrites = 0;
            LOCAL_BACKEND.handle = async (url, options) => {
              if (url === "/api/story-tasks" && options?.method === "POST") { window.storyWrites++; await gate; }
              return handle(url, options);
            };
          });
          await page.locator('[data-view="story"]').click();
          await page.locator("#storyOwner").fill("Browser account");
          await page.locator("#storyName").fill("One story");
          await page.locator("#addStoryTask").evaluate((button) => { button.click(); button.click(); });
          await page.evaluate(() => releaseStory());
          await page.waitForFunction(() => state.data.storyTasks.length === 1);
          assert.equal(await page.evaluate(() => storyWrites), 1);
        }
        await page.locator('[data-view="today"]').click();
        await page.locator("[data-toggle-task]").first().click();
        await page.waitForFunction(() => state.data.dueTasks.some((task) => task.completed));
        const backup = await page.evaluate(() => api("/api/export"));
        await page.evaluate(async (payload) => {
          await api("/api/reset", { method: "POST", body: "{}" });
          await api("/api/import", { method: "POST", body: JSON.stringify(payload) });
          await loadState();
        }, backup);
        assert.ok(await page.evaluate(() => state.data.dueTasks.some((task) => task.completed)));
        await page.evaluate(() => window.scrollTo(0, 0));
        await page.clock.runFor(2500);
        await page.screenshot({ path: path.join(__dirname, `../output/playwright/${pwa ? "pwa" : "desktop"}-regression.png`), fullPage: true });
        assert.deepEqual(errors, []);
        await context.close();
      });
    }
  } finally {
    if (browser) await browser.close();
    const exited = server.exitCode === null ? once(server, "exit") : Promise.resolve();
    server.kill();
    await exited;
    const resolved = path.resolve(directory);
    if (!resolved.startsWith(path.resolve(os.tmpdir()) + path.sep) || !path.basename(resolved).startsWith("leylinebook-browser-tests-")) throw new Error("Unexpected test data path");
    fs.rmSync(resolved, { recursive: true, force: true });
  }
});
