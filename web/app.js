"use strict";

const $ = (id) => document.getElementById(id);
let state = null;
let pending = false;
let offline = false;
let initialized = false;
let slot = "06:00-07:30";
let slotSignature = "";
let serverOffset = 0;
let refreshSequence = 0;
let confirmPlanId = null;
let primaryAction = "check";
let secondaryAction = "check";
let autoOverviewDay = "";

function show(id, visible) { $(id).hidden = !visible; }
function text(id, value) { if ($(id).textContent !== String(value)) $(id).textContent = value; }
function currentTime() { return Date.now() + serverOffset; }
function selectedDay() { return $("use-day").value; }
function shanghaiDay(offset = 0) {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
  }).formatToParts(new Date(currentTime() + offset * 86400000));
  const value = Object.fromEntries(parts.map(part => [part.type, part.value]));
  return `${value.year}-${value.month}-${value.day}`;
}
function validDay(day) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day) || day.startsWith("0000")) return false;
  const value = new Date(`${day}T12:00:00+08:00`);
  return Number.isFinite(value.getTime()) && value.toISOString().slice(0, 10) === day;
}
function matchesTarget(target) { return Boolean(target && target.use_day === selectedDay() && target.slot === slot); }
function matchesPlan() {
  return matchesTarget(state?.plan);
}
function plannedRelease() {
  if (!validDay(selectedDay())) return null;
  const use = new Date(`${selectedDay()}T${slot.split("-")[1]}:00+08:00`);
  return new Date(use.getTime() - 86400000);
}
function formatDay(day, options = {}) {
  return new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", month: "long", day: "numeric", ...options })
    .format(new Date(`${day}T12:00:00+08:00`));
}
function formatMoment(moment) {
  return new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(moment);
}
function formatElapsed(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const minutes = Math.floor(total / 60);
  const rest = total % 60;
  return minutes ? `${minutes} 分 ${String(rest).padStart(2, "0")} 秒` : `${rest} 秒`;
}
function jobElapsed(job) {
  const started = Date.parse(job?.started_at || "");
  return Number.isFinite(started) ? (currentTime() - started) / 1000 : 0;
}
function jobDuration(job) {
  const started = Date.parse(job?.started_at || "");
  const finished = Date.parse(job?.finished_at || "");
  return Number.isFinite(started) && Number.isFinite(finished) ? Math.max(0, (finished - started) / 1000) : null;
}
function jobTiming(job) {
  if (job?.active) return `已运行 ${formatElapsed(jobElapsed(job))}`;
  const duration = jobDuration(job);
  return duration == null ? "等待更新" : `本次耗时 ${formatElapsed(duration)}`;
}
function error(message) {
  const id = $("confirm-dialog").open ? "confirm-error" : "page-alert";
  text(id, message);
  show(id, true);
}
function clearError() { show("page-alert", false); show("confirm-error", false); }

async function refresh() {
  const sequence = ++refreshSequence;
  try {
    const response = await fetch("/api/state", { cache: "no-store", signal: AbortSignal.timeout(6000) });
    if (!response.ok) throw new Error("state");
    const next = await response.json();
    if (sequence !== refreshSequence) return;
    const newJob = next.job?.id && next.job.id !== state?.job?.id;
    const connectionFinished = state?.job?.kind === "connecting" && !next.job?.active &&
      next.connected && next.overview?.use_day;
    if (state && state.csrf !== next.csrf) {
      $("confirm-dialog").close();
      error("本地服务已重启。登录和等待中的预约不会自动恢复，请重新连接并检查；已提交的订单请先核对。");
    } else if ($("confirm-dialog").open && confirmPlanId !== next.plan_id) {
      $("confirm-dialog").close();
      error("账号或场次检查已更新，请重新检查后确认预约。");
    } else if ($("confirm-dialog").open && newJob) {
      $("confirm-dialog").close();
    } else if (offline) {
      clearError();
    }
    offline = false;
    state = next;
    serverOffset = Date.parse(state.server_now) - Date.now();
    if (!initialized) {
      const target = state.job?.target || state.plan || state.overview;
      $("use-day").value = target?.use_day || state.default_date;
      slot = target?.slot || slot;
      initialized = true;
    }
    if (newJob && state.job.target) {
      $("use-day").value = state.job.target.use_day;
      if (state.job.target.slot) slot = state.job.target.slot;
    }
    if (connectionFinished) {
      $("use-day").value = state.overview.use_day;
      if (!state.slots.includes(slot)) slot = state.slots[0];
    }
    if (state.connected && !state.slots.includes(slot) && !matchesTarget(state.job.target)) slot = state.slots[0];
    render();
  } catch (_) {
    if (sequence !== refreshSequence) return;
    offline = true;
    error("暂时连不上本地服务。请检查启动窗口是否仍在运行；已设预约的状态不能由当前页面确定。");
    render();
  }
}

