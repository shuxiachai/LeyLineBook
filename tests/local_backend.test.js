// 手机版 PWA 业务逻辑（static/local-backend.js）的回归测试。
// 运行：npm install && npm test（需要 fake-indexeddb，仅开发期依赖，不影响桌面/移动端运行时）。
"use strict";

process.env.TZ = "Australia/Sydney";

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const fs = require("node:fs");
const vm = require("node:vm");
const RealDate = Date;
const frozenNow = new RealDate("2026-09-10T12:00:00+10:00").getTime();
global.Date = class extends RealDate {
  constructor(...args) { super(...(args.length ? args : [frozenNow])); }
  static now() { return frozenNow; }
};

require("fake-indexeddb/auto");

// local-backend.js 依赖的最小浏览器环境垫片
global.location = { hostname: "test.local", search: "" };
global.window = global;
global.document = { documentElement: { classList: { add() {} } } };

const SOURCE = fs.readFileSync(
  path.join(__dirname, "..", "static", "local-backend.js"),
  "utf8"
);
const PYTHON_SOURCE = fs.readFileSync(path.join(__dirname, "..", "app.py"), "utf8");
const DESKTOP_VERSION = PYTHON_SOURCE.match(/APP_VERSION\s*=\s*"(\d+\.\d+\.\d+)"/)?.[1];
const SCHEDULING_CASES = JSON.parse(fs.readFileSync(
  path.join(__dirname, "fixtures", "scheduling_cases.json"),
  "utf8"
));
// eslint-disable-next-line no-eval -- local-backend.js 是浏览器脚本，无法直接 require
eval(SOURCE);

const BE = global.LOCAL_BACKEND;
assert.ok(BE, "LOCAL_BACKEND 未激活，local-backend.js 的域名门控逻辑可能已改动");

function api(p, method, body) {
  return BE.handle(p, { method, body: body ? JSON.stringify(body) : undefined });
}

function seedLegacyCredentials() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open("leylinebook", 1);
    request.onupgradeneeded = () => {
      request.result.createObjectStore("accounts", { keyPath: "id" });
    };
    request.onerror = () => reject(request.error);
    request.onsuccess = () => {
      const db = request.result;
      const transaction = db.transaction("accounts", "readwrite");
      transaction.objectStore("accounts").put({
        id: "legacy-account",
        name: "旧版凭据号",
        active: 1,
        deleted: 0,
        sort_order: 0,
        credentials: { username: "plain-user", password: "plain-password", note: "plain-note" },
      });
      transaction.oncomplete = () => { db.close(); resolve(); };
      transaction.onerror = () => reject(transaction.error);
    };
  });
}

function readRawAccount(id) {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open("leylinebook");
    request.onerror = () => reject(request.error);
    request.onsuccess = () => {
      const db = request.result;
      const getRequest = db.transaction("accounts", "readonly").objectStore("accounts").get(id);
      getRequest.onsuccess = () => { db.close(); resolve(getRequest.result); };
      getRequest.onerror = () => { db.close(); reject(getRequest.error); };
    };
  });
}

function simpleBackup(name, id = 1) {
  return {
    accounts: [{ id, name, active: 1, deleted: 0, sort_order: 0, created_at: "2026-01-01T00:00:00" }],
    tasks: [], records: [], customTags: [], carePlans: [], storyTasks: [], groupNotes: [],
  };
}

