const state = {
  data: null,
  feedShowAll: false,
  feedDefaultLimit: 5,
  feedExpanded: false,
  coachExpanded: false,
  expandedDays: new Set(),
  actionLocks: Object.create(null),
};

function setMessage(text, isError = false) {
  const el = document.getElementById("message");
  el.textContent = text;
  el.classList.toggle("toast-error", isError);
}

function getAuthToken() {
  return (localStorage.getItem("auth_token") || "").trim();
}

function parseJsonSafe(text) {
  try {
    return text ? JSON.parse(text) : {};
  } catch (_) {
    return {};
  }
}

function withThrottle(actionKey, fn) {
  const now = Date.now();
  const last = Number(state.actionLocks[actionKey] || 0);
  if (now - last < 800) return Promise.resolve(null);
  state.actionLocks[actionKey] = now;
  return Promise.resolve().then(fn);
}

function syncOutcomeText(baseText, data) {
  if (!data) return baseText;
  if (data.cloud_synced === true) return `${baseText}，云端已记`;
  if (data.cloud_synced === false && data.cloud_error) {
    return `${baseText}，仅本地`;
  }
  return `${baseText}`;
}

function hasUnsavedInput(sheetId) {
  const sheet = document.getElementById(sheetId);
  if (!sheet) return false;
  const fields = sheet.querySelectorAll("input, textarea, select");
  for (const field of fields) {
    if (field.disabled) continue;
    const current = String(field.value || "");
    const initial = String(field.defaultValue || "");
    if (current !== initial && current.trim() !== "") return true;
  }
  return false;
}

function closeSheet(id, opts = {}) {
  const { force = false } = opts;
  if (!force && hasUnsavedInput(id)) {
    const ok = window.confirm("当前内容未保存，确认离开？");
    if (!ok) return false;
  }
  document.getElementById(id).classList.add("hidden");
  const openSheets = Array.from(document.querySelectorAll(".sheet")).some((el) => !el.classList.contains("hidden"));
  if (!openSheets) document.getElementById("sheetMask").classList.add("hidden");
  return true;
}

function closeAllSheets(opts = {}) {
  const openSheets = Array.from(document.querySelectorAll(".sheet")).filter((el) => !el.classList.contains("hidden"));
  for (const sheet of openSheets) {
    const closed = closeSheet(sheet.id, opts);
    if (!closed) return false;
  }
  document.getElementById("sheetMask").classList.add("hidden");
  return true;
}

async function readApiResponse(res) {
  const text = await res.text();
  const data = parseJsonSafe(text);
  if (res.status === 401 || data.need_auth) {
    openSheet("authSheet");
    throw new Error("需要访问密钥，请先输入 AUTH_TOKEN");
  }
  if (!res.ok || !data.ok) {
    throw new Error(data.error || `请求失败(${res.status})`);
  }
  return data;
}

async function apiGet(url) {
  const token = getAuthToken();
  const res = await fetch(url, {
    headers: {
      ...(token ? { "X-Auth-Token": token } : {}),
    },
  });
  return readApiResponse(res);
}

async function apiPost(url, body) {
  const token = getAuthToken();
  const payload = body || {};
  if (token) {
    payload.auth_token = token;
  }
  const res = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(token ? { "X-Auth-Token": token } : {}),
    },
    body: JSON.stringify(payload),
  });
  return readApiResponse(res);
}

function fmtWeight(v) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "-";
  return `${Number(v).toFixed(1)} kg`;
}

function fmtWeightAbs(v) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "-";
  return `${Math.abs(Number(v)).toFixed(1)} kg`;
}

function fmtDays(v) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "-";
  return `${Number(v)}天`;
}

function fmtHours(v) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "-";
  return `${Number(v).toFixed(1)} h`;
}

function fmtMinutes(v) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "-";
  return `${Number(v)} 分钟`;
}

function toApiDatetime(inputId) {
  const raw = document.getElementById(inputId).value;
  return raw ? raw.replace("T", " ") : "";
}

