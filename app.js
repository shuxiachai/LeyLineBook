const state = {
  selectedDate: localDateString(gameDate()),
  data: null,
  currentView: "today",
  editingTaskId: null,
  editingTaskAccountId: null,
  editingTaskName: "",
  editingTaskIsNew: false,
  editingTaskNotes: new Set(),
  editingTransformerTaskId: null,
  editingCredentialsAccountId: null,
  accountDrag: null,
  selectedActivityType: null,
  selectedStoryType: "archon",
};

const recurrenceLabels = {
  daily: "每日",
  weekly: "每周",
  interval: "固定间隔",
  monthly: "每月固定日期",
  version: "版本周期",
  manual: "随时记录",
  once: "一次性",
};

const dailyTaskTagDefinitions = [
  { name: "体力", label: "体力" },
  { name: "狗粮", label: "狗粮" },
  { name: "质变仪", label: "质变仪" },
  { name: "壶", label: "壶" },
  { name: "爱可菲料理", label: "爱可菲料理" },
];
const abyssTaskTagDefinitions = [
  { name: "深境螺旋", label: "深境螺旋" },
  { name: "幻想真境剧诗", label: "剧诗" },
  { name: "危战", label: "危战" },
];
const noteTagDefinitions = ["好感队", "委托"];
const availableThemes = new Set(["black", "white", "green", "blue", "purple", "rose", "amber"]);
const characterBackgroundDatabase = "leylinebook-preferences";
const characterBackgroundStore = "visual-assets";
const characterBackgroundKey = "character-background";
const characterOpacityKey = "leylinebook-character-opacity-v2";
const characterPositionKey = "leylinebook-character-position-v1";
const characterZoomKey = "leylinebook-character-zoom-v1";
const characterThemeActiveKey = "leylinebook-character-theme-active";
const maximumCharacterImageBytes = 12 * 1024 * 1024;
const allowedCharacterImageTypes = new Set(["image/png", "image/jpeg", "image/webp"]);
let characterImageUrl = null;
let characterBackgroundRecord = null;
let characterPositionValue = 50;
let characterZoomValue = 100;

function refreshThemeButtons() {
  const selectedTheme = document.body.dataset.theme;
  const characterThemeActive = document.body.classList.contains("character-theme");
  document.querySelectorAll("[data-theme-option]").forEach((button) => {
    const active = !characterThemeActive && button.dataset.themeOption === selectedTheme;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
}

function applyTheme(theme, persist = true, exitCharacterTheme = true) {
  const selectedTheme = availableThemes.has(theme) ? theme : "green";
  if (exitCharacterTheme) deactivateCharacterTheme(persist);
  document.body.dataset.theme = selectedTheme;
  if (persist) localStorage.setItem("task-recorder-theme", selectedTheme);
  refreshThemeButtons();
}

function openCharacterBackgroundDatabase() {
  return new Promise((resolve, reject) => {
    if (!window.indexedDB) {
      reject(new Error("当前浏览器不支持保存本地背景图片"));
      return;
    }
    const request = window.indexedDB.open(characterBackgroundDatabase, 1);
    request.onupgradeneeded = () => {
      const database = request.result;
      if (!database.objectStoreNames.contains(characterBackgroundStore)) {
        database.createObjectStore(characterBackgroundStore, { keyPath: "id" });
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error("无法打开本地图片存储"));
  });
}

async function runCharacterBackgroundRequest(mode, action) {
  const database = await openCharacterBackgroundDatabase();
  return new Promise((resolve, reject) => {
    const transaction = database.transaction(characterBackgroundStore, mode);
    const store = transaction.objectStore(characterBackgroundStore);
    const request = action(store);
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error("本地图片操作失败"));
    transaction.oncomplete = () => database.close();
    transaction.onerror = () => database.close();
    transaction.onabort = () => database.close();
  });
}

function readCharacterBackground() {
  return runCharacterBackgroundRequest("readonly", (store) => store.get(characterBackgroundKey));
}

function saveCharacterBackground(file) {
  return runCharacterBackgroundRequest("readwrite", (store) => store.put({
    id: characterBackgroundKey,
    blob: file,
    name: file.name,
    type: file.type,
    size: file.size,
    savedAt: new Date().toISOString(),
  }));
}

function deleteCharacterBackground() {
  return runCharacterBackgroundRequest("readwrite", (store) => store.delete(characterBackgroundKey));
}

function applyCharacterOpacity(value, persist = true) {
  const parsed = Number.parseInt(value, 10);
  const opacity = Number.isFinite(parsed) ? Math.min(100, Math.max(20, parsed)) : 72;
  document.documentElement.style.setProperty("--character-opacity", String(opacity / 100));
  document.querySelector("#characterOpacity").value = String(opacity);
  document.querySelector("#characterOpacityValue").value = `${opacity}%`;
  if (persist) localStorage.setItem(characterOpacityKey, String(opacity));
}

function updateCharacterImagePlacement() {
  const zoom = characterZoomValue / 100;
  const offset = characterPositionValue * (1 - zoom);
  document.documentElement.style.setProperty("--character-zoom", String(zoom));
  document.documentElement.style.setProperty("--character-position-offset", `${offset}%`);
}

function applyCharacterPosition(value, persist = true) {
  const parsed = Number.parseInt(value, 10);
  const pos = Number.isFinite(parsed) ? Math.min(100, Math.max(0, parsed)) : 50;
  characterPositionValue = pos;
  updateCharacterImagePlacement();
  document.querySelector("#characterPosition").value = String(pos);
  const label = pos === 0 ? "顶部" : pos === 100 ? "底部" : pos === 50 ? "居中" : `${pos}%`;
  document.querySelector("#characterPositionValue").value = label;
  if (persist) localStorage.setItem(characterPositionKey, String(pos));
}

function applyCharacterZoom(value, persist = true) {
  const parsed = Number.parseInt(value, 10);
  const zoom = Number.isFinite(parsed) ? Math.min(200, Math.max(30, parsed)) : 100;
  characterZoomValue = zoom;
  updateCharacterImagePlacement();
  document.querySelector("#characterZoom").value = String(zoom);
  document.querySelector("#characterZoomValue").value = `${zoom}%`;
  if (persist) localStorage.setItem(characterZoomKey, String(zoom));
}

function updateCharacterThemeControls() {
  const hasImage = Boolean(characterBackgroundRecord?.blob instanceof Blob && characterBackgroundRecord.blob.size);
  const active = hasImage && document.body.classList.contains("character-theme");
  const enableButton = document.querySelector("#enableCharacterTheme");
  enableButton.disabled = !hasImage || active;
  enableButton.textContent = active ? "角色主题已启用" : "启用角色主题";
  document.querySelector("#removeCharacterImage").disabled = !hasImage;
  refreshThemeButtons();
}

function deactivateCharacterTheme(persist = true) {
  document.body.classList.remove("character-theme");
  document.querySelector("#characterBackdrop")?.classList.remove("active-theme");
  if (persist) localStorage.setItem(characterThemeActiveKey, "false");
  if (document.querySelector("#enableCharacterTheme")) updateCharacterThemeControls();
}

function clampColor(value) {
  return Math.max(0, Math.min(255, Math.round(value)));
}

function rgbToHsl(r, g, b) {
  const max = Math.max(r, g, b), min = Math.min(r, g, b);
  const l = (max + min) / 2;
  if (max === min) return [0, 0, l];
  const d = max - min;
  const s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
  let h;
  switch (max) {
    case r: h = (g - b) / d + (g < b ? 6 : 0); break;
    case g: h = (b - r) / d + 2; break;
    default: h = (r - g) / d + 4;
  }
  return [h / 6, s, l];
}

function hslChannel(p, q, t) {
  if (t < 0) t += 1;
  if (t > 1) t -= 1;
  if (t < 1 / 6) return Math.round((p + (q - p) * 6 * t) * 255);
  if (t < 1 / 2) return Math.round(q * 255);
  if (t < 2 / 3) return Math.round((p + (q - p) * (2 / 3 - t) * 6) * 255);
  return Math.round(p * 255);
}

function hslToRgb(h, s, l) {
  if (s === 0) { const v = Math.round(l * 255); return [v, v, v]; }
  const q = l < 0.5 ? l * (1 + s) : l + s - l * s;
  const p = 2 * l - q;
  return [hslChannel(p, q, h + 1 / 3), hslChannel(p, q, h), hslChannel(p, q, h - 1 / 3)];
}

async function extractCharacterPalette(blob) {
  const source = await createImageBitmap(blob);
  const canvas = document.createElement("canvas");
  canvas.width = 72;
  canvas.height = 72;
  const context = canvas.getContext("2d", { willReadFrequently: true });
  context.clearRect(0, 0, canvas.width, canvas.height);
  context.drawImage(source, 0, 0, canvas.width, canvas.height);
  source.close?.();
  const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
  let luminanceTotal = 0;
  let luminanceWeight = 0;
  let redTotal = 0;
  let greenTotal = 0;
  let blueTotal = 0;
  let colorWeight = 0;

  for (let index = 0; index < pixels.length; index += 4) {
    const alpha = pixels[index + 3] / 255;
    if (alpha < 0.12) continue;
    const red = pixels[index];
    const green = pixels[index + 1];
    const blue = pixels[index + 2];
    const highest = Math.max(red, green, blue);
    const lowest = Math.min(red, green, blue);
    const saturation = highest ? (highest - lowest) / highest : 0;
    const luminance = (0.2126 * red + 0.7152 * green + 0.0722 * blue) / 255;
    luminanceTotal += luminance * alpha;
    luminanceWeight += alpha;
    if (luminance < 0.05 || luminance > 0.96) continue;
    if (saturation < 0.1) continue;
    const weight = alpha * saturation * saturation * 3.5;
    redTotal += red * weight;
    greenTotal += green * weight;
    blueTotal += blue * weight;
    colorWeight += weight;
  }

  const tone = luminanceWeight && luminanceTotal / luminanceWeight > 0.57 ? "light" : "dark";
  if (!colorWeight) return { red: 70, green: 126, blue: 104, tone };

  const [hue, sat, light] = rgbToHsl(redTotal / colorWeight / 255, greenTotal / colorWeight / 255, blueTotal / colorWeight / 255);
  const boostedSat = Math.min(0.88, Math.max(0.60, sat * 1.5 + 0.15));
  const targetLight = 0.42;
  const pull = light < 0.22 ? 0.65 : light > 0.75 ? 0.55 : 0.15;
  const adjustedLight = light + (targetLight - light) * pull;
  const [red, green, blue] = hslToRgb(hue, boostedSat, adjustedLight);
  return { red, green, blue, tone };
}

async function activateCharacterTheme(persist = true) {
  const blob = characterBackgroundRecord?.blob;
  if (!(blob instanceof Blob) || !blob.size) throw new Error("请先选择角色图片");
  let palette;
  try {
    palette = await extractCharacterPalette(blob);
  } catch {
    palette = { red: 70, green: 126, blue: 104, tone: "dark" };
  }
  const accentLuminance = (0.2126 * palette.red + 0.7152 * palette.green + 0.0722 * palette.blue) / 255;
  document.body.style.setProperty("--character-accent-rgb", `${palette.red}, ${palette.green}, ${palette.blue}`);
  document.body.style.setProperty("--character-button-text", accentLuminance > 0.58 ? "#18201c" : "#ffffff");
  document.body.dataset.characterTone = palette.tone;
  document.body.classList.add("character-theme");
  document.querySelector("#characterBackdrop").classList.add("active-theme");
  if (persist) localStorage.setItem(characterThemeActiveKey, "true");
  updateCharacterThemeControls();
}

function renderCharacterBackground(record) {
  if (characterImageUrl) {
    URL.revokeObjectURL(characterImageUrl);
    characterImageUrl = null;
  }
  const blob = record?.blob;
  const hasImage = blob instanceof Blob && blob.size > 0;
  const backdrop = document.querySelector("#characterBackdrop");
  const preview = document.querySelector("#characterPreview");
  const backdropImage = document.querySelector("#characterBackdropImage");
  const previewImage = document.querySelector("#characterPreviewImage");
  const removeButton = document.querySelector("#removeCharacterImage");
  const storageNote = document.querySelector("#characterStorageNote");

  characterBackgroundRecord = hasImage ? record : null;
  backdrop.classList.toggle("has-image", hasImage);
  preview.classList.toggle("has-image", hasImage);
  removeButton.disabled = !hasImage;
  if (!hasImage) {
    backdropImage.removeAttribute("src");
    previewImage.removeAttribute("src");
    storageNote.textContent = "支持 PNG、JPEG、WebP，最大 12 MB。";
    deactivateCharacterTheme(false);
    updateCharacterThemeControls();
    return;
  }

  characterImageUrl = URL.createObjectURL(blob);
  backdropImage.src = characterImageUrl;
  previewImage.src = characterImageUrl;
  const sizeMb = (blob.size / 1024 / 1024).toFixed(1);
  storageNote.textContent = `已保存在当前浏览器：${record.name || "角色背景"}（${sizeMb} MB）`;
  updateCharacterThemeControls();
}

function validateCharacterImage(file) {
  if (!allowedCharacterImageTypes.has(file.type)) {
    throw new Error("请选择 PNG、JPEG 或 WebP 图片");
  }
  if (file.size > maximumCharacterImageBytes) {
    throw new Error("图片不能超过 12 MB");
  }
  return new Promise((resolve, reject) => {
    const image = new Image();
    const url = URL.createObjectURL(file);
    image.onload = () => {
      URL.revokeObjectURL(url);
      resolve();
    };
    image.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error("图片无法读取，请换一张图片重试"));
    };
    image.src = url;
  });
}

