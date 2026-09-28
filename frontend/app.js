/* 老人端 / 子女端 / 攻防演示台 —— 单页应用，无构建步骤 */

const $ = (id) => document.getElementById(id);
const state = { session: null, plan: null, popupOpenedAt: 0, dialect: "mandarin", running: false };

async function api(path, options) {
  // 账户余额这类状态随时在变，禁掉浏览器缓存，保证刷新一定拿到最新值
  const res = await fetch(path, { cache: "no-store", ...options });
  const text = await res.text();
  let data;
  try { data = text ? JSON.parse(text) : {}; } catch { data = { raw: text }; }
  if (!res.ok) throw new Error(data.detail ? JSON.stringify(data.detail) : `HTTP ${res.status}`);
  return data;
}
const post = (path, body) => api(path, {
  method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body || {}),
});

function toast(msg) {
  const el = $("toast");
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.hidden = true; }, 2600);
}

/* ---------------- 视图切换 ---------------- */

$("tabs").addEventListener("click", (e) => {
  const tab = e.target.closest(".tab");
  if (!tab) return;
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === tab));
  document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
  $(`view-${tab.dataset.view}`).classList.add("active");
  if (tab.dataset.view === "child") loadChild();
  if (tab.dataset.view === "demo") loadScripts();
});

$("dialect").addEventListener("change", (e) => {
  state.dialect = e.target.value;
  loadChips();
});

$("resetBtn").addEventListener("click", async () => {
  await post("/system/reset");
  $("responseCard").hidden = true;
  loadStatus(); loadSide(); loadChild();
  toast("演示数据已重置");
});

/* ---------------- 语音 ---------------- */

let recognizer = null;
function buildRecognizer() {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) return null;
  const r = new SR();
  r.lang = state.dialect === "cantonese" ? "zh-HK" : "zh-CN";
  r.interimResults = false;
  r.maxAlternatives = 1;
  r.onresult = (ev) => {
    const text = ev.results[0][0].transcript.trim();
    $("textInput").value = text;
    $("micHint").textContent = `已识别（${state.dialect === "cantonese" ? "粤语" : "普通话"}）：${text}`;
    send(text);
  };
  r.onerror = (ev) => {
    $("micHint").textContent = `浏览器语音识别不可用（${ev.error}），请直接打字或点常用说法。`;
    setMic(false);
  };
  r.onend = () => setMic(false);
  return r;
}

function setMic(on) {
  $("micBtn").classList.toggle("listening", on);
  $("micLabel").textContent = on ? "正在听…" : "点一下说话";
}

$("micBtn").addEventListener("click", () => {
  if (!recognizer) recognizer = buildRecognizer();
  if (!recognizer) {
    $("micHint").textContent = "当前浏览器不支持语音识别，请用 Edge/Chrome，或直接打字。";
    return;
  }
  recognizer.lang = state.dialect === "cantonese" ? "zh-HK" : "zh-CN";
  try { recognizer.start(); setMic(true); toast("请说话…"); }
  catch { setMic(false); }
});

async function speak(text) {
  if (!text) return;
  try {
    const r = await post("/tts", { text, dialect: state.dialect === "cantonese" ? "cantonese" : "mandarin", rate: 0.8 });
    if (r.audio_b64) {
      await new Audio(`data:audio/${r.audio_format || "mp3"};base64,${r.audio_b64}`).play();
      return;
    }
  } catch (e) { /* 云端 TTS 不可用时走浏览器合成 */ }
  if (!window.speechSynthesis) { toast("当前浏览器不支持语音播报"); return; }
  window.speechSynthesis.cancel();
  const u = new SpeechSynthesisUtterance(text);
  u.lang = state.dialect === "cantonese" ? "zh-HK" : "zh-CN";
  u.rate = 0.8;
  window.speechSynthesis.speak(u);
}

$("speakBtn").addEventListener("click", () => speak($("responseText").textContent));
$("sendBtn").addEventListener("click", () => send($("textInput").value));
$("textInput").addEventListener("keydown", (e) => { if (e.key === "Enter") send($("textInput").value); });