function nowLocalInputValue() {
  const d = new Date();
  const pad = (v) => String(v).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function dayString(date) {
  const pad = (v) => String(v).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

function mealFlag(meal, plan) {
  if (!plan || !meal || !meal.time) return "窗口未知";
  const [datePart] = meal.time.split(" ");
  const startTs = new Date(`${datePart}T${plan.start}:00`).getTime();
  const mealTs = new Date(meal.time.replace(" ", "T") + ":00").getTime();
  const endTs = startTs + Number(plan.hours || 8) * 3600 * 1000;
  return mealTs >= startTs && mealTs <= endTs ? "窗口内" : "窗口外";
}

function dayLabel(day) {
  const today = new Date();
  const todayStr = dayString(today);
  const y = new Date(today);
  y.setDate(today.getDate() - 1);
  const yesterdayStr = dayString(y);
  if (day === todayStr) return "今天";
  if (day === yesterdayStr) return "昨天";
  return day;
}

function goalMessage(goal) {
  if (!goal || goal.current_weight == null || goal.target_weight == null) {
    return {
      headline: "先设置减脂目标",
      subline: "设置后显示进度",
    };
  }

  const left = Number(goal.left_weight);
  const start = Number(goal.start_weight);
  const current = Number(goal.current_weight);
  const hasStart = !Number.isNaN(start);
  const hasCurrent = !Number.isNaN(current);

  if (goal.pace_status === "behind" || (hasStart && hasCurrent && current > start)) {
    return {
      headline: "出现反弹",
      subline: "先稳住",
    };
  }

  if (goal.reached === true) {
    return {
      headline: "已达标，继续稳定",
      subline:
        goal.lost_weight != null
          ? `较起始 -${fmtWeightAbs(goal.lost_weight)}`
          : "保持节奏",
    };
  }

  if (!Number.isNaN(left) && left > 0) {
    return {
      headline: `距离目标还差 ${left.toFixed(1)} kg`,
      subline:
        goal.lost_weight != null
          ? `较起始 -${fmtWeightAbs(goal.lost_weight)}`
          : "继续减脂",
    };
  }

  return {
    headline: "先设置减脂目标",
    subline: "设置后显示进度",
  };
}

function executionStatusOf(today) {
  return today?.execution_status || today?.status || "未记录";
}

function minutesFromTime(value) {
  const [h, m] = String(value || "10:00").split(":");
  return Number(h || 0) * 60 + Number(m || 0);
}

function currentWindowState(plan) {
  const safePlan = plan || { start: "10:00", hours: 8 };
  const start = minutesFromTime(safePlan.start || "10:00");
  const duration = Number(safePlan.hours || 8) * 60;
  const end = (start + duration) % 1440;
  const now = new Date();
  const current = now.getHours() * 60 + now.getMinutes();
  const inWindow = duration >= 1440 ? true : start <= end ? current >= start && current <= end : current >= start || current <= end;
  const elapsedInWindow = inWindow ? (current - start + 1440) % 1440 : 0;
  const progressPercent = duration >= 1440 ? 100 : Math.max(0, Math.min(100, Math.round((elapsedInWindow / duration) * 100)));
  const dotPercent = inWindow ? progressPercent : start > current ? 0 : 100;
  const minutesLeft = inWindow
    ? start <= end
      ? Math.max(0, end - current)
      : current >= start
        ? 1440 - current + end
        : end - current
    : start > current
      ? start - current
      : 1440 - current + start;
  return {
    inWindow,
    minutesLeft,
    progressPercent,
    dotPercent,
    start: safePlan.start || "10:00",
    end: `${String(Math.floor(end / 60)).padStart(2, "0")}:${String(end % 60).padStart(2, "0")}`,
  };
}

function fmtDurationMinutes(minutes) {
  if (minutes == null || Number.isNaN(Number(minutes))) return "-";
  const safe = Math.max(0, Number(minutes));
  const h = Math.floor(safe / 60);
  const m = safe % 60;
  if (h <= 0) return `${m}分钟`;
  if (m === 0) return `${h}小时`;
  return `${h}小时${m}分钟`;
}

function todayActionCopy(today, plan) {
  const mealCount = Number(today.meal_count || 0);
  const hasWeight = today.weight !== null && today.weight !== undefined;
  const executionStatus = executionStatusOf(today);
  const windowState = currentWindowState(plan);

  if (executionStatus === "未达标") {
    return {
      title: "今天有偏离",
      reason: windowState.inWindow
        ? "下一餐回窗口内"
        : `先喝水 · 等 ${windowState.start}`,
      badgeLabel: "需要调整",
      badgeClass: "status-bad",
      mealButton: windowState.inWindow ? "记录下一餐，回到窗口内" : "记录进食",
      weightButton: hasWeight ? "更新体重" : "记录体重",
    };
  }

  if (mealCount > 0 && executionStatus === "达标") {
    return {
      title: windowState.inWindow ? "现在可以进食" : "正在断食",
      reason: windowState.inWindow
        ? `剩余 ${fmtDurationMinutes(windowState.minutesLeft)}`
        : `只喝水 · ${fmtDurationMinutes(windowState.minutesLeft)}`,
      badgeLabel: "进行顺利",
      badgeClass: "status-good",
      mealButton: windowState.inWindow ? "记录这一餐" : "记录进食",
      weightButton: hasWeight ? "更新体重" : "记录体重",
    };
  }

  if (hasWeight && mealCount === 0) {
    return {
      title: windowState.inWindow ? "现在可以进食" : "正在断食",
      reason: windowState.inWindow
        ? `先记录第一餐`
        : `只喝水 · ${fmtDurationMinutes(windowState.minutesLeft)}`,
      badgeLabel: "待完成",
      badgeClass: "status-neutral",
      mealButton: windowState.inWindow ? "记录第一餐" : "记录进食",
      weightButton: "更新体重",
    };
  }

  return {
    title: windowState.inWindow ? "现在可以进食" : "正在断食",
    reason: windowState.inWindow
      ? "先记录第一餐"
      : `只喝水 · ${fmtDurationMinutes(windowState.minutesLeft)}`,
    badgeLabel: "待开始",
    badgeClass: "status-neutral",
    mealButton: windowState.inWindow ? "记录第一餐" : "记录进食",
    weightButton: hasWeight ? "更新体重" : "记录体重",
  };
}

function paceMessage(goal) {
  const pace = goal?.pace_status;
  if (pace === "reached") return "节奏：已达标";
  if (pace === "ahead") return "节奏：略快";
  if (pace === "on_track") return "节奏：正常";
  if (pace === "behind") return "节奏：落后";
  if (pace === "missed") return "节奏：未达标";
  if (pace === "not_started") return "节奏：未开始";
  return "节奏：待评估";
}

function paceToneClass(goal) {
  const pace = goal?.pace_status;
  if (pace === "reached" || pace === "ahead" || pace === "on_track") return "goal-good";
  if (pace === "behind" || pace === "missed") return "goal-bad";
  return "goal-normal";
}

function cycleHeadline(goal) {
  if (!goal?.start_date || !goal?.end_date) return "周期未设置";
  return `周期 ${goal.start_date} ~ ${goal.end_date}`;
}

function cycleGoalTag(goal) {
  if (!goal?.start_date || !goal?.end_date || goal?.target_weight == null || goal?.current_weight == null) {
    return { text: "周期状态：待评估", klass: "status-neutral" };
  }
  if (goal.reached === true) {
    return { text: "周期状态：已达标", klass: "status-good" };
  }
  if (goal.cycle_status === "ended" || goal.pace_status === "missed" || goal.pace_status === "behind") {
    return { text: "周期状态：需调整", klass: "status-bad" };
  }
  return { text: "周期状态：周期内有望达标", klass: "status-good" };
}

function weeklyPaceSummary(goal) {
  const weekTarget = goal?.week_target_loss;
  const weekActual = goal?.week_actual_loss;
  if (weekTarget == null || weekActual == null || Number.isNaN(Number(weekTarget)) || Number.isNaN(Number(weekActual))) {
    return "近7天建议下降 - | 当前变化 -";
  }

  const target = Number(weekTarget);
  const actual = Number(weekActual);
  const delta = actual - target;
  let trend = "正常";
  let advice = "，继续按当前节奏。";
  if (delta > 0.6) trend = "偏快";
  else if (delta < -0.6) trend = "偏慢";

  if (trend === "偏快") advice = "，偏快，注意稳定和补水。";
  if (trend === "偏慢") advice = "，偏慢，优先把窗口外进食降下来。";

  const directionText = actual >= 0 ? `下降 ${actual.toFixed(1)}kg` : `上升 ${Math.abs(actual).toFixed(1)}kg`;
  return `近7天建议下降 ${target.toFixed(1)}kg，当前${directionText}（${trend}）${advice}`;
}

function computeGoalVisual(goal) {
  if (!goal || goal.start_weight == null || goal.target_weight == null || goal.current_weight == null) {
    return { percent: 0, label: "未设置", tone: "normal" };
  }
  if (goal.progress_percent != null && !Number.isNaN(Number(goal.progress_percent))) {
    const percent = Math.round(Math.max(0, Math.min(100, Number(goal.progress_percent))));
    const isReached = goal.reached === true;
    const isAbnormal = Number(goal.current_weight) > Number(goal.start_weight);
    const tone = isReached ? "good" : isAbnormal ? "bad" : "normal";
    return { percent, label: `${percent}%`, tone };
  }
  const start = Number(goal.start_weight);
  const target = Number(goal.target_weight);
  const current = Number(goal.current_weight);
  const total = start - target;
  if (total <= 0) return { percent: 0, label: "异常", tone: "bad" };

  const done = Math.min(total, Math.max(0, start - current));
  const percent = Math.round((done / total) * 100);
  const isReached = goal.reached === true;
  const isAbnormal = current > start;
  const tone = isReached ? "good" : isAbnormal ? "bad" : "normal";
  return { percent, label: `${percent}%`, tone };
}

function evaluateDayBlock(block, plan) {
  const outCount = block.meals.filter((m) => mealFlag(m, plan) === "窗口外").length;
  if (outCount > 0) return "未达标";
  if (block.meals.length > 0) return "达标";
  return "未记录";
}

function groupFeed(data) {
  const grouped = new Map();
  const meals = data.meals || [];
  const weights = data.weights || [];
  const sleeps = data.sleeps || [];
  const exercises = data.exercises || [];

  meals.forEach((meal) => {
    const day = meal.date;
    if (!grouped.has(day)) grouped.set(day, { meals: [], weights: [], sleeps: [], exercises: [] });
    grouped.get(day).meals.push(meal);
  });
  weights.forEach((weight) => {
    const day = weight.date;
    if (!grouped.has(day)) grouped.set(day, { meals: [], weights: [], sleeps: [], exercises: [] });
    grouped.get(day).weights.push(weight);
  });
  sleeps.forEach((sleep) => {
    const day = sleep.date;
    if (!grouped.has(day)) grouped.set(day, { meals: [], weights: [], sleeps: [], exercises: [] });
    grouped.get(day).sleeps.push(sleep);
  });
  exercises.forEach((exercise) => {
    const day = exercise.date;
    if (!grouped.has(day)) grouped.set(day, { meals: [], weights: [], sleeps: [], exercises: [] });
    grouped.get(day).exercises.push(exercise);
  });

  const sortedDays = Array.from(grouped.keys()).sort((a, b) => (a < b ? 1 : -1));
  return sortedDays.map((day) => {
    const bucket = grouped.get(day);
    bucket.meals.sort((a, b) => (a.time < b.time ? 1 : -1));
    bucket.weights.sort((a, b) => (a.time < b.time ? 1 : -1));
    bucket.sleeps.sort((a, b) => (a.time < b.time ? 1 : -1));
    bucket.exercises.sort((a, b) => (a.time < b.time ? 1 : -1));
    const status = evaluateDayBlock(bucket, data.plan);
    return {
      day,
      label: dayLabel(day),
      status,
      meals: bucket.meals,
      weights: bucket.weights,
      sleeps: bucket.sleeps,
      exercises: bucket.exercises,
      outCount: bucket.meals.filter((m) => mealFlag(m, data.plan) === "窗口外").length,
    };
  });
}

function weekSnapshots(data) {
  const byDay = new Map(groupFeed(data).map((block) => [block.day, block]));
  const today = new Date();
  const snapshots = [];

  for (let i = 0; i < 7; i += 1) {
    const date = new Date(today);
    date.setDate(today.getDate() - i);
    const day = dayString(date);
    const block = byDay.get(day) || { meals: [], weights: [], sleeps: [], exercises: [], outCount: 0, status: "未记录" };
    snapshots.push({
      day,
      label: dayLabel(day),
      status: block.status || "未记录",
      meals: block.meals || [],
      weights: block.weights || [],
      sleeps: block.sleeps || [],
      exercises: block.exercises || [],
      outCount: block.outCount || 0,
    });
  }

  return snapshots;
}

function reminderDetail(snapshot, data) {
  if (snapshot.status === "未达标") {
    const firstOut = snapshot.meals.find((meal) => mealFlag(meal, data.plan) === "窗口外");
    if (firstOut) return `${snapshot.label} ${firstOut.time.slice(11)} 有窗口外进食。`;
    return `${snapshot.label} 有 ${snapshot.outCount} 次窗口外进食。`;
  }

  if (snapshot.status === "未记录") {
    if (snapshot.weights.length > 0) return `${snapshot.label} 记录了体重，但没有进食记录。`;
    return `${snapshot.label} 没有进食记录。`;
  }

  return "继续保持当前节奏。";
}

function appendReminderItem(container, tone, title, detail) {
  const item = document.createElement("article");
  item.className = `reminder-item reminder-${tone}`;
  item.innerHTML = `
    <p class="reminder-item-title">${title}</p>
    <p class="reminder-item-detail">${detail}</p>
  `;
  container.appendChild(item);
}

function setInputValue(id, value) {
  const el = document.getElementById(id);
  if (!el) return;
  const safe = value == null ? "" : String(value);
  el.value = safe;
  el.defaultValue = safe;
}

function openSheet(id) {
  document.getElementById("sheetMask").classList.remove("hidden");
  document.getElementById(id).classList.remove("hidden");
  if (id === "mealSheet") {
    const el = document.getElementById("mealTimeInput");
    if (!el.value) setInputValue("mealTimeInput", nowLocalInputValue());
  }
  if (id === "weightSheet") {
    const el = document.getElementById("weightTimeInput");
    if (!el.value) setInputValue("weightTimeInput", nowLocalInputValue());
  }
  if (id === "authSheet") {
    setInputValue("authTokenInput", getAuthToken());
  }
}

function renderHero(data) {
  const goal = data.goal || {};
  const visual = computeGoalVisual(goal);
  const goalSource = data.goal_source === "notion" ? "云端" : data.goal_source === "local" ? "本地" : "未设";

  document.getElementById("startWeightInline").textContent = fmtWeight(goal.start_weight);
  document.getElementById("currentWeightInline").textContent = fmtWeight(goal.current_weight);
  document.getElementById("targetWeightInline").textContent = fmtWeight(goal.target_weight);
  document.getElementById("goalProgressText").textContent = visual.label;
  document.getElementById("goalSourceText").textContent = goalSource;

  const progressText = document.getElementById("goalProgressText");
  const progressBar = document.getElementById("goalProgressBar");
  progressBar.style.width = `${visual.percent}%`;
  progressText.classList.remove("goal-good", "goal-bad", "goal-normal");
  progressBar.classList.remove("fill-good", "fill-bad", "fill-normal");
  progressText.classList.add(`goal-${visual.tone}`);
  progressBar.classList.add(`fill-${visual.tone}`);

  const paceEl = document.getElementById("paceText");
  paceEl.textContent = paceMessage(goal);
  paceEl.classList.remove("goal-good", "goal-bad", "goal-normal");
  paceEl.classList.add(paceToneClass(goal));

  const cycleTagEl = document.getElementById("cycleGoalTag");
  const cycleTag = cycleGoalTag(goal);
  cycleTagEl.textContent = cycleTag.text;
  cycleTagEl.className = `status-badge cycle-goal-tag ${cycleTag.klass}`;

  const left = goal.left_weight != null ? Math.max(0, Number(goal.left_weight)) : null;
  const remaining = goal.cycle_remaining_days;
  const summaryParts = [];
  if (left != null && !Number.isNaN(left)) summaryParts.push(`还差 ${left.toFixed(1)} kg`);
  if (remaining != null) summaryParts.push(`剩余 ${fmtDays(remaining)}`);
  document.getElementById("goalCycleSummary").textContent = summaryParts.length ? summaryParts.join(" · ") : "还差 - kg · 剩余 - 天";

  const elapsed = goal.cycle_elapsed_days;
  const total = goal.cycle_total_days;
  const daysToEnd = goal.cycle_days_to_end;
  if (goal.cycle_status === "ended" && daysToEnd != null) {
    document.getElementById("goalCycleSummary").textContent = `周期已结束 +${Math.abs(daysToEnd)}天`;
  } else {
    if (elapsed != null && total != null && summaryParts.length === 1) {
      document.getElementById("goalCycleSummary").textContent = `${summaryParts[0]} · 第 ${elapsed}/${total} 天`;
    }
  }

  const message = goalMessage(goal);

  document.getElementById("heroHeadline").textContent = message.headline;
  document.getElementById("heroSubline").textContent = message.subline;
}

function renderToday(data) {
  const today = data.today || {};
  const badge = document.getElementById("statusBadge");
  const plan = data.plan || { start: "10:00", hours: 8 };
  const copy = todayActionCopy(today, plan);
  if (data.goal?.is_sprint_phase) {
    copy.reason = `冲刺期：还剩 ${data.goal.cycle_remaining_days} 天。${copy.reason}`;
  }
  if (data.goal?.cycle_status === "ended" && data.goal?.reached !== true) {
    copy.reason = "本周期已结束且未达标，建议开启下一周期继续执行。";
  }
  badge.className = `status-badge ${copy.badgeClass}`;
  badge.textContent = copy.badgeLabel;
  const statusCard = document.querySelector(".today-hero-card");
  if (statusCard) {
    statusCard.classList.remove("today-good", "today-bad", "today-neutral");
    const toneClass = copy.badgeClass === "status-bad" ? "today-bad" : copy.badgeClass === "status-good" ? "today-good" : "today-neutral";
    statusCard.classList.add(toneClass);
  }

  document.getElementById("todayStatus").textContent = copy.title;
  document.getElementById("todayReason").textContent = copy.reason;
  const windowState = currentWindowState(plan);
  document.getElementById("fastingRule").textContent = windowState.inWindow ? "窗口内可进食" : "窗口外只喝水";

  document.getElementById("windowStartLabel").textContent = windowState.start;
  document.getElementById("windowEndLabel").textContent = windowState.end;
  document.getElementById("windowStateLabel").textContent = windowState.inWindow ? "当前在窗口内" : "当前在窗口外";
  const windowFill = document.getElementById("windowProgressFill");
  const windowDot = document.getElementById("windowNowDot");
  if (windowFill) windowFill.style.width = windowState.inWindow ? `${windowState.progressPercent}%` : "0%";
  if (windowDot) windowDot.style.left = `${windowState.dotPercent}%`;

  document.getElementById("windowChip").textContent =
    `窗口 ${windowState.start}-${windowState.end}`;
  document.getElementById("mealCountChip").textContent = `进食 ${today.meal_count || 0} 次`;
  document.getElementById("outWindowChip").textContent = `窗口外 ${today.meal_out_window_count || 0} 次`;
  document.getElementById("quickMealBtn").textContent = copy.mealButton;
  document.getElementById("quickWeightBtn").textContent = copy.weightButton;

  setInputValue("windowStartInput", plan.start || "10:00");
  setInputValue("windowHoursInput", plan.hours || 8);

  if (goalFilled(data.goal)) {
    setInputValue("goalStartDateInput", data.goal.start_date || "");
    setInputValue("goalEndDateInput", data.goal.end_date || "");
    setInputValue("goalStartWeightInput", data.goal.start_weight ?? "");
    setInputValue("goalTargetWeightInput", data.goal.target_weight ?? "");
  }
}

function renderCoach(data) {
  const today = data.today || {};
  const coach = data.coach || {};
  const msgEl = document.getElementById("coachMessage");
  const statusEl = document.getElementById("coachStatus");
  const actionsEl = document.getElementById("coachActions");
  const toggleBtn = document.getElementById("toggleCoachBtn");
  const tone = coach.status_tone || "neutral";
  const statusClass = tone === "bad" ? "status-bad" : tone === "good" ? "status-good" : "status-neutral";
  statusEl.className = `status-badge ${statusClass}`;
  statusEl.textContent = coach.status_label || "待完成";
  actionsEl.classList.toggle("hidden", !state.coachExpanded);
  toggleBtn.textContent = state.coachExpanded ? "收起" : "更多动作";

  document.getElementById("sleepChip").textContent = `睡眠 ${fmtHours(today.sleep_hours)}`;
  document.getElementById("exerciseChip").textContent = `运动 ${fmtMinutes(today.exercise_minutes)}`;
  if (tone === "bad") {
    msgEl.textContent = "偏离";
  } else if (tone === "good") {
    msgEl.textContent = "稳定";
  } else if (tone === "neutral") {
    msgEl.textContent = "补齐";
  } else {
    msgEl.textContent = (coach.message || "睡眠与运动可选").split("。")[0];
  }
}

function goalFilled(goal) {
  if (!goal) return false;
  return Boolean(goal.start_date || goal.end_date || goal.start_weight || goal.target_weight);
}

function renderReminders(data) {
  const title = document.getElementById("reminderTitle");
  const reviewSummary = document.getElementById("reviewSummary");
  const list = document.getElementById("reminderHighlights");
  const snapshots = weekSnapshots(data);
  const anomalies = snapshots.filter((snapshot) => snapshot.status === "未达标");
  const missing = snapshots.filter((snapshot) => snapshot.status === "未记录");
  const latestMeal = (data.meals || [])[0];
  const riskMeterFill = document.getElementById("riskMeterFill");
  const riskRatio = Math.max(0, Math.min(100, Math.round((anomalies.length / 7) * 100)));
  if (riskMeterFill) riskMeterFill.style.width = `${riskRatio}%`;

  let latestLabel = "最近 -";
  if (latestMeal && latestMeal.time) {
    const flag = mealFlag(latestMeal, data.plan);
    latestLabel = `最近 ${latestMeal.time.slice(5, 10)} ${flag}`;
  }

  list.innerHTML = "";

  if (anomalies.length > 0) {
    title.textContent = "偏离风险";
    reviewSummary.textContent = `需调整 · 偏离 ${anomalies.length} · 缺记 ${missing.length}`;
    anomalies.slice(0, 2).forEach((snapshot) => {
      appendReminderItem(list, "bad", `${snapshot.label}`, reminderDetail(snapshot, data));
    });
    return;
  }

  if (missing.length > 0) {
    title.textContent = "记录缺口";
    reviewSummary.textContent = `待补记 · 偏离 0 · 缺记 ${missing.length}`;
    missing.slice(0, 2).forEach((snapshot) => {
      appendReminderItem(list, "neutral", `${snapshot.label}`, reminderDetail(snapshot, data));
    });
    return;
  }

  title.textContent = "状态稳定";
  reviewSummary.textContent = `稳定 · 偏离 0 · 缺记 0 · ${latestLabel}`;
  appendReminderItem(
    list,
    "good",
    "节奏稳定",
    `窗口内 ${data.week_stats?.ok_days ?? 0} 天`
  );
}

function renderFeed(data) {
  const panel = document.getElementById("recordsPanel");
  const container = document.getElementById("feedList");
  const hint = document.getElementById("feedHint");
  const toggleBtn = document.getElementById("toggleRecentBtn");
  const toggleFeedBtn = document.getElementById("toggleFeedBtn");
  container.innerHTML = "";
  const filtered = groupFeed(data);
  const blocks = state.feedShowAll ? filtered : filtered.slice(0, state.feedDefaultLimit);
  toggleFeedBtn.textContent = state.feedExpanded ? "收起" : "全部记录";
  panel.classList.toggle("hidden", !state.feedExpanded);
  if (!state.feedExpanded) return;

  const canToggle = filtered.length > state.feedDefaultLimit;
  toggleBtn.classList.toggle("hidden", !canToggle);
  if (canToggle) {
    toggleBtn.textContent = state.feedShowAll ? "只看最近5条" : "查看全部";
    hint.textContent = state.feedShowAll ? `已显示全部 ${filtered.length} 条记录` : "默认显示最近5条记录";
  } else {
    hint.textContent = "已显示全部记录";
  }

  if (!blocks.length) {
    const empty = document.createElement("div");
    empty.className = "feed-empty";
    empty.textContent = "暂无动态记录。";
    container.appendChild(empty);
    return;
  }

  blocks.forEach((block) => {
    const expanded = state.expandedDays.has(block.day);
    const card = document.createElement("article");
    card.className = "day-card";

    const header = document.createElement("div");
    header.className = "day-card-head";

    const titleWrap = document.createElement("div");
    const title = document.createElement("h4");
    title.textContent = block.label;
    const subtitle = document.createElement("p");
    subtitle.className = "day-card-sub";
    subtitle.textContent = `${block.meals.length} 次进食 · 窗口外 ${block.outCount} 次 · 体重 ${block.weights.length} 次 · 睡眠 ${block.sleeps.length} 次 · 运动 ${block.exercises.length} 次`;
    titleWrap.append(title, subtitle);

    const right = document.createElement("div");
    right.className = "day-card-right";
    const status = document.createElement("span");
    const pillTone = block.status === "达标" ? "day-pill-good" : block.status === "未达标" ? "day-pill-bad" : "day-pill-neutral";
    status.className = `day-pill ${pillTone}`;
    status.textContent = block.status;
    const toggle = document.createElement("button");
    toggle.className = "day-toggle";
    toggle.textContent = expanded ? "收起" : "展开";
    toggle.addEventListener("click", () => {
      if (state.expandedDays.has(block.day)) {
        state.expandedDays.delete(block.day);
      } else {
        state.expandedDays.add(block.day);
      }
      renderFeed(state.data);
    });
    right.append(status, toggle);

    header.append(titleWrap, right);
    card.appendChild(header);

    if (expanded) {
      const body = document.createElement("div");
      body.className = "day-card-body";
      block.meals.forEach((meal) => {
        const item = document.createElement("div");
        item.className = "feed-item";
        item.innerHTML = `
          <span class="feed-item-tag meal-tag">进食</span>
          <div class="feed-item-main">
            <p>${meal.food}</p>
            <span>${meal.time.slice(11)} · ${mealFlag(meal, data.plan)}</span>
          </div>
        `;
        body.appendChild(item);
      });
      block.weights.forEach((weight) => {
        const item = document.createElement("div");
        item.className = "feed-item";
        item.innerHTML = `
          <span class="feed-item-tag weight-tag">体重</span>
          <div class="feed-item-main">
            <p>${fmtWeight(weight.weight)}</p>
            <span>${weight.time.slice(11)}${weight.note ? ` · ${weight.note}` : ""}</span>
          </div>
        `;
        body.appendChild(item);
      });
      block.sleeps.forEach((sleep) => {
        const item = document.createElement("div");
        item.className = "feed-item";
        item.innerHTML = `
          <span class="feed-item-tag sleep-tag">睡眠</span>
          <div class="feed-item-main">
            <p>${fmtHours(sleep.hours)}</p>
            <span>${sleep.time.slice(11)}${sleep.note ? ` · ${sleep.note}` : ""}</span>
          </div>
        `;
        body.appendChild(item);
      });
      block.exercises.forEach((exercise) => {
        const item = document.createElement("div");
        item.className = "feed-item";
        item.innerHTML = `
          <span class="feed-item-tag exercise-tag">运动</span>
          <div class="feed-item-main">
            <p>${exercise.kind ? `${exercise.kind} ` : ""}${exercise.minutes} 分钟</p>
            <span>${exercise.time.slice(11)}${exercise.note ? ` · ${exercise.note}` : ""}</span>
          </div>
        `;
        body.appendChild(item);
      });
      card.appendChild(body);
    }

    container.appendChild(card);
  });
}

function render() {
  if (!state.data) return;
  renderHero(state.data);
  renderToday(state.data);
  renderCoach(state.data);
  renderReminders(state.data);
  renderFeed(state.data);
}

async function refreshStatus() {
  try {
    state.data = await apiGet("/api/status");
    render();
  } catch (err) {
    setMessage(err.message, true);
  }
}

async function onMealSubmit() {
  const food = document.getElementById("mealFoodInput").value.trim();
  const note = document.getElementById("mealNoteInput").value.trim();
  const time = toApiDatetime("mealTimeInput");
  if (!food) {
    setMessage("请填食物", true);
    return;
  }

  await withThrottle("saveMeal", async () => {
    try {
      const data = await apiPost("/api/meal", { food, note, time });
      setMessage(syncOutcomeText(data.in_window ? "已记录进食" : "已记录，注意这次在窗口外", data), !data.in_window);
      setInputValue("mealFoodInput", "");
      setInputValue("mealTimeInput", "");
      setInputValue("mealNoteInput", "");
      closeSheet("mealSheet", { force: true });
      await refreshStatus();
    } catch (err) {
      setMessage(err.message, true);
    }
  });
}

async function onWeightSubmit() {
  const value = Number(document.getElementById("weightValueInput").value || 0);
  const note = document.getElementById("weightNoteInput").value.trim();
  const time = toApiDatetime("weightTimeInput");
  if (!value || value <= 0) {
    setMessage("请填体重", true);
    return;
  }

  await withThrottle("saveWeight", async () => {
    try {
      const data = await apiPost("/api/weight", { value, note, time });
      setMessage(syncOutcomeText("已记录体重", data));
      setInputValue("weightValueInput", "");
      setInputValue("weightTimeInput", "");
      setInputValue("weightNoteInput", "");
      closeSheet("weightSheet", { force: true });
      await refreshStatus();
    } catch (err) {
      setMessage(err.message, true);
    }
  });
}

function onSleepPreset(hours, label) {
  return async () => {
    const confirmed = window.confirm(`确认提交：${label}？`);
    if (!confirmed) return;
    await withThrottle(`sleep-${hours}`, async () => {
      try {
        const data = await apiPost("/api/sleep", { hours, note: `一键打卡:${label}` });
        setMessage(syncOutcomeText(`已记录睡眠：${label}`, data));
        await refreshStatus();
      } catch (err) {
        setMessage(err.message, true);
      }
    });
  };
}

function onExercisePreset(minutes, label) {
  return async () => {
    const confirmed = window.confirm(`确认提交：${label}？`);
    if (!confirmed) return;
    await withThrottle(`exercise-${minutes}`, async () => {
      try {
        const data = await apiPost("/api/exercise", { minutes, kind: "快走", note: `一键打卡:${label}` });
        setMessage(syncOutcomeText(`已记录运动：${label}`, data));
        await refreshStatus();
      } catch (err) {
        setMessage(err.message, true);
      }
    });
  };
}

async function onResetData(scope) {
  const confirmed = window.confirm(
    scope === "all"
      ? "确认清空云端测试数据？这会归档 Notion 里的测试记录。"
      : "确认初始化本地数据？这会清空浏览器和本地文件里的测试记录。",
  );
  if (!confirmed) return;

  await withThrottle(`reset-${scope}`, async () => {
    try {
      const data = await apiPost("/api/reset", { scope });
      setMessage(syncOutcomeText(scope === "all" ? "已清空云端测试数据" : "已初始化本地数据", data));
      closeSheet("settingSheet", { force: true });
      await refreshStatus();
    } catch (err) {
      setMessage(err.message, true);
    }
  });
}

async function onSaveSetting() {
  const startDate = document.getElementById("goalStartDateInput").value;
  const endDate = document.getElementById("goalEndDateInput").value;
  const startWeight = Number(document.getElementById("goalStartWeightInput").value || 0);
  const targetWeight = Number(document.getElementById("goalTargetWeightInput").value || 0);
  const start = document.getElementById("windowStartInput").value;
  const hours = Number(document.getElementById("windowHoursInput").value || 8);

  if (!startDate || !endDate || !startWeight || !targetWeight) {
    setMessage("请填完整", true);
    return;
  }
  if (endDate < startDate) {
    setMessage("结束早于开始", true);
    return;
  }

  await withThrottle("saveSetting", async () => {
    try {
      const goalResult = await apiPost("/api/goal", {
        start_date: startDate,
        end_date: endDate,
        start_weight: startWeight,
        target_weight: targetWeight,
      });
      const windowResult = await apiPost("/api/window", { start, hours });
      const cloudSynced = goalResult.cloud_synced === false || windowResult.cloud_synced === false ? false : goalResult.cloud_synced ?? windowResult.cloud_synced;
      const cloudError = goalResult.cloud_error || windowResult.cloud_error;
      const syntheticResult = { cloud_synced: cloudSynced, cloud_error: cloudError };
      setMessage(syncOutcomeText("已保存设置", syntheticResult));
      closeSheet("settingSheet", { force: true });
      await refreshStatus();
    } catch (err) {
      setMessage(err.message, true);
    }
  });
}

function setupEvents() {
  document.getElementById("openAuthBtn").addEventListener("click", () => openSheet("authSheet"));
  document.getElementById("openSettingBtn").addEventListener("click", () => openSheet("settingSheet"));
  document.getElementById("quickMealBtn").addEventListener("click", () => openSheet("mealSheet"));
  document.getElementById("quickWeightBtn").addEventListener("click", () => openSheet("weightSheet"));
  document.getElementById("toggleCoachBtn").addEventListener("click", () => {
    state.coachExpanded = !state.coachExpanded;
    renderCoach(state.data);
  });
  document.getElementById("sleepShortBtn").addEventListener("click", onSleepPreset(5.5, "睡眠<6h"));
  document.getElementById("sleepMidBtn").addEventListener("click", onSleepPreset(6.5, "睡眠6-7h"));
  document.getElementById("sleepLongBtn").addEventListener("click", onSleepPreset(7.5, "睡眠>7h"));
  document.getElementById("exercise20Btn").addEventListener("click", onExercisePreset(20, "运动20分钟"));
  document.getElementById("exercise10Btn").addEventListener("click", onExercisePreset(10, "再加10分钟"));

  document.getElementById("saveMealBtn").addEventListener("click", onMealSubmit);
  document.getElementById("saveWeightBtn").addEventListener("click", onWeightSubmit);
  document.getElementById("saveSettingBtn").addEventListener("click", onSaveSetting);
  document.getElementById("resetLocalBtn").addEventListener("click", () => onResetData("local"));
  document.getElementById("resetAllBtn").addEventListener("click", () => onResetData("all"));
  document.getElementById("saveAuthBtn").addEventListener("click", () => {
    withThrottle("saveAuth", async () => {
      const token = document.getElementById("authTokenInput").value.trim();
      if (token) {
        localStorage.setItem("auth_token", token);
      } else {
        localStorage.removeItem("auth_token");
      }
      setMessage(token ? "密钥已保存" : "密钥为空", !token);
      closeSheet("authSheet", { force: true });
      if (token) await refreshStatus();
    });
  });

  document.querySelectorAll("[data-close]").forEach((btn) => {
    btn.addEventListener("click", () => closeSheet(btn.getAttribute("data-close")));
  });
  document.getElementById("sheetMask").addEventListener("click", closeAllSheets);
  document.getElementById("toggleRecentBtn").addEventListener("click", () => {
    state.feedShowAll = !state.feedShowAll;
    renderFeed(state.data);
  });
  document.getElementById("toggleFeedBtn").addEventListener("click", () => {
    state.feedExpanded = !state.feedExpanded;
    renderFeed(state.data);
  });
}

setupEvents();
refreshStatus();
setInterval(refreshStatus, 60 * 1000);