async function chooseCharacterBackground(event) {
  const input = event.currentTarget;
  const file = input.files?.[0];
  if (!file) return;
  try {
    await validateCharacterImage(file);
    await saveCharacterBackground(file);
    renderCharacterBackground({ blob: file, name: file.name });
    await activateCharacterTheme();
    showToast("角色主题已启用");
  } finally {
    input.value = "";
  }
}

async function removeCharacterBackground() {
  await deleteCharacterBackground();
  deactivateCharacterTheme();
  renderCharacterBackground(null);
  showToast("角色背景已移除");
}

async function initializeCharacterBackground() {
  applyCharacterOpacity(localStorage.getItem(characterOpacityKey) || "72", false);
  applyCharacterPosition(localStorage.getItem(characterPositionKey) || "50", false);
  applyCharacterZoom(localStorage.getItem(characterZoomKey) || "100", false);
  try {
    const record = await readCharacterBackground();
    renderCharacterBackground(record);
    const storedActive = localStorage.getItem(characterThemeActiveKey);
    if (record?.blob && storedActive !== "false") await activateCharacterTheme(false);
  } catch (error) {
    renderCharacterBackground(null);
    showToast(error.message);
  }
}

function gameDate() {
  return new Date(Date.now() - 4 * 60 * 60 * 1000);
}

function localDateString(value) {
  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, "0");
  const day = String(value.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function localDateTimeInputValue(value) {
  const date = value instanceof Date ? value : new Date(value);
  return `${localDateString(date)}T${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}

function formatLocalDateTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value || "").replace("T", " ");
  return `${localDateString(date)} ${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}

function formatCooldown(seconds) {
  const totalMinutes = Math.max(0, Math.ceil(Number(seconds || 0) / 60));
  const days = Math.floor(totalMinutes / 1440);
  const hours = Math.floor((totalMinutes % 1440) / 60);
  const minutes = totalMinutes % 60;
  return [days ? `${days}天` : "", hours ? `${hours}小时` : "", (!days && minutes) ? `${minutes}分钟` : ""].filter(Boolean).join(" ") || "即将可用";
}

function formatActivityDate(isoDate) {
  const d = new Date(isoDate + "T00:00:00");
  return `${d.getMonth() + 1}月${d.getDate()}日`;
}

function calcActivityEndDate(startIso, durationDays) {
  const d = new Date(startIso + "T00:00:00");
  d.setDate(d.getDate() + durationDays);
  return localDateString(d);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const result = await response.json();
  if (!response.ok || !result.success) {
    throw new Error(result.error || "操作失败");
  }
  return result.data;
}

function showToast(message) {
  const toast = document.querySelector("#toast");
  toast.textContent = message;
  toast.classList.add("show");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.remove("show"), 2200);
}

function confirmAction({ title = "请确认", message, confirmText = "确认" }) {
  const dialog = document.querySelector("#confirmDialog");
  document.querySelector("#confirmDialogTitle").textContent = title;
  document.querySelector("#confirmDialogMessage").textContent = message;
  document.querySelector("#confirmDialogSubmit").textContent = confirmText;
  dialog.returnValue = "cancel";
  dialog.showModal();
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirm"), { once: true });
  });
}

async function loadState() {
  state.data = await api(`/api/state?date=${encodeURIComponent(state.selectedDate)}`);
  renderToday();
  renderAccounts();
  renderActivities();
  renderStoryTasks();
  renderHistoryAccountFilter();
}

function renderHistoryAccountFilter() {
  const select = document.querySelector("#historyAccount");
  const current = select.value;
  const active = (state.data?.accounts || []).filter((a) => a.active);
  const inactive = (state.data?.accounts || []).filter((a) => !a.active);
  select.innerHTML = '<option value="">全部号主</option>' +
    active.map((a) => `<option value="${a.id}">${escapeHtml(a.name)}</option>`).join("") +
    (inactive.length ? `<option disabled>──── 已停用 ────</option>` + inactive.map((a) => `<option value="${a.id}">${escapeHtml(a.name)}</option>`).join("") : "");
  if (current) select.value = current;
}

function makeProxyBadge(proxyUntil) {
  if (!proxyUntil) return "";
  const today = localDateString(gameDate());
  const daysLeft = Math.ceil((new Date(proxyUntil + "T00:00:00") - new Date(today + "T00:00:00")) / 86400000);
  const cls = daysLeft < 0 ? "proxy-expired" : daysLeft <= 3 ? "proxy-urgent" : daysLeft <= 7 ? "proxy-warning" : "proxy-normal";
  const label = daysLeft < 0 ? `${formatActivityDate(proxyUntil)} 已到期` : `${formatActivityDate(proxyUntil)} 到期`;
  return `<span class="proxy-badge ${cls}">${label}</span>`;
}