async function loadChips() {
  const data = await api("/asr/fallback-lines");
  const lines = data.lines.filter((l) => l.dialect === state.dialect);
  $("chips").innerHTML = "";
  lines.forEach((l) => {
    const b = document.createElement("button");
    b.className = "ghost";
    b.textContent = l.text;
    b.onclick = () => { $("textInput").value = l.text; send(l.text); };
    $("chips").appendChild(b);
  });
}

/* ---------------- 老人端主流程 ---------------- */

async function send(text) {
  if (!text || !text.trim() || state.running) return;
  state.running = true;
  try {
    const payload = await post("/agent/plan", {
      text: text.trim(),
      dialect: state.dialect === "cantonese" ? "cantonese" : "auto",
      // 带上上一轮会话：如果上一轮停在追问，这一句会被当作缺失信息的回答
      session_id: state.session || null,
    });
    state.plan = payload;
    state.session = payload.session_id;
    renderResponse(payload);
    speak(payload.elder_text);
    loadSide();
    loadStatus();
  } catch (err) {
    toast("请求失败：" + err.message);
  } finally {
    state.running = false;
  }
}

function renderResponse(p) {
  const card = $("responseCard");
  card.hidden = false;
  const level = p.risk ? p.risk.level : null;
  const badge = $("riskBadge");
  badge.textContent = level ? `权限等级 ${level}` : "待补充信息";
  badge.className = "badge" + (level ? " " + level : "");
  $("responseText").textContent = p.elder_text;

  const actions = $("responseActions");
  actions.innerHTML = "";
  const options = $("optionsBox");
  options.innerHTML = "";
  options.hidden = true;

  if (p.status === "clarify") {
    if (p.options && p.options.length) {
      options.hidden = false;
      p.options.forEach((o) => {
        const b = document.createElement("button");
        b.className = "option-btn";
        b.innerHTML = `<b>${o.name}</b><br><span class="muted">${o.note}</span>`;
        // 只说名字即可，后端会把它并进上一轮的转账意图
        b.onclick = () => send(o.name);
        options.appendChild(b);
      });
    }
    return;
  }
  if (p.status === "blocked") {
    addBtn(actions, "查看防诈详情", "ghost", () => switchTo("demo"));
    addBtn(actions, "紧急止付", "primary", () => send("救命，我可能被骗了"));
    return;
  }
  if (p.status === "pending_auth") { renderWaiting(p); return; }
  if (p.status === "executed") {
    addBtn(actions, "再念一遍结果", "ghost", () => speak(p.result.steps[0].summary));
    return;
  }
  if (p.status === "pending_confirm") {
    const step = p.plan.steps[0] || {};
    const level = p.risk.level;
    if (level === "L2") {
      const b = addBtn(actions, "确认并请子女授权", "primary", () => doConfirm(true, true));
      b.disabled = false;
    } else if (level === "L1") {
      addBtn(actions, "确认（需二次弹窗）", "primary", () => openPopup(step));
    } else {
      addBtn(actions, "确认执行", "primary", () => doConfirm(true, false));
    }
    addBtn(actions, "算了，先不办", "ghost", () => { $("responseCard").hidden = true; });
  }
}

function addBtn(host, label, cls, fn) {
  const b = document.createElement("button");
  b.className = cls;
  b.textContent = label;
  b.onclick = fn;
  host.appendChild(b);
  return b;
}

function switchTo(view) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === view));
  document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
  $(`view-${view}`).classList.add("active");
  history.replaceState(null, "", "#" + view);
  if (view === "demo") loadScripts();
  if (view === "child") loadChild();
}

/* 支持 #demo / #child 深链，便于演示时并行开三个窗口各占一个视图 */
function applyHash() {
  const view = (location.hash || "").replace("#", "");
  if (["elder", "child", "demo"].includes(view)) switchTo(view);
}

/* 二次确认弹窗：确认按钮延迟 1 秒激活，防误触。
   转账 / 理财这类动钱的操作突出金额与收款人；挂失、守护日历这类非动钱操作改为突出事由。 */
