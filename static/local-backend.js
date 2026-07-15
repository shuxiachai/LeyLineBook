/*
 * LeyLineBook 手机端本地后端
 * 用 IndexedDB 复刻 app.py 的 /api/* 接口，使前端脱离 Python 服务器也能运行（PWA）。
 *
 * 同步友好设计（为后期手机↔电脑同步预留）：
 *   · 主键 id 全部使用字符串 UUID（crypto.randomUUID）
 *   · 每条记录带 updated_at；写入即刷新
 *   · 全部软删除（deleted 0/1），不物理删除
 *   · 完成记录 task_records 以 (task_id, task_date, note) 为逻辑唯一键，作为“完成状态”的唯一真相来源
 *
 * 仅在“本地模式”（非 127.0.0.1/localhost 访问，即 GitHub Pages 等静态托管）下启用。
 */
(function () {
  "use strict";

  const DB_NAME = "leylinebook";
  const DB_VERSION = 1;
  const STORES = ["accounts", "tasks", "task_records", "custom_task_tags", "care_plans", "story_tasks", "app_meta"];

  const OFFICIAL_VERSION_ANCHOR = "2026-05-20";
  const VERSION_LENGTH_DAYS = 42;
  const TASK_PRESETS = {
    "体力": { recurrence: "daily" },
    "狗粮": { recurrence: "daily" },
    "探索派遣": { recurrence: "interval" },
    "质变仪": { recurrence: "interval", interval_days: 7 },
    "壶": { recurrence: "interval", interval_days: 3 },
    "爱可菲料理": { recurrence: "weekly" },
    "深境螺旋": { recurrence: "monthly", monthly_day: 16 },
    "幻想真境剧诗": { recurrence: "monthly", monthly_day: 1 },
    "危战": { recurrence: "version" },
  };
  const RESERVED_ACTIVITY_NAMES = new Set([...Object.keys(TASK_PRESETS), "剧诗", "深渊", "捡材料", "尘歌壶"]);
  const TASK_SORT_ORDER = { "体力": 0, "狗粮": 1, "质变仪": 2, "壶": 3, "爱可菲料理": 4, "探索派遣": 5, "深境螺旋": 10, "幻想真境剧诗": 11, "危战": 12 };
  const ACTIVITY_TASK_SORT_ORDER = 6;
  const VALID_DURATIONS = { "大活动": [16, 23], "小活动": [7, 10] };
  const STORY_TASK_TYPES = { archon: "魔神任务", legend: "传说任务", world: "世界任务" };

  /* ---------- 时间 / 游戏日 ---------- */
  const pad = (n) => String(n).padStart(2, "0");
  const isoDate = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const parseDate = (s) => { const [y, m, d] = s.split("-").map(Number); return new Date(y, m - 1, d); };
  const addDays = (d, n) => { const r = new Date(d); r.setDate(r.getDate() + n); return r; };
  function gameToday() { const d = new Date(); d.setHours(d.getHours() - 4); return isoDate(d); }
  function nowText() { const d = new Date(); return `${isoDate(d)}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`; }
  // Python weekday(): 周一=0..周日=6
  const pyWeekday = (d) => (d.getDay() + 6) % 7;

  function weeklyCycleStart(refStr) {
    const ref = parseDate(refStr);
    let monday = addDays(ref, -pyWeekday(ref));
    const now = new Date();
    if (refStr === gameToday() && pyWeekday(ref) === 0 && now.getHours() < 4) {
      monday = addDays(monday, -7);
    }
    return isoDate(monday);
  }
  const weeklyCycleKey = (refStr) => `weekly:${weeklyCycleStart(refStr)}`;

  function monthlyOccurrence(refStr, day) { const d = parseDate(refStr); return isoDate(new Date(d.getFullYear(), d.getMonth(), day)); }
  function nextMonthOccurrence(refStr, day) { const d = parseDate(refStr); return isoDate(new Date(d.getFullYear(), d.getMonth() + 1, day)); }

  function versionWindow(anchor, refStr) {
    if (!anchor) return null;
    const anchorD = parseDate(anchor);
    const ref = parseDate(refStr || gameToday());
    const diffDays = Math.floor((ref - anchorD) / 86400000);
    const cycleOffset = Math.floor(diffDays / VERSION_LENGTH_DAYS);
    const start = addDays(anchorD, cycleOffset * VERSION_LENGTH_DAYS);
    return {
      anchorDate: isoDate(anchorD),
      versionStart: isoDate(start),
      eventStart: isoDate(addDays(start, 7)),
      eventStartTime: "10:00",
      eventEnd: isoDate(addDays(start, 41)),
      eventEndTime: "03:59",
      nextVersionStart: isoDate(addDays(start, VERSION_LENGTH_DAYS)),
    };
  }

  function exactDueMoment(value) {
    const text = String(value || "").trim();
    if (!text.includes("T")) return null;
    return new Date(text);
  }

  const uuid = () => (crypto.randomUUID ? crypto.randomUUID() : "id-" + Date.now() + "-" + Math.random().toString(16).slice(2));

  /* ---------- IndexedDB 封装 ---------- */
  let _db = null;
  function openDB() {
    return new Promise((resolve, reject) => {
      if (_db) return resolve(_db);
      const req = indexedDB.open(DB_NAME, DB_VERSION);
      req.onupgradeneeded = () => {
        const db = req.result;
        for (const name of STORES) {
          if (!db.objectStoreNames.contains(name)) {
            db.createObjectStore(name, { keyPath: name === "app_meta" ? "key" : "id" });
          }
        }
      };
      req.onsuccess = () => { _db = req.result; resolve(_db); };
      req.onerror = () => reject(req.error);
    });
  }
  function tx(store, mode) { return _db.transaction(store, mode).objectStore(store); }
  const reqP = (r) => new Promise((res, rej) => { r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error); });
  const getAll = (store) => reqP(tx(store, "readonly").getAll());
  const getOne = (store, id) => reqP(tx(store, "readonly").get(id));
  async function putRec(store, rec) { rec.updated_at = nowText(); await reqP(tx(store, "readwrite").put(rec)); return rec; }
  const delRec = (store, id) => reqP(tx(store, "readwrite").delete(id));

  async function metaGet(key, fallback) { const r = await getOne("app_meta", key); return r ? r.value : fallback; }
  async function metaSet(key, value) { await reqP(tx("app_meta", "readwrite").put({ key, value })); }

  const liveAccounts = (rows) => rows.filter((a) => !a.deleted);
  const activeTasks = (rows) => rows.filter((t) => t.active && !t.deleted);

  async function getVersionAnchor() {
    let a = await metaGet("version_anchor_date", null);
    if (!a) { a = OFFICIAL_VERSION_ANCHOR; await metaSet("version_anchor_date", a); }
    return a;
  }
  async function getScheduleSettings() {
    const anchor = await getVersionAnchor();
    const win = versionWindow(anchor);
    return { versionAnchorDate: anchor, versionStartDate: win.versionStart, warWindow: win };
  }

  /* ---------- load_state（读路径核心） ---------- */
  async function loadState(selectedDate) {
    const accountsRaw = await getAll("accounts");
    const accounts = liveAccounts(accountsRaw).map((a) => { const c = { ...a }; delete c.credentials; return c; })
      .sort((x, y) => (Number(!x.active) - Number(!y.active)) || (x.sort_order - y.sort_order));
    const allTaskRows = activeTasks(await getAll("tasks"));
    const records = await getAll("task_records");
    const customTags = (await getAll("custom_task_tags")).filter((t) => !t.deleted);
    const carePlans = (await getAll("care_plans")).filter((p) => !p.deleted).map((p) => ({ ...p, tasks: JSON.parse(p.tasks) }));

    const accById = new Map(accountsRaw.map((a) => [a.id, a]));
    const activeAccountIds = new Set(liveAccounts(accountsRaw).filter((a) => a.active).map((a) => a.id));

    // 仅活跃号主下的活跃任务参与今日展示
    const tasks = allTaskRows.filter((t) => activeAccountIds.has(t.account_id)).map((t) => {
      const acc = accById.get(t.account_id) || {};
      return { ...t, account_name: acc.name, account_proxy_until: acc.proxy_until };
    }).sort((a, b) => {
      const aa = accById.get(a.account_id) || {}, ba = accById.get(b.account_id) || {};
      return (aa.sort_order - ba.sort_order) || String(a.account_id).localeCompare(String(b.account_id))
        || (a.sort_order - b.sort_order) || ((a.custom_tag_id ? 1 : 0) - (b.custom_tag_id ? 1 : 0));
    });

    const settings = await getScheduleSettings();
    const warWindow = versionWindow(settings.versionAnchorDate, selectedDate);
    const today = gameToday();
    const selectedCutoff = selectedDate === today ? new Date() : new Date(selectedDate + "T23:59:59.999");
    const selectedWeekKey = weeklyCycleKey(selectedDate);

    const recByTaskDateNote = new Map();
    for (const r of records) recByTaskDateNote.set(`${r.task_id}|${r.task_date}|${r.note || ""}`, r);
    const completedThisDate = (taskId) => recByTaskDateNote.has(`${taskId}|${selectedDate}|`);
    const everCompleted = new Set(records.map((r) => r.task_id));

    const completedWeekly = new Map(); // task_id -> task_date（该周完成记录）
    for (const r of records) if (r.note === selectedWeekKey) completedWeekly.set(r.task_id, r.task_date);

    const prevDay = isoDate(addDays(parseDate(selectedDate), -1));
    const prevDayFood = new Map();
    const taskById = new Map(allTaskRows.map((t) => [t.id, t]));
    for (const r of records) {
      const t = taskById.get(r.task_id);
      if (t && t.name === "狗粮" && r.task_date === prevDay && (r.note || "") === "") prevDayFood.set(r.task_id, r.completed_at);
    }
    // monthly：task_date >= next_due 的完成记录视为“本期已完成”
    const completedMonthly = new Set();
    for (const r of records) {
      const t = taskById.get(r.task_id);
      if (t && t.recurrence === "monthly" && t.active && !t.deleted && t.next_due && r.task_date >= t.next_due) completedMonthly.add(r.task_id);
    }
    // version：本版本窗口内任意完成记录
    const completedVersion = new Set();
    for (const r of records) if (r.task_date >= warWindow.eventStart && r.task_date <= warWindow.eventEnd) completedVersion.add(r.task_id);

    const nowMs = Date.now();
    const dueTasks = [];
    const allTasks = [];
    for (const raw of tasks) {
      const task = { ...raw };
      task.completed = completedThisDate(task.id);
      task.completed_ever = everCompleted.has(task.id);
      allTasks.push(task);
      const rec = task.recurrence;
      if (rec === "daily") {
        if (task.name === "狗粮" && prevDayFood.has(task.id)) task.prev_day_completed_at = prevDayFood.get(task.id);
        dueTasks.push(task);
      } else if (rec === "weekly") {
        const weekDone = completedWeekly.has(task.id);
        const doneOnSelected = completedWeekly.get(task.id) === selectedDate;
        task.completed = doneOnSelected;
        task.event_end = isoDate(addDays(parseDate(weeklyCycleStart(selectedDate)), 7));
        task.event_end_time = "04:00";
        if (!weekDone || doneOnSelected) dueTasks.push(task);
      } else if (rec === "interval") {
        let preciseDue = (task.name === "质变仪" || task.name === "探索派遣") ? exactDueMoment(task.next_due) : null;
        if (task.name === "壶" && task.next_due && !String(task.next_due).includes("T") && !task.completed) {
          const pd = parseDate(task.next_due); const potDue = new Date(pd.getFullYear(), pd.getMonth(), pd.getDate(), 4, 0);
          if (potDue.getTime() > nowMs) preciseDue = potDue;
        }
        const isDue = task.next_due && (preciseDue ? preciseDue <= selectedCutoff : task.next_due <= selectedDate);
        if (preciseDue) {
          task.available_at = `${isoDate(preciseDue)}T${pad(preciseDue.getHours())}:${pad(preciseDue.getMinutes())}`;
          task.cooldown_remaining_seconds = Math.max(0, Math.floor((preciseDue.getTime() - nowMs) / 1000));
        }
        const stillCooling = preciseDue && preciseDue.getTime() > nowMs;
        if (task.completed || isDue || stillCooling) dueTasks.push(task);
      } else if (rec === "monthly") {
        const show = (task.completed || (task.next_due && task.next_due <= selectedDate)) && (!completedMonthly.has(task.id) || task.completed);
        if (show) {
          const sel = parseDate(selectedDate);
          const deadline = sel.getDate() < task.monthly_day ? monthlyOccurrence(selectedDate, task.monthly_day) : nextMonthOccurrence(selectedDate, task.monthly_day);
          task.event_end = deadline; task.event_end_time = "03:59";
          dueTasks.push(task);
        }
      } else if (rec === "version") {
        const inWindow = warWindow.eventStart <= selectedDate && selectedDate <= warWindow.eventEnd;
        if (inWindow && (!completedVersion.has(task.id) || task.completed)) {
          task.next_due = warWindow.eventStart; task.event_end = warWindow.eventEnd; task.event_end_time = warWindow.eventEndTime;
          dueTasks.push(task);
        }
      } else if (rec === "once") {
        if (task.next_due && task.next_due <= selectedDate && (!task.completed_ever || task.completed)) dueTasks.push(task);
      }
    }

    const nextDay = addDays(parseDate(gameToday()), 1);
    const endOfGameToday = new Date(nextDay.getFullYear(), nextDay.getMonth(), nextDay.getDate(), 4, 0);
    const longCooling = (t) => !t.completed && t.available_at && new Date(t.available_at) >= endOfGameToday;
    const countable = dueTasks.filter((t) => !longCooling(t));
    const completedCount = countable.filter((t) => t.completed).length;
    const DAILY_CATEGORY = new Set(["体力", "狗粮", "质变仪", "壶", "爱可菲料理", "探索派遣"]);
    const dailyTasks = countable.filter((t) => DAILY_CATEGORY.has(t.name));

    return {
      date: selectedDate, accounts, tasks: allTasks, dueTasks,
      accountNotes: [], customTags, carePlans, storyTasks: await listStoryTasks(), settings,
      summary: {
        total: countable.length, completed: completedCount, remaining: countable.length - completedCount,
        dailyTotal: dailyTasks.length, dailyCompleted: dailyTasks.filter((t) => t.completed).length,
      },
    };
  }

  /* ---------- 完成 / 撤销 ---------- */
  async function insertPresetTask(accountId, taskName, notes) {
    const preset = TASK_PRESETS[taskName];
    let nextDue = null;
    if (preset.recurrence === "interval") nextDue = gameToday();
    else if (preset.recurrence === "monthly") nextDue = monthlyOccurrence(gameToday(), preset.monthly_day);
    else if (preset.recurrence === "version") { const w = versionWindow(await getVersionAnchor()); nextDue = w ? w.eventStart : null; }
    return putRec("tasks", {
      id: uuid(), account_id: accountId, name: taskName, recurrence: preset.recurrence,
      interval_days: preset.interval_days || null, monthly_day: preset.monthly_day || null,
      next_due: nextDue, notes: notes || "", active: 1, deleted: 0,
      sort_order: TASK_SORT_ORDER[taskName] || 0, custom_tag_id: null, created_at: nowText(),
    });
  }

  function expeditionHours(notes) { return String(notes || "").split(/[、,，]/).some((p) => p.trim() === "派遣:15小时") ? 15 : 20; }

  async function toggleTask(taskId, taskDate, completed, usedAt, restartCycle) {
    const task = await getOne("tasks", taskId);
    if (!task || !task.active || task.deleted) throw new Error("没有找到该任务");
    const records = await getAll("task_records");
    const cycleKey = task.recurrence === "weekly" ? weeklyCycleKey(taskDate) : "";
    const match = task.recurrence === "weekly"
      ? records.find((r) => r.task_id === taskId && r.note === cycleKey)
      : records.find((r) => r.task_id === taskId && r.task_date === taskDate && (r.note || "") === "");

    if (completed && !match) {
      const previousDue = task.next_due;
      let preciseUsed = null;
      if ((task.name === "质变仪" || task.name === "探索派遣") && task.recurrence === "interval") {
        preciseUsed = usedAt ? new Date(usedAt) : (() => { const d = new Date(); d.setSeconds(0, 0); return d; })();
      }
      const completedAt = preciseUsed ? `${isoDate(preciseUsed)}T${pad(preciseUsed.getHours())}:${pad(preciseUsed.getMinutes())}:00` : nowText();
      await putRec("task_records", { id: uuid(), task_id: taskId, task_date: taskDate, completed_at: completedAt, previous_next_due: previousDue, note: cycleKey, deleted: 0, created_at: nowText() });

      if (task.recurrence === "interval") {
        let nextDue;
        if (preciseUsed && task.name === "质变仪") { const n = new Date(preciseUsed.getTime() + 24 * task.interval_days * 3600000); nextDue = `${isoDate(n)}T${pad(n.getHours())}:${pad(n.getMinutes())}`; }
        else if (preciseUsed && task.name === "探索派遣") { const n = new Date(preciseUsed.getTime() + expeditionHours(task.notes) * 3600000); nextDue = `${isoDate(n)}T${pad(n.getHours())}:${pad(n.getMinutes())}`; }
        else {
          if (!task.interval_days) throw new Error("任务冷却天数未配置，请检查任务设置");
          const base = restartCycle ? parseDate(taskDate) : new Date(Math.max(parseDate(previousDue || taskDate), parseDate(taskDate)));
          nextDue = isoDate(addDays(base, task.interval_days));
        }
        task.next_due = nextDue; await putRec("tasks", task);
      } else if (task.recurrence === "monthly") {
        const base = new Date(Math.max(parseDate(previousDue || taskDate), parseDate(taskDate)));
        const baseStr = isoDate(base);
        task.next_due = base.getDate() < task.monthly_day ? monthlyOccurrence(baseStr, task.monthly_day) : nextMonthOccurrence(baseStr, task.monthly_day);
        await putRec("tasks", task);
      } else if (task.recurrence === "version") { task.next_due = null; await putRec("tasks", task); }
    } else if (!completed && match) {
      if (["interval", "monthly", "version"].includes(task.recurrence)) { task.next_due = match.previous_next_due; await putRec("tasks", task); }
      await delRec("task_records", match.id);
    }
  }

  async function completeAll(taskDate, taskIds) { for (const id of taskIds) await toggleTask(id, taskDate, true); }

  /* ---------- 号主 ---------- */
  function parseProxyUntil(p) { const raw = String((p && p.proxyUntil) || "").trim(); if (!raw) return null; if (!/^\d{4}-\d{2}-\d{2}$/.test(raw)) throw new Error("截止日期格式无效"); return raw; }
  async function createAccount(p) {
    const name = String(p.name || "").trim(); if (!name) throw new Error("请填写号主名称");
    const accounts = await getAll("accounts");
    const maxOrder = accounts.reduce((m, a) => Math.max(m, a.sort_order || 0), 0);
    const acc = await putRec("accounts", { id: uuid(), name: name.slice(0, 100), owner: String(p.owner || "").trim().slice(0, 100), notes: String(p.notes || "").trim().slice(0, 500), proxy_until: parseProxyUntil(p), active: 1, deleted: 0, sort_order: maxOrder + 1, created_at: nowText(), credentials: null });
    if (p.dailyTask) await putRec("tasks", { id: uuid(), account_id: acc.id, name: String(p.dailyTask).slice(0, 100), recurrence: "daily", interval_days: null, monthly_day: null, next_due: null, notes: "", active: 1, deleted: 0, sort_order: 0, custom_tag_id: null, created_at: nowText() });
    if (p.planId) {
      const plan = await getOne("care_plans", p.planId);
      if (!plan) throw new Error("托管方案不存在，请刷新后重试");
      const allTags = (await getAll("custom_task_tags")).filter((t) => !t.deleted);
      for (const tn of JSON.parse(plan.tasks)) {
        if (TASK_PRESETS[tn]) await insertPresetTask(acc.id, tn);
        else if (VALID_DURATIONS[tn]) for (const tag of allTags.filter((t) => t.category === tn)) await putRec("tasks", { id: uuid(), account_id: acc.id, name: tag.name, recurrence: "once", interval_days: null, monthly_day: null, next_due: tag.start_date || gameToday(), notes: "", active: 1, deleted: 0, sort_order: ACTIVITY_TASK_SORT_ORDER, custom_tag_id: tag.id, created_at: nowText() });
      }
    }
    const clean = { ...acc }; delete clean.credentials; return clean;
  }
  async function updateAccount(id, p) {
    const acc = await getOne("accounts", id); if (!acc || !acc.active || acc.deleted) throw new Error("没有找到该号主");
    const name = String(p.name || "").trim(); if (!name) throw new Error("请填写号主名称");
    Object.assign(acc, { name: name.slice(0, 100), owner: String(p.owner || "").trim().slice(0, 100), notes: String(p.notes || "").trim().slice(0, 500), proxy_until: parseProxyUntil(p) });
    await putRec("accounts", acc);
  }
  async function archiveAccount(id) { const a = await getOne("accounts", id); if (!a || !a.active) throw new Error("没有找到该号主"); a.active = 0; await putRec("accounts", a); }
  async function reactivateAccount(id) { const a = await getOne("accounts", id); if (!a || a.active) throw new Error("没有找到该号主"); a.active = 1; await putRec("accounts", a); }
  async function purgeAccount(id) {
    const a = await getOne("accounts", id); if (!a) throw new Error("没有找到该号主"); a.deleted = 1; a.active = 0; await putRec("accounts", a);
    for (const t of await getAll("tasks")) if (t.account_id === id && !t.deleted) { t.deleted = 1; t.active = 0; await putRec("tasks", t); }
  }
  async function reorderAccounts(p) {
    const ids = p.accountIds || []; let order = 0;
    for (const id of ids) { const a = await getOne("accounts", id); if (a) { a.sort_order = order++; await putRec("accounts", a); } }
  }

  /* ---------- 号主任务标签 / 备注 ---------- */
  async function setAccountTaskTag(accountId, p) {
    const taskName = String(p.tag || "").trim(); if (!TASK_PRESETS[taskName]) throw new Error("任务标签无效");
    const enabled = !!p.enabled;
    const existing = activeTasks(await getAll("tasks")).filter((t) => t.account_id === accountId && t.name === taskName).sort((a, b) => String(b.id).localeCompare(String(a.id)))[0];
    const notes = "notes" in p ? normalizeNotes(p.notes) : null;
    if (!enabled) { if (existing) { existing.active = 0; await putRec("tasks", existing); } return; }
    if (existing) { if (notes !== null) { existing.notes = notes; await putRec("tasks", existing); } return; }
    await insertPresetTask(accountId, taskName, notes || "");
  }
  function normalizeNotes(raw) {
    if (!Array.isArray(raw)) throw new Error("备注格式无效");
    const notes = []; for (const v of raw.slice(0, 20)) { const n = String(v).trim().slice(0, 40); if (n && !notes.includes(n)) notes.push(n); }
    const joined = notes.join("、"); if (joined.length > 500) throw new Error("备注内容过长"); return joined;
  }
  async function setTaskNotes(taskId, p) { const t = await getOne("tasks", taskId); if (!t || !t.active || t.deleted) throw new Error("没有找到该任务"); t.notes = normalizeNotes(p.notes || []); await putRec("tasks", t); }
  async function archiveTask(id) { const t = await getOne("tasks", id); if (!t || !t.active) throw new Error("没有找到该任务"); t.active = 0; await putRec("tasks", t); }

  /* ---------- 限时活动（custom_task_tags） ---------- */
  async function listCustomTags() { return (await getAll("custom_task_tags")).filter((t) => !t.deleted).sort((a, b) => a.created_at.localeCompare(b.created_at)); }
  async function createCustomTag(p) {
    const name = String(p.name || "").trim(); if (!name) throw new Error("请填写活动名称");
    if (RESERVED_ACTIVITY_NAMES.has(name)) throw new Error("活动名称不能与内置任务重名");
    const category = String(p.category || "").trim(); if (!VALID_DURATIONS[category]) throw new Error("活动类型无效");
    const duration = Number(p.durationDays || 0); if (!(duration >= 1 && duration <= 365)) throw new Error("活动时长无效，应为 1 到 365 天");
    const raw = String(p.startDate || "").trim(); const start = raw || gameToday();
    if (!/^\d{4}-\d{2}-\d{2}$/.test(start)) throw new Error("开始日期格式无效");
    return putRec("custom_task_tags", { id: uuid(), name: name.slice(0, 100), category, duration_days: duration, start_date: start, deleted: 0, created_at: nowText() });
  }
  async function deleteCustomTag(id) {
    const tag = await getOne("custom_task_tags", id); if (!tag) throw new Error("没有找到该任务标签");
    tag.deleted = 1; await putRec("custom_task_tags", tag);
    for (const t of await getAll("tasks")) if (t.custom_tag_id === id && t.active) { t.active = 0; await putRec("tasks", t); }
  }
  async function setAccountCustomTag(accountId, p) {
    const tagId = p.tagId; if (!tagId) throw new Error("请选择有效的活动标签");
    const tag = await getOne("custom_task_tags", tagId); if (!tag || tag.deleted) throw new Error("自定义任务标签不存在");
    const enabled = !!p.enabled;
    const existing = activeTasks(await getAll("tasks")).find((t) => t.account_id === accountId && t.custom_tag_id === tagId);
    if (!enabled) { if (existing) { existing.active = 0; await putRec("tasks", existing); } return; }
    if (existing) return;
    await putRec("tasks", { id: uuid(), account_id: accountId, name: tag.name, recurrence: "once", interval_days: null, monthly_day: null, next_due: tag.start_date || gameToday(), notes: "", active: 1, deleted: 0, sort_order: ACTIVITY_TASK_SORT_ORDER, custom_tag_id: tagId, created_at: nowText() });
  }
  async function enableCustomTagForAll(tagId) {
    const tag = await getOne("custom_task_tags", tagId); if (!tag || tag.deleted) throw new Error("自定义任务标签不存在");
    const accounts = liveAccounts(await getAll("accounts")).filter((a) => a.active);
    const tasks = activeTasks(await getAll("tasks")); let count = 0;
    for (const acc of accounts) {
      if (tasks.some((t) => t.account_id === acc.id && t.custom_tag_id === tagId)) continue;
      await putRec("tasks", { id: uuid(), account_id: acc.id, name: tag.name, recurrence: "once", interval_days: null, monthly_day: null, next_due: tag.start_date || gameToday(), notes: "", active: 1, deleted: 0, sort_order: ACTIVITY_TASK_SORT_ORDER, custom_tag_id: tagId, created_at: nowText() });
      count++;
    }
    return { enabled: count };
  }
  async function cleanupExpiredActivities() {
    const now = new Date();
    for (const tag of (await getAll("custom_task_tags")).filter((t) => !t.deleted && t.start_date)) {
      const end = new Date(addDays(parseDate(tag.start_date), tag.duration_days).getTime()); end.setHours(3, 59, 0, 0);
      if (end < now) { tag.deleted = 1; await putRec("custom_task_tags", tag); for (const t of await getAll("tasks")) if (t.custom_tag_id === tag.id && t.active) { t.active = 0; await putRec("tasks", t); } }
    }
  }

  /* ---------- 托管方案 ---------- */
  function parsePlanTasks(p) {
    const raw = p.tasks; if (!Array.isArray(raw) || !raw.length) throw new Error("方案至少需要勾选一个任务");
    const tasks = []; for (const v of raw) { const n = String(v).trim(); if (!TASK_PRESETS[n] && !VALID_DURATIONS[n]) throw new Error(`方案中包含无效任务：${n}`); if (!tasks.includes(n)) tasks.push(n); }
    return JSON.stringify(tasks);
  }
  async function createCarePlan(p) { const name = String(p.name || "").trim(); if (!name) throw new Error("请填写方案名称"); const plan = await putRec("care_plans", { id: uuid(), name: name.slice(0, 20), tasks: parsePlanTasks(p), deleted: 0, created_at: nowText() }); return { ...plan, tasks: JSON.parse(plan.tasks) }; }
  async function updateCarePlan(id, p) { const plan = await getOne("care_plans", id); if (!plan || plan.deleted) throw new Error("没有找到该托管方案"); plan.name = String(p.name || "").trim().slice(0, 20); plan.tasks = parsePlanTasks(p); await putRec("care_plans", plan); }
  async function deleteCarePlan(id) { const plan = await getOne("care_plans", id); if (!plan || plan.deleted) throw new Error("没有找到该托管方案"); plan.deleted = 1; await putRec("care_plans", plan); }

  /* ---------- 剧情任务 ---------- */
  async function storyBonusDeadline(taskType) {
    if (taskType === "world") return null;
    const win = versionWindow(await getVersionAnchor());
    const vStart = parseDate(win.versionStart), vEnd = parseDate(win.eventEnd);
    let day;
    if (taskType === "archon") day = vEnd;
    else { const halfEnd = addDays(vStart, 20); day = parseDate(gameToday()) <= halfEnd ? halfEnd : vEnd; }
    return `${isoDate(day)}T15:00`;
  }
  async function listStoryTasks() {
    const accById = new Map((await getAll("accounts")).map((a) => [a.id, a]));
    return (await getAll("story_tasks")).filter((s) => s.active && !s.deleted).map((s) => ({ ...s, account_name: (s.owner_name || (accById.get(s.account_id) || {}).name || "临时号主") }))
      .sort((a, b) => (Number(!!a.completed_at) - Number(!!b.completed_at)) || String(a.created_at).localeCompare(String(b.created_at)));
  }
  async function createStoryTask(p) {
    const taskType = String(p.taskType || "").trim(); if (!STORY_TASK_TYPES[taskType]) throw new Error("请选择剧情任务类型");
    const name = String(p.name || "").trim().slice(0, 100) || STORY_TASK_TYPES[taskType];
    const ownerName = String(p.ownerName || "").trim().slice(0, 100);
    let accountId = p.accountId || null; if (ownerName) accountId = null;
    if (!ownerName && !accountId) throw new Error("请选择号主或填写临时号主");
    const hasBonus = !!p.hasBonus && taskType !== "world";
    const deadline = hasBonus ? await storyBonusDeadline(taskType) : null;
    return putRec("story_tasks", { id: uuid(), account_id: accountId, owner_name: ownerName, name, task_type: taskType, has_bonus: hasBonus ? 1 : 0, bonus_deadline: deadline, completed_at: null, active: 1, deleted: 0, created_at: nowText() });
  }
  async function toggleStoryTask(id, completed) { const s = await getOne("story_tasks", id); if (!s || !s.active) throw new Error("没有找到该剧情任务"); s.completed_at = completed ? nowText() : null; await putRec("story_tasks", s); }
  async function archiveStoryTask(id) { const s = await getOne("story_tasks", id); if (!s || !s.active) throw new Error("没有找到该剧情任务"); s.active = 0; await putRec("story_tasks", s); }

  /* ---------- 危战锚点 ---------- */
  async function updateVersionStart(p) {
    const raw = String(p.versionStartDate || "").trim(); if (!/^\d{4}-\d{2}-\d{2}$/.test(raw)) throw new Error("请选择有效的版本开始日期");
    if (pyWeekday(parseDate(raw)) !== 2) throw new Error("版本开始日期应为星期三");
    await metaSet("version_anchor_date", raw);
    const settings = await getScheduleSettings();
    for (const t of await getAll("tasks")) if (t.recurrence === "version" && t.active && !t.deleted) { t.next_due = settings.warWindow.eventStart; await putRec("tasks", t); }
    return settings;
  }

  /* ---------- 备份 导出 / 导入 ---------- */
  async function buildBackup() {
    return {
      exportedAt: nowText(),
      accounts: (await getAll("accounts")).map((a) => { const c = { ...a }; delete c.credentials; return c; }),
      tasks: await getAll("tasks"), records: await getAll("task_records"),
      storyTasks: await getAll("story_tasks"), customTags: await getAll("custom_task_tags"),
      carePlans: await getAll("care_plans"), groupNotes: [],
    };
  }
  async function importBackup(payload) {
    if (!payload || !Array.isArray(payload.accounts)) throw new Error("备份文件格式无效，请选择由本程序导出的 JSON 文件");
    // 清空现有（软删除语义在导入场景下直接物理重建，因为是整库替换）
    for (const store of ["accounts", "tasks", "task_records", "custom_task_tags", "care_plans", "story_tasks"]) {
      await reqP(tx(store, "readwrite").clear());
    }
    // 整数/旧 ID → UUID 重映射，外键跟着换，为后期同步铺路
    const idMap = new Map();
    const mapId = (old) => { if (old == null) return null; const k = `${old}`; if (!idMap.has(k)) idMap.set(k, uuid()); return idMap.get(k); };
    for (const a of payload.accounts) await reqP(tx("accounts", "readwrite").put({ id: mapId(a.id), name: a.name || "", owner: a.owner || "", notes: a.notes || "", proxy_until: a.proxy_until ?? null, active: a.active ?? 1, deleted: a.deleted ?? 0, sort_order: a.sort_order ?? 0, created_at: a.created_at || nowText(), updated_at: nowText(), credentials: null }));
    for (const t of payload.customTags || []) await reqP(tx("custom_task_tags", "readwrite").put({ id: mapId(t.id), name: t.name, category: t.category || "大活动", duration_days: t.duration_days ?? 16, start_date: t.start_date ?? null, deleted: t.deleted ?? 0, created_at: t.created_at || nowText(), updated_at: nowText() }));
    for (const t of payload.tasks || []) await reqP(tx("tasks", "readwrite").put({ id: mapId(t.id), account_id: mapId(t.account_id), name: t.name || "", recurrence: t.recurrence || "daily", interval_days: t.interval_days ?? null, monthly_day: t.monthly_day ?? null, next_due: t.next_due ?? null, notes: t.notes || "", active: t.active ?? 1, deleted: t.deleted ?? 0, sort_order: t.sort_order ?? 0, custom_tag_id: mapId(t.custom_tag_id), created_at: t.created_at || nowText(), updated_at: nowText() }));
    for (const r of payload.records || []) await reqP(tx("task_records", "readwrite").put({ id: mapId(r.id), task_id: mapId(r.task_id), task_date: r.task_date || "", completed_at: r.completed_at || nowText(), previous_next_due: r.previous_next_due ?? null, note: r.note || "", deleted: 0, created_at: nowText(), updated_at: nowText() }));
    for (const s of payload.storyTasks || []) await reqP(tx("story_tasks", "readwrite").put({ id: mapId(s.id), account_id: mapId(s.account_id), owner_name: s.owner_name || "", name: s.name || "", task_type: s.task_type || "world", has_bonus: s.has_bonus ?? 0, bonus_deadline: s.bonus_deadline ?? null, completed_at: s.completed_at ?? null, active: s.active ?? 1, deleted: 0, created_at: s.created_at || nowText(), updated_at: nowText() }));
    for (const p of payload.carePlans || []) await reqP(tx("care_plans", "readwrite").put({ id: mapId(p.id), name: p.name, tasks: p.tasks || "[]", deleted: 0, created_at: p.created_at || nowText(), updated_at: nowText() }));
  }
  async function resetDatabase() { for (const store of STORES) await reqP(tx(store, "readwrite").clear()); }

  /* ---------- 凭据（Web Crypto 暂存明文占位；不参与同步） ---------- */
  async function getCredentials(id) { const a = await getOne("accounts", id); if (!a) throw new Error("没有找到该号主"); const c = a.credentials; return c ? { username: c.username || "", password: c.password || "", note: c.note || "" } : { username: "", password: "", note: "" }; }
  async function setCredentials(id, p) {
    const a = await getOne("accounts", id); if (!a) throw new Error("没有找到该号主");
    const u = String(p.username || "").trim().slice(0, 200), pw = String(p.password || "").trim().slice(0, 500), note = String(p.note || "").trim().slice(0, 200);
    a.credentials = (!u && !pw && !note) ? null : { username: u, password: pw, note }; await putRec("accounts", a);
  }

  /* ---------- 路由 ---------- */
  const routes = [
    ["GET", /^\/api\/state$/, (m, b, q) => loadState(q.date || gameToday())],
    ["GET", /^\/api\/settings$/, () => getScheduleSettings()],
    ["GET", /^\/api\/history$/, (m, b, q) => listHistory(q)],
    ["GET", /^\/api\/export$/, () => buildBackup()],
    ["GET", /^\/api\/update\/check$/, () => ({ current: "PWA", latest: "PWA", hasUpdate: false, downloadUrl: null })],
    ["GET", /^\/api\/update\/progress$/, () => ({ status: "idle", downloaded: 0, total: 0, error: "" })],
    ["POST", /^\/api\/shutdown$/, () => null],
    ["GET", /^\/api\/heartbeat$/, () => null],
    ["POST", /^\/api\/import$/, (m, b) => importBackup(b)],
    ["POST", /^\/api\/reset$/, () => resetDatabase()],
    ["POST", /^\/api\/accounts$/, (m, b) => createAccount(b)],
    ["PUT", /^\/api\/accounts\/([^/]+)$/, (m, b) => updateAccount(m[1], b)],
    ["DELETE", /^\/api\/accounts\/([^/]+)$/, (m) => archiveAccount(m[1])],
    ["POST", /^\/api\/accounts\/([^/]+)\/reactivate$/, (m) => reactivateAccount(m[1])],
    ["POST", /^\/api\/accounts\/([^/]+)\/purge$/, (m) => purgeAccount(m[1])],
    ["POST", /^\/api\/accounts\/reorder$/, (m, b) => reorderAccounts(b)],
    ["POST", /^\/api\/accounts\/([^/]+)\/task-tags$/, (m, b) => setAccountTaskTag(m[1], b)],
    ["POST", /^\/api\/accounts\/([^/]+)\/custom-tags$/, (m, b) => setAccountCustomTag(m[1], b)],
    ["GET", /^\/api\/accounts\/([^/]+)\/credentials$/, (m) => getCredentials(m[1])],
    ["PUT", /^\/api\/accounts\/([^/]+)\/credentials$/, (m, b) => setCredentials(m[1], b)],
    ["DELETE", /^\/api\/accounts\/([^/]+)\/credentials$/, (m) => setCredentials(m[1], {})],
    ["POST", /^\/api\/tasks$/, () => { throw new Error("手机版暂不支持新增自定义任务，请在电脑版配置后导出导入"); }],
    ["PUT", /^\/api\/tasks\/([^/]+)$/, () => { throw new Error("手机版暂不支持编辑自定义任务，请在电脑版修改后导出导入"); }],
    ["DELETE", /^\/api\/tasks\/([^/]+)$/, (m) => archiveTask(m[1])],
    ["POST", /^\/api\/tasks\/([^/]+)\/notes$/, (m, b) => setTaskNotes(m[1], b)],
    ["POST", /^\/api\/tasks\/([^/]+)\/toggle$/, (m, b) => toggleTask(m[1], b.date, !!b.completed, b.usedAt, !!b.restartCycle)],
    ["POST", /^\/api\/tasks\/complete-all$/, (m, b) => completeAll(b.date, b.taskIds || [])],
    ["POST", /^\/api\/story-tasks$/, (m, b) => createStoryTask(b)],
    ["POST", /^\/api\/story-tasks\/([^/]+)\/toggle$/, (m, b) => toggleStoryTask(m[1], !!b.completed)],
    ["DELETE", /^\/api\/story-tasks\/([^/]+)$/, (m) => archiveStoryTask(m[1])],
    ["GET", /^\/api\/custom-tags$/, () => listCustomTags()],
    ["POST", /^\/api\/custom-tags$/, (m, b) => createCustomTag(b)],
    ["DELETE", /^\/api\/custom-tags\/([^/]+)$/, (m) => deleteCustomTag(m[1])],
    ["POST", /^\/api\/custom-tags\/([^/]+)\/enable-all$/, (m) => enableCustomTagForAll(m[1])],
    ["POST", /^\/api\/care-plans$/, (m, b) => createCarePlan(b)],
    ["PUT", /^\/api\/care-plans\/([^/]+)$/, (m, b) => updateCarePlan(m[1], b)],
    ["DELETE", /^\/api\/care-plans\/([^/]+)$/, (m) => deleteCarePlan(m[1])],
    ["PUT", /^\/api\/settings\/version$/, (m, b) => updateVersionStart(b)],
    ["POST", /^\/api\/update\/apply$/, () => { throw new Error("手机版通过网页自动更新，无需手动更新"); }],
  ];

  async function listHistory(q) {
    const start = q.start || "0001-01-01", end = q.end || gameToday(), accId = q.accountId || null;
    const tasks = new Map((await getAll("tasks")).map((t) => [t.id, t]));
    const accById = new Map((await getAll("accounts")).map((a) => [a.id, a]));
    return (await getAll("task_records")).filter((r) => r.task_date >= start && r.task_date <= end).map((r) => {
      const t = tasks.get(r.task_id) || {}; const a = accById.get(t.account_id) || {};
      return { id: r.id, task_date: r.task_date, completed_at: r.completed_at, note: r.note, task_name: t.name, account_name: a.name, account_id: t.account_id };
    }).filter((row) => !accId || row.account_id === accId)
      .sort((a, b) => b.task_date.localeCompare(a.task_date) || String(b.completed_at).localeCompare(String(a.completed_at)));
  }

  async function handle(path, options = {}) {
    await openDB();
    const method = (options.method || "GET").toUpperCase();
    const [rawPath, rawQuery] = path.split("?");
    const query = {}; if (rawQuery) for (const kv of rawQuery.split("&")) { const [k, v] = kv.split("="); query[decodeURIComponent(k)] = decodeURIComponent(v || ""); }
    let body = {}; if (options.body) { try { body = JSON.parse(options.body); } catch { body = {}; } }
    if (method === "GET" && rawPath === "/api/state") await cleanupExpiredActivities();
    for (const [m, re, fn] of routes) {
      if (m !== method) continue;
      const match = re.exec(rawPath);
      if (match) { const data = await fn(match, body, query); return data === undefined ? null : data; }
    }
    throw new Error(`接口不存在: ${method} ${rawPath}`);
  }

  // 门控：仅在静态托管（非本机服务器，如 GitHub Pages）下启用；
  // 本机 127.0.0.1/localhost 走真实 Python 服务器。测试可用 ?local=1 强制启用。
  const host = location.hostname;
  const isServerMode = (host === "127.0.0.1" || host === "localhost") && !location.search.includes("local=1");
  if (!isServerMode) {
    window.LOCAL_BACKEND = { handle, _debug: { loadState, buildBackup, importBackup } };
    document.documentElement.classList.add("pwa-mode");
  }
})();
