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

test("account creation isolation and actionable legacy time confirmation", { timeout: 120000 }, async (t) => {
  const pwaOnly = Boolean(process.env.LEYLINEBOOK_PWA_ONLY);
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "leyline-account-time-"));
  const token = randomBytes(32).toString("hex");
  const server = spawn(process.env.PYTHON || "python", ["-B", "-X", "utf8", "scripts/browser_test_server.py"], {
    cwd: path.join(__dirname, ".."), windowsHide: true,
    env: { ...process.env, LEYLINEBOOK_DATA_DIR: directory, LEYLINEBOOK_SESSION_TOKEN: token },
  });
  let stderr = "";
  server.stderr.on("data", (data) => { stderr += data.toString(); });
  let browser;
  let releaseA = () => {};
  try {
    const port = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error(`Isolated server timed out: ${stderr}`)), 15000);
      let output = "";
      server.stdout.on("data", (data) => {
        output += data.toString();
        const match = /TEST_SERVER=(\{[^\r\n]+\})/.exec(output);
        if (match) { clearTimeout(timer); resolve(JSON.parse(match[1]).port); }
      });
      server.once("error", (error) => { clearTimeout(timer); reject(error); });
      server.once("exit", () => { clearTimeout(timer); reject(new Error(stderr)); });
    });
    const base = `http://127.0.0.1:${port}`;
    browser = await chromium.launch({ channel: process.env.PLAYWRIGHT_CHANNEL || undefined });
    const screenshots = path.join(__dirname, "../output/playwright");
    fs.mkdirSync(screenshots, { recursive: true });
    const context = await browser.newContext({ timezoneId: "Australia/Sydney", viewport: { width: 1280, height: 900 } });
    const page = await context.newPage();
    page.setDefaultTimeout(10000);
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/api/update/check", (route) => route.fulfill({ json: { success: true, data: { hasUpdate: false, current: "test" } } }));
    if (!pwaOnly) {
      await page.goto(`${base}/#session=${token}`);
      await page.waitForFunction(() => state.data && !state.loadingDate);
    }

    await t.test("delayed A creation never reads B credentials or closes B dialog", { skip: pwaOnly }, async () => {
      let requestedA;
      let createdAId;
      const requested = new Promise((resolve) => { requestedA = resolve; });
      const gate = new Promise((resolve) => { releaseA = resolve; });
      await page.route("**/api/accounts", async (route) => {
        if (route.request().method() === "POST" && route.request().postDataJSON().name === "Synthetic A") {
          const response = await route.fetch();
          createdAId = (await response.json()).data.id;
          requestedA();
          await gate;
          await route.fulfill({ response });
        } else await route.continue();
      });
      await page.locator('[data-view="accounts"]').click();
      await page.locator("#addAccount").click();
      await page.locator("#accountName").fill("Synthetic A");
      await page.locator("#accountProxyUntil").fill("2099-12-31");
      await page.locator("#accountCredUsername").fill("synthetic-A-user");
      await page.locator("#accountCredPassword").fill("synthetic-A-password");
      await page.locator("#saveAccount").click();
      await requested;
      await page.locator('#accountDialog [data-close-dialog="accountDialog"]').last().click();
      await page.locator("#addAccount").click();
      await page.locator("#accountName").fill("Synthetic B");
      await page.locator("#accountProxyUntil").fill("2099-12-31");
      await page.locator("#accountCredUsername").fill("synthetic-B-user");
      await page.locator("#accountCredPassword").fill("synthetic-B-password");
      const savedA = page.waitForResponse((response) => response.url().endsWith(`/api/accounts/${createdAId}/credentials`) && response.request().method() === "PUT");
      releaseA();
      await savedA;
      await page.waitForFunction(() => state.data.accounts.some((account) => account.name === "Synthetic A"));
      assert.equal(await page.locator("#accountDialog").evaluate((element) => element.open), true);
      assert.equal(await page.locator("#accountName").inputValue(), "Synthetic B");
      assert.equal(await page.locator("#accountCredPassword").inputValue(), "synthetic-B-password");
      const aCredentials = await page.evaluate(async (id) => api(`/api/accounts/${id}/credentials`), createdAId);
      assert.equal(aCredentials.username, "synthetic-A-user");
      assert.equal(aCredentials.password, "synthetic-A-password");
      await page.locator("#saveAccount").click();
      await page.waitForFunction(() => !document.querySelector("#accountDialog").open && state.data.accounts.some((account) => account.name === "Synthetic B"));
      const bCredentials = await page.evaluate(async () => {
        const id = state.data.accounts.find((account) => account.name === "Synthetic B").id;
        return api(`/api/accounts/${id}/credentials`);
      });
      assert.equal(bCredentials.username, "synthetic-B-user");
      assert.equal(bCredentials.password, "synthetic-B-password");
    });

    for (const pwa of pwaOnly ? [true] : [false, true]) {
      await t.test(pwa ? "mobile PWA time confirmation" : "desktop time confirmation", async () => {
        if (pwa) {
          await page.setViewportSize({ width: 390, height: 844 });
          await page.goto(`${base}/?local=1`);
          await page.waitForFunction(() => state.data && !state.loadingDate);
          assert.equal(await page.evaluate(() => Boolean(window.LOCAL_BACKEND)), true);
        }
        const payload = { accounts: [{ id: 1, name: "Legacy calibrated" }], tasks: [{ id: 1, account_id: 1, name: "质变仪", recurrence: "interval", interval_days: 7, next_due: "2026-10-04T02:30" }], records: [] };
        await page.locator("#importFileInput").setInputFiles({ name: "legacy-time.json", mimeType: "application/json", buffer: Buffer.from(JSON.stringify(payload)) });
        await page.locator("#timeResolutionDialog").waitFor({ state: "visible" });
        assert.equal(await page.locator("#timeSourceZone").inputValue(), "");
        await page.locator("#timeSourceZone").fill("Australia/Sydney");
        assert.match(await page.locator("#timeResolutionError").textContent(), /不存在/);
        await page.locator("#timeResolutionRows input").fill("2026-10-04T03:30");
        assert.equal(await page.locator("#timeResolutionRows select").inputValue(), "2026-10-03T16:30:00Z");
        assert.equal(await page.locator("#timeResolutionDialog").evaluate((element) => element.scrollWidth <= element.clientWidth), true);
        await page.screenshot({ path: path.join(screenshots, pwa ? "time-confirmation-mobile.png" : "time-confirmation-desktop.png"), fullPage: true });
        await page.locator('#timeResolutionForm button[type="submit"]').click();
        await page.locator("#confirmDialogSubmit").click();
        await page.waitForFunction(() => state.data.accounts.some((account) => account.name === "Legacy calibrated"));
        const backup = await page.evaluate(() => api("/api/export"));
        assert.equal(backup.data.tasks[0].next_due, "2026-10-03T16:30:00Z");
        assert.equal(backup.data.timeLegacyArchive[0].entries[0].original, "2026-10-04T02:30");
        await page.evaluate(() => { window.pendingTimeChoice = confirmTimeEntries([{ key: "sample", original: "2026-04-05T02:30", label: "Repeated hour" }], "Australia/Sydney"); });
        await page.locator("#timeResolutionDialog").waitFor({ state: "visible" });
        assert.equal(await page.locator("#timeResolutionRows select").inputValue(), "");
        assert.equal(await page.locator("#timeResolutionRows select option").count(), 3);
        await page.locator("#timeResolutionRows select").selectOption("2026-04-04T16:30:00Z");
        await page.locator('#timeResolutionForm button[type="submit"]').click();
        assert.equal((await page.evaluate(() => window.pendingTimeChoice)).entries[0].offsetMinutes, 600);
      });
      await t.test(pwa ? "PWA precise ordering and import API guards" : "desktop precise ordering and import API guards", async () => {
        const request = (url, method = "GET", body) => page.evaluate(async ({ url, method, body }) => {
          const options = { method, body: body === undefined ? undefined : JSON.stringify(body) };
          // Exercise the backend contract directly, independently of the selected-date UI guard.
          if (window.LOCAL_BACKEND) return window.LOCAL_BACKEND.handle(url, options);
          const response = await fetch(url, { ...options, headers: { "Content-Type": "application/json", "X-LeyLineBook-Session": sessionStorage.getItem("leylinebook-session") } });
          const result = await response.json();
          if (!response.ok || !result.success) throw new Error(`${response.status}: ${result.error}`);
          return result.data;
        }, { url, method, body });
        const snapshot = async () => JSON.stringify((await request("/api/export")).data);
        await request("/api/import", "POST", { accounts: [{ id: 1, name: "Cross zone browser" }], tasks: [{ id: 1, account_id: 1, name: "探索派遣", recurrence: "interval", notes: "派遣:15小时", next_due: "2025-01-01" }], records: [] });
        const task = (await request("/api/export")).data.tasks[0];
        const toggle = (date, completed, usedAt) => request(`/api/tasks/${task.id}/toggle`, "POST", { date, completed, usedAt });
        const empty = await snapshot();
        for (const value of [undefined, "", " ", "\t\n", "invalid"]) {
          const expectedError = value === "invalid" ? (pwa ? /请确认时区后提交带偏移量的使用时间/ : /400: 备份中的日期格式无效/) : /实际使用时间/;
          await assert.rejects(toggle("2025-09-10", true, value), expectedError);
          assert.equal(await snapshot(), empty);
        }
        await toggle("2025-09-13", true, "2025-09-13T04:00:00+14:00");
        const first = await snapshot();
        await assert.rejects(toggle("2025-09-12", true, "2025-09-13T04:59:00Z"), /尚未到期/);
        assert.equal(await snapshot(), first);
        await toggle("2025-09-12", true, "2025-09-12T20:00:00-10:00");
        assert.equal((await request("/api/export")).data.tasks[0].next_due, "2025-09-13T21:00:00Z");
        const second = await snapshot();
        await assert.rejects(toggle("2025-09-13", false), /先撤销/);
        assert.equal(await snapshot(), second);
        await toggle("2025-09-12", false);
        assert.equal((await request("/api/export")).data.tasks[0].next_due, "2025-09-13T05:00:00Z");
        await toggle("2025-09-13", false);
        assert.equal((await request("/api/export")).data.tasks[0].next_due, "2025-01-01");

        const payload = { accounts: [{ id: 1, name: "IANA browser" }], tasks: [{ id: 1, account_id: 1, name: "质变仪", recurrence: "interval", interval_days: 7, next_due: "2026-10-04T02:30" }], records: [] };
        const entry = { key: "tasks/0/next_due", original: "2026-10-04T02:30", local: "2026-10-04T02:30", instant: "2026-10-03T16:30:00Z", offsetMinutes: 600 };
        const beforeImport = await snapshot();
        for (const sourceZone of ["Australia/Sydney", "Not/A_Zone"]) {
          payload.timeResolution = { confirmed: true, sourceZone, entries: [entry] };
          await assert.rejects(request("/api/import", "POST", payload), pwa ? /时区|zone/i : /400:.*时区/);
          assert.equal(await snapshot(), beforeImport);
        }
        payload.tasks[0].next_due = "2026-04-05T02:30";
        for (const [instant, offsetMinutes] of [["2026-04-04T15:30:00Z", 660], ["2026-04-04T16:30:00Z", 600]]) {
          payload.timeResolution = { confirmed: true, sourceZone: "Australia/Sydney", entries: [{ ...entry, original: "2026-04-05T02:30", local: "2026-04-05T02:30", instant, offsetMinutes }] };
          await request("/api/import", "POST", payload);
          const exported = (await request("/api/export")).data;
          assert.equal(exported.tasks[0].next_due, instant);
          assert.deepEqual(exported.timeLegacyArchive[0], payload.timeResolution);
          const current = await request("/api/state?date=2026-04-05");
          assert.equal(current.tasks[0].available_at, instant);
          assert.equal(current.tasks[0].time_semantics, "absolute");
        }
      });
    }
    assert.deepEqual(errors, []);
    await context.close();
  } finally {
    releaseA();
    if (browser) await browser.close();
    if (server.exitCode == null) { const stopped = once(server, "exit"); server.kill(); await stopped; }
    const resolved = path.resolve(directory);
    if (!resolved.startsWith(path.resolve(os.tmpdir()) + path.sep)) throw new Error("Refusing cleanup outside the test temp root");
    fs.rmSync(resolved, { recursive: true, force: true });
  }
});