function gameToday() {
  const d = new Date();
  d.setHours(d.getHours() - 4);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function addDaysStr(s, n) {
  const [y, m, d] = s.split("-").map(Number);
  const dt = new Date(y, m - 1, d + n);
  const pad = (x) => String(x).padStart(2, "0");
  return `${dt.getFullYear()}-${pad(dt.getMonth() + 1)}-${pad(dt.getDate())}`;
}

test("PWA v1 upgrade removes legacy credentials", async () => {
  await seedLegacyCredentials();
  const backup = await api("/api/export", "GET");
  const account = await readRawAccount("legacy-account");
  assert.equal(backup.format, "leylinebook-backup");
  assert.equal(backup.schemaVersion, 3);
  assert.equal(backup.appVersion, DESKTOP_VERSION);
  assert.equal(Object.hasOwn(account, "credentials"), false);
  await assert.rejects(api("/api/accounts/legacy-account/credentials", "GET"), /手机版不保存账号凭据/);
  await api("/api/reset", "POST", {});
});

function secondTab() {
  const context = { window: {}, location: global.location, document: global.document, indexedDB, IDBKeyRange, crypto: global.crypto, Date };
  vm.runInNewContext(SOURCE, context);
  return (path, method, body) => context.window.LOCAL_BACKEND.handle(path, { method, body: JSON.stringify(body) });
}

test("PWA concurrent tabs serialize read-check-write and commit before success", async () => {
  await api("/api/reset", "POST", {});
  const other = secondTab();
  const attempts = await Promise.allSettled([
    api("/api/accounts", "POST", { name: "Concurrent", dailyTask: "Daily" }),
    other("/api/accounts", "POST", { name: "Concurrent", dailyTask: "Daily" }),
  ]);
  assert.equal(attempts.filter((result) => result.status === "fulfilled").length, 1);
  const backup = await api("/api/export", "GET");
  const task = backup.data.tasks[0];
  await Promise.all([
    api(`/api/tasks/${task.id}/toggle`, "POST", { date: gameToday(), completed: true }),
    other(`/api/tasks/${task.id}/toggle`, "POST", { date: gameToday(), completed: true }),
  ]);
  assert.equal((await api("/api/export", "GET")).data.records.length, 1);
  await api("/api/reset", "POST", {});
});

test("PWA request success followed by transaction abort rejects the API", async () => {
  await api("/api/reset", "POST", {});
  const original = IDBObjectStore.prototype.put;
  let requestSucceeded = false;
  IDBObjectStore.prototype.put = function (...args) {
    const request = original.apply(this, args);
    if (this.name === "care_plans") {
      const transaction = this.transaction;
      request.addEventListener("success", () => { requestSucceeded = true; transaction.abort(); });
    }
    return request;
  };
  try { await assert.rejects(api("/api/care-plans", "POST", { name: "Abort", tasks: ["体力"] }), /abort|写入失败/i); }
  finally { IDBObjectStore.prototype.put = original; }
  assert.equal(requestSucceeded, true);
  assert.equal((await api("/api/export", "GET")).data.carePlans.length, 0);
});

test("PWA enum validation rejects inherited object properties", async () => {
  await api("/api/reset", "POST", {});
  for (const value of ["__proto__", "constructor", "toString"]) {
    await assert.rejects(api("/api/care-plans", "POST", { name: "Invalid", tasks: [value] }));
    await assert.rejects(api("/api/custom-tags", "POST", { name: "Invalid", category: value, durationDays: 10 }));
    await assert.rejects(api("/api/story-tasks", "POST", { name: "Invalid", ownerName: "Owner", taskType: value }));
  }
  const data = (await api("/api/export", "GET")).data;
  assert.equal(data.carePlans.length + data.customTags.length + data.storyTasks.length, 0);
});

test("PWA backup validation shares the desktop contract and preserves settings", async () => {
  const fixtures = JSON.parse(fs.readFileSync(path.join(__dirname, "fixtures/backup_cases.json"), "utf8"));
  await api("/api/import", "POST", fixtures.valid);
  const before = (await api("/api/export", "GET")).data;
  for (const example of fixtures.invalid) {
    const payload = structuredClone(fixtures.valid);
    const row = example.collection === "settings" ? payload.settings : payload[example.collection][0];
    row[example.field] = example.value;
    await assert.rejects(api("/api/import", "POST", payload), `should reject ${JSON.stringify(example)}`);
    assert.deepEqual((await api("/api/export", "GET")).data, before);
  }
  const backup = await api("/api/export", "GET");
  await api("/api/reset", "POST", {});
  await api("/api/import", "POST", backup);
  assert.equal((await api("/api/settings", "GET")).versionAnchorDate, "2026-06-03");
  backup.schemaVersion = 2;
  delete backup.data.settings;
  await api("/api/import", "POST", backup);
  assert.equal((await api("/api/settings", "GET")).versionAnchorDate, "2026-05-20");
  await api("/api/reset", "POST", {});
});

test("PWA historical undo and multiple expeditions preserve newer schedules", async () => {
  await api("/api/reset", "POST", {});
  const account = await api("/api/accounts", "POST", { name: "Cycles" });
  await api(`/api/accounts/${account.id}/task-tags`, "POST", { tag: "壶", enabled: true });
  const pot = (await api("/api/export", "GET")).data.tasks[0];
  await api(`/api/tasks/${pot.id}/toggle`, "POST", { date: "2026-06-01", completed: true });
  await api(`/api/tasks/${pot.id}/toggle`, "POST", { date: "2026-06-04", completed: true });
  const before = (await api("/api/export", "GET")).data.tasks[0].next_due;
  await assert.rejects(api(`/api/tasks/${pot.id}/toggle`, "POST", { date: "2026-06-01", completed: false }), /较新/);
  assert.equal((await api("/api/export", "GET")).data.tasks[0].next_due, before);
  await api(`/api/accounts/${account.id}/task-tags`, "POST", { tag: "探索派遣", enabled: true, notes: ["派遣:15小时"] });
  const expedition = (await api("/api/export", "GET")).data.tasks.find((task) => task.name === "探索派遣");
  for (const usedAt of ["2026-06-14T04:10", "2026-06-14T04:10", "2026-06-14T19:20"]) {
    await api(`/api/tasks/${expedition.id}/toggle`, "POST", { date: "2026-06-14", completed: true, usedAt });
  }
  const data = (await api("/api/export", "GET")).data;
  assert.equal(data.records.filter((record) => record.task_id === expedition.id).length, 2);
  assert.equal(data.tasks.find((task) => task.id === expedition.id).next_due, "2026-06-15T10:20");
  await api("/api/reset", "POST", {});
});

test("PWA concurrent exports always contain a consistent relational snapshot", async () => {
  await api("/api/reset", "POST", {});
  const other = secondTab();
  for (let index = 0; index < 5; index++) {
    const [, backup] = await Promise.all([
      other("/api/accounts", "POST", { name: `Snapshot ${index}`, dailyTask: "Daily" }),
      api("/api/export", "GET"),
    ]);
    const ids = new Set(backup.data.accounts.map((account) => account.id));
    assert.ok(backup.data.tasks.every((task) => ids.has(task.account_id)));
  }
  await api("/api/reset", "POST", {});
});

test("Service worker activation retains unrelated origin caches", async () => {
  const scope = "https://example.test/LeyLineBook/";
  const deleted = [];
  const listeners = {};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../static/sw.js"), "utf8"), {
    self: { registration: { scope }, addEventListener: (name, handler) => { listeners[name] = handler; }, clients: { claim() {} } },
    caches: { keys: async () => ["another-project", "leylinebook-shell-v4", `leylinebook-shell:${scope}:v3`, `leylinebook-shell:${scope}:v5`, "leylinebook-shell:https://example.test/other/:v2"], delete: async (key) => { deleted.push(key); } },
  });
  let completed;
  listeners.activate({ waitUntil(promise) { completed = promise; } });
  await completed;
  assert.deepEqual(deleted, ["leylinebook-shell-v4", `leylinebook-shell:${scope}:v3`]);
});

