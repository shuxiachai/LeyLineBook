"use strict";
process.env.TZ = "Australia/Sydney";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { webcrypto } = require("node:crypto");
const { IDBFactory } = require("fake-indexeddb");
const TIME = require("../static/time-contract.js");
const timeSource = fs.readFileSync(path.join(__dirname, "../static/time-contract.js"), "utf8");
const source = fs.readFileSync(path.join(__dirname, "../static/local-backend.js"), "utf8");

function backend(initial = "2027-01-01T12:00:00Z") {
  let now = new Date(initial).getTime();
  class Clock extends Date {
    constructor(...args) { super(...(args.length ? args : [now])); }
    static now() { return now; }
  }
  const context = { Date: Clock, crypto: webcrypto, indexedDB: new IDBFactory(), window: {}, location: { hostname: "test.local", search: "" }, document: { documentElement: { classList: { add() {} } } } };
  vm.runInNewContext(timeSource + "\n" + source, context);
  return {
    api: (url, method = "GET", body) => context.window.LOCAL_BACKEND.handle(url, { method, body: body ? JSON.stringify(body) : undefined }),
    setNow: (value) => { now = new Date(value).getTime(); },
  };
}

test("Sydney spring/fall elapsed cooldowns survive storage and undo", async (t) => {
  for (const [start, hours] of [["2026-03-29T12:00+11:00", 168], ["2026-09-27T12:00+10:00", 168], ["2026-04-04T18:00+11:00", 15], ["2026-04-04T18:00+11:00", 20], ["2026-10-03T18:00+10:00", 15], ["2026-10-03T18:00+10:00", 20]]) {
    await t.test(`${start} + ${hours}h`, async () => {
      const { api } = backend();
      const account = await api("/api/accounts", "POST", { name: "Cooldown" });
      await api(`/api/accounts/${account.id}/task-tags`, "POST", { tag: hours === 168 ? "质变仪" : "探索派遣", enabled: true, notes: hours === 15 ? ["派遣:15小时"] : [] });
      const before = (await api("/api/export")).data.tasks[0];
      await api(`/api/tasks/${before.id}/toggle`, "POST", { date: start.slice(0, 10), completed: true, usedAt: start });
      const after = await api("/api/export");
      assert.equal((new Date(after.data.tasks[0].next_due) - new Date(start)) / 3600000, hours);
      assert.ok(after.data.tasks[0].next_due.endsWith("Z"));
      assert.equal(after.schemaVersion, 4);
      assert.equal(after.timeContractVersion, 2);
      await api(`/api/tasks/${before.id}/toggle`, "POST", { date: start.slice(0, 10), completed: false });
      assert.equal((await api("/api/export")).data.tasks[0].next_due, before.next_due);
    });
  }
});

test("Intl resolution rejects nonexistent times and distinguishes both repeated hours", () => {
  assert.deepEqual(TIME.candidates("2026-10-04T02:30", "Australia/Sydney"), []);
  assert.deepEqual(TIME.candidates("2026-04-05T02:30", "Australia/Sydney"), ["2026-04-04T15:30:00Z", "2026-04-04T16:30:00Z"]);
  assert.deepEqual(TIME.candidates("2026-10-04T03:30", "Australia/Sydney"), ["2026-10-03T16:30:00Z"]);
  assert.throws(() => TIME.instant("2026-02-30T12:00:00Z"));
  assert.throws(() => TIME.instant("2026-04-05T02:30"));
  const second = TIME.instant("2026-04-05T02:30+10:00");
  assert.equal(TIME.utcText(TIME.minuteInstant(second)), "2026-04-04T16:30:00Z");
});

test("PWA expiry boundaries, absolute available_at, and historical batch guard", async () => {
  const { api, setNow } = backend("2026-10-03T18:00+10:00");
  const account = await api("/api/accounts", "POST", { name: "Boundaries" });
  await api(`/api/accounts/${account.id}/task-tags`, "POST", { tag: "探索派遣", enabled: true, notes: ["派遣:15小时"] });
  const task = (await api("/api/export")).data.tasks[0];
  const before = JSON.stringify((await api("/api/export")).data);
  await assert.rejects(api("/api/tasks/complete-all", "POST", { date: "2026-09-10", taskIds: [task.id] }), /实际使用时间/);
  assert.equal(JSON.stringify((await api("/api/export")).data), before);
  await api(`/api/tasks/${task.id}/toggle`, "POST", { date: "2026-10-03", completed: true, usedAt: "2026-10-03T18:00+10:00" });
  for (const [now, remaining] of [["2026-10-03T22:59:59Z", 1], ["2026-10-03T23:00:00Z", 0], ["2026-10-03T23:00:01Z", 0]]) {
    setNow(now);
    const state = await api("/api/state?date=2026-10-04");
    const current = state.tasks.find((row) => row.id === task.id);
    assert.equal(current.available_at, "2026-10-03T23:00:00Z");
    assert.equal(current.cooldown_remaining_seconds, remaining);
    assert.equal(current.time_semantics, "absolute");
    assert.ok(state.dueTasks.some((row) => row.id === task.id));
  }
});

