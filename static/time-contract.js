/* Absolute cooldowns and unresolved legacy wall times are separate contracts. */
(function (root) {
  "use strict";
  const VERSION = 2;
  const WALL = /^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,6}))?)?$/;
  const INSTANT = /^(.*)(Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)$/;
  const preciseTask = (task) => task.recurrence === "interval" && ["质变仪", "探索派遣"].includes(task.name);
  function wallMillis(value) {
    const match = WALL.exec(String(value || ""));
    if (!match) throw new Error("时间格式无效");
    const canonical = `${match[1]}T${match[2]}:${match[3]}:${match[4] || "00"}`;
    const parsed = new Date(`${canonical}Z`);
    if (!Number.isFinite(parsed.getTime()) || parsed.toISOString().slice(0, 19) !== canonical) throw new Error("时间格式无效");
    return parsed.getTime() + Number(`0.${match[5] || "0"}`) * 1000;
  }
  function isInstant(value) { return typeof value === "string" && INSTANT.test(value); }
  function instant(value) {
    const match = INSTANT.exec(String(value || ""));
    if (!match) throw new Error("请确认时区后提交带偏移量的使用时间");
    wallMillis(match[1]);
    const result = new Date(value);
    if (!Number.isFinite(result.getTime())) throw new Error("时间格式无效");
    return result;
  }
  function utcText(value) { return value.toISOString().replace(/\.\d{3}Z$/, "Z"); }
  function minuteInstant(value) { return new Date(Math.floor(value.getTime() / 60000) * 60000); }
  function localText(value) {
    const pad = (n) => String(n).padStart(2, "0");
    return `${String(value.getFullYear()).padStart(4, "0")}-${pad(value.getMonth() + 1)}-${pad(value.getDate())}T${pad(value.getHours())}:${pad(value.getMinutes())}:${pad(value.getSeconds())}`;
  }
  function remaining(value, now = new Date()) {
    return (isInstant(value) ? instant(value).getTime() - now.getTime() : wallMillis(value) - wallMillis(localText(now))) / 1000;
  }
  function compareRecords(a, b) {
    return b.task_date.localeCompare(a.task_date) || new Date(b.completed_at).getTime() - new Date(a.completed_at).getTime();
  }
  function comparePreciseRecords(a, b) {
    return new Date(b.completed_at).getTime() - new Date(a.completed_at).getTime() || String(b.id).localeCompare(String(a.id), "en", { numeric: true });
  }
  function candidates(value, zone) {
    const nominal = wallMillis(value);
    const format = new Intl.DateTimeFormat("en-CA", { timeZone: zone, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" });
    const inZone = (ms) => {
      const parts = Object.fromEntries(format.formatToParts(new Date(ms)).map((p) => [p.type, p.value]));
      return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}:${parts.second}`;
    };
    const offsets = new Set();
    // Intl supplies timezone rules; sample both sides of nearby transitions.
    for (let hours = -48; hours <= 48; hours += 6) {
      const sample = Math.floor((nominal + hours * 3600000) / 1000) * 1000;
      offsets.add(wallMillis(inZone(sample)) - sample);
    }
    return [...offsets].map((offset) => nominal - offset)
      .filter((ms) => wallMillis(inZone(ms)) === Math.floor(nominal / 1000) * 1000)
      .sort((a, b) => a - b).map((ms) => utcText(new Date(ms)));
  }
  function legacyEntries(data) {
    const entries = [];
    const tasks = new Map((data.tasks || []).map((task) => [String(task.id), task]));
    const add = (collection, index, field, value, label) => {
      if (typeof value === "string" && value.includes("T") && !isInstant(value)) {
        wallMillis(value);
        entries.push({ key: `${collection}/${index}/${field}`, original: value, label });
      }
    };
    (data.tasks || []).forEach((task, index) => {
      if (preciseTask(task)) add("tasks", index, "next_due", task.next_due, `${task.name} #${task.id} 到期`);
    });
    (data.records || []).forEach((record, index) => {
      const task = tasks.get(String(record.task_id));
      if (!task || !preciseTask(task)) return;
      for (const field of ["completed_at", "previous_next_due"]) add("records", index, field, record[field], `${task.name} ${record.task_date} ${field}`);
    });
    return entries;
  }
  function resolveBackup(data, resolution) {
    const entries = legacyEntries(data);
    if (!entries.length) return normalizeAbsoluteTimes(JSON.parse(JSON.stringify(data)));
    if (!resolution || resolution.confirmed !== true || typeof resolution.sourceZone !== "string" || !resolution.sourceZone.trim() || resolution.sourceZone.length > 100 || !Array.isArray(resolution.entries)) throw new Error("旧冷却时间缺少时区，请先确认来源时区和重复小时");
    const byKey = new Map(resolution.entries.map((entry) => [entry.key, entry]));
    if (byKey.size !== entries.length || resolution.entries.length !== entries.length) throw new Error("旧时间确认记录不完整");
    const result = JSON.parse(JSON.stringify(data));
    for (const entry of entries) {
      const chosen = byKey.get(entry.key);
      if (!chosen || chosen.original !== entry.original) throw new Error("旧时间确认记录已失效");
      if (!candidates(chosen.local, resolution.sourceZone).includes(chosen.instant) || (wallMillis(chosen.local) - instant(chosen.instant).getTime()) / 60000 !== chosen.offsetMinutes) throw new Error("旧时间的时区或重复小时选择无效");
      const [collection, index, field] = entry.key.split("/");
      result[collection][Number(index)][field] = utcText(instant(chosen.instant));
    }
    result.timeLegacyArchive = [...(result.timeLegacyArchive || []), resolution];
    return normalizeAbsoluteTimes(result);
  }
  function normalizeAbsoluteTimes(result) {
    const tasks = new Map((result.tasks || []).map((task) => [String(task.id), task]));
    for (const task of result.tasks || []) {
      if (preciseTask(task) && isInstant(task.next_due)) task.next_due = utcText(instant(task.next_due));
    }
    for (const record of result.records || []) {
      const task = tasks.get(String(record.task_id));
      if (!task || !preciseTask(task)) continue;
      for (const field of ["completed_at", "previous_next_due"]) {
        if (isInstant(record[field])) record[field] = utcText(instant(record[field]));
      }
      if (isInstant(record.completed_at) && (record.note || "").startsWith("expedition:")) record.note = `expedition:${record.completed_at}`;
    }
    return result;
  }
  root.LEYLINE_TIME = { VERSION, isInstant, instant, utcText, minuteInstant, localText, remaining, compareRecords, comparePreciseRecords, wallMillis, candidates, legacyEntries, resolveBackup };
  if (typeof module !== "undefined" && module.exports) module.exports = root.LEYLINE_TIME;
})(globalThis);