test("local-backend.js 业务逻辑", async (t) => {
  const today = gameToday();
  let acc;

  await t.test("建号返回 UUID，每日任务出现在 dueTasks", async () => {
    acc = await api("/api/accounts", "POST", {
      name: "验证号",
      proxyUntil: "2099-12-31",
      dailyTask: "每日+好感",
    });
    assert.equal(typeof acc.id, "string");
    assert.ok(acc.id.length > 10, "账号 id 应为 UUID 字符串，不是自增整数");

    const st = await api(`/api/state?date=${today}`, "GET");
    assert.equal(st.accounts.length, 1);
    assert.ok(
      st.dueTasks.some((t2) => t2.account_id === acc.id),
      "新建号主的每日任务应出现在 dueTasks 中"
    );
  });

  await t.test("壶（3天冷却）：完成后 next_due = 今天+3，冷却中仍显示", async () => {
    await api(`/api/accounts/${acc.id}/task-tags`, "POST", { tag: "壶", enabled: true });
    let st = await api(`/api/state?date=${today}`, "GET");
    const potTask = st.tasks.find((t2) => t2.name === "壶");

    await api(`/api/tasks/${potTask.id}/toggle`, "POST", { date: today, completed: true });
    st = await api(`/api/state?date=${today}`, "GET");
    const pot = st.tasks.find((t2) => t2.name === "壶");
    assert.equal(pot.next_due, addDaysStr(today, 3));

    const potDue = st.dueTasks.find((t2) => t2.name === "壶");
    assert.ok(potDue, "冷却中的壶应仍在 dueTasks 中（灰色卡片显示倒计时），不应消失");
    assert.equal(potDue.completed, true);
  });

  await t.test("壶撤销完成：next_due 恢复到完成前的值", async () => {
    let st = await api(`/api/state?date=${today}`, "GET");
    const pot = st.tasks.find((t2) => t2.name === "壶");

    await api(`/api/tasks/${pot.id}/toggle`, "POST", { date: today, completed: false });
    st = await api(`/api/state?date=${today}`, "GET");
    const potAfter = st.tasks.find((t2) => t2.name === "壶");
    assert.equal(potAfter.next_due, today);
  });

  await t.test("探索派遣：默认 20 小时冷却，带精确时间戳", async () => {
    await api(`/api/accounts/${acc.id}/task-tags`, "POST", { tag: "探索派遣", enabled: true });
    let st = await api(`/api/state?date=${today}`, "GET");
    const exp = st.tasks.find((t2) => t2.name === "探索派遣");

    await api(`/api/tasks/${exp.id}/toggle`, "POST", {
      date: today,
      completed: true,
      usedAt: `${today}T10:00`,
    });
    st = await api(`/api/state?date=${today}`, "GET");
    const expDue = st.dueTasks.find((t2) => t2.name === "探索派遣");
    assert.equal(typeof expDue.cooldown_remaining_seconds, "number");
    assert.ok(expDue.available_at, "派遣完成后应带精确的 available_at 时间戳");
  });

  await t.test("爱可菲料理（每周）：完成当天可见，跨周消失", async () => {
    await api(`/api/accounts/${acc.id}/task-tags`, "POST", { tag: "爱可菲料理", enabled: true });
    let st = await api(`/api/state?date=${today}`, "GET");
    assert.ok(st.dueTasks.some((t2) => t2.name === "爱可菲料理"));

    const aike = st.tasks.find((t2) => t2.name === "爱可菲料理");
    await api(`/api/tasks/${aike.id}/toggle`, "POST", { date: today, completed: true });
    st = await api(`/api/state?date=${today}`, "GET");
    assert.ok(st.dueTasks.some((t2) => t2.name === "爱可菲料理" && t2.completed));
  });

  await t.test("一键完成：进度统计正确更新", async () => {
    let st = await api(`/api/state?date=${today}`, "GET");
    const before = st.summary.completed;
    const ids = st.dueTasks
      .filter((t2) => !t2.completed && !(t2.cooldown_remaining_seconds > 0))
      .map((t2) => t2.id);

    await api("/api/tasks/complete-all", "POST", { date: today, taskIds: ids });
    st = await api(`/api/state?date=${today}`, "GET");
    assert.ok(st.summary.completed >= before);
  });

  await t.test("备份导入：整数 ID（旧桌面版格式）正确重映射为 UUID", async () => {
    const legacy = {
      accounts: [
        {
          id: 1,
          name: "老号A",
          proxy_until: "2099-01-01",
          active: 1,
          deleted: 0,
          sort_order: 0,
          created_at: "2026-01-01T00:00:00",
        },
      ],
      tasks: [
        {
          id: 10,
          account_id: 1,
          name: "体力",
          recurrence: "daily",
          active: 1,
          sort_order: 0,
          created_at: "2026-01-01T00:00:00",
        },
      ],
      records: [
        {
          id: 100,
          task_id: 10,
          task_date: "2026-06-01",
          completed_at: "2026-06-01T12:00:00",
          note: "",
        },
      ],
      customTags: [],
      carePlans: [],
      storyTasks: [],
    };
    await api("/api/import", "POST", legacy);

    const st = await api(`/api/state?date=${today}`, "GET");
    assert.equal(st.accounts.length, 1);
    assert.equal(st.accounts[0].name, "老号A");
    assert.notEqual(st.accounts[0].id, 1, "导入后 id 应被重新分配为 UUID，而不是沿用原始整数");
    assert.equal(typeof st.accounts[0].id, "string");

    const task = st.tasks.find((t2) => t2.name === "体力");
    assert.equal(task.account_id, st.accounts[0].id, "任务外键应重连到重映射后的新账号 id");
  });

  await t.test("新版备份可往返导入，未知版本会被拒绝", async () => {
    const backup = await BE._debug.buildBackup();
    assert.equal(backup.format, "leylinebook-backup");
    assert.equal(backup.schemaVersion, 3);
    assert.ok(Array.isArray(backup.data.accounts));

    await BE._debug.importBackup(backup);
    const after = await BE._debug.buildBackup();
    assert.equal(after.data.accounts[0].name, "老号A");
    await assert.rejects(
      BE._debug.importBackup({ ...backup, schemaVersion: 999 }),
      /不支持的备份版本/
    );
  });

  await t.test("备份导入写入失败时整体回滚，保留原有数据", async () => {
    const before = await BE._debug.buildBackup();
    const originalPut = IDBObjectStore.prototype.put;
    let writeCount = 0;
    IDBObjectStore.prototype.put = function (...args) {
      writeCount += 1;
      if (writeCount === 2) throw new Error("模拟导入写入失败");
      return originalPut.apply(this, args);
    };

    try {
      await assert.rejects(
        BE._debug.importBackup({
          accounts: [{ id: 1, name: "不应保留的号主", created_at: "2026-01-01T00:00:00" }],
          tasks: [{ id: 1, account_id: 1, name: "体力", recurrence: "daily", created_at: "2026-01-01T00:00:00" }],
          records: [],
          customTags: [],
          carePlans: [],
          storyTasks: [],
        }),
        /模拟导入写入失败/
      );
    } finally {
      IDBObjectStore.prototype.put = originalPut;
    }

    const after = await BE._debug.buildBackup();
    for (const key of ["accounts", "tasks", "records", "storyTasks", "customTags", "carePlans"]) {
      assert.deepEqual(after.data[key], before.data[key], `${key} 应在失败后保持不变`);
    }
  });

  await t.test("导入前快照可恢复，并且最多保留五个", async () => {
    const before = await api(`/api/state?date=${today}`, "GET");
    const expectedName = before.accounts[0].name;
    await api("/api/import", "POST", simpleBackup("临时导入号", 901));

    let snapshots = await api("/api/import-snapshots", "GET");
    assert.ok(snapshots.length > 0);
    await api(`/api/import-snapshots/${snapshots[0].id}/restore`, "POST", {});
    const restored = await api(`/api/state?date=${today}`, "GET");
    assert.equal(restored.accounts[0].name, expectedName);

    for (let i = 0; i < 6; i += 1) {
      await api("/api/import", "POST", simpleBackup(`快照轮转${i}`, 1000 + i));
    }
    snapshots = await api("/api/import-snapshots", "GET");
    assert.equal(snapshots.length, 5);
  });

  await t.test("托管方案：建号自动套用方案里的任务", async () => {
    const plan = await api("/api/care-plans", "POST", {
      name: "普托",
      tasks: ["体力", "狗粮", "壶"],
    });
    assert.deepEqual(plan.tasks, ["体力", "狗粮", "壶"]);

    const acc2 = await api("/api/accounts", "POST", {
      name: "套餐号",
      proxyUntil: "2099-12-31",
      planId: plan.id,
    });
    const st = await api(`/api/state?date=${today}`, "GET");
    const acc2Tasks = st.tasks
      .filter((t2) => t2.account_id === acc2.id)
      .map((t2) => t2.name)
      .sort();
    assert.deepEqual(acc2Tasks, ["体力", "壶", "狗粮"]);
  });

  await t.test("危战版本窗口正确计算", async () => {
    const st = await api(`/api/state?date=${today}`, "GET");
    assert.ok(st.settings.warWindow && st.settings.warWindow.eventStart);
  });

  await t.test("托管方案的大活动/小活动开关：建号时只套用勾选的分类", async () => {
    const bigTag = await api("/api/custom-tags", "POST", {
      name: "方案活动A",
      category: "大活动",
      durationDays: 16,
      startDate: today,
    });
    await api("/api/custom-tags", "POST", {
      name: "方案活动B",
      category: "小活动",
      durationDays: 7,
      startDate: today,
    });

    const plan = await api("/api/care-plans", "POST", {
      name: "活动托",
      tasks: ["体力", "大活动"],
    });
    const acc3 = await api("/api/accounts", "POST", {
      name: "活动方案号",
      proxyUntil: "2099-12-31",
      planId: plan.id,
    });

    const st = await api(`/api/state?date=${today}`, "GET");
    const names = new Set(
      st.tasks.filter((t2) => t2.account_id === acc3.id).map((t2) => t2.name)
    );
    assert.deepEqual(names, new Set(["体力", "方案活动A"]));
    assert.equal(
      names.has("方案活动B"),
      false,
      "未勾选的小活动分类不应被套用"
    );

    const activityTask = st.tasks.find(
      (t2) => t2.account_id === acc3.id && t2.name === "方案活动A"
    );
    assert.equal(activityTask.custom_tag_id, bigTag.id, "活动任务应正确关联到对应的自定义标签 id");
  });
});