test("legacy import is explicit, calibratable, archived and idempotent across timezones", async () => {
  const { api } = backend();
  const payload = { accounts: [{ id: 1, name: "Legacy" }], tasks: [{ id: 1, account_id: 1, name: "质变仪", recurrence: "interval", interval_days: 7, next_due: "2026-10-04T02:30" }], records: [] };
  await assert.rejects(api("/api/import", "POST", payload), /来源时区/);
  assert.equal((await api("/api/export")).data.accounts.length, 0);
  payload.timeResolution = { confirmed: true, sourceZone: "Australia/Sydney", entries: [{ key: "tasks/0/next_due", original: "2026-10-04T02:30", local: "2026-10-04T03:30", instant: "2026-10-03T16:30:00Z", offsetMinutes: 660 }] };
  await api("/api/import", "POST", payload);
  const exported = await api("/api/export");
  assert.equal(exported.data.tasks[0].next_due, "2026-10-03T16:30:00Z");
  assert.equal(exported.data.timeLegacyArchive[0].entries[0].original, "2026-10-04T02:30");
  try {
    process.env.TZ = "Asia/Shanghai";
    await api("/api/import", "POST", exported);
    assert.equal((await api("/api/export")).data.tasks[0].next_due, exported.data.tasks[0].next_due);
    process.env.TZ = "Australia/Sydney";
    await api("/api/import", "POST", await api("/api/export"));
    assert.equal((await api("/api/export")).data.timeLegacyArchive.length, 1);
  } finally { process.env.TZ = "Australia/Sydney"; }
  assert.equal(payload.tasks[0].next_due, "2026-10-04T02:30");
});

test("null completed_at never replaces data or silently loses a record", async () => {
  const { api } = backend();
  const account = await api("/api/accounts", "POST", { name: "Keep" });
  const before = JSON.stringify((await api("/api/export")).data);
  const payload = { accounts: [{ id: 1, name: "Bad" }], tasks: [{ id: 1, account_id: 1, name: "Daily", recurrence: "daily" }], records: [{ id: 1, task_id: 1, task_date: "2026-09-10", completed_at: null }] };
  await assert.rejects(api("/api/import", "POST", payload), /完成时间不能为空/);
  assert.equal(JSON.stringify((await api("/api/export")).data), before);
  assert.ok(account.id);
});

test("monthly presets include the current cycle before/at/after reset and across year", async () => {
  for (const [now, expected] of [["2026-09-13", "2026-08-16"], ["2026-09-16", "2026-09-16"], ["2026-09-17", "2026-09-16"], ["2026-01-01", "2025-12-16"]]) {
    const { api } = backend(`${now}T12:00+10:00`);
    const account = await api("/api/accounts", "POST", { name: now });
    await api(`/api/accounts/${account.id}/task-tags`, "POST", { tag: "深境螺旋", enabled: true });
    await api(`/api/accounts/${account.id}/task-tags`, "POST", { tag: "幻想真境剧诗", enabled: true });
    const tasks = (await api("/api/export")).data.tasks;
    assert.equal(tasks.find((row) => row.name === "深境螺旋").next_due, expected);
    assert.equal(tasks.find((row) => row.name === "幻想真境剧诗").next_due, `${now.slice(0, 7)}-01`);
  }
});

test("precise order and undo follow instants when business dates run backwards", async (t) => {
  for (const hours of [15, 20, 168]) await t.test(`${hours}h`, async () => {
    const { api } = backend();
    await api("/api/import", "POST", { accounts: [{ id: 1, name: "Cross zone" }], tasks: [{ id: 1, account_id: 1, name: hours === 168 ? "质变仪" : "探索派遣", recurrence: "interval", interval_days: 7, notes: hours === 15 ? "派遣:15小时" : "", next_due: "2026-01-01" }], records: [] });
    const task = (await api("/api/export")).data.tasks[0];
    const toggle = (date, completed, usedAt) => api(`/api/tasks/${task.id}/toggle`, "POST", { date, completed, usedAt });
    const snapshot = async () => JSON.stringify((await api("/api/export")).data);
    const first = new Date("2026-09-12T14:00:00Z").getTime();
    await toggle("2026-09-13", true, "2026-09-13T04:00:00+14:00");
    const firstDue = TIME.utcText(new Date(first + hours * 3600000));
    const before = await snapshot();
    await assert.rejects(toggle("2026-09-12", true, TIME.utcText(new Date(first + hours * 3600000 - 60000))), /尚未到期/);
    assert.equal(await snapshot(), before);
    const second = first + (hours + 1) * 3600000;
    await toggle("2026-09-12", true, TIME.utcText(new Date(second)));
    const after = await snapshot();
    const records = JSON.parse(after).records.sort(TIME.comparePreciseRecords);
    assert.equal(records[0].task_date, "2026-09-12");
    assert.equal(records[0].completed_at, TIME.utcText(new Date(second)));
    assert.equal(records[0].previous_next_due, firstDue);
    for (const [moment, error] of [[first + 3600000, /较新的/], [second + 60000, /尚未到期/]]) {
      await assert.rejects(toggle("2026-09-25", true, TIME.utcText(new Date(moment))), error);
      assert.equal(await snapshot(), after);
    }
    await assert.rejects(toggle("2026-09-13", false), /先撤销/);
    assert.equal(await snapshot(), after);
    await toggle("2026-09-12", false);
    assert.equal((await api("/api/export")).data.tasks[0].next_due, firstDue);
    await toggle("2026-09-13", false);
    assert.equal((await api("/api/export")).data.tasks[0].next_due, "2026-01-01");
  });
});