function openPopup(step) {
  const amount = step.params.amount || 0;
  const moneyOp = ["transfer.execute", "transfer.schedule",
    "wealth.purchase", "wealth.redeem"].includes(step.tool);
  $("cfPayeeLine").hidden = !moneyOp;
  $("cfPayee").textContent = step.params.payee || "—";
  const amountEl = $("cfAmount");
  if (moneyOp) {
    amountEl.className = "modal-amount";
    amountEl.textContent = `¥ ${amount.toLocaleString("zh-CN", { minimumFractionDigits: 2 })}`;
  } else {
    amountEl.className = "modal-amount text";
    amountEl.textContent = step.summary || "请确认本次操作";
  }
  $("cfOk").textContent = moneyOp ? "确认转账" : "确认办理";
  const risk = state.plan.risk;
  $("cfNote").textContent = (risk.reasons || []).join("；");
  $("confirmMask").hidden = false;
  state.popupOpenedAt = Date.now();
  const ok = $("cfOk");
  ok.disabled = true;
  let n = 1;
  $("cfCountdown").textContent = moneyOp
    ? "请仔细核对收款人与金额，1 秒后按钮才可点击"
    : "请仔细核对，1 秒后按钮才可点击";
  const timer = setInterval(() => {
    n -= 1;
    if (n <= 0) {
      clearInterval(timer);
      ok.disabled = false;
      $("cfCountdown").textContent = moneyOp ? "请确认收款人与金额无误" : "请确认无误后点击下方按钮";
    }
  }, 1000);
}

$("cfOk").addEventListener("click", () => {
  $("confirmMask").hidden = true;
  doConfirm(true, true);
});

/* "我有点拿不准" 走犹豫升权：连续两次，系统自动提升权限等级 */
$("cfCancel").addEventListener("click", () => {
  $("confirmMask").hidden = true;
  doConfirm(true, true, 5000);
});

async function doConfirm(voice, popup, hesitationMs) {
  try {
    const payload = await post("/agent/confirm", {
      session_id: state.session,
      ack_voice: !!voice,
      ack_popup: !!popup,
      hesitation_ms: hesitationMs || 0,
    });
    if (payload.error) { toast(payload.error); return; }
    state.plan = payload;
    renderResponse(payload);
    speak(payload.elder_text);
    loadSide();
    loadStatus();
  } catch (err) {
    toast("确认失败：" + err.message);
  }
}

function renderWaiting(p) {
  const actions = $("responseActions");
  actions.innerHTML = "";
  const wrap = document.createElement("div");
  wrap.className = "muted";
  wrap.innerHTML = `授权请求已发送给 <b>张伟</b>，票据将在 5 分钟后自动作废。`;
  actions.appendChild(wrap);
  addBtn(actions, "让子女打开授权页", "ghost", () => switchTo("child"));
  const poll = setInterval(async () => {
    const t = await api(`/auth/ticket/${p.ticket_id}`);
    if (t.status === "approved") {
      clearInterval(poll);
      const done = await post("/agent/execute", { session_id: state.session });
      state.plan = done;
      renderResponse(done);
      speak(done.elder_text || done.result.steps[0].summary);
      loadStatus();
      loadSide();
      toast("子女已同意，转账完成");
    } else if (t.status === "rejected" || t.status === "expired") {
      clearInterval(poll);
      toast(t.status === "rejected" ? "子女拒绝了这笔转账" : "授权超时，票据已作废");
      $("responseCard").hidden = true;
    }
  }, 1200);
}

/* ---------------- 侧栏与状态 ---------------- */