function groupTasks(tasks) {
  return tasks.reduce((groups, task) => {
    const current = groups.get(task.account_id) || { name: task.account_name, proxyUntil: task.account_proxy_until, tasks: [] };
    groups.set(task.account_id, { ...current, tasks: [...current.tasks, task] });
    return groups;
  }, new Map());
}

function taskGroupsHtml(tasks, emptyText) {
  const groups = groupTasks(tasks);
  if (!groups.size) return `<div class="empty-state">${escapeHtml(emptyText)}</div>`;
  return [...groups.entries()].map(([accountId, group]) => {
    const done = group.tasks.filter((task) => task.completed).length;
    const chips = group.tasks.map((task) => {
      let dueText = "";
      if (task.name === "质变仪" && task.available_at) dueText = `<small class="schedule-meta">下次可用 ${escapeHtml(formatLocalDateTime(task.available_at))}</small>`;
      else if (task.recurrence === "interval" && task.next_due) dueText = `<small class="schedule-meta">到期 ${escapeHtml(task.next_due)}</small>`;
      if (task.recurrence === "weekly" && task.event_end) dueText = `<small class="schedule-meta">下次刷新 ${escapeHtml(task.event_end)} ${escapeHtml(task.event_end_time)}</small>`;
      if (task.recurrence === "monthly" && task.event_end) dueText = `<small class="schedule-meta">截止 ${escapeHtml(task.event_end)} ${escapeHtml(task.event_end_time)}</small>`;
      if (task.recurrence === "version" && task.event_end) dueText = `<small class="schedule-meta">截止 ${escapeHtml(task.event_end)} ${escapeHtml(task.event_end_time)}</small>`;
      const noteText = task.notes ? `<small class="task-note">备注：${escapeHtml(task.notes)}</small>` : "";
      return `<button class="task-chip ${task.completed ? "completed" : ""}" data-toggle-task="${task.id}" data-completed="${task.completed}"><span class="task-chip-content"><span class="task-title">${escapeHtml(task.name)}</span>${dueText}${noteText}</span></button>`;
    }).join("");
    return `<article class="task-group" data-account-id="${accountId}"><div class="task-group-head"><h3>${escapeHtml(group.name)}${makeProxyBadge(group.proxyUntil)}</h3><span>${done} / ${group.tasks.length} 完成</span></div><div class="task-list">${chips}</div></article>`;
  }).join("");
}

function coolingTasksHtml(tasks) {
  const groups = groupTasks(tasks);
  return [...groups.entries()].map(([accountId, group]) => {
    const chips = group.tasks.map((task) => `<div class="task-chip cooldown-chip"><span class="task-chip-content"><span class="task-title">${escapeHtml(task.name)}</span><small class="schedule-meta">可用时间 ${escapeHtml(formatLocalDateTime(task.available_at))}</small><small class="schedule-meta cooldown-remaining">剩余 ${escapeHtml(formatCooldown(task.cooldown_remaining_seconds))}</small></span></div>`).join("");
    return `<article class="task-group" data-account-id="${accountId}"><div class="task-group-head"><h3>${escapeHtml(group.name)}</h3></div><div class="task-list">${chips}</div></article>`;
  }).join("");
}

function renderToday() {
  const { summary, dueTasks, manualTasks, coolingTasks = [] } = state.data;
  const dailyTaskNames = new Set(dailyTaskTagDefinitions.map((task) => task.name));
  const fallbackDailyTasks = dueTasks.filter((task) => dailyTaskNames.has(task.name));
  const dailyTotal = Number.isFinite(summary.dailyTotal) ? summary.dailyTotal : fallbackDailyTasks.length;
  const dailyCompleted = Number.isFinite(summary.dailyCompleted)
    ? summary.dailyCompleted
    : fallbackDailyTasks.filter((task) => task.completed).length;
  const percent = summary.total ? Math.round(summary.completed / summary.total * 100) : 100;
  const dailyPercent = dailyTotal ? Math.round(dailyCompleted / dailyTotal * 100) : 100;
  document.querySelector("#selectedDate").value = state.selectedDate;
  document.querySelector("#progressText").textContent = `${summary.completed} / ${summary.total}`;
  document.querySelector("#progressBar").style.width = `${percent}%`;
  document.querySelector("#dailyProgressText").textContent = `${dailyCompleted} / ${dailyTotal}`;
  document.querySelector("#dailyProgressBar").style.width = `${dailyPercent}%`;
  document.querySelector("#remainingCount").textContent = summary.remaining;
  document.querySelector("#navRemaining").textContent = summary.remaining;
  document.querySelector("#completeAll").disabled = summary.remaining === 0;
  document.querySelector("#dueTaskGroups").innerHTML = taskGroupsHtml(dueTasks, "这一天没有待办任务，轻松收工。");
  document.querySelector("#cooldownSection").classList.toggle("hidden", coolingTasks.length === 0);
  document.querySelector("#cooldownTaskGroups").innerHTML = coolingTasksHtml(coolingTasks);
  document.querySelector("#manualTaskGroups").innerHTML = taskGroupsHtml(manualTasks, "还没有配置专项任务。");
  renderScheduleSettings();
}

function taskDescription(task) {
  if (task.recurrence === "weekly") return "每周一 04:00 刷新";
  if (task.name === "质变仪" && task.available_at) return `168 小时冷却 · 下次 ${formatLocalDateTime(task.available_at)}`;
  if (task.recurrence === "interval") return `每 ${task.interval_days} 天 · 下次 ${task.next_due}`;
  if (task.recurrence === "monthly") return `每月 ${task.monthly_day} 日刷新 · 下次 ${task.next_due}`;
  if (task.recurrence === "version") {
    const window = state.data.settings?.warWindow;
    return window ? `版本周期 · ${window.eventStart} 10:00 至 ${window.eventEnd} 03:59` : "版本周期 · 请设置版本更新日";
  }
  if (task.recurrence === "once") return `到期 ${task.next_due}`;
  return recurrenceLabels[task.recurrence];
}

function renderAccounts() {
  const tasksByAccount = groupTasks(state.data.tasks);
  const customTags = state.data.customTags || [];
  const renderAccountCard = (account) => {
    const tasks = tasksByAccount.get(account.id)?.tasks || [];
    const activeNames = new Set(tasks.map((task) => task.name));
    const activeCustomTagIds = new Set(tasks.map((task) => task.custom_tag_id).filter(Boolean));
    const renderTaskTags = (definitions) => definitions.map((definition) => {
      const active = activeNames.has(definition.name);
      return `<button class="config-tag ${active ? "active" : ""}" data-account-id="${account.id}" data-task-tag="${escapeHtml(definition.name)}" data-enabled="${active}" aria-pressed="${active}" title="${active ? "点击设置备注" : "点击添加任务"}" ${!account.active ? "disabled" : ""}>${active ? "✓ " : "+ "}${escapeHtml(definition.label)}</button>`;
    }).join("");
    const renderCustomTags = () => customTags.map((tag) => {
      const active = activeCustomTagIds.has(tag.id);
      return `<button class="config-tag ${active ? "active" : ""}" data-account-id="${account.id}" data-custom-tag-id="${tag.id}" data-enabled="${active}" aria-pressed="${active}" ${!account.active ? "disabled" : ""}>${active ? "✓ " : "+ "}${escapeHtml(tag.name)}</button>`;
    }).join("");
    const dailySection = `<section class="account-task-group"><span class="account-task-group-label">每日</span><div class="tag-list">${renderTaskTags(dailyTaskTagDefinitions)}</div></section>`;
    const abyssSection = `<section class="account-task-group"><span class="account-task-group-label">深渊</span><div class="tag-list">${renderTaskTags(abyssTaskTagDefinitions)}</div></section>`;
    const customSection = customTags.length ? `<section class="account-task-group"><span class="account-task-group-label">活动</span><div class="tag-list">${renderCustomTags()}</div></section>` : "";
    const actions = account.active
      ? `<button class="icon-button" data-credentials-account="${account.id}" title="账号凭据">密</button><button class="icon-button" data-edit-account="${account.id}" title="编辑号主">✎</button><button class="icon-button" data-delete-account="${account.id}" title="停用号主">✕</button><span class="drag-handle" data-drag-account="${account.id}" role="button" tabindex="0" aria-label="拖动${escapeHtml(account.name)}调整顺序" title="按住拖动调整顺序">↕</span>`
      : `<button class="icon-button" data-reactivate-account="${account.id}" title="重新启用">↺</button><button class="icon-button danger" data-purge-account="${account.id}" title="彻底删除号主">␡</button>`;
    const proxyBadge = makeProxyBadge(account.proxy_until);
    return `<article class="account-card account-row${account.active ? "" : " inactive"}" data-account-row="${account.id}"><div class="account-identity"><h3>${escapeHtml(account.name)}</h3>${account.owner ? `<small>${escapeHtml(account.owner)}</small>` : ""}${proxyBadge}</div><div class="account-sections">${dailySection}${customSection}${abyssSection}</div><div class="card-actions">${actions}</div></article>`;
  };

  const activeAccounts = state.data.accounts.filter((a) => a.active);
  const inactiveAccounts = state.data.accounts.filter((a) => !a.active);
  let html = activeAccounts.map(renderAccountCard).join("");
  if (inactiveAccounts.length) {
    html += `<div class="inactive-section-label">已停用</div>` + inactiveAccounts.map(renderAccountCard).join("");
  }
  document.querySelector("#accountCards").innerHTML = html || '<div class="empty-state">还没有号主，先新增一个。</div>';
}

