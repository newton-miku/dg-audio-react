"use strict";

const $ = (id) => document.getElementById(id);

const cfgKeys = ["chA", "chB", "ceilA", "ceilB", "sensitivity", "release_ms",
  "threshold_auto", "threshold_db", "boost_on", "boost_mode", "safety_cap",
  "ab_link", "srcA", "srcB", "waveA", "waveB",
  "modeA", "modeB", "styleA", "styleB", "shapeA", "shapeB"];

let saveTimer = null;
function scheduleSave() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(saveConfig, 250);
}

function controlSnapshot() {
  return {
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
    ab_link: $("abLink").checked,
    srcA: $("srcA").value, srcB: $("srcB").value,
    waveA: $("waveA").value, waveB: $("waveB").value,
    modeA: $("modeA").value, modeB: $("modeB").value,
    styleA: $("styleA").value, styleB: $("styleB").value,
    shapeA: $("shapeA").value, shapeB: $("shapeB").value,
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

const MODE_TXT = { beat: "节拍跟随", hybrid: "混合", follow: "普通音频" };

function updateModeHint() {
  const link = $("abLink").checked;
  const mA = $("modeA").value;
  const mB = link ? mA : $("modeB").value;
  const el = $("modeHint");
  const what = (m) => MODE_TXT[m] || m;
  const tail = "【节拍跟随】会自动追踪节拍——不只节拍器，音乐里的强拍（底鼓/军鼓）也算；"
    + "锁不上时仍会连续随声音输出，只是没有“每拍加重”。";
  if (link) {
    el.textContent = "A、B 都用「" + what(mA) + "」。" + (mA === "follow" ? "普通音频：输出连续、强度与频率随声音实时变，无节拍也能用。" : tail);
  } else {
    el.textContent = "A = " + what(mA) + "，B = " + what(mB) + "（两通道各自独立）。";
  }
}

// ---------- 可视化：输出波形 + 输入频谱 ----------
function fitCanvas(cv) {
  const dpr = window.devicePixelRatio || 1;
  const w = Math.max(80, cv.clientWidth || 600);
  const h = Math.max(40, cv.clientHeight || 120);
  const W = Math.round(w * dpr), H = Math.round(h * dpr);
  if (cv.width !== W || cv.height !== H) { cv.width = W; cv.height = H; }
  const ctx = cv.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, w, h };
}

function drawScope(scope, freqA, freqB) {
  const cv = $("scope");
  if (!cv || !cv.getContext) return;
  const { ctx, w, h } = fitCanvas(cv);
  ctx.clearRect(0, 0, w, h);
  const n = 60;
  const laneH = h / 2 - 3;
  // 中缝
  ctx.fillStyle = "rgba(255,255,255,.09)";
  ctx.fillRect(0, h / 2 - 1, w, 1);
  const arr = (scope || []).slice(-n);
  const off = n - arr.length;
  const slotW = w / n;
  const bw = Math.max(1, slotW - 0.8);
  const aTop = h / 2 - 3, bBase = h - 2;
  for (let i = 0; i < arr.length; i++) {
    const e = arr[i];
    const x = (off + i) * slotW;
    const ha = Math.max(0, Math.min(1, (e[0] || 0) / 100)) * laneH;
    const hb = Math.max(0, Math.min(1, (e[1] || 0) / 100)) * laneH;
    ctx.fillStyle = "rgba(110,168,255,.80)";
    if (ha > 0) ctx.fillRect(x, aTop - ha, bw, ha);
    ctx.fillStyle = "rgba(255,77,109,.80)";
    if (hb > 0) ctx.fillRect(x, bBase - hb, bw, hb);
    if (e[4]) { ctx.fillStyle = "rgba(255,190,60,.95)"; ctx.fillRect(x, h / 2 - 4, bw, 2); }
  }
  // 频率线
  const line = (idx, base, style) => {
    if (arr.length < 2) return;
    ctx.strokeStyle = style; ctx.lineWidth = 1; ctx.beginPath();
    for (let i = 0; i < arr.length; i++) {
      const x = (off + i) * slotW + slotW / 2;
      const y = base - Math.max(0, Math.min(1, (arr[i][idx] || 0) / 240)) * laneH;
      if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y);
    }
    ctx.stroke();
  };
  line(2, aTop, "rgba(230,233,239,.40)");
  line(3, bBase, "rgba(230,233,239,.28)");
  $("freqAText").textContent = freqA || "-";
  $("freqBText").textContent = freqB || "-";
}