async function loadStatus() {
  try {
    const s = await api("/system/status");
    const acc = s.elder;
    $("accountStrip").innerHTML = `
      <div class="kv"><span>户主</span><b>${acc.owner}</b></div>
      <div class="kv"><span>活期可用余额</span><b>¥ ${acc.balance.toLocaleString("zh-CN", { minimumFractionDigits: 2 })}</b></div>
      <div class="kv"><span>专项锁定</span><b>¥ ${acc.locked.toLocaleString("zh-CN", { minimumFractionDigits: 2 })}</b></div>
      <div class="kv"><span>免密额度</span><b>¥ ${s.limits.free_limit}</b></div>
      <div class="kv"><span>子女授权门槛</span><b>¥ ${s.auth_threshold}</b></div>`;
    // 这个胶囊说明的是「意图理解走哪条路」，不是「系统是不是 AI」——
    // 未配置模型时走的是同样在架构里的本地规则解析器，且离线也能完整演示。
    const pill = $("statusPill");
    pill.textContent = s.llm_live
      ? `意图理解：${s.llm_provider}`
      : "意图理解：本地规则解析 · 离线可跑";
    pill.className = "pill " + (s.llm_live ? "ok" : "warn");
    pill.title = s.llm_live
      ? "大模型已接入：意图理解走通义千问，其输出仍需通过与原话的一致性校验"
      : "未配置 DASHSCOPE_API_KEY，意图理解走本地规则解析器。配置后自动切换到通义千问，"
        + "详见 README「AI 能力与模型接入」一节。";
    $("freeLimit").value = s.limits.free_limit;
    $("limitsNote").textContent =
      `当前免密 ¥${s.limits.free_limit} · 单笔上限 ¥${s.limits.single_limit} · 单日上限 ¥${s.limits.daily_limit}`;
  } catch (e) { /* 忽略 */ }
}

async function loadSide() {
  try {
    const bill = await api("/mock-bank/bill-report");
    const cat = Object.entries(bill.by_category).sort((a, b) => b[1] - a[1]).slice(0, 4)
      .map(([k, v]) => `${k} ¥${v.toFixed(2)}`).join(" · ");
    $("billReport").innerHTML =
      `<div>本月支出 <b>¥${bill.total_out.toFixed(2)}</b>，收入 <b>¥${bill.income.toFixed(2)}</b></div>
       <div class="muted">${cat}</div>
       <div class="muted">可疑交易 ${bill.abnormal.length} 笔</div>`;
  } catch (e) { /* 忽略 */ }

  try {
    const subs = await api("/mock-bank/subscriptions");
    $("subsBox").innerHTML =
      `<div>每月自动扣费合计 <b>¥${subs.monthly_total.toFixed(2)}</b></div>
       <div class="muted">可疑订阅 ${subs.suspicious_count} 项</div>
       <button class="ghost small" onclick="window.__cancelSub()">一键取消诱导订阅</button>
       <div class="muted" id="subRemark"></div>`;
  } catch (e) { /* 忽略 */ }

  try {
    const g = await api("/guard/events");
    const sch = await api("/mock-bank/schedules");
    const list = (g.due.length ? g.due : g.events).slice(0, 4);
    const rows = list.map((e) =>
      `<div>${e.date} · <b>${e.title}</b>${e.days_left !== undefined ? ` · 还剩 ${e.days_left} 天` : ""}</div>`
    ).join("");
    const fixed = sch.schedules.map((s) =>
      `<div class="muted">每月 ${s.day} 号 · 定期给 ${s.payee} 转 ¥${s.amount.toFixed(2)}</div>`
    ).join("");
    $("guardBox").innerHTML = rows + fixed;
  } catch (e) { /* 忽略 */ }

  try {
    const a = await api("/audit?limit=5");
    const rows = a.entries.slice().reverse().map((e) =>
      `<li>${e.stage}｜${e.actor}｜${(e.note || "").slice(0, 42)}</li>`).join("");
    $("auditMini").innerHTML = rows || `<li class="muted">还没有操作，说一句试试</li>`;
  } catch (e) { /* 忽略 */ }
}

window.__cancelSub = async () => {
  const subs = await api("/mock-bank/subscriptions");
  const bad = subs.items.find((s) => s.suspicious && s.status === "active");
  if (!bad) { toast("没有发现可疑订阅"); return; }
  await post("/mock-bank/subscriptions/cancel", { subscription_id: bad.id });
  toast(`已取消「${bad.merchant}」`);
  loadSide();
  loadStatus();
};

/* ---------------- 子女端 ---------------- */

