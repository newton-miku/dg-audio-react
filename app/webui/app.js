"use strict";

const $ = (id) => document.getElementById(id);

const cfgKeys = ["mode", "beat_shape", "style", "chA", "chB", "ceilA", "ceilB", "sensitivity", "release_ms",
  "threshold_auto", "threshold_db", "boost_on", "boost_mode", "safety_cap"];

let saveTimer = null;
function scheduleSave() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(saveConfig, 250);
}

function controlSnapshot() {
  return {
    mode: $("modeSel").value,
    beat_shape: $("beatShape").value,
    style: $("styleSel").value,
    chA: $("chA").checked,
    chB: $("chB").checked,
    ceilA: +$("ceilA").value,
    ceilB: +$("ceilB").value,
    sensitivity: +$("sens").value,
    release_ms: +$("relMs").value,
    threshold_auto: $("gateAuto").checked,
    threshold_db: +$("thresh").value,
    boost_on: $("boostOn").checked,
    boost_mode: $("boostMode").value,
    safety_cap: +$("safeCap").value,
  };
}

async function saveConfig() {
  try {
    const r = await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(controlSnapshot()),
    });
    await r.json();
  } catch (e) { showError("保存配置失败: " + e); }
}

async function sendCmd(body) {
  try {
    const r = await fetch("/api/cmd", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const j = await r.json();
    if (!r.ok) showError((j && j.error) || "命令失败");
    return j;
  } catch (e) { showError("命令发送失败: " + e); }
}

function showError(msg) {
  $("errorLine").textContent = msg || "";
}

function updateModeHint(mode) {
  const el = $("modeHint");
  if (mode === "beat") {
    el.textContent = "输出一直存在、强弱/频率随声音实时变；检测到视频里的节拍器后，每一拍点会额外加重一拍（状态栏显示锁定的 BPM）。";
  } else if (mode === "hybrid") {
    el.textContent = "连续跟随为底，锁定节拍时每拍加重，未锁定时响亮的瞬态/重音也会轻强调——更适合音乐。";
  } else {
    el.textContent = "普通音频：没有固定节拍也能用——强度与频率都随声音实时变（说话/音乐/电影都行）。";
  }
}

function dbToPct(db) { return Math.max(0, Math.min(100, ((db + 120) / 120) * 100)); }

let netInfo = null;
let qrKey = null;   // 当前已加载二维码对应的键

async function refreshQR() {
  const box = $("qrBox");
  if (!box || box.classList.contains("hidden")) return;
  qrKey = (netInfo ? netInfo.ws_url : "?") + "|" + Date.now();
  $("qrImg").src = "/api/qr.png?v=" + encodeURIComponent(qrKey);
}

async function loadNet() {
  try {
    netInfo = await fetch("/api/net").then((r) => r.json());
    const dl = $("ipDatalist");
    dl.innerHTML = "";
    const opts = [netInfo.auto].concat(netInfo.candidates || []);
    new Set(opts).forEach((ip) => {
      const o = document.createElement("option");
      o.value = ip;
      dl.appendChild(o);
    });
    const inp = $("ipInput");
    inp.value = netInfo.chosen || "";
    inp.placeholder = netInfo.chosen ? netInfo.chosen : "自动：" + netInfo.auto;
    $("wsUrlText").textContent = netInfo.ws_url;
  } catch (e) { showError("读取网络信息失败: " + e); }
}

function render(state) {
  const bound = !!state.bound;

  // 状态点
  const dot = $("statusDot");
  dot.className = "dot " + (state.error ? "warn" : bound ? "ok" : "off");
  $("statusText").textContent = state.status || "";
  $("targetLine").textContent = bound ? "已绑定： " + state.target : "";
  showError(state.error || "");

  // 二维码（未绑定时显示）
  if (!bound) {
    $("qrBox").classList.remove("hidden");
    if (qrKey === null) refreshQR();
    $("connHint").textContent = "请用 DG-Lab App 扫描上方二维码绑定。";
  } else {
    $("qrBox").classList.add("hidden");
    qrKey = null;
    $("connHint").textContent = "";
  }

  // 开始/停止
  const btn = $("startBtn");
  if (!bound) { btn.disabled = true; btn.textContent = "等待绑定…"; }
  else {
    btn.disabled = false;
    btn.textContent = state.enabled ? "停止（清空+归零）" : "开始（随声音输出）";
  }

  // App 当前强度
  $("aText").textContent = state.a;  $("aLimitText").textContent = state.a_limit;
  $("bText").textContent = state.b;  $("bLimitText").textContent = state.b_limit;

  // 电平
  const lvl = state.level_db == null ? -120 : state.level_db;
  const gate = state.gate_db == null ? -120 : state.gate_db;
  $("dbText").textContent = lvl.toFixed(0) + " dB";
  $("dbFill").style.width = dbToPct(lvl) + "%";
  const gTick = $("gateTick");
  gTick.style.left = "calc(" + dbToPct(gate) + "% - 1px)";

  // 幅度
  const amp = state.amp || 0;
  $("ampText").textContent = Math.round(amp * 100) + "%";
  $("ampFill").style.width = Math.round(amp * 100) + "%";
  $("outAText").textContent = Math.round((state.outA || 0) * 100);
  $("outBText").textContent = Math.round((state.outB || 0) * 100);
  $("kickText").textContent = Math.round((state.kick || 0) * 100);
  $("bpmText").textContent = state.bpm ? Math.round(state.bpm) : "-";
  $("lockText").textContent = state.locked && state.bpm ? Math.round(state.bpm) + " BPM" : "未锁定";
  const ps = state.pulses_sent || 0;
  $("diagText").textContent = (state.wave_on ? "◆ 正在下发波形 · 累计 " : "累计已发波形 ") + ps + " 条";
  $("boostAText").textContent = state.boostA || 0;
  $("boostBText").textContent = state.boostB || 0;
  if ($("gateAuto") && $("gateAuto").checked) $("threshVal").textContent = "自动";
}

// ---------- WebSocket 遥测 ----------
function connectWS() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  let ws;
  try { ws = new WebSocket(proto + "://" + location.host + "/ws"); }
  catch (e) { setTimeout(connectWS, 1500); return; }

  ws.onmessage = (ev) => {
    try { render(JSON.parse(ev.data)); } catch (e) { /* ignore */ }
  };
  ws.onclose = () => setTimeout(connectWS, 1000);
  ws.onerror = () => { try { ws.close(); } catch (e) {} };
}