async function command(route, data = {}) {
  if (pending || !state || offline) return false;
  pending = true;
  clearError();
  render();
  try {
    const response = await fetch(`/api/${route}`, {
      method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": state.csrf },
      body: JSON.stringify({ ...data, revision: state.revision }), signal: AbortSignal.timeout(8000),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "操作未完成，请刷新状态后再试。");
    await refresh();
    return true;
  } catch (exc) {
    error(exc.name === "TimeoutError" || exc.name === "TypeError"
      ? "没有收到本地服务的确认，请先等待页面恢复状态，避免重复操作。" : exc.message);
    await refresh();
    return false;
  } finally {
    pending = false;
    render();
  }
}

async function refreshDefaultOverview() {
  if (!state?.connected || state.job?.active || pending || offline || !state.overview?.automatic) return;
  const day = state.default_date;
  if (!validDay(day) || state.overview.use_day === day || autoOverviewDay === day) return;
  autoOverviewDay = day;
  if (!(await command("overview", {use_day: day, automatic: true}))) autoOverviewDay = "";
}

function renderSlots(disabled) {
  const slots = state?.slots || [];
  const displayedSlots = slots.includes(slot) ? slots : [...slots, slot].sort();
  const signature = JSON.stringify([displayedSlots, slot, disabled]);
  if (signature === slotSignature) return;
  slotSignature = signature;
  $("slots").replaceChildren(...displayedSlots.map((value) => {
    const button = document.createElement("button");
    button.className = "slot-button";
    button.type = "button";
    button.textContent = value.replace("-", "–");
    button.setAttribute("aria-pressed", String(value === slot));
    button.setAttribute("aria-label", `使用时段 ${value.replace("-", " 至 ")}`);
    button.disabled = disabled || !slots.includes(value);
    button.addEventListener("click", () => { slot = value; clearError(); render(); });
    return button;
  }));
}

function renderOverview() {
  const panel = $("overview-panel");
  const grid = $("overview-grid");
  const day = selectedDay();
  const overview = state?.overview?.use_day === day ? state.overview : null;
  const job = state?.job || {};
  const busy = Boolean(job.active || pending);
  const activeScan = Boolean(job.active && (job.kind === "scanning" || job.phase === "scanning" ||
    (job.kind === "connecting" && job.phase === "scanning")) && job.target?.use_day === day);
  const visible = Boolean(state?.connected);
  show("overview-panel", visible);
  if (!visible) return;
  text("overview-title", validDay(day) ? `余票总览 · ${formatDay(day)}` : "余票总览");
  text("overview-updated", activeScan ? "正在查询各时段" : overview?.checked_at ?
    `检查于 ${formatMoment(new Date(overview.checked_at))}` : "等待查询该日期");
  text("overview-refresh", overview ? "刷新该日余票" : "查询该日全部余票");
  $("overview-refresh").disabled = busy || offline || !validDay(day);
  grid.replaceChildren();
  const appendCard = (node) => {
    if (typeof grid.append === "function") grid.append(node);
    else grid.replaceChildren(...grid.children, node);
  };
  if (activeScan && !overview) {
    const loading = document.createElement("div");
    loading.className = "overview-loading";
    loading.textContent = job.values?.slot ? `正在查询 ${job.values.slot}（${job.values.index || 1}/${job.values.total || "?"}）` : "正在读取全部时段…";
    appendCard(loading);
    text("overview-note", "这是只读查询，不会创建订单。完成后会一次显示全部时段。");
    return;
  }
  if (!overview) {
    const empty = document.createElement("div");
    empty.className = "overview-loading";
    empty.textContent = validDay(day) ? "该日期的余票尚未查询。" : "请先选择使用日期。";
    appendCard(empty);
    text("overview-note", "切换日期后会自动查询；也可以点击上方按钮重新查询全部时段。");
    return;
  }
  const rows = overview?.slots || [];
  rows.forEach((row) => {
    const card = document.createElement("button");
    card.type = "button";
    card.className = `overview-card ${row.status || "unknown"}`;
    card.disabled = busy || offline;
    card.setAttribute("aria-label", `${row.slot}，${row.remaining == null ? "余量未知" : `${row.remaining} 张可约`}`);
    const slotText = document.createElement("span");
    slotText.className = "overview-card-slot";
    slotText.textContent = row.slot.replace("-", "–");
    const value = document.createElement("strong");
    value.className = "overview-card-value";
    value.textContent = row.remaining == null ? "余量未知" : row.remaining > 0 ? `${row.remaining} 张可约` : "当前无票";
    if (typeof card.append === "function") card.append(slotText, value);
    else card.replaceChildren(slotText, value);
    card.addEventListener("click", () => {
      slot = row.slot;
      $("use-day").value = overview.use_day;
      clearError();
      render();
    });
    appendCard(card);
  });
  text("overview-note", "总览只读票种详情；需要预约时，点选目标时段后再检查这场预约。");
}

function jobCopy(job) {
  const values = job.values || {};
  const preflightSteps = {
    login: "正在登录并确认入口有效。",
    config: "正在读取场馆预约规则。",
    health_commitment: "正在检查场馆附加健康承诺。",
    catalog: "正在读取健身票和开放时段。",
    detail: "正在核对票价、日期模式和目标时段。",
    stock_short_circuit: "目标场次当前没有可用余票，已跳过较慢的须知和订单查询。",
    notice: "正在读取当前购票须知。",
    orders: "正在查询本人已有订单，避免重复预约。",
  };
  const steps = {
    connecting: ["正在连接官方预约平台", "检查本人入口，并读取可预约时段。"],
    scanning: ["正在读取各时段余票", values.slot ? `正在查询 ${values.slot}（第 ${values.index || 1}/${values.total || "?"} 个时段）。` : "登录已确认，正在读取全部健身时段。"],
    overview_ready: ["余票总览已更新", "已读取所选日期的全部健身时段。"],
    checking_plan: ["正在检查这场预约", preflightSteps[values.step] || "依次确认登录、票种、价格、须知及已有订单。"],
    scheduling: ["抢票任务运行中", "放票前准备，到点高频查询；发现有票后只提交一次。"],
    waiting: ["抢票任务已启动", "会在放票前准备，到点每约 0.5 秒查询一次，最多运行 30 秒。"],
    preparing: ["正在做放票前检查", "刷新登录，并检查是否已经有同场订单。"],
    checking: ["正在检查余票", values.remaining == null ? "官网还没有返回目标日期的明确余量。" : `最新余量 ${values.remaining} 张，限时检查中。`],
    reading_notice: ["按官网要求等待", `保留购票须知 ${values.seconds || 5} 秒倒计时，之后再次核对余量。`],
    submitting: ["正在提交订单", "订单请求只发送一次；没有及时响应时会继续查订单。"],
    confirming: ["正在核对出票结果", `第 ${values.check || 1} 轮核对。此时不会再次提交订单。`],
    reconciling: ["正在恢复订单核对", "仅查询本人订单，不会重新下单。"],
    stopping: ["正在停止", "等待当前查询结束，不会发送新的订单请求。"],
    warmup_unavailable: ["正在恢复连接", "预热暂未完成，将在查询窗口内重新检查。"],
    confirmed: ["已确认出票", "已查到这场未使用的订单。入场凭证请在官方预约平台查看。"],
    already_exists: ["这场已经有票了", "已查到本人同场未使用订单，没有重复预约。"],
    order_found_needs_review: ["查到了订单，请核对状态", "此订单未处于正常未使用状态，请在官方预约平台查看。"],
    cancelled: ["预约已停止", "没有发送新的订单请求，可以重新选择场次。"],
    window_ended: ["本次检查已结束", "在查询窗口内未完成预约，没有提交订单。"],
    query_unavailable: ["暂时无法确认余量", "查询窗口内未能取得可靠结果，没有提交订单。"],
    unknown: ["出票结果待确认", "请继续核对订单，或在官方预约平台查看，暂时不要重复下单。"],
    rejected_unconfirmed: ["提交未获确认", "请先核对官网订单，程序不会自动重发。"],
    error: ["这一步没有完成", job.message || "请重新检查；如已提交，请先核对订单。"],
    login_required: ["需要更新入口链接", job.message],
  };
  if (job.phase === "query_retry") return ["官网暂时响应较慢", `等待 ${values.delay_seconds} 秒后进行第 ${values.attempt} 次重试。`];
  return steps[job.phase] || ["", ""];
}

function render() {
  const job = state?.job || {};
  const busy = Boolean(job.active || pending);
  const connected = Boolean(state?.connected);
  const matched = matchesPlan();
  const plan = matched ? state.plan : null;
  const stale = Boolean(plan && currentTime() - Date.parse(state.checked_at) > 600000);
  const jobMatched = matchesTarget(job.target);
  const terminal = jobMatched && !job.active ? job.phase : null;
  const reviewingOrder = jobMatched && job.kind === "reconciling";
  const day = selectedDay();
  const release = plannedRelease();
  const recovered = (state?.recoveries || []).find(item => item.use_day === day && item.slot === slot);
  const firstRecovery = state?.recoveries?.[0];
  text("summary-title", reviewingOrder ? "本次订单核对" : "你的下一场");
  $("use-day").disabled = busy || offline;
  for (const [id, offset] of [["day-today", 0], ["day-tomorrow", 1]]) {
    const button = $(id);
    button.disabled = busy || offline;
    button.setAttribute("aria-pressed", String(day === shanghaiDay(offset)));
  }
  $("entry").disabled = busy || offline;
  $("connect-button").disabled = busy || offline;
  text("connect-button", job.active && job.kind === "connecting" ? "连接中" : "连接");
  $("change-entry").disabled = busy || offline;
  show("connection-form", !connected);
  show("connected-account", connected);
  const accountError = job.phase === "login_required" || job.result?.requires_login ||
    (job.phase === "error" && job.kind === "connecting" && !connected);
  show("account-error", accountError);
  text("account-error", accountError ? job.message : "");
  text("connection-status", connected ? "已连接" : job.active && job.kind === "connecting" ? "连接中" : "尚未连接");
  $("connection-status").classList.toggle("good", connected);
  if (connected) text("verified-at", `${new Intl.DateTimeFormat("zh-CN", {timeZone:"Asia/Shanghai",hour:"2-digit",minute:"2-digit",hourCycle:"h23"}).format(new Date(state.verified_at))} 检查可用`);
  renderSlots(busy || offline);
  renderOverview();
  if (validDay(day)) {
    text("selected-date", formatDay(day));
    text("selected-weekday", formatDay(day, { year: "numeric", month: undefined, day: undefined, weekday: "long" }));
  } else {
    text("selected-date", "请选择日期");
    text("selected-weekday", "");
  }
  const times = slot.split("-");
  $("selected-slot").replaceChildren(document.createTextNode(times[0]), Object.assign(document.createElement("span"), {textContent:"—"}), document.createTextNode(times[1]));
  const passed = release && release.getTime() <= currentTime();
  text("release-label", reviewingOrder ? "原预计放票" : "预计放票");
  text("release-time", release ? formatMoment(release) : "—");
  text("release-note", reviewingOrder ? "此处展示原场次时间。核对订单不会再次下单。" :
    passed ? "预计放票时间已过，确认后将立即检查当前余票。" : "按场馆规则，通常在前一天该时段结束时开放。");

  let stockText = "待检查", stockClass = "";
  if (plan) {
    const row = plan.current_rows.find(item => item.date === day && item.slot === slot);
    if (row?.remaining != null) {
      stockText = row.remaining > 0 ? `${row.remaining} 张可约` : "当前无票";
      stockClass = row.remaining > 0 ? "available" : "";
    } else stockText = "该日余量未返回";
    if (plan.existing_order) { stockText = "已有订单"; stockClass = "available"; }
  } else {
    const overviewRow = state?.overview?.use_day === day ? state.overview.slots.find(item => item.slot === slot) : null;
    if (overviewRow) {
      stockText = overviewRow.remaining == null ? "余量未知" : overviewRow.remaining > 0 ? `${overviewRow.remaining} 张可约` : "当前无票";
      stockClass = overviewRow.status === "available" ? "available" : overviewRow.status === "unknown" ? "attention" : "";
    }
  }
  if (job.active && jobMatched) { stockText = job.kind === "scheduling" ? "预约中" : "检查中"; stockClass = "waiting"; }
  if (["confirmed", "already_exists"].includes(terminal)) { stockText = "已确认"; stockClass = "available"; }
  if (recovered || ["unknown", "rejected_unconfirmed"].includes(terminal)) { stockText = "待核对"; stockClass = "attention"; }
  text("stock-status", stockText);
  $("stock-status").className = `status-pill ${stockClass}`;

  const showProgress = Boolean(job.active || (jobMatched && !["idle", "connected", "ready"].includes(job.phase)) || (!job.target && ["error", "login_required"].includes(job.phase)));
  const [title, copy] = jobCopy(job);
  show("progress-panel", showProgress && Boolean(title));
  text("progress-title", title);
  text("progress-copy", job.message || copy);
  text("progress-elapsed", job.active ? jobTiming(job) : jobDuration(job) == null ? "" : jobTiming(job));
  $("progress-dot").classList.toggle("finished", !job.active);
  show("recovery-banner", Boolean(firstRecovery));
  if (firstRecovery) text("recovery-copy", `${firstRecovery.use_day} · ${firstRecovery.slot.replace("-", "–")} · 先确认是否已出票`);
  $("recovery-button").disabled = busy || offline || !connected;

  primaryAction = "check";
  secondaryAction = "check";
  let label = "检查余票";
  let hint = connected ? "先检查这场预约，再确认是否开始抢票。" : "连接账号后，即可检查并开始抢票。";
  let disabled = !connected || !validDay(day) || offline || busy;
  show("secondary-button", false);
  $("primary-button").classList.remove("done");
  $("secondary-button").classList.remove("stop");
  if (busy) {
    label = job.kind === "scheduling" ? "预约进行中" : "正在检查";
    if (job.can_cancel && !pending) {
      secondaryAction = "cancel";
      text("secondary-button", job.kind === "scheduling" ? "停止预约" : "停止检查");
      $("secondary-button").classList.add("stop");
      show("secondary-button", true);
    }
    hint = pending && !job.active ? "正在确认本地任务状态，请稍候。" :
      job.can_cancel ? "关闭网页后继续运行，可随时回来查看。" : "正在提交或核对，请等待结果。";
  } else if (recovered || ["unknown", "rejected_unconfirmed"].includes(terminal)) {
    label = "继续核对订单";
    primaryAction = "reconcile";
    hint = "此操作只核对订单，不会再次下单。";
  } else if (["confirmed", "already_exists"].includes(terminal) || plan?.existing_order?.status === "0") {
    label = "已确认这场预约";
    disabled = true;
    hint = "入场凭证及订单详情请在官方预约平台查看。";
    $("primary-button").classList.add("done");
  } else if (plan?.existing_order || terminal === "order_found_needs_review") {
    label = "请在官方预约平台核对";
    disabled = true;
    hint = "已有同场订单，当前不会重复预约。";
  } else if (plan?.fast_only) {
    label = stockText === "当前无票" ? "当前无票" : "余量未知";
    disabled = true;
    hint = stockText === "当前无票" ? "快速查询已完成；有票后再检查即可进入预约。" : "官网没有返回明确余量，程序不会据此预约。";
  } else if (stale) {
    label = "重新检查余票";
    hint = "上次检查已超过 10 分钟，更新后即可设定预约。";
  } else if (plan && !["error", "window_ended", "query_unavailable", "login_required"].includes(terminal)) {
    label = passed ? "立即开始抢票" : "开始抢票";
    primaryAction = "schedule";
    hint = passed ? "确认后立即高频查询 30 秒，有票时按官网规则只提交一次。" : "现在启动；放票前准备，到点每约 0.5 秒查询一次，最多 30 秒。";
    text("secondary-button", "重新检查");
    show("secondary-button", true);
  }
  text("primary-button", label);
  $("primary-button").disabled = disabled;
  $("secondary-button").disabled = pending || offline;
  text("action-hint", offline ? "请先恢复与本地服务的连接。" : hint);
  $("confirm-submit").disabled = !$("accept-notice").checked || pending || busy || offline ||
    !connected || !matched || stale || confirmPlanId !== state?.plan_id;
  if ($("confirm-dialog").open && stale) error("上次检查已超过 10 分钟，请返回并重新检查余票。");
  $("confirm-back").disabled = pending;
  renderResultBanner({job, jobMatched, plan, overview: state?.overview?.use_day === day ? state.overview : null,
    recovered, terminal, stockText, stockClass, connected, day, slot});
  updateCountdown();
}

function renderResultBanner({job, jobMatched, plan, overview, recovered, terminal, stockText, stockClass, connected, day, slot}) {
  const visible = connected && Boolean(job.active || plan || overview || recovered || terminal || ["error", "login_required"].includes(job.phase));
  show("result-banner", visible);
  if (!visible) return;
  let title = "等待检查";
  let detail = "选择日期和时段后，点击“检查余票”。";
  let tone = "neutral";
  if (job.active && (jobMatched || job.phase === "scanning" || job.kind === "connecting")) {
    title = job.kind === "scheduling" ? "预约任务运行中" : job.phase === "scanning" ? "正在读取各时段余票" : "正在查询这场预约";
    detail = job.message || jobCopy(job)[1];
    tone = "waiting";
  } else if (terminal === "confirmed" || terminal === "already_exists") {
    title = "已确认出票";
    detail = "已查到本人这场未使用订单，请在官方预约平台查看凭证。";
    tone = "available";
  } else if (recovered || terminal === "unknown" || terminal === "rejected_unconfirmed") {
    title = "结果需要核对";
    detail = "程序不会重复提交，请先在官方预约平台查看订单。";
    tone = "attention";
  } else if (plan) {
    title = `检查完成 · ${stockText}`;
    detail = stockText === "当前无票" ? "本次检查没有查到目标日期的可用票。" :
      stockText === "该日余量未返回" ? "官网没有返回目标日期的明确余量，不能据此判断售罄。" :
      plan.existing_order ? "已查到同场订单，不会重复预约。" : "已核对票价、须知和本人订单，可以继续设定预约。";
    tone = stockClass || "neutral";
  } else if (overview) {
    title = `余票总览已更新 · ${formatDay(overview.use_day)}`;
    detail = "已读取全部健身时段；点选目标时段后可继续检查预约条件。";
    tone = "neutral";
  } else if (["error", "login_required"].includes(job.phase)) {
    title = job.phase === "login_required" ? "入口需要更新" : "检查没有完成";
    detail = job.message || "请查看下方提示后重试。";
    tone = "attention";
  }
  text("result-title", title);
  text("result-detail", detail);
  text("result-time", job.active ? jobTiming(job) :
    jobDuration(job) != null ? jobTiming(job) :
    state?.checked_at ? `更新于 ${formatMoment(new Date(state.checked_at))}` : "等待更新");
  $("result-banner").className = `result-banner ${tone}`;
  text("stock-status", stockText);
}

function updateCountdown() {
  const job = state?.job;
  const release = matchesTarget(job?.target) && matchesPlan() ? state?.plan?.release_at : null;
  const seconds = release ? Math.ceil((Date.parse(release) - currentTime()) / 1000) : 0;
  const visible = job?.active && job.kind === "scheduling" && seconds > 0;
  show("countdown", Boolean(visible));
  if (visible) {
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor(seconds % 3600 / 60);
    const rest = seconds % 60;
    text("countdown-value", [hours, minutes, rest].map(value => String(value).padStart(2, "0")).join(" : "));
  }
}

function openConfirmation() {
  if (!matchesPlan() || state.plan?.fast_only || state.job.active || currentTime() - Date.parse(state.checked_at) > 600000) return;
  const plan = state.plan;
  confirmPlanId = state.plan_id;
  const release = new Date(plan.release_at);
  const passed = release.getTime() <= currentTime();
  text("confirm-title", passed ? "立即开始抢票" : "开始抢票");
  text("confirm-session", `${formatDay(plan.use_day)} · ${plan.slot.replace("-", "–")}`);
  text("confirm-release-label", passed ? "执行时间" : "预计放票");
  text("confirm-release", passed ? "确认后立即开始" : formatMoment(release));
  text("notice-text", plan.notice.text || "官网当前没有提供额外购票须知。");
  $("accept-notice").checked = false;
  show("confirm-error", false);
  $("confirm-submit").disabled = true;
  $("confirm-dialog").showModal();
}

$("connection-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const entry = $("entry").value.trim();
  if (!entry) return;
  if (await command("connect", { entry })) $("entry").value = "";
});
$("change-entry").addEventListener("click", async () => {
  if (await command("disconnect")) $("entry").focus();
});
async function selectOverviewDay(day) {
  $("use-day").value = day;
  clearError();
  render();
  if (validDay(day) && state?.connected && !state.job?.active && !pending && !offline &&
      state.overview?.use_day !== day) {
    await command("overview", { use_day: day });
  }
}
$("use-day").addEventListener("change", () => selectOverviewDay(selectedDay()));
$("day-today").addEventListener("click", () => selectOverviewDay(shanghaiDay(0)));
$("day-tomorrow").addEventListener("click", () => selectOverviewDay(shanghaiDay(1)));
$("overview-refresh").addEventListener("click", () => command("overview", { use_day: selectedDay() }));
$("primary-button").addEventListener("click", () => {
  if (primaryAction === "schedule") openConfirmation();
  else command(primaryAction, { use_day: selectedDay(), slot });
});
$("secondary-button").addEventListener("click", () => command(secondaryAction, { use_day: selectedDay(), slot, job_id: state?.job.id }));
$("recovery-button").addEventListener("click", () => {
  const item = state?.recoveries?.[0];
  if (item) command("reconcile", item);
});
$("accept-notice").addEventListener("change", () => render());
$("confirm-back").addEventListener("click", () => $("confirm-dialog").close());
$("confirm-submit").addEventListener("click", async () => {
  render();
  if ($("confirm-submit").disabled) return;
  const accepted = await command("schedule", {plan_id: confirmPlanId, notice_hash: state.plan?.notice.hash, accept: true});
  if (accepted) $("confirm-dialog").close();
});
$("help-button").addEventListener("click", () => $("help-dialog").showModal());
$("help-close").addEventListener("click", () => $("help-dialog").close());
$("confirm-dialog").addEventListener("cancel", event => { if (pending) event.preventDefault(); });
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
setInterval(() => {
  updateCountdown();
  if (state?.job?.active) {
    text("progress-elapsed", jobTiming(state.job));
    text("result-time", jobTiming(state.job));
  }
}, 1000);
async function poll() {
  await refresh();
  await refreshDefaultOverview();
  setTimeout(poll, state?.job.active || offline ? 1000 : 4000);
}
poll();