async function loadChild() {
  const data = await api("/auth/pending");
  const box = $("ticketsBox");
  const pending = data.tickets.filter((t) => t.status === "pending");
  $("pendingCount").textContent = pending.length;
  box.innerHTML = pending.length ? "" : `<p class="muted">目前没有待处理的授权请求。</p>`;
  pending.forEach((t) => {
    const el = document.createElement("div");
    el.className = "ticket";
    el.innerHTML = `
      <div class="ticket-amount">¥ ${t.amount.toLocaleString("zh-CN", { minimumFractionDigits: 2 })}</div>
      <div>收款人：<b>${t.payee}</b>　<i class="muted">${t.payee_account}</i></div>
      <div class="ticket-meta">触发原因：${t.reason}</div>
      <div class="ticket-meta">票据 ${t.id} · 5 分钟内有效 · 一次有效，用后即废</div>
      <div class="ticket-actions">
        <label>可下调金额</label><input type="number" value="${t.amount}" max="${t.amount}" step="0.01">
        <button class="primary" data-act="ok">同意</button>
        <button class="ghost" data-act="no">拒绝</button>
      </div>`;
    el.querySelector('[data-act=ok]').onclick = async () => {
      const amt = parseFloat(el.querySelector("input").value);
      try {
        await post("/auth/approve", { ticket_id: t.id, approver: "张伟", amount: amt,
                                      ttl_seconds: 300, approve: true });
        toast(`已授权 ¥${amt}`);
        loadChild();
      } catch (err) { toast("授权失败：" + err.message); }
    };
    el.querySelector('[data-act=no]').onclick = async () => {
      try {
        await post("/auth/approve", { ticket_id: t.id, approver: "张伟", approve: false });
        toast("已拒绝该笔转账");
        loadChild();
      } catch (err) { toast("操作失败：" + err.message); }
    };
    box.appendChild(el);
  });

  const notes = $("notifyBox");
  notes.innerHTML = data.notifications.slice().reverse().slice(0, 8)
    .map((n) => {
      const { title, detail } = notifyLine(n);
      const time = n.ts ? String(n.ts).slice(11, 16) : "";
      return `<li><span class="muted">${time}</span> <b>${title}</b>${detail ? ` · ${detail}` : ""}</li>`;
    })
    .join("") || `<li class="muted">暂无通知</li>`;
}

const NOTIFY_LABEL = {
  auth_request: "收到一笔待授权转账",
  emergency_stop: "老人触发紧急止付",
  blocked: "一笔操作被风控拦下",
  wealth_purchase: "老人申购了理财产品",
  wealth_redeem: "老人赎回了理财",
  card_loss: "老人挂失了银行卡",
  card_apply: "老人提交了办卡申请",
  guard_run: "守护日历动作链已执行",
  transfer_schedule: "老人设置了定期转账",
};

function notifyLine(n) {
  const title = n.title || NOTIFY_LABEL[n.type] || n.type || "通知";
  let detail = n.detail || "";
  if (!detail) {
    if (n.ticket) detail = `¥${n.ticket.amount} → ${n.ticket.payee}`;
    else if (n.reason) detail = String(n.reason).slice(0, 60);
  }
  return { title, detail };
}

$("saveLimits").addEventListener("click", async () => {
  const v = parseFloat($("freeLimit").value);
  try {
    const r = await post(`/mock-bank/limits?free_limit=${encodeURIComponent(v)}`);
    toast("已收紧：" + (r.changes.join("；") || "无变化"));
    loadStatus();
  } catch (err) { toast("提交失败：" + err.message); }
});

$("deployGuard").addEventListener("click", async () => {
  const event = $("guardEvent").value.trim();
  const date = $("guardDate").value;
  if (!event || !date) { toast("请填写事件名称与日期"); return; }
  try {
    const r = await post("/guard/deploy", {
      event, date,
      lock_amount: parseFloat($("guardLock").value) || 0,
      advance_days: parseInt($("guardAdvance").value, 10) || 0,
      actions: ["锁定活期", "提前订购鲜花", "提前订购蛋糕"],
    });
    $("deployBox").textContent = `已预置「${event}」，到期前 ${r.event.advance_days} 天执行动作链。`;
    toast("守护日历已预置");
    loadSide();
  } catch (err) { toast("预置失败：" + err.message); }
});

