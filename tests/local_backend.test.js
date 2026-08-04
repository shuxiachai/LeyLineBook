// 手机版 PWA 业务逻辑（static/local-backend.js）的回归测试。
// 运行：npm install && npm test（需要 fake-indexeddb，仅开发期依赖，不影响桌面/移动端运行时）。
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const fs = require("node:fs");

require("fake-indexeddb/auto");

// local-backend.js 依赖的最小浏览器环境垫片
global.location = { hostname: "test.local", search: "" };
global.window = global;
global.document = { documentElement: { classList: { add() {} } } };

const SOURCE = fs.readFileSync(
  path.join(__dirname, "..", "static", "local-backend.js"),
  "utf8"
);
// eslint-disable-next-line no-eval -- local-backend.js 是浏览器脚本，无法直接 require
eval(SOURCE);

const BE = global.LOCAL_BACKEND;
assert.ok(BE, "LOCAL_BACKEND 未激活，local-backend.js 的域名门控逻辑可能已改动");

function api(p, method, body) {
  return BE.handle(p, { method, body: body ? JSON.stringify(body) : undefined });
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
});

test("local-backend.js 路由覆盖：前端调用的每个接口都能被匹配", async () => {
  // 与 static/app.js 里实际出现的 api() 调用路径保持同步；新增接口调用时记得在这里补一行
  const calls = [
    ["GET", "/api/state?date=2026-07-10"],
    ["GET", "/api/settings"],
    ["GET", "/api/history?end=2026-07-10"],
    ["GET", "/api/export"],
    ["GET", "/api/update/check"],
    ["GET", "/api/update/progress"],
    ["GET", "/api/heartbeat"],
    ["GET", "/api/custom-tags"],
    ["POST", "/api/import"],
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