function renderActivities() {
  const tags = state.data.customTags || [];
  const byCategory = {};
  for (const tag of tags) {
    (byCategory[tag.category] || (byCategory[tag.category] = [])).push(tag);
  }
  const categoryOrder = ["大活动", "小活动"];
  let html = "";
  for (const category of categoryOrder) {
    const items = byCategory[category];
    if (!items?.length) continue;
    html += `<div class="activity-group"><div class="activity-group-label">${escapeHtml(category)}</div>`;
    html += items.map((tag) => {
      const dateRange = tag.start_date
        ? `${formatActivityDate(tag.start_date)} 10:00 — ${formatActivityDate(calcActivityEndDate(tag.start_date, tag.duration_days))} 03:59`
        : "";
      return `<div class="activity-item"><div class="activity-item-info"><span class="activity-name">${escapeHtml(tag.name)}</span>${dateRange ? `<span class="activity-date-range">${dateRange}</span>` : ""}</div><span class="activity-badge">${tag.duration_days} 天</span><button class="icon-button" data-remove-custom-tag="${tag.id}" aria-label="删除">✕</button></div>`;
    }).join("");
    html += `</div>`;
  }
  document.querySelector("#activityList").innerHTML = html || '<div class="empty-state">还没有活动，先添加一个。</div>';
}

const storyTypeLabels = { archon: "魔神任务", legend: "传说任务", world: "世界任务" };

function storyDeadlineForType(type) {
  const window = state.data?.settings?.warWindow;
  if (!window || type === "world") return null;
  if (type === "archon") return `${window.eventEnd}T15:00`;
  const versionStart = new Date(`${window.versionStart}T00:00:00`);
  const halfEnd = new Date(versionStart);
  halfEnd.setDate(halfEnd.getDate() + 20);
  const today = localDateString(gameDate());
  const halfEndText = localDateString(halfEnd);
  return `${today <= halfEndText ? halfEndText : window.eventEnd}T15:00`;
}

function updateStoryForm() {
  document.querySelectorAll("[data-story-type]").forEach((button) => {
    button.classList.toggle("selected", button.dataset.storyType === state.selectedStoryType);
  });
  const bonus = document.querySelector("#storyHasBonus");
  const isWorld = state.selectedStoryType === "world";
  if (isWorld) bonus.checked = false;
  bonus.disabled = isWorld;
  const deadline = bonus.checked ? storyDeadlineForType(state.selectedStoryType) : null;
  document.querySelector("#storyDeadlinePreview").textContent = deadline
    ? `额外奖励截止：${formatLocalDateTime(deadline)}`
    : "无额外奖励期限，任务会一直保留到完成";
}

function storyItemHtml(task) {
  const expired = task.bonus_deadline && new Date(task.bonus_deadline) < new Date();
  const deadlineText = task.has_bonus
    ? `${expired ? "额外奖励已结束" : "额外奖励截止"}：${formatLocalDateTime(task.bonus_deadline)}`
    : "无额外奖励期限";
  return `<article class="story-item ${task.completed_at ? "completed" : ""}">
    <div class="story-account">${escapeHtml(task.account_name)}</div>
    <div class="story-main">
      <div class="story-name-row"><span class="story-name">${escapeHtml(task.name)}</span><span class="story-type-badge">${escapeHtml(storyTypeLabels[task.task_type])}</span></div>
      <span class="story-deadline ${expired ? "expired" : ""}">${escapeHtml(deadlineText)}</span>
    </div>
    <div class="story-actions">
      <button class="button ${task.completed_at ? "ghost" : "primary"}" data-toggle-story="${task.id}" data-completed="${Boolean(task.completed_at)}">${task.completed_at ? "撤销完成" : "标记完成"}</button>
      <button class="icon-button danger" data-delete-story="${task.id}" aria-label="删除剧情任务">×</button>
    </div>
  </article>`;
}

function renderStoryTasks() {
  const tasks = state.data.storyTasks || [];
  const pending = tasks.filter((task) => !task.completed_at);
  const completed = tasks.filter((task) => task.completed_at);
  document.querySelector("#storyRemaining").textContent = pending.length;

  document.querySelector("#storyOwnerOptions").innerHTML = state.data.accounts
    .filter((account) => account.active)
    .map((account) => `<option value="${escapeHtml(account.name)}"></option>`)
    .join("");

  const pendingHtml = pending.length
    ? `<section><h3 class="story-section-title">待完成 · ${pending.length}</h3><div class="story-items">${pending.map(storyItemHtml).join("")}</div></section>`
    : '<div class="empty-state">目前没有待完成的剧情任务。</div>';
  const completedHtml = completed.length
    ? `<section><h3 class="story-section-title">已完成 · ${completed.length}</h3><div class="story-items">${completed.map(storyItemHtml).join("")}</div></section>`
    : "";
  document.querySelector("#storyTaskList").innerHTML = pendingHtml + completedHtml;
  updateStoryForm();
}

async function addStoryTask() {
  const name = document.querySelector("#storyName").value.trim();
  const ownerName = document.querySelector("#storyOwner").value.trim();
  if (!ownerName) {
    showToast("请选择或输入号主");
    return;
  }
  const matchedAccount = state.data.accounts.find(
    (account) => account.active && account.name === ownerName,
  );
  await api("/api/story-tasks", {
    method: "POST",
    body: JSON.stringify({
      accountId: matchedAccount?.id || null,
      ownerName: matchedAccount ? "" : ownerName,
      name,
      taskType: state.selectedStoryType,
      hasBonus: document.querySelector("#storyHasBonus").checked,
    }),
  });
  document.querySelector("#storyName").value = "";
  await loadState();
  showToast("剧情任务已添加");
}

function accountOrderFromPage() {
  return [...document.querySelectorAll("[data-account-row]:not(.inactive)")]
    .map((row) => Number(row.dataset.accountRow));
}

function clearAccountDragStyles() {
  document.querySelectorAll("[data-account-row]").forEach((row) => {
    row.classList.remove("dragging");
  });
}

async function saveAccountOrder() {
  const accountIds = accountOrderFromPage();
  await api("/api/accounts/reorder", {
    method: "POST",
    body: JSON.stringify({ accountIds }),
  });
  await loadState();
  showToast("号主顺序已保存");
}

function bindAccountSorting() {
  const activateDrag = () => {
    if (!state.accountDrag || state.accountDrag.active) return;
    state.accountDrag.active = true;
    const row = document.querySelector(`[data-account-row="${state.accountDrag.accountId}"]`);
    row?.classList.add("dragging");
  };

  document.addEventListener("pointerdown", (event) => {
    const handle = event.target.closest("[data-drag-account]");
    if (!handle || event.button !== 0) return;
    event.preventDefault();
    const accountId = Number(handle.dataset.dragAccount);
    const row = document.querySelector(`[data-account-row="${accountId}"]`);
    if (!row) return;
    handle.setPointerCapture(event.pointerId);
    state.accountDrag = {
      accountId,
      active: false,
      handle,
      pointerId: event.pointerId,
      startY: event.clientY,
      timer: window.setTimeout(activateDrag, 180),
    };
  });

  document.addEventListener("pointermove", (event) => {
    if (!state.accountDrag) return;
    if (!state.accountDrag.active && Math.abs(event.clientY - state.accountDrag.startY) > 6) {
      activateDrag();
    }
    if (!state.accountDrag.active) return;
    event.preventDefault();
    if (event.clientY < 80) window.scrollBy(0, -18);
    if (event.clientY > window.innerHeight - 45) window.scrollBy(0, 18);
    const pointedElement = document.elementFromPoint(event.clientX, event.clientY);
    const targetRow = pointedElement?.closest("[data-account-row]");
    const draggedRow = document.querySelector(`[data-account-row="${state.accountDrag.accountId}"]`);
    if (!targetRow || !draggedRow || targetRow === draggedRow) return;
    const bounds = targetRow.getBoundingClientRect();
    const insertAfter = event.clientY > bounds.top + bounds.height / 2;
    targetRow.parentElement.insertBefore(draggedRow, insertAfter ? targetRow.nextSibling : targetRow);
  });

  document.addEventListener("pointerup", (event) => {
    if (!state.accountDrag) return;
    const drag = state.accountDrag;
    window.clearTimeout(drag.timer);
    if (drag.handle.hasPointerCapture(drag.pointerId)) drag.handle.releasePointerCapture(drag.pointerId);
    state.accountDrag = null;
    clearAccountDragStyles();
    if (!drag.active) return;
    saveAccountOrder().catch(async (error) => {
      await loadState();
      showToast(error.message);
    });
  });

  document.addEventListener("pointercancel", () => {
    if (!state.accountDrag) return;
    window.clearTimeout(state.accountDrag.timer);
    state.accountDrag = null;
    clearAccountDragStyles();
    renderAccounts();
  });
}

function parseTaskNotes(notes) {
  return String(notes || "").split(/[、,，]/).map((item) => item.trim()).filter(Boolean);
}