document.querySelectorAll("[data-proxy]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const target = btn.dataset.proxy;
    const isWealth = /国债|安心存/.test(target);
    const amount = isWealth ? 5000 : 168;
    try {
      const r = await post(`/mock-bank/proxy?kind=${isWealth ? "wealth" : "bill"}` +
        `&target=${encodeURIComponent(target)}&amount=${amount}`);
      $("proxyBox").textContent = r.summary;
      toast(r.summary);
      loadStatus(); loadSide();
    } catch (err) { toast("办理失败：" + err.message); }
  });
});

/* ---------------- 攻防演示台 ---------------- */

let scriptsLoaded = false;
async function loadScripts() {
  if (scriptsLoaded) return;
  const data = await api("/demo/scripts");
  const box = $("scriptChips");
  box.innerHTML = "";
  data.scripts.forEach((s) => {
    const b = document.createElement("button");
    b.className = "ghost";
    b.textContent = `${s.id} ${s.name}`;
    b.dataset.scriptId = s.id;
    b.setAttribute("aria-pressed", "false");
    b.title = s.expected;
    b.onclick = () => runScript(s.id);
    box.appendChild(b);
  });
  scriptsLoaded = true;
  if (data.scripts.length) runScript(data.scripts[0].id);
}

let runningScript = null;
async function runScript(id) {
  if (runningScript === id) return;          // 同一剧本不重复触发
  runningScript = id;
  const box = $("scriptChips");
  const buttons = [...box.querySelectorAll("button")];
  buttons.forEach((b) => {
    const on = b.dataset.scriptId === id;
    b.classList.toggle("active", on);
    b.setAttribute("aria-pressed", String(on));
  });
  const current = buttons.find((b) => b.dataset.scriptId === id);
  current?.classList.add("loading");
  try {
    const r = await post("/demo/attack", { script_id: id });
    renderTrace($("traceVuln"), r.vulnerable.trace);
    renderTrace($("traceDefend"), r.defended.trace);
    $("vulnLoss").textContent = r.vulnerable.lost > 0
      ? `损失 ¥${r.vulnerable.lost.toLocaleString("zh-CN")}` : "无损失";
    $("vulnLoss").className = "pill " + (r.vulnerable.lost > 0 ? "danger" : "ok");
    $("defendLoss").textContent = r.defended.lost > 0
      ? `损失 ¥${r.defended.lost.toLocaleString("zh-CN")}` : "资金零损失";
    $("defendLoss").className = "pill " + (r.defended.lost > 0 ? "danger" : "ok");
    $("verdictBox").hidden = false;
    $("verdictText").textContent = `${r.verdict}　（${r.script.name}）`;
    $("verdictExpect").textContent = "防御预期：" + r.script.expected;
  } catch (err) {
    toast("运行失败：" + err.message);
  } finally {
    current?.classList.remove("loading");
    runningScript = null;
  }
}

function renderTrace(host, trace) {
  host.innerHTML = trace.map((t) =>
    `<div class="trace-item ${t.tone}">
       <div><span class="stage">${t.stage}</span><span class="actor">${t.actor}</span></div>
       <div>${t.text}</div>
     </div>`).join("");
}

/* ---------------- 实时推送 ---------------- */

function connectWs() {
  try {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws/auth`);
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      const { title, detail } = notifyLine(msg);
      toast(detail ? `${title}：${detail}` : title);
      if ($("view-child").classList.contains("active")) loadChild();
    };
    ws.onclose = () => setTimeout(connectWs, 3000);
    ws.onerror = () => ws.close();
  } catch { setTimeout(connectWs, 3000); }
}

/* ---------------- 启动 ---------------- */

loadStatus();
loadSide();
loadChips();
connectWs();
applyHash();
window.addEventListener("hashchange", applyHash);
setInterval(() => { if ($("view-child").classList.contains("active")) loadChild(); }, 5000);