test("PWA 边界条件与原子写入", async (t) => {
  await t.test("精确冷却按次日 04:00 划分游戏日", async () => {
    await api("/api/reset", "POST", {});
    await api("/api/import", "POST", {
      accounts: [{ id: "game-day-account", name: "游戏日边界号", active: 1, sort_order: 0 }],
      tasks: [
        { id: "early-task", account_id: "game-day-account", name: "质变仪", recurrence: "interval", interval_days: 7, next_due: "2026-06-21T02:00", active: 1, sort_order: 2 },
        { id: "reset-task", account_id: "game-day-account", name: "探索派遣", recurrence: "interval", next_due: "2026-06-21T04:00", active: 1, sort_order: 5 },
      ],
      records: [], customTags: [], carePlans: [], storyTasks: [], groupNotes: [],
    });

    const juneTwentieth = await api("/api/state?date=2026-06-20", "GET");
    assert.ok(juneTwentieth.dueTasks.some((task) => task.name === "质变仪"));
    assert.equal(juneTwentieth.dueTasks.some((task) => task.name === "探索派遣"), false);
    const juneTwentyFirst = await api("/api/state?date=2026-06-21", "GET");
    assert.ok(juneTwentyFirst.dueTasks.some((task) => task.name === "探索派遣"));
  });

  await t.test("彻底删除的号主不能复活，关联剧情任务不再显示", async () => {
    await api("/api/reset", "POST", {});
    const account = await api("/api/accounts", "POST", { name: "删除状态测试号" });
    await api("/api/story-tasks", "POST", {
      accountId: account.id,
      name: "关联剧情任务",
      taskType: "world",
    });
    await api(`/api/accounts/${account.id}`, "DELETE", {});
    await api(`/api/accounts/${account.id}/purge`, "POST", {});

    await assert.rejects(
      api(`/api/accounts/${account.id}/reactivate`, "POST", {}),
      /没有找到该号主/
    );
    const current = await api(`/api/state?date=${gameToday()}`, "GET");
    assert.equal(current.accounts.length, 0);
    assert.equal(current.storyTasks.length, 0);
  });

  await t.test("失效托管方案不会留下创建了一半的号主", async () => {
    await api("/api/reset", "POST", {});
    await assert.rejects(
      api("/api/accounts", "POST", { name: "不应被创建", planId: "missing-plan" }),
      /托管方案不存在/
    );
    const backup = await api("/api/export", "GET");
    assert.equal(backup.data.accounts.length, 0);
    assert.equal(backup.data.tasks.length, 0);
  });

  await t.test("只有独立剧情任务时也会创建导入前快照", async () => {
    await api("/api/reset", "POST", {});
    await api("/api/story-tasks", "POST", {
      ownerName: "临时号主",
      name: "快照剧情任务",
      taskType: "world",
    });
    await api("/api/import", "POST", simpleBackup("导入目标", 3001));

    const snapshots = await api("/api/import-snapshots", "GET");
    assert.equal(snapshots.length, 1);
    assert.equal(snapshots[0].accountCount, 0);
    assert.equal(snapshots[0].recordCount, 0);
    assert.equal(snapshots[0].storyTaskCount, 1);
  });

  await t.test("旧版 manual 任务导入后会转为可见的一次性任务", async () => {
    await api("/api/reset", "POST", {});
    await api("/api/import", "POST", {
      accounts: [{ id: "legacy-account", name: "旧备份号", active: 1, sort_order: 0 }],
      tasks: [{ id: "legacy-manual-task", account_id: "legacy-account", name: "旧专项任务", recurrence: "manual", active: 1 }],
      records: [], customTags: [], carePlans: [], storyTasks: [], groupNotes: [],
    });

    const current = await api(`/api/state?date=${gameToday()}`, "GET");
    const task = current.dueTasks.find((item) => item.name === "旧专项任务");
    assert.ok(task);
    assert.equal(task.recurrence, "once");
  });

  await t.test("软删除的托管方案导入后保持隐藏", async () => {
    await api("/api/reset", "POST", {});
    await api("/api/import", "POST", {
      accounts: [], tasks: [], records: [], customTags: [], storyTasks: [], groupNotes: [],
      carePlans: [{ id: "deleted-plan", name: "已删除方案", tasks: '["体力"]', deleted: 1 }],
    });

    const current = await api(`/api/state?date=${gameToday()}`, "GET");
    assert.equal(current.carePlans.length, 0);
    const backup = await api("/api/export", "GET");
    assert.equal(backup.data.carePlans[0].deleted, 1);
  });

  await t.test("号主、活动和托管方案名称保持唯一", async () => {
    await api("/api/reset", "POST", {});
    await api("/api/accounts", "POST", { name: "唯一号主" });
    await assert.rejects(api("/api/accounts", "POST", { name: "唯一号主" }), /名称已存在/);

    await api("/api/custom-tags", "POST", {
      name: "唯一活动",
      category: "大活动",
      durationDays: 16,
      startDate: gameToday(),
    });
    await assert.rejects(
      api("/api/custom-tags", "POST", { name: "唯一活动", category: "大活动", durationDays: 16, startDate: gameToday() }),
      /名称已存在/
    );

    await api("/api/care-plans", "POST", { name: "唯一方案", tasks: ["体力"] });
    await assert.rejects(
      api("/api/care-plans", "POST", { name: "唯一方案", tasks: ["狗粮"] }),
      /名称已存在/
    );
  });

  await t.test("一键完成写入失败会整体回滚", async () => {
    await api("/api/reset", "POST", {});
    await api("/api/import", "POST", {
      accounts: [{ id: "atomic-account", name: "原子写入号", active: 1, sort_order: 0 }],
      tasks: [
        { id: "atomic-task-1", account_id: "atomic-account", name: "体力", recurrence: "daily", active: 1, sort_order: 0 },
        { id: "atomic-task-2", account_id: "atomic-account", name: "狗粮", recurrence: "daily", active: 1, sort_order: 1 },
      ],
      records: [], customTags: [], carePlans: [], storyTasks: [], groupNotes: [],
    });
    const before = await api(`/api/state?date=${gameToday()}`, "GET");
    const taskIds = before.dueTasks.map((task) => task.id);
    const originalPut = IDBObjectStore.prototype.put;
    let recordWrites = 0;
    IDBObjectStore.prototype.put = function (...args) {
      if (this.name === "task_records" && ++recordWrites === 2) {
        throw new Error("模拟批量写入失败");
      }
      return originalPut.apply(this, args);
    };

    try {
      await assert.rejects(
        api("/api/tasks/complete-all", "POST", { date: gameToday(), taskIds }),
        /模拟批量写入失败/
      );
    } finally {
      IDBObjectStore.prototype.put = originalPut;
    }

    const after = await api("/api/export", "GET");
    assert.equal(after.data.records.length, 0);
  });
});