function renderTaskNoteEditor() {
  if (!state.editingTaskName) return;
  const presets = state.editingTaskName === "体力" ? noteTagDefinitions : [];
  const presetSection = document.querySelector("#presetNoteSection");
  presetSection.classList.toggle("hidden", presets.length === 0);
  document.querySelector("#presetNoteTags").innerHTML = presets.map((note) => {
    const active = state.editingTaskNotes.has(note);
    return `<button type="button" class="config-tag note ${active ? "active" : ""}" data-task-note-choice="${escapeHtml(note)}" aria-pressed="${active}">${active ? "✓ " : "+ "}${escapeHtml(note)}</button>`;
  }).join("");
  document.querySelector("#selectedNoteTags").innerHTML = [...state.editingTaskNotes]
    .filter((note) => !presets.includes(note))
    .map((note) => `<button type="button" class="config-tag note active" data-task-note-choice="${escapeHtml(note)}" aria-label="移除备注 ${escapeHtml(note)}">× ${escapeHtml(note)}</button>`)
    .join("") || '<span class="muted-note">还没有其他备注</span>';
}

function openTaskNoteDialog(task) {
  state.editingTaskId = task.id || null;
  state.editingTaskAccountId = task.account_id;
  state.editingTaskName = task.name;
  state.editingTaskIsNew = !task.id;
  state.editingTaskNotes = new Set(parseTaskNotes(task.notes));
  document.querySelector("#taskNoteDialogTitle").textContent = `${task.name} · 任务设置`;
  document.querySelector("#removeConfiguredTask").classList.toggle("hidden", state.editingTaskIsNew);
  document.querySelector("#customTaskNote").value = "";
  renderTaskNoteEditor();
  document.querySelector("#taskNoteDialog").showModal();
}

function addCustomTaskNote() {
  const input = document.querySelector("#customTaskNote");
  const note = input.value.trim();
  if (!note) {
    showToast("请先填写备注");
    return;
  }
  state.editingTaskNotes.add(note);
  input.value = "";
  renderTaskNoteEditor();
}

async function saveTaskNotes(event) {
  event.preventDefault();
  if (state.editingTaskIsNew) {
    await api(`/api/accounts/${state.editingTaskAccountId}/task-tags`, {
      method: "POST",
      body: JSON.stringify({
        tag: state.editingTaskName,
        enabled: true,
        notes: [...state.editingTaskNotes],
      }),
    });
  } else {
    await api(`/api/tasks/${state.editingTaskId}/notes`, {
      method: "POST",
      body: JSON.stringify({ notes: [...state.editingTaskNotes] }),
    });
  }
  document.querySelector("#taskNoteDialog").close();
  await loadState();
  showToast(state.editingTaskIsNew ? "任务和备注已添加" : "任务和备注已保存");
}

function openTransformerUsageDialog(task) {
  state.editingTransformerTaskId = task.id;
  const now = new Date();
  if (state.selectedDate !== localDateString(gameDate())) {
    const [hours, minutes] = [now.getHours(), now.getMinutes()];
    now.setFullYear(Number(state.selectedDate.slice(0, 4)), Number(state.selectedDate.slice(5, 7)) - 1, Number(state.selectedDate.slice(8, 10)));
    now.setHours(hours, minutes, 0, 0);
  }
  document.querySelector("#transformerUsedAt").value = localDateTimeInputValue(now);
  document.querySelector("#transformerUsageDialog").showModal();
}

async function saveTransformerUsage(event) {
  event.preventDefault();
  const usedAt = document.querySelector("#transformerUsedAt").value;
  await api(`/api/tasks/${state.editingTransformerTaskId}/toggle`, {
    method: "POST",
    body: JSON.stringify({ date: state.selectedDate, completed: true, usedAt }),
  });
  document.querySelector("#transformerUsageDialog").close();
  await loadState();
  showToast("已记录使用时间，168 小时后可再次使用");
}

async function removeConfiguredTask() {
  const task = state.data.tasks.find((item) => item.id === state.editingTaskId);
  if (!task) return;
  const confirmed = await confirmAction({
    title: `移除“${task.name}”任务？`,
    message: "移除后不再出现在任务列表中，已有的完成历史仍会保留。",
    confirmText: "确认移除",
  });
  if (!confirmed) return;
  await api(`/api/accounts/${task.account_id}/task-tags`, {
    method: "POST",
    body: JSON.stringify({ tag: task.name, enabled: false }),
  });
  document.querySelector("#taskNoteDialog").close();
  await loadState();
  showToast("任务已移除");
}

async function openCredentialsDialog(accountId) {
  const account = state.data.accounts.find((a) => a.id === accountId);
  state.editingCredentialsAccountId = accountId;
  document.querySelector("#credentialsAccountId").value = accountId;
  document.querySelector("#credentialsDialogEyebrow").textContent = account?.name || "账号凭据";
  document.querySelector("#credentialsUsername").value = "";
  document.querySelector("#credentialsPassword").value = "";
  document.querySelector("#credentialsPassword").type = "password";
  document.querySelector("#togglePassword").textContent = "显示";
  document.querySelector("#credentialsNote").value = "";
  const data = await api(`/api/accounts/${accountId}/credentials`);
  document.querySelector("#credentialsUsername").value = data.username || "";
  document.querySelector("#credentialsPassword").value = data.password || "";
  document.querySelector("#credentialsNote").value = data.note || "";
  const hasData = data.username || data.password || data.note;
  document.querySelector("#clearCredentials").classList.toggle("hidden", !hasData);
  document.querySelector("#credentialsDialog").showModal();
}

async function saveCredentials(event) {
  event.preventDefault();
  const id = document.querySelector("#credentialsAccountId").value;
  await api(`/api/accounts/${id}/credentials`, {
    method: "PUT",
    body: JSON.stringify({
      username: document.querySelector("#credentialsUsername").value,
      password: document.querySelector("#credentialsPassword").value,
      note: document.querySelector("#credentialsNote").value,
    }),
  });
  document.querySelector("#credentialsDialog").close();
  showToast("凭据已保存");
}

async function clearCredentials() {
  const confirmed = await confirmAction({
    title: "清除账号凭据？",
    message: "清除后，该号主保存的账号和密码将永久删除。",
    confirmText: "确认清除",
  });
  if (!confirmed) return;
  await api(`/api/accounts/${state.editingCredentialsAccountId}/credentials`, { method: "DELETE", body: "{}" });
  document.querySelector("#credentialsDialog").close();
  showToast("凭据已清除");
}

function openAccountDialog(account = null) {
  document.querySelector("#accountDialogTitle").textContent = account ? "编辑号主" : "新增号主";
  document.querySelector("#accountId").value = account?.id || "";
  document.querySelector("#accountName").value = account?.name || "";
  document.querySelector("#accountOwner").value = account?.owner || "";
  document.querySelector("#accountProxyUntil").value = account?.proxy_until || "";
  document.querySelector("#accountNotes").value = account?.notes || "";
  updateProxyDaysLeft();
  const isNew = !account;
  document.querySelector("#accountCredentialsSection").classList.toggle("hidden", !isNew);
  if (isNew) {
    document.querySelector("#accountCredUsername").value = "";
    document.querySelector("#accountCredPassword").value = "";
    document.querySelector("#accountCredPassword").type = "password";
    document.querySelector("#accountTogglePassword").textContent = "显示";
    document.querySelector("#accountCredNote").value = "";
  }
  document.querySelector("#accountDialog").showModal();
}

function updateScheduleFields() {
  const recurrence = document.querySelector("#taskRecurrence").value;
  const scheduled = recurrence === "interval" || recurrence === "once" || recurrence === "monthly";
  document.querySelector("#scheduleFields").classList.toggle("hidden", !scheduled);
  document.querySelector("#intervalField").classList.toggle("hidden", recurrence !== "interval");
  document.querySelector("#monthlyField").classList.toggle("hidden", recurrence !== "monthly");
  document.querySelector("#taskNextDue").parentElement.classList.toggle("hidden", recurrence === "monthly");
  document.querySelector("#taskNextDue").required = recurrence === "interval" || recurrence === "once";
  document.querySelector("#versionHint").classList.toggle("hidden", recurrence !== "version");
}

function openTaskDialog(accountId, task = null) {
  const accountSelect = document.querySelector("#taskAccount");
  accountSelect.innerHTML = state.data.accounts.map((account) => `<option value="${account.id}">${escapeHtml(account.name)}</option>`).join("");
  document.querySelector("#taskDialogTitle").textContent = task ? "编辑任务" : "新增任务";
  document.querySelector("#taskId").value = task?.id || "";
  accountSelect.value = String(task?.account_id || accountId || state.data.accounts[0]?.id || "");
  accountSelect.disabled = Boolean(task);
  document.querySelector("#taskName").value = task?.name || "";
  document.querySelector("#taskRecurrence").value = task?.recurrence || "daily";
  document.querySelector("#taskInterval").value = task?.interval_days || 7;
  document.querySelector("#taskMonthlyDay").value = task?.monthly_day || 1;
  document.querySelector("#taskNextDue").value = task?.next_due || state.selectedDate;
  document.querySelector("#taskNotes").value = task?.notes || "";
  updateScheduleFields();
  document.querySelector("#taskDialog").showModal();
}