// ---------- 初始化 ----------
async function init() {
  // 控件事件
  const bindSlider = (id, valId) => {
    const el = $(id), val = $(valId);
    el.addEventListener("input", () => {
      val.textContent = el.value;
      scheduleSave();
    });
    el.addEventListener("change", () => { val.textContent = el.value; scheduleSave(); });
  };
  bindSlider("ceilA", "ceilAval"); bindSlider("ceilB", "ceilBval");
  bindSlider("thresh", "threshVal"); bindSlider("sens", "sensVal"); bindSlider("relMs", "relMsVal");
  bindSlider("safeCap", "safeCapVal");
  ["modeSel", "styleSel", "beatShape"].forEach((id) => {
    $(id).addEventListener("change", () => {
      if (id === "modeSel") updateModeHint($(id).value);
      scheduleSave();
    });
  });
  ["chA", "chB"].forEach((id) => {
    $(id).addEventListener("change", scheduleSave);
  });
  ["boostOn", "boostMode"].forEach((id) => {
    $(id).addEventListener("change", scheduleSave);
  });
  const gateAutoEl = $("gateAuto");
  const setGateUI = (auto) => { $("thresh").disabled = auto; if (auto) $("threshVal").textContent = "自动"; };
  gateAutoEl.addEventListener("change", () => { setGateUI(gateAutoEl.checked); scheduleSave(); });

  // 直接在电脑声音电平条上拖动设门限（会切到手动）
  const dbMeter = $("dbMeter");
  let gateDragging = false;
  const setGateFromPx = (ev) => {
    const r = dbMeter.getBoundingClientRect();
    const p = Math.min(1, Math.max(0, (ev.clientX - r.left) / r.width));
    let db = Math.round(p * 120 - 120);
    db = Math.max(-100, Math.min(-10, db));
    if (gateAutoEl.checked) gateAutoEl.checked = false;
    setGateUI(false);
    $("thresh").value = db;
    $("threshVal").textContent = db;
    scheduleSave();
  };
  if (dbMeter) {
    dbMeter.addEventListener("pointerdown", (e) => {
      gateDragging = true;
      if (dbMeter.setPointerCapture) dbMeter.setPointerCapture(e.pointerId);
      setGateFromPx(e);
    });
    dbMeter.addEventListener("pointermove", (e) => { if (gateDragging) setGateFromPx(e); });
    const endDrag = () => { gateDragging = false; };
    dbMeter.addEventListener("pointerup", endDrag);
    dbMeter.addEventListener("pointercancel", endDrag);
  }

  $("startBtn").addEventListener("click", async () => {
    const j = await fetch("/api/state").then((r) => r.json());
    await sendCmd({ action: j.enabled ? "stop" : "start" });
  });
  $("recalBtn").addEventListener("click", () => sendCmd({ action: "recal" }));
  $("resetBoostBtn").addEventListener("click", () => sendCmd({ action: "reset_boost" }));
  $("applyDevBtn").addEventListener("click", () => sendCmd({ action: "set_device", device: $("devSel").value }));
  $("testA").addEventListener("click", () => sendCmd({ action: "test", ch: "A" }));
  $("testB").addEventListener("click", () => sendCmd({ action: "test", ch: "B" }));

  // 局域网地址（二维码里的 IP）
  const applyIp = async () => {
    const v = $("ipInput").value.trim();
    await sendCmd({ action: "set_ip", ip: v });
    await loadNet();
    refreshQR();
  };
  $("ipApply").addEventListener("click", applyIp);
  $("ipAuto").addEventListener("click", async () => {
    await sendCmd({ action: "set_ip", ip: "" });
    await loadNet();
    refreshQR();
  });
  $("ipInput").addEventListener("keydown", (e) => { if (e.key === "Enter") applyIp(); });

  // 读配置 & 设备
  try {
    const cfg = await fetch("/api/config").then((r) => r.json());
    $("modeSel").value = cfg.mode || "beat";
    updateModeHint(cfg.mode || "beat");
    $("styleSel").value = cfg.style || "mid";
    $("beatShape").value = cfg.beat_shape || "sharp";
    $("chA").checked = !!cfg.chA; $("chB").checked = !!cfg.chB;
    $("ceilA").value = cfg.ceilA; $("ceilAval").textContent = cfg.ceilA;
    $("ceilB").value = cfg.ceilB; $("ceilBval").textContent = cfg.ceilB;
    gateAutoEl.checked = cfg.threshold_auto !== false;
    $("thresh").value = (cfg.threshold_db == null ? -50 : cfg.threshold_db);
    $("threshVal").textContent = $("thresh").value;
    setGateUI(gateAutoEl.checked);
    $("boostOn").checked = !!cfg.boost_on;
    $("boostMode").value = cfg.boost_mode || "recover";
    $("safeCap").value = cfg.safety_cap || 160;
    $("safeCapVal").textContent = cfg.safety_cap || 160;
    $("sens").value = cfg.sensitivity; $("sensVal").textContent = cfg.sensitivity;
    $("relMs").value = cfg.release_ms; $("relMsVal").textContent = cfg.release_ms;
  } catch (e) { showError("读取配置失败: " + e); }

  try {
    const dd = await fetch("/api/devices").then((r) => r.json());
    const sel = $("devSel");
    const optAuto = document.createElement("option");
    optAuto.value = ""; optAuto.textContent = "（自动 - 系统默认播放声音）";
    sel.appendChild(optAuto);
    (dd.devices || []).forEach((d) => {
      const o = document.createElement("option");
      o.value = (d.loopback ? "[环回] " : "[输入] ") + d.name;
      o.textContent = o.value;
      sel.appendChild(o);
    });
    if (dd.selected) sel.value = dd.selected;
  } catch (e) { showError("读取设备失败: " + e); }

  await loadNet();
  connectWS();
}

init();