test("historical blank and invalid usedAt cannot write records or deadlines", async () => {
  for (const name of ["质变仪", "探索派遣"]) {
    const { api } = backend();
    const account = await api("/api/accounts", "POST", { name });
    await api(`/api/accounts/${account.id}/task-tags`, "POST", { tag: name, enabled: true });
    const task = (await api("/api/export")).data.tasks[0];
    const toggle = (date, completed, usedAt) => api(`/api/tasks/${task.id}/toggle`, "POST", { date, completed, usedAt });
    const before = JSON.stringify((await api("/api/export")).data);
    for (const value of [undefined, "", " ", "\t\n", "invalid", "2026-02-30T12:00:00Z"]) {
      await assert.rejects(toggle("2026-09-10", true, value));
      assert.equal(JSON.stringify((await api("/api/export")).data), before);
    }
    for (const value of ["2026-09-10T02:00:00Z", "2026-09-10T12:00:00+10:00"]) {
      await toggle("2026-09-10", true, value);
      assert.equal((await api("/api/export")).data.records[0].completed_at, "2026-09-10T02:00:00Z");
      await toggle("2026-09-10", false);
    }
    await toggle("2027-01-01", true);
    assert.equal((await api("/api/export")).data.records[0].completed_at, "2027-01-01T12:00:00Z");
  }
});

test("calendar tasks retain date-based order and daily historical completion", async () => {
  for (const recurrence of ["daily", "interval", "monthly"]) {
    const { api } = backend();
    await api("/api/import", "POST", { accounts: [{ id: 1, name: "Calendar" }], tasks: [{ id: 1, account_id: 1, name: "Custom", recurrence, interval_days: recurrence === "interval" ? 3 : null, monthly_day: recurrence === "monthly" ? 16 : null, next_due: "2026-09-10" }], records: [] });
    const task = (await api("/api/export")).data.tasks[0];
    await api(`/api/tasks/${task.id}/toggle`, "POST", { date: "2026-09-13", completed: true });
    const before = JSON.stringify((await api("/api/export")).data);
    const operation = api(`/api/tasks/${task.id}/toggle`, "POST", { date: "2026-09-12", completed: true, usedAt: "2026-09-14T00:00:00Z" });
    if (recurrence === "daily") await operation;
    else {
      await assert.rejects(operation, /较新的/);
      assert.equal(JSON.stringify((await api("/api/export")).data), before);
    }
  }
});

test("PWA IANA confirmation rejects gaps and mismatches without replacing data and accepts both folds", async () => {
  const { api } = backend();
  await api("/api/accounts", "POST", { name: "Keep" });
  const payload = { accounts: [{ id: 1, name: "Fold" }], tasks: [{ id: 1, account_id: 1, name: "质变仪", recurrence: "interval", interval_days: 7, next_due: "2026-04-05T02:30" }], records: [] };
  const entry = { key: "tasks/0/next_due", original: "2026-04-05T02:30", local: "2026-04-05T02:30", instant: "2026-04-04T16:30:00Z", offsetMinutes: 600 };
  for (const [sourceZone, choice] of [["Not/A_Zone", entry], ["Australia/Sydney", { ...entry, local: "2026-10-04T02:30", instant: "2026-10-03T16:30:00Z", offsetMinutes: 600 }], ["Australia/Sydney", { ...entry, offsetMinutes: 660 }], ["Asia/Shanghai", entry]]) {
    const before = JSON.stringify((await api("/api/export")).data);
    payload.timeResolution = { confirmed: true, sourceZone, entries: [choice] };
    await assert.rejects(api("/api/import", "POST", payload));
    assert.equal(JSON.stringify((await api("/api/export")).data), before);
  }
  for (const [instant, offsetMinutes] of [["2026-04-04T15:30:00Z", 660], ["2026-04-04T16:30:00Z", 600]]) {
    payload.timeResolution = { confirmed: true, sourceZone: "Australia/Sydney", entries: [{ ...entry, instant, offsetMinutes }] };
    await api("/api/import", "POST", payload);
    const exported = (await api("/api/export")).data;
    assert.equal(exported.tasks[0].next_due, instant);
    assert.equal(JSON.stringify(exported.timeLegacyArchive[0]), JSON.stringify(payload.timeResolution));
  }
});