async function saveAccount(event) {
  event.preventDefault();
  const id = document.querySelector("#accountId").value;
  const payload = {
    name: document.querySelector("#accountName").value,
    owner: document.querySelector("#accountOwner").value,
    proxyUntil: document.querySelector("#accountProxyUntil").value,
    notes: document.querySelector("#accountNotes").value,
  };
  const result = await api(id ? `/api/accounts/${id}` : "/api/accounts", { method: id ? "PUT" : "POST", body: JSON.stringify(payload) });
  if (!id) {
    const credUsername = document.querySelector("#accountCredUsername").value.trim();
    const credPassword = document.querySelector("#accountCredPassword").value.trim();
    const credNote = document.querySelector("#accountCredNote").value.trim();
    if (credUsername || credPassword || credNote) {
      await api(`/api/accounts/${result.id}/credentials`, {
        method: "PUT",
        body: JSON.stringify({ username: credUsername, password: credPassword, note: credNote }),
      });
    }
  }
  document.querySelector("#accountDialog").close();
  await loadState();
  showToast(id ? "号主信息已更新" : "号主已添加");
}

async function saveTask(event) {
  event.preventDefault();
  const id = document.querySelector("#taskId").value;
  const payload = {
    accountId: Number(document.querySelector("#taskAccount").value),
    name: document.querySelector("#taskName").value,
    recurrence: document.querySelector("#taskRecurrence").value,
    intervalDays: Number(document.querySelector("#taskInterval").value),
    monthlyDay: Number(document.querySelector("#taskMonthlyDay").value),
    nextDue: document.querySelector("#taskNextDue").value,
    notes: document.querySelector("#taskNotes").value,
  };
  await api(id ? `/api/tasks/${id}` : "/api/tasks", { method: id ? "PUT" : "POST", body: JSON.stringify(payload) });
  document.querySelector("#taskDialog").close();
  await loadState();
  showToast(id ? "任务已更新" : "任务已添加");
}

async function handleAction(target) {
  const dialogId = target.closest("[data-close-dialog]")?.dataset.closeDialog;
  if (dialogId) {
    document.querySelector(`#${dialogId}`).close();
    return;
  }

  const noteChoice = target.closest("[data-task-note-choice]")?.dataset.taskNoteChoice;
  if (noteChoice) {
    if (state.editingTaskNotes.has(noteChoice)) state.editingTaskNotes.delete(noteChoice);
    else state.editingTaskNotes.add(noteChoice);
    renderTaskNoteEditor();
    return;
  }

  const storyToggle = target.closest("[data-toggle-story]");
  if (storyToggle) {
    const completed = storyToggle.dataset.completed !== "true";
    await api(`/api/story-tasks/${storyToggle.dataset.toggleStory}/toggle`, {
      method: "POST",
      body: JSON.stringify({ completed }),
    });
    await loadState();
    showToast(completed ? "剧情任务已完成" : "已撤销完成");
    return;
  }

  const storyDeleteId = target.closest("[data-delete-story]")?.dataset.deleteStory;
  if (storyDeleteId) {
    const task = (state.data.storyTasks || []).find((item) => item.id === Number(storyDeleteId));
    const confirmed = await confirmAction({
      title: "删除剧情任务？",
      message: `确认删除“${task?.name || "该任务"}”吗？`,
      confirmText: "确认删除",
    });
    if (!confirmed) return;
    await api(`/api/story-tasks/${storyDeleteId}`, { method: "DELETE", body: "{}" });
    await loadState();
    showToast("剧情任务已删除");
    return;
  }

  const taskTagButton = target.closest("[data-task-tag]");
  if (taskTagButton) {
    const accountId = Number(taskTagButton.dataset.accountId);
    const taskName = taskTagButton.dataset.taskTag;
    const task = state.data.tasks.find((item) => item.account_id === accountId && item.name === taskName);
    openTaskNoteDialog(task || { id: null, account_id: accountId, name: taskName, notes: "" });
    return;
  }

  const taskId = target.closest("[data-toggle-task]")?.dataset.toggleTask;
  if (taskId) {
    const button = target.closest("[data-toggle-task]");
    const completed = button.dataset.completed !== "true";
    const task = state.data.dueTasks.find((item) => item.id === Number(taskId));
    if (completed && task?.name === "质变仪") {
      openTransformerUsageDialog(task);
      return;
    }
    await api(`/api/tasks/${taskId}/toggle`, { method: "POST", body: JSON.stringify({ date: state.selectedDate, completed }) });
    await loadState();
    showToast(completed ? "已记录完成" : "已撤销记录");
    return;
  }

  const credentialsBtn = target.closest("[data-credentials-account]");
  if (credentialsBtn) {
    await openCredentialsDialog(Number(credentialsBtn.dataset.credentialsAccount));
    return;
  }

  const editAccountId = target.closest("[data-edit-account]")?.dataset.editAccount;
  if (editAccountId) {
    openAccountDialog(state.data.accounts.find((item) => item.id === Number(editAccountId)));
    return;
  }
  const deleteAccountId = target.closest("[data-delete-account]")?.dataset.deleteAccount;
  if (deleteAccountId) {
    const confirmed = await confirmAction({
      title: "停用这个号主？",
      message: "停用后，该号主及其任务不会再显示，历史记录仍会保留。",
      confirmText: "确认停用",
    });
    if (!confirmed) return;
    await api(`/api/accounts/${deleteAccountId}`, { method: "DELETE", body: "{}" });
    await loadState();
    showToast("号主已停用");
    return;
  }
  const reactivateAccountId = target.closest("[data-reactivate-account]")?.dataset.reactivateAccount;
  if (reactivateAccountId) {
    await api(`/api/accounts/${reactivateAccountId}/reactivate`, { method: "POST", body: "{}" });
    await loadState();
    showToast("号主已重新启用");
    return;
  }
  const purgeAccountId = target.closest("[data-purge-account]")?.dataset.purgeAccount;
  if (purgeAccountId) {
    const account = state.data.accounts.find((a) => a.id === Number(purgeAccountId));
    const confirmed = await confirmAction({ title: "彻底删除号主", message: `确认删除「${account?.name}」？删除后不再显示，历史完成记录仍可查询。`, confirmLabel: "删除", danger: true });
    if (!confirmed) return;
    await api(`/api/accounts/${purgeAccountId}/purge`, { method: "POST", body: "{}" });
    await loadState();
    showToast("号主已删除");
    return;
  }
  const customTagButton = target.closest("[data-custom-tag-id]");
  if (customTagButton) {
    const accountId = customTagButton.dataset.accountId;
    const tagId = Number(customTagButton.dataset.customTagId);
    const enabled = customTagButton.dataset.enabled !== "true";
    await api(`/api/accounts/${accountId}/custom-tags`, {
      method: "POST",
      body: JSON.stringify({ tagId, enabled }),
    });
    await loadState();
    showToast(enabled ? "任务已添加" : "任务已移除");
    return;
  }
  const removeCustomTagId = target.closest("[data-remove-custom-tag]")?.dataset.removeCustomTag;
  if (removeCustomTagId) {
    await api(`/api/custom-tags/${removeCustomTagId}`, { method: "DELETE", body: "{}" });
    await loadState();
    showToast("自定义任务已删除");
    return;
  }
  const addTaskId = target.closest("[data-add-task]")?.dataset.addTask;
  if (addTaskId) {
    openTaskDialog(Number(addTaskId));
    return;
  }
  const editTaskId = target.closest("[data-edit-task]")?.dataset.editTask;
  if (editTaskId) {
    openTaskDialog(null, state.data.tasks.find((item) => item.id === Number(editTaskId)));
    return;
  }
  const deleteTaskId = target.closest("[data-delete-task]")?.dataset.deleteTask;
  if (deleteTaskId) {
    const confirmed = await confirmAction({
      title: "停用这个任务？",
      message: "停用后任务不会再显示，已有的完成历史仍会保留。",
      confirmText: "确认停用",
    });
    if (!confirmed) return;
    await api(`/api/tasks/${deleteTaskId}`, { method: "DELETE", body: "{}" });
    await loadState();
    showToast("任务已停用");
  }
}

function switchView(view) {
  state.currentView = view;
  document.querySelectorAll(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.view === view));
  document.querySelectorAll(".view").forEach((item) => item.classList.toggle("active", item.id === `view-${view}`));
  const titles = { today: ["任务总览", "今日任务"], accounts: ["配置中心", "号主与任务"], activities: ["活动管理", "活动任务"], story: ["剧情记录", "剧情任务"], history: ["记录查询", "历史记录"], settings: ["本地数据", "设置与备份"] };
  document.querySelector("#eyebrow").textContent = titles[view][0];
  document.querySelector("#pageTitle").textContent = titles[view][1];
  document.querySelector("#todayControls").classList.toggle("hidden", view !== "today");
  if (view === "history") loadHistory();
}

async function loadHistory() {
  const start = document.querySelector("#historyStart").value;
  const end = document.querySelector("#historyEnd").value;
  const accountId = document.querySelector("#historyAccount").value;
  let url = `/api/history?end=${encodeURIComponent(end)}`;
  if (start) url += `&start=${encodeURIComponent(start)}`;
  if (accountId) url += `&accountId=${encodeURIComponent(accountId)}`;
  const rows = await api(url);
  document.querySelector("#historyRows").innerHTML = rows.map((row) => `<tr><td>${escapeHtml(row.task_date)}</td><td>${escapeHtml(row.account_name)}</td><td>${escapeHtml(row.task_name)}</td><td>${escapeHtml(row.completed_at.replace("T", " "))}</td></tr>`).join("") || '<tr><td colspan="4" class="empty-state">这个日期范围内还没有记录。</td></tr>';
}