function drawSpectrum(spec) {
  const cv = $("spectrum");
  if (!cv || !cv.getContext) return;
  const { ctx, w, h } = fitCanvas(cv);
  ctx.clearRect(0, 0, w, h);
  const arr = spec || [];
  if (!arr.length) return;
  const n = arr.length;
  const bw = w / n;
  for (let i = 0; i < n; i++) {
    const v = Math.max(0, Math.min(1, arr[i]));
    const bh = v * (h - 3);
    const t = i / Math.max(1, n - 1);
    const r = Math.round(110 + (255 - 110) * t);
    const g = Math.round(168 + (77 - 168) * t);
    const b = Math.round(255 + (109 - 255) * t);
    ctx.fillStyle = "rgba(" + r + "," + g + "," + b + ",.85)";
    ctx.fillRect(i * bw, h - bh, Math.max(1, bw - 1), bh);
  }
  ctx.fillStyle = "rgba(255,255,255,.10)";
  ctx.fillRect(0, h - 1, w, 1);
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
  $("lockText").textContent = state.locked && state.bpm
    ? Math.round(state.bpm) + " BPM" + (state.low_on ? "·低频" : "")
    : "未锁定";
  const ps = state.pulses_sent || 0;
  $("diagText").textContent = (state.wave_on ? "◆ 正在下发波形 · 累计 " : "累计已发波形 ") + ps + " 条";
  $("boostAText").textContent = state.boostA || 0;
  $("boostBText").textContent = state.boostB || 0;
  if ($("gateAuto") && $("gateAuto").checked) $("threshVal").textContent = "自动";

  // 可视化
  drawScope(state.scope, state.freqA, state.freqB);
  drawSpectrum(state.spec);
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
  let cfgWaveA = null, cfgWaveB = null;
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

  // A/B 联动：联动时 B 跟随 A 且不可单独改
  const linkEl = $("abLink");
  const B_KEYS = [["modeB", "modeA"], ["styleB", "styleA"], ["shapeB", "shapeA"], ["srcB", "srcA"], ["waveB", "waveA"]];
  const syncLink = () => {
    const link = linkEl.checked;
    B_KEYS.forEach(([b, a]) => {
      $(b).disabled = link;
      if (link) $(b).value = $(a).value;
    });
  };
  linkEl.addEventListener("change", () => { syncLink(); updateModeHint(); scheduleSave(); });
  ["modeA", "styleA", "shapeA", "srcA", "waveA"].forEach((id) => {
    $(id).addEventListener("change", () => { syncLink(); updateModeHint(); scheduleSave(); });
  });
  B_KEYS.forEach(([b]) => $(b).addEventListener("change", () => { updateModeHint(); scheduleSave(); }));
  ["srcA", "srcB"].forEach((id) => {
    $(id).addEventListener("change", () => { syncWaveRows(); scheduleSave(); });
  });
  const syncWaveRows = () => {
    const link = linkEl.checked;
    $("waveRowA").style.display = $("srcA").value === "official" ? "" : "none";
    $("waveRowB").style.display = ($("srcB").value === "official" || (link && $("srcA").value === "official")) ? "" : "none";
  };

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
    $("srcA").value = cfg.srcA || "map";
    $("srcB").value = cfg.srcB || "map";
    cfgWaveA = cfg.waveA || null;
    cfgWaveB = cfg.waveB || null;
    $("modeA").value = cfg.modeA || cfg.mode || "beat";
    $("styleA").value = cfg.styleA || cfg.style || "mid";
    $("shapeA").value = cfg.shapeA || cfg.beat_shape || "sharp";
    $("modeB").value = cfg.modeB || cfg.mode || "beat";
    $("styleB").value = cfg.styleB || cfg.style || "mid";
    $("shapeB").value = cfg.shapeB || cfg.beat_shape || "sharp";
    linkEl.checked = cfg.ab_link !== false;
    syncLink();
    updateModeHint();
    syncWaveRows();
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

  try {
    const wv = await fetch("/api/waves").then((r) => r.json());
    const fill = (selId, val) => {
      const sel = $(selId);
      sel.innerHTML = "";
      (wv.waves || []).forEach((w) => {
        const o = document.createElement("option");
        o.value = w.key; o.textContent = w.cn;
        sel.appendChild(o);
      });
      if (val) sel.value = val;
    };
    fill("waveA", cfgWaveA); fill("waveB", cfgWaveB);
  } catch (e) { /* 官方波形列表加载失败不影响其他功能 */ }
  await loadNet();
  connectWS();
}

init();