test("local-backend.js 路由覆盖：前端调用的每个接口都能被匹配", async () => {
  // 与 static/app.js 里实际出现的 api() 调用路径保持同步；新增接口调用时记得在这里补一行
  const calls = [
    ["GET", "/api/state?date=2026-07-10"],
    ["GET", "/api/settings"],
    ["GET", "/api/history?end=2026-07-10"],
    ["GET", "/api/export"],
    ["GET", "/api/import-snapshots"],
    ["GET", "/api/update/check"],
    ["GET", "/api/update/progress"],
    ["GET", "/api/heartbeat"],
    ["GET", "/api/custom-tags"],
    ["POST", "/api/import"],
    ["POST", "/api/import-snapshots/x/restore"],
    ["POST", "/api/reset"],
    ["POST", "/api/shutdown"],
    ["POST", "/api/accounts"],
    ["PUT", "/api/accounts/x"],
    ["DELETE", "/api/accounts/x"],
    ["POST", "/api/accounts/x/reactivate"],
    ["POST", "/api/accounts/x/purge"],
    ["POST", "/api/accounts/reorder"],
    ["POST", "/api/accounts/x/task-tags"],
    ["POST", "/api/accounts/x/custom-tags"],
    ["GET", "/api/accounts/x/credentials"],
    ["PUT", "/api/accounts/x/credentials"],
    ["DELETE", "/api/accounts/x/credentials"],
    ["POST", "/api/tasks"],
    ["PUT", "/api/tasks/x"],
    ["DELETE", "/api/tasks/x"],
    ["POST", "/api/tasks/x/notes"],
    ["POST", "/api/tasks/x/toggle"],
    ["POST", "/api/tasks/complete-all"],
    ["POST", "/api/story-tasks"],
    ["POST", "/api/story-tasks/x/toggle"],
    ["DELETE", "/api/story-tasks/x"],
    ["POST", "/api/custom-tags"],
    ["DELETE", "/api/custom-tags/x"],
    ["POST", "/api/custom-tags/x/enable-all"],
    ["POST", "/api/care-plans"],
    ["PUT", "/api/care-plans/x"],
    ["DELETE", "/api/care-plans/x"],
    ["PUT", "/api/settings/version"],
    ["POST", "/api/update/apply"],
  ];

  for (const [method, p] of calls) {
    try {
      await api(p, method, {});
    } catch (error) {
      assert.ok(
        !String(error.message).includes("接口不存在"),
        `路由未覆盖: ${method} ${p}`
      );
      // 其它业务错误（如"没有找到该号主"）说明路由匹配成功，属正常
    }
  }
});