function calendarMonthsAgo(reference, months) {
  const targetMonthStart = new Date(reference.getFullYear(), reference.getMonth() - months, 1);
  const targetMonthEnd = new Date(reference.getFullYear(), reference.getMonth() - months + 1, 0);
  return new Date(
    targetMonthStart.getFullYear(),
    targetMonthStart.getMonth(),
    Math.min(reference.getDate(), targetMonthEnd.getDate()),
  );
}

async function loadRecentHistory(months) {
  const end = gameDate();
  const start = calendarMonthsAgo(end, months);
  document.querySelector("#historyStart").value = localDateString(start);
  document.querySelector("#historyEnd").value = localDateString(end);
  await loadHistory();
}

async function exportBackup() {
  const data = await api("/api/export");
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json;charset=utf-8" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `地脉簿备份-${localDateString(gameDate())}.json`;
  link.click();
  URL.revokeObjectURL(link.href);
  showToast("备份已导出");
}

function renderScheduleSettings() {
  const settings = state.data?.settings;
  if (!settings) return;
  document.querySelector("#versionStartDate").value = settings.versionStartDate || "";
  const window = settings.warWindow;
  document.querySelector("#warWindowText").textContent = window
    ? `本期版本 ${window.versionStart} 开始；危战 ${window.eventStart} 10:00 开放，${window.eventEnd} 03:59 截止；下个版本预计 ${window.nextVersionStart}。`
    : "根据原神42天大版本周期自动计算开放和关闭时间。";
}

async function saveVersionDate() {
  const versionStartDate = document.querySelector("#versionStartDate").value;
  const settings = await api("/api/settings/version", {
    method: "PUT",
    body: JSON.stringify({ versionStartDate }),
  });
  state.data.settings = settings;
  renderScheduleSettings();
  await loadState();
  showToast("版本周期锚点已校准");
}

async function importBackup(event) {
  const file = event.target.files?.[0];
  if (!file) return;
  const text = await file.text();
  event.target.value = "";
  let data;
  try {
    data = JSON.parse(text);
  } catch {
    showToast("文件格式无效，请选择由本程序导出的 JSON 备份文件");
    return;
  }
  const confirmed = await confirmAction({
    title: "导入备份？",
    message: "当前所有数据将被替换，操作不可撤销。建议先导出当前数据作为备份。账号密码因加密绑定本机，导入后需重新填写。",
    confirmText: "确认导入",
  });
  if (!confirmed) return;
  const response = await fetch("/api/import", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: text,
  });
  if (!response.ok) {
    const result = await response.json().catch(() => ({}));
    showToast(result.error || "导入失败");
    return;
  }
  await loadState();
  showToast("备份已导入");
}

async function resetDatabase() {
  const confirmed = await confirmAction({
    title: "清空所有数据？",
    message: "将删除全部号主、任务和历史记录，恢复初始状态。操作不可撤销，建议先导出备份。",
    confirmText: "确认清空",
  });
  if (!confirmed) return;
  await api("/api/reset", { method: "POST", body: "{}" });
  await loadState();
  showToast("数据库已清空");
}

let _updatePollTimer = null;

async function checkForUpdate(silent = false) {
  const btn = document.querySelector("#checkUpdateBtn");
  if (btn) { btn.disabled = true; btn.textContent = "检查中…"; }
  try {
    const result = await api("/api/update/check");
    if (result.hasUpdate) {
      document.querySelector("#updateBannerVersion").textContent = `v${result.latest}`;
      document.querySelector("#updateBanner").hidden = false;
    } else if (!silent) {
      showToast(`已是最新版本 v${result.current}`);
    }
    return result;
  } catch (error) {
    if (!silent) showToast("检查更新失败：" + error.message);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "检查更新"; }
  }
}

async function applyUpdate() {
  document.querySelector("#updateBannerApplyBtn").disabled = true;
  document.querySelector("#updateBannerApplyBtn").textContent = "更新中…";
  const checkBtn = document.querySelector("#checkUpdateBtn");
  if (checkBtn) checkBtn.disabled = true;
  const progressBox = document.querySelector("#updateProgress");
  const progressFill = document.querySelector("#updateProgressFill");
  const progressLabel = document.querySelector("#updateProgressLabel");
  if (progressBox) progressBox.hidden = false;
  try {
    await api("/api/update/apply", { method: "POST", body: "{}" });
    _updatePollTimer = window.setInterval(async () => {
      try {
        const s = await api("/api/update/progress");
        const pct = s.total ? Math.round(s.downloaded / s.total * 100) : 0;
        if (progressFill) progressFill.style.width = `${pct}%`;
        if (progressLabel) {
          progressLabel.textContent = s.total ? `正在下载… ${pct}%` : "正在下载…";
        }
        if (s.status === "done") {
          clearInterval(_updatePollTimer);
          if (progressFill) progressFill.style.width = "100%";
          if (progressLabel) progressLabel.textContent = "下载完成，正在重启…";
        } else if (s.status === "error") {
          clearInterval(_updatePollTimer);
          showToast("更新失败：" + s.error);
          if (progressBox) progressBox.hidden = true;
          document.querySelector("#updateBannerApplyBtn").disabled = false;
          document.querySelector("#updateBannerApplyBtn").textContent = "立即更新";
          if (checkBtn) checkBtn.disabled = false;
        }
      } catch { /* ignore poll errors */ }
    }, 500);
  } catch (error) {
    showToast("更新失败：" + error.message);
    document.querySelector("#updateBannerApplyBtn").disabled = false;
    document.querySelector("#updateBannerApplyBtn").textContent = "立即更新";
    if (checkBtn) checkBtn.disabled = false;
    if (progressBox) progressBox.hidden = true;
  }
}

async function shutdownApp() {
  const confirmed = await confirmAction({
    title: "关闭地脉簿？",
    message: "关闭后需要重新双击启动文件才能继续使用，已经保存的数据不会丢失。",
    confirmText: "确认关闭",
  });
  if (!confirmed) return;
  await api("/api/shutdown", { method: "POST", body: "{}" });
  document.querySelector("#shutdownScreen").classList.remove("hidden");
}

function updateProxyDaysLeft() {
  const val = document.querySelector("#accountProxyUntil").value;
  const el = document.querySelector("#proxyDaysLeft");
  if (!val) { el.textContent = ""; return; }
  const today = localDateString(gameDate());
  const days = Math.ceil((new Date(val + "T00:00:00") - new Date(today + "T00:00:00")) / 86400000);
  if (days === 0) el.textContent = "今天到期";
  else if (days > 0) el.textContent = `距今 ${days} 天`;
  else el.textContent = `已过期 ${-days} 天`;
}

