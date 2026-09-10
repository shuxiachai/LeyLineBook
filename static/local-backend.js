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
  const DB_VERSION = 2;
  const STORES = ["accounts", "tasks", "task_records", "custom_task_tags", "care_plans", "story_tasks", "app_meta", "backup_snapshots"];
  const IMPORT_STORES = ["accounts", "tasks", "task_records", "custom_task_tags", "care_plans", "story_tasks"];
  const BACKUP_FORMAT = "leylinebook-backup";
  const BACKUP_SCHEMA_VERSION = 3;
  const APP_VERSION = "3.0.5";
  const SNAPSHOT_LIMIT = 5;

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
  const TASK_RECURRENCES = new Set(["daily", "weekly", "interval", "once", "monthly", "version"]);

  /* ---------- 时间 / 游戏日 ---------- */
  const pad = (n) => String(n).padStart(2, "0");
  const isoDate = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  function parseDate(value) {
    const text = String(value || "").trim();
    if (!/^\d{4}-\d{2}-\d{2}$/.test(text)) throw new Error("日期格式无效");
    const [year, month, day] = text.split("-").map(Number);
    const parsed = new Date(year, month - 1, day);
    if (isoDate(parsed) !== text) throw new Error("日期格式无效");
    return parsed;
  }
  const addDays = (d, n) => { const r = new Date(d); r.setDate(r.getDate() + n); return r; };
  const calendarDayNumber = (d) => Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()) / 86400000;
  function gameToday() { const d = new Date(); d.setHours(d.getHours() - 4); return isoDate(d); }
  function gameDayEnd(refStr) { const d = addDays(parseDate(refStr), 1); d.setHours(4, 0, 0, 0); return d; }
  function nowText() { const d = new Date(); return `${isoDate(d)}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`; }
  // Python weekday(): 周一=0..周日=6
  const pyWeekday = (d) => (d.getDay() + 6) % 7;

  function weeklyCycleStart(refStr, current = new Date()) {
    const ref = parseDate(refStr);
    let monday = addDays(ref, -pyWeekday(ref));
    if (refStr === isoDate(current) && pyWeekday(ref) === 0 && current.getHours() < 4) {
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
    const diffDays = calendarDayNumber(ref) - calendarDayNumber(anchorD);
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
    const parsed = new Date(text);
    if (Number.isNaN(parsed.getTime())) throw new Error("任务到期时间格式无效");
    return parsed;
  }

  function optionalLocalDateTime(value, current = new Date()) {
    const text = String(value || "").trim();
    if (!text) return null;
    const match = /^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/.exec(text);
    if (!match) throw new Error("使用时间应为本地时间");
    const day = parseDate(match[1]);
    const hours = Number(match[2]);
    const minutes = Number(match[3]);
    const seconds = Number(match[4] || 0);
    if (hours > 23 || minutes > 59 || seconds > 59) throw new Error("使用时间格式无效");
    const parsed = new Date(day.getFullYear(), day.getMonth(), day.getDate(), hours, minutes, seconds);
    if (parsed > new Date(current.getTime() + 5 * 60000)) throw new Error("使用时间不能晚于当前时间");
    parsed.setSeconds(0, 0);
    return parsed;
  }

  const uuid = () => (crypto.randomUUID ? crypto.randomUUID() : "id-" + Date.now() + "-" + Math.random().toString(16).slice(2));

  /* ---------- IndexedDB 封装 ---------- */
  let _db = null;
  let activeTransaction = null;
  let operationQueue = Promise.resolve();
  function openDB() {
    return new Promise((resolve, reject) => {
      if (_db) return resolve(_db);
      const req = indexedDB.open(DB_NAME, DB_VERSION);
      req.onupgradeneeded = (event) => {
        const db = req.result;
        for (const name of STORES) {
          if (!db.objectStoreNames.contains(name)) {
            db.createObjectStore(name, { keyPath: name === "app_meta" ? "key" : "id" });
          }
        }
        if (event.oldVersion < 2 && db.objectStoreNames.contains("accounts")) {
          const cursorRequest = req.transaction.objectStore("accounts").openCursor();
          cursorRequest.onsuccess = () => {
            const cursor = cursorRequest.result;
            if (!cursor) return;
            const account = cursor.value;
            if (Object.prototype.hasOwnProperty.call(account, "credentials")) {
              delete account.credentials;
              cursor.update(account);
            }
            cursor.continue();
          };
        }
      };
      req.onsuccess = () => {
        _db = req.result;
        _db.onversionchange = () => { _db.close(); _db = null; };
        resolve(_db);
      };
      req.onerror = () => reject(req.error);
    });
  }
  // One public operation owns its reads, validation and writes. IndexedDB also
  // serializes overlapping readwrite scopes from other tabs.
  function withTransaction(mode, operation) {
    const run = async () => {
      await openDB();
      return new Promise((resolve, reject) => {
        const transaction = _db.transaction(STORES, mode);
        activeTransaction = transaction;
        let result, failure, finished = false;
        transaction.oncomplete = () => {
          activeTransaction = null;
          if (finished) resolve(result);
          else reject(new Error("本地事务提前结束，请重试"));
        };
        transaction.onerror = (event) => { failure = failure || event.target.error || transaction.error; };
        transaction.onabort = () => {
          activeTransaction = null;
          reject(failure || transaction.error || new Error("本地数据写入失败"));
        };
        Promise.resolve().then(operation).then((value) => { result = value; finished = true; }, (error) => {
          failure = error;
          try { transaction.abort(); } catch { reject(error); }
        });
      });
    };
    const result = operationQueue.then(run, run);
    operationQueue = result.catch(() => {});
    return result;
  }
  function tx(store) {
    if (!activeTransaction) throw new Error("本地操作缺少事务");
    return activeTransaction.objectStore(store);
  }
  const reqP = (r) => new Promise((res, rej) => { r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error); });
  const getAll = (store) => reqP(tx(store, "readonly").getAll());
  const getOne = (store, id) => reqP(tx(store, "readonly").get(id));
  async function putRec(store, rec) { rec.updated_at = nowText(); await reqP(tx(store, "readwrite").put(rec)); return rec; }
  const delRec = (store, id) => reqP(tx(store, "readwrite").delete(id));

  function runAtomic(storeNames, queueRequests) {
    if (!activeTransaction || activeTransaction.mode !== "readwrite") throw new Error("本地操作缺少写事务");
    queueRequests(activeTransaction);
    return Promise.resolve();
  }

  function applyBatch({ puts = {}, deletes = {}, clears = [] }) {
    const storeNames = [...new Set([...Object.keys(puts), ...Object.keys(deletes), ...clears])];
    if (!storeNames.length) return Promise.resolve();
    const updatedAt = nowText();
    return runAtomic(storeNames, (transaction) => {
      for (const storeName of clears) transaction.objectStore(storeName).clear();
      for (const [storeName, records] of Object.entries(puts)) {
        const store = transaction.objectStore(storeName);
        for (const record of records) {
          record.updated_at = updatedAt;
          store.put(record);
        }
      }
      for (const [storeName, ids] of Object.entries(deletes)) {
        const store = transaction.objectStore(storeName);
        for (const id of ids) store.delete(id);
      }
    });
  }

  async function metaGet(key, fallback) { const r = await getOne("app_meta", key); return r ? r.value : fallback; }
  async function metaSet(key, value) { await reqP(tx("app_meta", "readwrite").put({ key, value })); }

  const liveAccounts = (rows) => rows.filter((a) => !a.deleted);
  const activeTasks = (rows) => rows.filter((t) => t.active && !t.deleted);
  const canonicalName = (value) => String(value || "").trim().toLocaleLowerCase();
  function ensureUniqueName(rows, name, excludeId = null, ignoreDeleted = false) {
    const wanted = canonicalName(name);
    if (rows.some((row) => row.id !== excludeId && (!ignoreDeleted || !row.deleted) && canonicalName(row.name) === wanted)) {
      throw new Error("名称已存在");
    }
  }

  async function getVersionAnchor() {
    let a = await metaGet("version_anchor_date", null);
    if (!a) a = OFFICIAL_VERSION_ANCHOR;
    return a;
  }
  async function getScheduleSettings() {
    const anchor = await getVersionAnchor();
    const win = versionWindow(anchor);
    return { versionAnchorDate: anchor, versionStartDate: win.versionStart, warWindow: win };
  }

  /* ---------- load_state（读路径核心） ---------- */
  async function loadState(selectedDate) {
    parseDate(selectedDate);
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
    const now = new Date();
    const nowMs = now.getTime();
    const isCurrentGameDay = selectedDate === gameToday();
    const selectedGameDayEnd = gameDayEnd(selectedDate);
    const selectedWeekKey = weeklyCycleKey(selectedDate);

    const completedThisDate = (taskId) => records.some((r) => r.task_id === taskId && r.task_date === selectedDate && (!(r.note || "") || r.note.startsWith("expedition:")));
    const everCompleted = new Set(records.map((r) => r.task_id));

    const completedWeekly = new Set();
    const completedWeeklyOnSelected = new Set();
    for (const r of records) {
      if (r.note !== selectedWeekKey) continue;
      completedWeekly.add(r.task_id);
      if (r.task_date === selectedDate) completedWeeklyOnSelected.add(r.task_id);
    }

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
        const doneOnSelected = completedWeeklyOnSelected.has(task.id);
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
        const isDue = task.next_due && (preciseDue
          ? (isCurrentGameDay ? preciseDue <= now : preciseDue < selectedGameDayEnd)
          : task.next_due <= selectedDate);
        if (task.name === "探索派遣" && isCurrentGameDay && isDue) task.completed = false;
        if (preciseDue) {
          task.available_at = `${isoDate(preciseDue)}T${pad(preciseDue.getHours())}:${pad(preciseDue.getMinutes())}`;
          task.cooldown_remaining_seconds = Math.max(0, Math.floor((preciseDue.getTime() - nowMs) / 1000));
        }
        const stillCooling = isCurrentGameDay && preciseDue && preciseDue.getTime() > nowMs;
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

    const longCooling = (t) => !t.completed && t.available_at && new Date(t.available_at) >= selectedGameDayEnd;
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
  async function buildPresetTask(accountId, taskName, notes) {
    const preset = TASK_PRESETS[taskName];
    let nextDue = null;
    if (preset.recurrence === "interval") nextDue = gameToday();
    else if (preset.recurrence === "monthly") nextDue = monthlyOccurrence(gameToday(), preset.monthly_day);
    else if (preset.recurrence === "version") { const w = versionWindow(await getVersionAnchor()); nextDue = w ? w.eventStart : null; }
    return {
      id: uuid(), account_id: accountId, name: taskName, recurrence: preset.recurrence,
      interval_days: preset.interval_days || null, monthly_day: preset.monthly_day || null,
      next_due: nextDue, notes: notes || "", active: 1, deleted: 0,
      sort_order: TASK_SORT_ORDER[taskName] || 0, custom_tag_id: null, created_at: nowText(),
    };
  }

  async function insertPresetTask(accountId, taskName, notes) {
    return putRec("tasks", await buildPresetTask(accountId, taskName, notes));
  }

  function expeditionHours(notes) { return String(notes || "").split(/[、,，]/).some((p) => p.trim() === "派遣:15小时") ? 15 : 20; }

  function prepareTaskToggle(taskRow, records, taskDate, completed, usedAt, restartCycle) {
    parseDate(taskDate);
    if (!taskRow || !taskRow.active || taskRow.deleted) throw new Error("没有找到该任务");
    const task = { ...taskRow };
    const isExpedition = task.name === "探索派遣" && task.recurrence === "interval";
    const used = isExpedition && completed ? (optionalLocalDateTime(usedAt) || optionalLocalDateTime(nowText())) : null;
    let cycleKey = task.recurrence === "weekly" ? weeklyCycleKey(taskDate) : "";
    if (used) cycleKey = `expedition:${isoDate(used)}T${pad(used.getHours())}:${pad(used.getMinutes())}`;
    const taskRecords = records.filter((r) => r.task_id === task.id).sort((a, b) => b.task_date.localeCompare(a.task_date) || b.completed_at.localeCompare(a.completed_at));
    const match = isExpedition
      ? taskRecords.find((r) => completed ? r.note === cycleKey : r.task_date === taskDate)
      : task.recurrence === "weekly"
      ? records.find((r) => r.task_id === task.id && r.note === cycleKey)
      : records.find((r) => r.task_id === task.id && r.task_date === taskDate && (r.note || "") === "");
    const mutation = { task: null, record: null, deleteRecordId: null };

    if (["interval", "monthly", "version"].includes(task.recurrence)) {
      if (!completed && match && taskRecords[0]?.id !== match.id) throw new Error("请先撤销该任务较新的完成记录");
      if (completed && !match && taskRecords[0]?.task_date > taskDate) throw new Error("不能在较新的完成记录之前补记周期任务");
    }
    if (used && !match && taskRecords.length) {
      const due = exactDueMoment(task.next_due);
      if (due && used < due) {
        if (!usedAt) return mutation;
        throw new Error("派遣尚未到期，请检查收取时间");
      }
    }

    if (completed && !match) {
      const previousDue = task.next_due;
      let preciseUsed = null;
      if ((task.name === "质变仪" || task.name === "探索派遣") && task.recurrence === "interval") {
        preciseUsed = used || optionalLocalDateTime(usedAt) || (() => { const d = new Date(); d.setSeconds(0, 0); return d; })();
      }
      const completedAt = preciseUsed ? `${isoDate(preciseUsed)}T${pad(preciseUsed.getHours())}:${pad(preciseUsed.getMinutes())}:00` : nowText();
      mutation.record = { id: uuid(), task_id: task.id, task_date: taskDate, completed_at: completedAt, previous_next_due: previousDue, note: cycleKey, deleted: 0, created_at: nowText() };

      if (task.recurrence === "interval") {
        let nextDue;
        if (preciseUsed && task.name === "质变仪") {
          const intervalDays = Number(task.interval_days);
          if (!Number.isInteger(intervalDays) || intervalDays < 1) throw new Error("任务冷却天数未配置，请检查任务设置");
          const n = new Date(preciseUsed.getTime() + 24 * intervalDays * 3600000); nextDue = `${isoDate(n)}T${pad(n.getHours())}:${pad(n.getMinutes())}`;
        }
        else if (preciseUsed && task.name === "探索派遣") { const n = new Date(preciseUsed.getTime() + expeditionHours(task.notes) * 3600000); nextDue = `${isoDate(n)}T${pad(n.getHours())}:${pad(n.getMinutes())}`; }
        else {
          const intervalDays = Number(task.interval_days);
          if (!Number.isInteger(intervalDays) || intervalDays < 1) throw new Error("任务冷却天数未配置，请检查任务设置");
          const base = restartCycle ? parseDate(taskDate) : new Date(Math.max(parseDate(previousDue || taskDate), parseDate(taskDate)));
          nextDue = isoDate(addDays(base, intervalDays));
        }
        task.next_due = nextDue; mutation.task = task;
      } else if (task.recurrence === "monthly") {
        const monthlyDay = Number(task.monthly_day);
        if (!Number.isInteger(monthlyDay) || monthlyDay < 1 || monthlyDay > 28) throw new Error("每月刷新日期配置无效");
        const base = new Date(Math.max(parseDate(previousDue || taskDate), parseDate(taskDate)));
        const baseStr = isoDate(base);
        task.next_due = base.getDate() < monthlyDay ? monthlyOccurrence(baseStr, monthlyDay) : nextMonthOccurrence(baseStr, monthlyDay);
        mutation.task = task;
      } else if (task.recurrence === "version") { task.next_due = null; mutation.task = task; }
    } else if (!completed && match) {
      if (["interval", "monthly", "version"].includes(task.recurrence)) { task.next_due = match.previous_next_due; mutation.task = task; }
      mutation.deleteRecordId = match.id;
    }
    return mutation;
  }

  async function applyTaskMutations(mutations) {
    const taskPuts = mutations.filter((item) => item.task).map((item) => item.task);
    const recordPuts = mutations.filter((item) => item.record).map((item) => item.record);
    const recordDeletes = mutations.filter((item) => item.deleteRecordId).map((item) => item.deleteRecordId);
    await applyBatch({
      puts: { tasks: taskPuts, task_records: recordPuts },
      deletes: { task_records: recordDeletes },
    });
  }

  async function toggleTask(taskId, taskDate, completed, usedAt, restartCycle) {
    const task = await getOne("tasks", taskId);
    if (!task || !task.active || task.deleted) throw new Error("没有找到该任务");
    const records = await getAll("task_records");
    await applyTaskMutations([prepareTaskToggle(task, records, taskDate, completed, usedAt, restartCycle)]);
  }

  async function completeAll(taskDate, taskIds) {
    if (!Array.isArray(taskIds)) throw new Error("任务列表格式无效");
    parseDate(taskDate);
    const ids = [...new Set(taskIds)];
    const tasksById = new Map((await getAll("tasks")).map((task) => [task.id, task]));
    const records = await getAll("task_records");
    const mutations = [];
    for (const id of ids) {
      const task = tasksById.get(id);
      if (!task || !task.active || task.deleted) throw new Error("没有找到该任务");
      const mutation = prepareTaskToggle(task, records, taskDate, true);
      mutations.push(mutation);
      if (mutation.record) records.push(mutation.record);
    }
    await applyTaskMutations(mutations);
  }

  /* ---------- 号主 ---------- */
  function parseProxyUntil(p) {
    const raw = String((p && p.proxyUntil) || "").trim();
    if (!raw) return null;
    try { parseDate(raw); } catch { throw new Error("截止日期格式无效"); }
    return raw;
  }
  async function createAccount(p) {
    const name = String(p.name || "").trim(); if (!name) throw new Error("请填写号主名称");
    if (name.length > 100) throw new Error("号主名称不能超过 100 个字符");
    const accounts = await getAll("accounts");
    ensureUniqueName(accounts, name);
    const maxOrder = accounts.reduce((m, a) => Math.max(m, a.sort_order || 0), 0);
    let planTasks = [];
    let allTags = [];
    if (p.planId) {
      const plan = await getOne("care_plans", p.planId);
      if (!plan || plan.deleted) throw new Error("托管方案不存在，请刷新后重试");
      let parsedTasks;
      try { parsedTasks = JSON.parse(plan.tasks); } catch { throw new Error("托管方案数据损坏，请重新创建方案"); }
      planTasks = JSON.parse(parsePlanTasks({ tasks: parsedTasks }));
      allTags = (await getAll("custom_task_tags")).filter((t) => !t.deleted);
    }
    const acc = { id: uuid(), name: name.slice(0, 100), owner: String(p.owner || "").trim().slice(0, 100), notes: String(p.notes || "").trim().slice(0, 500), proxy_until: parseProxyUntil(p), active: 1, deleted: 0, sort_order: maxOrder + 1, created_at: nowText() };
    const newTasks = [];
    const dailyTask = String(p.dailyTask || "").trim();
    if (dailyTask) newTasks.push({ id: uuid(), account_id: acc.id, name: dailyTask.slice(0, 100), recurrence: "daily", interval_days: null, monthly_day: null, next_due: null, notes: "", active: 1, deleted: 0, sort_order: 0, custom_tag_id: null, created_at: nowText() });
    for (const taskName of planTasks) {
      if (Object.hasOwn(TASK_PRESETS, taskName)) newTasks.push(await buildPresetTask(acc.id, taskName));
      else if (Object.hasOwn(VALID_DURATIONS, taskName)) {
        for (const tag of allTags.filter((item) => item.category === taskName)) {
          newTasks.push({ id: uuid(), account_id: acc.id, name: tag.name, recurrence: "once", interval_days: null, monthly_day: null, next_due: tag.start_date || gameToday(), notes: "", active: 1, deleted: 0, sort_order: ACTIVITY_TASK_SORT_ORDER, custom_tag_id: tag.id, created_at: nowText() });
        }
      }
    }
    await applyBatch({ puts: { accounts: [acc], tasks: newTasks } });
    return { ...acc };
  }
  async function updateAccount(id, p) {
    const acc = await getOne("accounts", id); if (!acc || !acc.active || acc.deleted) throw new Error("没有找到该号主");
    const name = String(p.name || "").trim(); if (!name) throw new Error("请填写号主名称");
    if (name.length > 100) throw new Error("号主名称不能超过 100 个字符");
    ensureUniqueName(await getAll("accounts"), name, id);
    Object.assign(acc, { name: name.slice(0, 100), owner: String(p.owner || "").trim().slice(0, 100), notes: String(p.notes || "").trim().slice(0, 500), proxy_until: parseProxyUntil(p) });
    await putRec("accounts", acc);
  }
  async function archiveAccount(id) { const a = await getOne("accounts", id); if (!a || !a.active || a.deleted) throw new Error("没有找到该号主"); a.active = 0; await putRec("accounts", a); }
  async function reactivateAccount(id) { const a = await getOne("accounts", id); if (!a || a.active || a.deleted) throw new Error("没有找到该号主"); a.active = 1; await putRec("accounts", a); }
  async function purgeAccount(id) {
    const a = await getOne("accounts", id); if (!a || a.deleted) throw new Error("没有找到该号主");
    a.deleted = 1; a.active = 0;
    const tasks = (await getAll("tasks")).filter((task) => task.account_id === id && !task.deleted);
    for (const task of tasks) { task.deleted = 1; task.active = 0; }
    await applyBatch({ puts: { accounts: [a], tasks } });
  }
  async function reorderAccounts(p) {
    const ids = p.accountIds;
    if (!Array.isArray(ids) || !ids.length) throw new Error("号主顺序不能为空");
    if (ids.length !== new Set(ids).size) throw new Error("号主顺序中存在重复项");
    const activeAccounts = liveAccounts(await getAll("accounts")).filter((account) => account.active);
    const activeById = new Map(activeAccounts.map((account) => [account.id, account]));
    if (ids.length !== activeAccounts.length || ids.some((id) => !activeById.has(id))) {
      throw new Error("号主顺序与当前名单不一致，请刷新后重试");
    }
    const reordered = ids.map((id, sortOrder) => ({ ...activeById.get(id), sort_order: sortOrder }));
    await applyBatch({ puts: { accounts: reordered } });
  }

  /* ---------- 号主任务标签 / 备注 ---------- */
  async function setAccountTaskTag(accountId, p) {
    const taskName = String(p.tag || "").trim(); if (!Object.hasOwn(TASK_PRESETS, taskName)) throw new Error("任务标签无效");
    const account = await getOne("accounts", accountId);
    if (!account || !account.active || account.deleted) throw new Error("没有找到该号主");
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
    if (name.length > 100) throw new Error("活动名称不能超过 100 个字符");
    if (RESERVED_ACTIVITY_NAMES.has(name)) throw new Error("活动名称不能与内置任务重名");
    ensureUniqueName(await getAll("custom_task_tags"), name, null, true);
    const category = String(p.category || "").trim(); if (!Object.hasOwn(VALID_DURATIONS, category)) throw new Error("活动类型无效");
    const duration = Number(p.durationDays || 0); if (!Number.isInteger(duration) || duration < 1 || duration > 365) throw new Error("活动时长无效，应为 1 到 365 天");
    const raw = String(p.startDate || "").trim(); const start = raw || gameToday();
    try { parseDate(start); } catch { throw new Error("开始日期格式无效"); }
    return putRec("custom_task_tags", { id: uuid(), name: name.slice(0, 100), category, duration_days: duration, start_date: start, deleted: 0, created_at: nowText() });
  }
  async function deleteCustomTag(id) {
    const tag = await getOne("custom_task_tags", id); if (!tag) throw new Error("没有找到该任务标签");
    tag.deleted = 1;
    const tasks = (await getAll("tasks")).filter((task) => task.custom_tag_id === id && task.active);
    for (const task of tasks) task.active = 0;
    await applyBatch({ puts: { custom_task_tags: [tag], tasks } });
  }
  async function setAccountCustomTag(accountId, p) {
    const tagId = p.tagId; if (!tagId) throw new Error("请选择有效的活动标签");
    const tag = await getOne("custom_task_tags", tagId); if (!tag || tag.deleted) throw new Error("自定义任务标签不存在");
    const account = await getOne("accounts", accountId);
    if (!account || !account.active || account.deleted) throw new Error("没有找到该号主");
    const enabled = !!p.enabled;
    const existing = activeTasks(await getAll("tasks")).find((t) => t.account_id === accountId && t.custom_tag_id === tagId);
    if (!enabled) { if (existing) { existing.active = 0; await putRec("tasks", existing); } return; }
    if (existing) return;
    await putRec("tasks", { id: uuid(), account_id: accountId, name: tag.name, recurrence: "once", interval_days: null, monthly_day: null, next_due: tag.start_date || gameToday(), notes: "", active: 1, deleted: 0, sort_order: ACTIVITY_TASK_SORT_ORDER, custom_tag_id: tagId, created_at: nowText() });
  }
  async function enableCustomTagForAll(tagId) {
    const tag = await getOne("custom_task_tags", tagId); if (!tag || tag.deleted) throw new Error("自定义任务标签不存在");
    const accounts = liveAccounts(await getAll("accounts")).filter((a) => a.active);
    const tasks = activeTasks(await getAll("tasks"));
    const newTasks = [];
    for (const acc of accounts) {
      if (tasks.some((t) => t.account_id === acc.id && t.custom_tag_id === tagId)) continue;
      newTasks.push({ id: uuid(), account_id: acc.id, name: tag.name, recurrence: "once", interval_days: null, monthly_day: null, next_due: tag.start_date || gameToday(), notes: "", active: 1, deleted: 0, sort_order: ACTIVITY_TASK_SORT_ORDER, custom_tag_id: tagId, created_at: nowText() });
    }
    await applyBatch({ puts: { tasks: newTasks } });
    return { enabled: newTasks.length };
  }
  async function cleanupExpiredActivities() {
    const now = new Date();
    const expiredTags = [];
    for (const tag of (await getAll("custom_task_tags")).filter((item) => !item.deleted && item.start_date)) {
      const end = new Date(addDays(parseDate(tag.start_date), Number(tag.duration_days)).getTime()); end.setHours(3, 59, 0, 0);
      if (end < now) { tag.deleted = 1; expiredTags.push(tag); }
    }
    if (!expiredTags.length) return;
    const expiredIds = new Set(expiredTags.map((tag) => tag.id));
    const tasks = (await getAll("tasks")).filter((task) => expiredIds.has(task.custom_tag_id) && task.active);
    for (const task of tasks) task.active = 0;
    await applyBatch({ puts: { custom_task_tags: expiredTags, tasks } });
  }

  /* ---------- 托管方案 ---------- */
  function parsePlanTasks(p) {
    const raw = p.tasks; if (!Array.isArray(raw) || !raw.length) throw new Error("方案至少需要勾选一个任务");
    const tasks = []; for (const v of raw) { const n = String(v).trim(); if (!Object.hasOwn(TASK_PRESETS, n) && !Object.hasOwn(VALID_DURATIONS, n)) throw new Error(`方案中包含无效任务：${n}`); if (!tasks.includes(n)) tasks.push(n); }
    return JSON.stringify(tasks);
  }
  async function createCarePlan(p) { const name = String(p.name || "").trim(); if (!name) throw new Error("请填写方案名称"); if (name.length > 20) throw new Error("方案名称不能超过 20 个字符"); ensureUniqueName(await getAll("care_plans"), name, null, true); const plan = await putRec("care_plans", { id: uuid(), name, tasks: parsePlanTasks(p), deleted: 0, created_at: nowText() }); return { ...plan, tasks: JSON.parse(plan.tasks) }; }
  async function updateCarePlan(id, p) { const plan = await getOne("care_plans", id); if (!plan || plan.deleted) throw new Error("没有找到该托管方案"); const name = String(p.name || "").trim(); if (!name) throw new Error("请填写方案名称"); if (name.length > 20) throw new Error("方案名称不能超过 20 个字符"); ensureUniqueName(await getAll("care_plans"), name, id, true); plan.name = name; plan.tasks = parsePlanTasks(p); await putRec("care_plans", plan); }
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
    return (await getAll("story_tasks")).filter((story) => {
      if (!story.active || story.deleted) return false;
      if (!story.account_id) return true;
      const account = accById.get(story.account_id);
      return Boolean(account && !account.deleted);
    }).map((s) => ({ ...s, account_name: (s.owner_name || (accById.get(s.account_id) || {}).name || "临时号主") }))
      .sort((a, b) => (Number(!!a.completed_at) - Number(!!b.completed_at)) || String(a.created_at).localeCompare(String(b.created_at)));
  }
  async function createStoryTask(p) {
    const taskType = String(p.taskType || "").trim(); if (!Object.hasOwn(STORY_TASK_TYPES, taskType)) throw new Error("请选择剧情任务类型");
    const name = String(p.name || "").trim().slice(0, 100) || STORY_TASK_TYPES[taskType];
    const ownerName = String(p.ownerName || "").trim().slice(0, 100);
    let accountId = p.accountId || null; if (ownerName) accountId = null;
    if (!ownerName && !accountId) throw new Error("请选择号主或填写临时号主");
    if (accountId) {
      const account = await getOne("accounts", accountId);
      if (!account || !account.active || account.deleted) throw new Error("请选择有效的号主");
    }
    const hasBonus = !!p.hasBonus && taskType !== "world";
    const deadline = hasBonus ? await storyBonusDeadline(taskType) : null;
    return putRec("story_tasks", { id: uuid(), account_id: accountId, owner_name: ownerName, name, task_type: taskType, has_bonus: hasBonus ? 1 : 0, bonus_deadline: deadline, completed_at: null, active: 1, deleted: 0, created_at: nowText() });
  }
  async function toggleStoryTask(id, completed) { const s = await getOne("story_tasks", id); if (!s || !s.active || s.deleted) throw new Error("没有找到该剧情任务"); s.completed_at = completed ? nowText() : null; await putRec("story_tasks", s); }
  async function archiveStoryTask(id) { const s = await getOne("story_tasks", id); if (!s || !s.active || s.deleted) throw new Error("没有找到该剧情任务"); s.active = 0; await putRec("story_tasks", s); }

  /* ---------- 危战锚点 ---------- */
  async function updateVersionStart(p) {
    const raw = String(p.versionStartDate || "").trim();
    try { parseDate(raw); } catch { throw new Error("请选择有效的版本开始日期"); }
    if (pyWeekday(parseDate(raw)) !== 2) throw new Error("版本开始日期应为星期三");
    const warWindow = versionWindow(raw);
    const tasks = (await getAll("tasks")).filter((task) => task.recurrence === "version" && task.active && !task.deleted);
    for (const task of tasks) task.next_due = warWindow.eventStart;
    await applyBatch({ puts: { app_meta: [{ key: "version_anchor_date", value: raw }], tasks } });
    return { versionAnchorDate: raw, versionStartDate: warWindow.versionStart, warWindow };
  }

  /* ---------- 备份 导出 / 导入 ---------- */
  async function buildBackup() {
    return {
      format: BACKUP_FORMAT,
      schemaVersion: BACKUP_SCHEMA_VERSION,
      appVersion: APP_VERSION,
      exportedAt: nowText(),
      data: {
        accounts: (await getAll("accounts")).map((a) => { const c = { ...a }; delete c.credentials; return c; }),
        tasks: await getAll("tasks"), records: await getAll("task_records"),
        storyTasks: await getAll("story_tasks"), customTags: await getAll("custom_task_tags"),
        carePlans: await getAll("care_plans"), groupNotes: [],
        settings: { versionAnchorDate: await getVersionAnchor() },
      },
    };
  }

  function normalizeBackupPayload(payload) {
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
      throw new Error("备份文件格式无效，请选择由本程序导出的 JSON 文件");
    }
    if (payload.schemaVersion == null && Array.isArray(payload.accounts)) return payload;
    if (payload.format !== BACKUP_FORMAT) {
      throw new Error("备份文件格式无效，请选择由本程序导出的 JSON 文件");
    }
    if (![2, BACKUP_SCHEMA_VERSION].includes(payload.schemaVersion)) {
      throw new Error(`不支持的备份版本：${String(payload.schemaVersion ?? "未知")}`);
    }
    if (!payload.data || typeof payload.data !== "object" || Array.isArray(payload.data)) {
      throw new Error("备份文件格式无效，请选择由本程序导出的 JSON 文件");
    }
    return payload.data;
  }

  function getImportRows(payload, key) {
    const rows = payload[key] ?? [];
    if (!Array.isArray(rows)) throw new Error("备份文件格式无效，请选择由本程序导出的 JSON 文件");
    if (rows.some((row) => !row || typeof row !== "object" || Array.isArray(row))) {
      throw new Error("备份文件格式无效，请选择由本程序导出的 JSON 文件");
    }
    return rows;
  }

  function createImportIdMap(rows, label) {
    const ids = new Map();
    for (const row of rows) {
      if (row.id == null) throw new Error(`备份中的${label}缺少 ID`);
      const oldId = String(row.id);
      if (ids.has(oldId)) throw new Error(`备份中的${label} ID 重复`);
      ids.set(oldId, uuid());
    }
    return ids;
  }

  function requireMappedId(ids, oldId, label) {
    if (oldId == null || !ids.has(String(oldId))) throw new Error(`备份中的${label}引用不存在`);
    return ids.get(String(oldId));
  }

  function optionalMappedId(ids, oldId, label) {
    if (oldId == null) return null;
    return requireMappedId(ids, oldId, label);
  }

  function normalizeImportedPlanTasks(value) {
    let tasks = value;
    if (typeof tasks === "string") {
      try { tasks = JSON.parse(tasks); } catch { throw new Error("备份中的托管方案数据无效"); }
    }
    try { return parsePlanTasks({ tasks }); } catch { throw new Error("备份中的托管方案数据无效"); }
  }

  function importedRecurrence(value) {
    const recurrence = value === undefined ? "daily" : value;
    if (recurrence === "manual") return "once";
    if (!TASK_RECURRENCES.has(recurrence)) throw new Error("备份中的任务类型无效");
    return recurrence;
  }

  function validateBackupSource(source) {
    for (const [collection, rows] of Object.entries(source)) {
      const names = new Set();
      for (const row of rows) {
        for (const [field, limit] of [["name", 100], ["owner", 100], ["owner_name", 100], ["notes", 2000], ["note", 2000]]) {
          if (field in row && (typeof row[field] !== "string" || row[field].length > limit)) throw new Error(`备份中的 ${field} 字段无效`);
        }
        if (["accounts", "tasks", "story_tasks", "custom_task_tags", "care_plans"].includes(collection) && !String(row.name || "").trim()) throw new Error("备份中的名称不能为空");
        if (["accounts", "custom_task_tags", "care_plans"].includes(collection) && (collection === "accounts" || !row.deleted)) {
          const name = canonicalName(row.name);
          if (names.has(name)) throw new Error("备份中的名称重复");
          names.add(name);
        }
        for (const field of ["active", "deleted", "has_bonus"]) {
          if (field in row && ![0, 1, false, true].includes(row[field])) throw new Error(`备份中的 ${field} 状态无效`);
        }
        if ("sort_order" in row && (!Number.isInteger(row.sort_order) || row.sort_order < 0 || row.sort_order > 2147483647)) throw new Error("备份中的排序值无效");
        for (const field of ["proxy_until", "start_date", "task_date"]) if (row[field] != null && row[field] !== "") parseDate(row[field]);
        for (const field of ["next_due", "previous_next_due", "completed_at", "bonus_deadline", "created_at"]) validateImportedMoment(row[field]);
      }
    }
    for (const account of source.accounts) {
      if (!String(account.name || "").trim()) throw new Error("备份中的号主名称不能为空");
      if (account.proxy_until) {
        try { parseDate(account.proxy_until); } catch { throw new Error("备份中的号主截止日期无效"); }
      }
    }
    for (const tag of source.custom_task_tags) {
      if (tag.deleted) continue;
      if (!String(tag.name || "").trim()) throw new Error("备份中的活动名称不能为空");
      if (!Object.hasOwn(VALID_DURATIONS, tag.category === undefined ? "大活动" : tag.category)) throw new Error("备份中的活动类型无效");
      const duration = tag.duration_days === undefined ? 16 : tag.duration_days;
      if (!Number.isInteger(duration) || duration < 1 || duration > 365) throw new Error("备份中的活动时长无效");
      if (tag.start_date) {
        try { parseDate(tag.start_date); } catch { throw new Error("备份中的活动开始日期无效"); }
      }
    }
    for (const task of source.tasks) {
      if (!String(task.name || "").trim()) throw new Error("备份中的任务名称不能为空");
      const recurrence = importedRecurrence(task.recurrence);
      const precise = recurrence === "interval" && ["探索派遣", "质变仪"].includes(task.name);
      if (task.next_due && !precise) parseDate(task.next_due);
      if (task.next_due) {
        const due = String(task.next_due);
        if (due.includes("T")) {
          if (Number.isNaN(new Date(due).getTime())) throw new Error("备份中的任务到期时间无效");
        } else {
          try { parseDate(due); } catch { throw new Error("备份中的任务到期日期无效"); }
        }
      }
      if (recurrence === "monthly") {
        const monthlyDay = task.monthly_day;
        if (!Number.isInteger(monthlyDay) || monthlyDay < 1 || monthlyDay > 28) throw new Error("备份中的每月任务配置无效");
      }
      if (recurrence === "interval" && task.name !== "探索派遣") {
        const intervalDays = task.interval_days;
        if (!Number.isInteger(intervalDays) || intervalDays < 1 || intervalDays > 365) throw new Error("备份中的周期任务配置无效");
      }
    }
    const recordKeys = new Set();
    const tasksById = new Map(source.tasks.map((task) => [String(task.id), task]));
    for (const record of source.task_records) {
      try { parseDate(record.task_date); } catch { throw new Error("备份中的完成记录日期无效"); }
      const key = JSON.stringify([String(record.task_id), record.task_date, record.note || ""]);
      if (recordKeys.has(key)) throw new Error("备份中的完成记录重复");
      recordKeys.add(key);
      const task = tasksById.get(String(record.task_id));
      const precise = task?.recurrence === "interval" && ["探索派遣", "质变仪"].includes(task.name);
      if (record.previous_next_due && !precise) parseDate(record.previous_next_due);
    }
    for (const story of source.story_tasks) {
      if (!Object.hasOwn(STORY_TASK_TYPES, story.task_type === undefined ? "world" : story.task_type)) throw new Error("备份中的剧情任务类型无效");
      if (!String(story.name || "").trim()) throw new Error("备份中的剧情任务名称不能为空");
    }
  }

  function validateImportedMoment(value) {
    if (value == null || value === "") return;
    if (typeof value !== "string") throw new Error("备份中的日期格式无效");
    if (/^\d{4}-\d{2}-\d{2}$/.test(value)) { parseDate(value); return; }
    const match = /^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})(?::(\d{2})(?:\.\d{1,6})?)?$/.exec(value);
    if (!match) throw new Error("备份中的日期格式无效");
    parseDate(match[1]);
    if (Number(match[2]) > 23 || Number(match[3]) > 59 || Number(match[4] || 0) > 59) throw new Error("备份中的日期格式无效");
  }

  function prepareBackupImport(payload) {
    const data = normalizeBackupPayload(payload);
    if (!Array.isArray(data.accounts)) {
      throw new Error("备份文件格式无效，请选择由本程序导出的 JSON 文件");
    }
    if (data.settings !== undefined && (!data.settings || typeof data.settings !== "object" || Array.isArray(data.settings))) throw new Error("备份中的设置格式无效");
    if (data.settings && "versionAnchorDate" in data.settings) parseDate(data.settings.versionAnchorDate);

    const source = {
      accounts: getImportRows(data, "accounts"),
      tasks: getImportRows(data, "tasks"),
      task_records: getImportRows(data, "records"),
      custom_task_tags: getImportRows(data, "customTags"),
      care_plans: getImportRows(data, "carePlans"),
      story_tasks: getImportRows(data, "storyTasks"),
    };
    validateBackupSource(source);
    const accountIds = createImportIdMap(source.accounts, "号主");
    const taskIds = createImportIdMap(source.tasks, "任务");
    const recordIds = createImportIdMap(source.task_records, "完成记录");
    const tagIds = createImportIdMap(source.custom_task_tags, "活动标签");
    const planIds = createImportIdMap(source.care_plans, "托管方案");
    const storyIds = createImportIdMap(source.story_tasks, "剧情任务");
    const importedAt = nowText();
    const anchor = data.settings?.versionAnchorDate || OFFICIAL_VERSION_ANCHOR;
    parseDate(anchor);

    return {
      app_meta: [{ key: "version_anchor_date", value: anchor }],
      accounts: source.accounts.map((account) => ({
        id: accountIds.get(String(account.id)),
        name: account.name || "",
        owner: account.owner || "",
        notes: account.notes || "",
        proxy_until: account.proxy_until ?? null,
        active: account.deleted ? 0 : (account.active ?? 1),
        deleted: account.deleted ? 1 : 0,
        sort_order: account.sort_order ?? 0,
        created_at: account.created_at || importedAt,
        updated_at: importedAt,
      })),
      custom_task_tags: source.custom_task_tags.map((tag) => ({
        id: tagIds.get(String(tag.id)),
        name: tag.name,
        category: tag.category || "大活动",
        duration_days: tag.duration_days ?? 16,
        start_date: tag.start_date ?? null,
        deleted: tag.deleted ? 1 : 0,
        created_at: tag.created_at || importedAt,
        updated_at: importedAt,
      })),
      tasks: source.tasks.map((task) => ({
        id: taskIds.get(String(task.id)),
        account_id: requireMappedId(accountIds, task.account_id, "任务号主"),
        name: task.name || "",
        recurrence: importedRecurrence(task.recurrence),
        interval_days: task.interval_days ?? null,
        monthly_day: task.monthly_day ?? null,
        next_due: task.recurrence === "manual" ? (task.next_due || gameToday()) : (task.next_due ?? null),
        notes: task.notes || "",
        active: task.deleted ? 0 : (task.active ?? 1),
        deleted: task.deleted ? 1 : 0,
        sort_order: task.sort_order ?? 0,
        custom_tag_id: optionalMappedId(tagIds, task.custom_tag_id, "任务活动标签"),
        created_at: task.created_at || importedAt,
        updated_at: importedAt,
      })),
      task_records: source.task_records.map((record) => ({
        id: recordIds.get(String(record.id)),
        task_id: requireMappedId(taskIds, record.task_id, "完成记录任务"),
        task_date: record.task_date || "",
        completed_at: record.completed_at || importedAt,
        previous_next_due: record.previous_next_due ?? null,
        note: record.note || "",
        deleted: 0,
        created_at: importedAt,
        updated_at: importedAt,
      })),
      story_tasks: source.story_tasks.map((story) => ({
        id: storyIds.get(String(story.id)),
        account_id: optionalMappedId(accountIds, story.account_id, "剧情任务号主"),
        owner_name: story.owner_name || "",
        name: story.name || "",
        task_type: story.task_type || "world",
        has_bonus: story.has_bonus ?? 0,
        bonus_deadline: story.bonus_deadline ?? null,
        completed_at: story.completed_at ?? null,
        active: story.deleted ? 0 : (story.active ?? 1),
        deleted: story.deleted ? 1 : 0,
        created_at: story.created_at || importedAt,
        updated_at: importedAt,
      })),
      care_plans: source.care_plans.map((plan) => ({
        id: planIds.get(String(plan.id)),
        name: plan.name,
        tasks: normalizeImportedPlanTasks(plan.tasks),
        deleted: plan.deleted ? 1 : 0,
        created_at: plan.created_at || importedAt,
        updated_at: importedAt,
      })),
    };
  }

  function replaceImportedData(rowsByStore) {
    return applyBatch({ puts: rowsByStore, clears: IMPORT_STORES });
  }

  async function importBackup(payload) {
    const rowsByStore = prepareBackupImport(payload);
    await saveImportSnapshot();
    await replaceImportedData(rowsByStore);
  }

  let snapshotSequence = 0;
  async function saveImportSnapshot() {
    const backup = await buildBackup();
    if (!Object.values(backup.data).some((rows) => Array.isArray(rows) && rows.length)) return;
    const createdAt = new Date().toISOString();
    await putRec("backup_snapshots", {
      id: uuid(),
      created_at: createdAt,
      created_order: Date.now() * 1000 + snapshotSequence++,
      reason: "pre-import",
      backup,
    });
    const snapshots = (await getAll("backup_snapshots"))
      .sort((a, b) => (b.created_order || 0) - (a.created_order || 0));
    for (const stale of snapshots.slice(SNAPSHOT_LIMIT)) await delRec("backup_snapshots", stale.id);
  }

  async function listImportSnapshots() {
    return (await getAll("backup_snapshots"))
      .sort((a, b) => (b.created_order || 0) - (a.created_order || 0))
      .map((snapshot) => {
        const data = normalizeBackupPayload(snapshot.backup);
        return {
          id: snapshot.id,
          createdAt: snapshot.created_at,
          accountCount: Array.isArray(data.accounts) ? data.accounts.length : 0,
          recordCount: Array.isArray(data.records) ? data.records.length : 0,
          taskCount: Array.isArray(data.tasks) ? data.tasks.length : 0,
          storyTaskCount: Array.isArray(data.storyTasks) ? data.storyTasks.length : 0,
        };
      });
  }

  async function restoreImportSnapshot(id) {
    const snapshot = await getOne("backup_snapshots", id);
    if (!snapshot) throw new Error("没有找到该导入快照");
    await importBackup(snapshot.backup);
  }
  async function resetDatabase() {
    await runAtomic(STORES, (transaction) => {
      for (const store of STORES) transaction.objectStore(store).clear();
    });
  }

  /* ---------- 凭据 ---------- */
  function rejectPwaCredentials() {
    throw new Error("手机版不保存账号凭据，请在 Windows 版使用此功能");
  }

  /* ---------- 路由 ---------- */
  const routes = [
    ["GET", /^\/api\/state$/, (m, b, q) => loadState(q.date || gameToday())],
    ["GET", /^\/api\/settings$/, () => getScheduleSettings()],
    ["GET", /^\/api\/history$/, (m, b, q) => listHistory(q)],
    ["GET", /^\/api\/export$/, () => buildBackup()],
    ["GET", /^\/api\/import-snapshots$/, () => listImportSnapshots()],
    ["GET", /^\/api\/update\/check$/, () => ({ current: "PWA", latest: "PWA", hasUpdate: false, downloadUrl: null })],
    ["GET", /^\/api\/update\/progress$/, () => ({ status: "idle", downloaded: 0, total: 0, error: "" })],
    ["POST", /^\/api\/shutdown$/, () => null],
    ["GET", /^\/api\/heartbeat$/, () => null],
    ["POST", /^\/api\/import$/, (m, b) => importBackup(b)],
    ["POST", /^\/api\/import-snapshots\/([^/]+)\/restore$/, (m) => restoreImportSnapshot(m[1])],
    ["POST", /^\/api\/reset$/, () => resetDatabase()],
    ["POST", /^\/api\/accounts$/, (m, b) => createAccount(b)],
    ["PUT", /^\/api\/accounts\/([^/]+)$/, (m, b) => updateAccount(m[1], b)],
    ["DELETE", /^\/api\/accounts\/([^/]+)$/, (m) => archiveAccount(m[1])],
    ["POST", /^\/api\/accounts\/([^/]+)\/reactivate$/, (m) => reactivateAccount(m[1])],
    ["POST", /^\/api\/accounts\/([^/]+)\/purge$/, (m) => purgeAccount(m[1])],
    ["POST", /^\/api\/accounts\/reorder$/, (m, b) => reorderAccounts(b)],
    ["POST", /^\/api\/accounts\/([^/]+)\/task-tags$/, (m, b) => setAccountTaskTag(m[1], b)],
    ["POST", /^\/api\/accounts\/([^/]+)\/custom-tags$/, (m, b) => setAccountCustomTag(m[1], b)],
    ["GET", /^\/api\/accounts\/([^/]+)\/credentials$/, () => rejectPwaCredentials()],
    ["PUT", /^\/api\/accounts\/([^/]+)\/credentials$/, () => rejectPwaCredentials()],
    ["DELETE", /^\/api\/accounts\/([^/]+)\/credentials$/, () => rejectPwaCredentials()],
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
    const method = (options.method || "GET").toUpperCase();
    const mode = method === "GET" && !path.startsWith("/api/state") ? "readonly" : "readwrite";
    return withTransaction(mode, () => dispatch(path, options));
  }

  async function dispatch(path, options) {
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
    window.LOCAL_BACKEND = {
      handle,
      _debug: {
        loadState: (day) => withTransaction("readonly", () => loadState(day)),
        buildBackup: () => withTransaction("readonly", buildBackup),
        importBackup: (payload) => withTransaction("readwrite", () => importBackup(payload)),
        weeklyCycleStart, versionWindow,
      },
    };
    document.documentElement.classList.add("pwa-mode");
  }
})();