test("桌面端与 PWA 共用调度场景：PWA 结果符合共享契约", async (t) => {
  await t.test("每周一 04:00 边界", () => {
    for (const item of SCHEDULING_CASES.weeklyCases) {
      const actual = BE._debug.weeklyCycleStart(item.reference, new Date(item.current));
      assert.equal(actual, item.expected, item.name);
    }
  });

  await t.test("42 天版本窗口", () => {
    for (const item of SCHEDULING_CASES.versionCases) {
      const actual = BE._debug.versionWindow(SCHEDULING_CASES.versionAnchorDate, item.reference);
      assert.deepEqual(actual, item.expected, item.reference);
    }
  });

  await t.test("到期任务与汇总", async () => {
    for (const item of SCHEDULING_CASES.stateCases) {
      await api("/api/import", "POST", {
        ...SCHEDULING_CASES.stateFixture,
        records: item.records,
      });
      await api("/api/settings/version", "PUT", {
        versionStartDate: SCHEDULING_CASES.versionAnchorDate,
      });
      const current = await api(`/api/state?date=${item.selectedDate}`, "GET");
      assert.deepEqual(current.dueTasks.map((task) => task.name), item.expected.dueNames, item.name);
      assert.deepEqual(
        current.dueTasks.filter((task) => task.completed).map((task) => task.name),
        item.expected.completedNames,
        item.name
      );
      assert.deepEqual(current.summary, item.expected.summary, item.name);
    }
  });
});