function bindEvents() {
  document.addEventListener("click", (event) => handleAction(event.target).catch((error) => showToast(error.message)));
  bindAccountSorting();
  document.querySelectorAll(".nav-item").forEach((item) => item.addEventListener("click", () => switchView(item.dataset.view)));
  document.querySelector("#selectedDate").addEventListener("change", async (event) => { state.selectedDate = event.target.value; await loadState(); });
  document.querySelector("#goToday").addEventListener("click", async () => { state.selectedDate = localDateString(gameDate()); await loadState(); });
  document.querySelector("#completeAll").addEventListener("click", async () => {
    const taskIds = state.data.dueTasks.filter((task) => !task.completed).map((task) => task.id);
    if (!taskIds.length) return;
    await api("/api/tasks/complete-all", { method: "POST", body: JSON.stringify({ date: state.selectedDate, taskIds }) });
    await loadState();
    showToast("当天待办已全部完成");
  });
  document.querySelector("#addAccount").addEventListener("click", () => openAccountDialog());
  document.querySelectorAll("[data-story-type]").forEach((button) => {
    button.addEventListener("click", () => {
      state.selectedStoryType = button.dataset.storyType;
      document.querySelector("#storyHasBonus").checked = state.selectedStoryType !== "world";
      updateStoryForm();
    });
  });
  document.querySelector("#storyHasBonus").addEventListener("change", updateStoryForm);
  document.querySelector("#addStoryTask").addEventListener("click", () => addStoryTask().catch((error) => showToast(error.message)));
  document.querySelector("#storyName").addEventListener("keydown", (event) => {
    if (event.key === "Enter") addStoryTask().catch((error) => showToast(error.message));
  });
  document.querySelectorAll(".proxy-quick-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const d = new Date();
      d.setDate(d.getDate() + Number(btn.dataset.proxyDays));
      document.querySelector("#accountProxyUntil").value = localDateString(d);
      updateProxyDaysLeft();
    });
  });
  document.querySelector("#accountProxyUntil").addEventListener("input", updateProxyDaysLeft);

  const DURATION_OPTIONS = { "大活动": [16, 23], "小活动": [7, 10] };
  document.querySelectorAll(".activity-cat-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const category = btn.dataset.activityCategory;
      document.querySelectorAll(".activity-cat-btn").forEach((b) => b.classList.remove("selected"));
      btn.classList.add("selected");
      document.querySelector("#activityDurBtns").innerHTML = DURATION_OPTIONS[category]
        .map((d) => `<button class="activity-dur-btn" data-activity-days="${d}">${d} 天</button>`)
        .join("")
        + `<button class="activity-dur-btn" data-activity-days="custom">自定义</button>`
        + `<input class="activity-custom-days hidden" id="activityCustomDays" type="number" min="1" max="365" placeholder="输入天数">`;
      document.querySelector("#activityDurRow").classList.remove("hidden");
      document.querySelector("#activityDateRow").classList.add("hidden");
      document.querySelector("#activityStartDate").value = "";
      document.querySelector("#activityEndDate").textContent = "—";
      state.selectedActivityType = { category, durationDays: null };
      document.querySelector("#addActivityBtn").disabled = true;
    });
  });
  const updateEndDateDisplay = () => {
    const start = document.querySelector("#activityStartDate").value;
    const days = state.selectedActivityType?.durationDays;
    if (start && days) {
      document.querySelector("#activityEndDate").textContent = formatActivityDate(calcActivityEndDate(start, days)) + " 03:59";
    }
  };
  const applyDuration = (days) => {
    state.selectedActivityType.durationDays = days;
    const dateRow = document.querySelector("#activityDateRow");
    dateRow.classList.remove("hidden");
    if (!document.querySelector("#activityStartDate").value) {
      document.querySelector("#activityStartDate").value = localDateString(gameDate());
    }
    updateEndDateDisplay();
    document.querySelector("#addActivityBtn").disabled = false;
  };
  document.querySelector("#activityDurBtns").addEventListener("click", (event) => {
    const btn = event.target.closest(".activity-dur-btn");
    if (!btn) return;
    document.querySelectorAll(".activity-dur-btn").forEach((b) => b.classList.remove("selected"));
    btn.classList.add("selected");
    const customInput = document.querySelector("#activityCustomDays");
    if (btn.dataset.activityDays === "custom") {
      customInput.classList.remove("hidden");
      customInput.focus();
      state.selectedActivityType.durationDays = null;
      document.querySelector("#addActivityBtn").disabled = true;
      return;
    }
    customInput.classList.add("hidden");
    applyDuration(Number(btn.dataset.activityDays));
  });
  document.querySelector("#activityDurBtns").addEventListener("input", (event) => {
    if (event.target.id !== "activityCustomDays") return;
    const days = parseInt(event.target.value);
    if (days >= 1 && days <= 365) {
      applyDuration(days);
    } else {
      state.selectedActivityType.durationDays = null;
      document.querySelector("#addActivityBtn").disabled = true;
    }
  });
  document.querySelector("#activityStartDate").addEventListener("change", updateEndDateDisplay);
  document.querySelector("#addActivityBtn").addEventListener("click", async () => {
    if (!state.selectedActivityType?.durationDays) { showToast("请选择活动类型和时长"); return; }
    const name = document.querySelector("#activityNameInput").value.trim() || state.selectedActivityType.category;
    const startDate = document.querySelector("#activityStartDate").value || localDateString(gameDate());
    await api("/api/custom-tags", { method: "POST", body: JSON.stringify({ name, ...state.selectedActivityType, startDate }) }).catch((e) => { showToast(e.message); throw e; });
    document.querySelector("#activityNameInput").value = "";
    document.querySelectorAll(".activity-cat-btn").forEach((b) => b.classList.remove("selected"));
    document.querySelector("#activityDurRow").classList.add("hidden");
    document.querySelector("#activityDurBtns").innerHTML = "";
    document.querySelector("#activityDateRow").classList.add("hidden");
    document.querySelector("#activityStartDate").value = "";
    document.querySelector("#activityEndDate").textContent = "—";
    state.selectedActivityType = null;
    document.querySelector("#addActivityBtn").disabled = true;
    await loadState();
    showToast("活动已添加");
  });
  document.querySelector("#activityNameInput").addEventListener("keydown", (event) => {
    if (event.key === "Enter") document.querySelector("#addActivityBtn").click();
  });
  document.querySelector("#credentialsForm").addEventListener("submit", (event) => saveCredentials(event).catch((error) => showToast(error.message)));
  document.querySelector("#clearCredentials").addEventListener("click", () => clearCredentials().catch((error) => showToast(error.message)));
  document.querySelector("#togglePassword").addEventListener("click", () => {
    const input = document.querySelector("#credentialsPassword");
    const btn = document.querySelector("#togglePassword");
    const show = input.type === "password";
    input.type = show ? "text" : "password";
    btn.textContent = show ? "隐藏" : "显示";
  });
  document.querySelector("#accountTogglePassword").addEventListener("click", () => {
    const input = document.querySelector("#accountCredPassword");
    const btn = document.querySelector("#accountTogglePassword");
    const show = input.type === "password";
    input.type = show ? "text" : "password";
    btn.textContent = show ? "隐藏" : "显示";
  });
  document.querySelector("#accountForm").addEventListener("submit", (event) => saveAccount(event).catch((error) => showToast(error.message)));
  document.querySelector("#taskForm").addEventListener("submit", (event) => saveTask(event).catch((error) => showToast(error.message)));
  document.querySelector("#taskNoteForm").addEventListener("submit", (event) => saveTaskNotes(event).catch((error) => showToast(error.message)));
  document.querySelector("#transformerUsageForm").addEventListener("submit", (event) => saveTransformerUsage(event).catch((error) => showToast(error.message)));
  document.querySelector("#addCustomTaskNote").addEventListener("click", addCustomTaskNote);
  document.querySelector("#customTaskNote").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      addCustomTaskNote();
    }
  });
  document.querySelector("#removeConfiguredTask").addEventListener("click", () => removeConfiguredTask().catch((error) => showToast(error.message)));
  document.querySelector("#taskRecurrence").addEventListener("change", updateScheduleFields);
  document.querySelector("#loadHistory").addEventListener("click", () => loadHistory().catch((error) => showToast(error.message)));
  document.querySelectorAll("[data-history-months]").forEach((button) => {
    button.addEventListener("click", () => loadRecentHistory(Number(button.dataset.historyMonths)).catch((error) => showToast(error.message)));
  });
  document.querySelector("#exportBackup").addEventListener("click", () => exportBackup().catch((error) => showToast(error.message)));
  document.querySelector("#importBackupBtn").addEventListener("click", () => document.querySelector("#importFileInput").click());
  document.querySelector("#importFileInput").addEventListener("change", (event) => importBackup(event).catch((error) => showToast(error.message)));
  document.querySelector("#resetDatabaseBtn").addEventListener("click", () => resetDatabase().catch((error) => showToast(error.message)));
  document.querySelector("#saveVersionDate").addEventListener("click", () => saveVersionDate().catch((error) => showToast(error.message)));
  document.querySelector("#shutdownApp").addEventListener("click", () => shutdownApp().catch((error) => showToast(error.message)));
  document.querySelector("#checkUpdateBtn").addEventListener("click", () => checkForUpdate(false).catch((error) => showToast(error.message)));
  document.querySelector("#updateBannerApplyBtn").addEventListener("click", () => applyUpdate().catch((error) => showToast(error.message)));
  document.querySelector("#updateBannerDismissBtn").addEventListener("click", () => { document.querySelector("#updateBanner").hidden = true; });
  document.querySelector("#chooseCharacterImage").addEventListener("click", () => document.querySelector("#characterImageInput").click());
  document.querySelector("#characterImageInput").addEventListener("change", (event) => chooseCharacterBackground(event).catch((error) => showToast(error.message)));
  document.querySelector("#enableCharacterTheme").addEventListener("click", () => activateCharacterTheme().then(() => showToast("角色主题已启用")).catch((error) => showToast(error.message)));
  document.querySelector("#removeCharacterImage").addEventListener("click", () => removeCharacterBackground().catch((error) => showToast(error.message)));
  document.querySelector("#characterOpacity").addEventListener("input", (event) => applyCharacterOpacity(event.target.value));
  document.querySelector("#characterPosition").addEventListener("input", (event) => applyCharacterPosition(event.target.value));
  document.querySelector("#characterZoom").addEventListener("input", (event) => applyCharacterZoom(event.target.value));
  document.querySelectorAll("[data-theme-option]").forEach((button) => {
    button.addEventListener("click", () => {
      applyTheme(button.dataset.themeOption);
      showToast(`已切换为${button.textContent.trim()}主题`);
    });
  });
}

async function sendHeartbeat() {
  try { await fetch("/api/heartbeat"); } catch { /* server gone */ }
}

function startHeartbeat() {
  sendHeartbeat();
  window.setInterval(sendHeartbeat, 15000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) sendHeartbeat(); });
}

async function initialize() {
  applyTheme(localStorage.getItem("task-recorder-theme") || "green", false, false);
  const now = gameDate();
  document.querySelector("#historyStart").value = "";
  document.querySelector("#historyEnd").value = localDateString(now);
  bindEvents();
  startHeartbeat();
  await initializeCharacterBackground();
  await loadState();
  window.setInterval(() => {
    if (state.currentView === "today" && state.selectedDate === localDateString(gameDate()) && !document.querySelector("dialog[open]")) {
      loadState().catch((error) => showToast(error.message));
    }
  }, 60000);
  checkForUpdate(true).catch(() => {});
}

initialize().catch((error) => showToast(error.message));
