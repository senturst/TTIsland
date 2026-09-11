"use strict";

// 管理后台前端（纯原生 JS，无构建）。
// 所有服务端返回的文本都用 textContent / value 写入，绝不用 innerHTML，避免 XSS。
// fetch 同源默认带 cookie，登录后的会话 cookie（httpOnly）会自动随请求发送。

const $ = (id) => document.getElementById(id);

function show(el) { el.classList.remove("hidden"); }
function hide(el) { el.classList.add("hidden"); }

async function api(path, opts = {}) {
  const res = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  let data = null;
  try { data = await res.json(); } catch (_e) { /* 空响应 */ }
  return { ok: res.ok, status: res.status, data };
}

function setStatus(box, msg, kind) {
  box.textContent = msg || "";
  box.className = "status" + (kind ? " " + kind : "");
}

// ---------------------------------------------------------------------------
// 登录态
// ---------------------------------------------------------------------------
async function boot() {
  const r = await api("/api/admin/whoami");
  if (r.ok) {
    enterApp();
  } else {
    show($("login"));
    hide($("app"));
  }
}

function enterApp() {
  hide($("login"));
  show($("app"));
  loadConfigs();
  loadPlayers();
}

$("login-btn").addEventListener("click", async () => {
  const token = $("token").value;
  const r = await api("/api/admin/login", {
    method: "POST",
    body: JSON.stringify({ token }),
  });
  if (r.ok) {
    $("token").value = "";
    enterApp();
  } else {
    setStatus($("login-err"), r.data?.detail || "登录失败", "err");
  }
});

$("logout-btn").addEventListener("click", async () => {
  await api("/api/admin/logout", { method: "POST" });
  hide($("app"));
  show($("login"));
});

// ---------------------------------------------------------------------------
// 选项卡
// ---------------------------------------------------------------------------
document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    tab.classList.add("active");
    const name = tab.dataset.tab;
    if (name === "config") { show($("config-panel")); hide($("players-panel")); }
    else { hide($("config-panel")); show($("players-panel")); }
  });
});

// ---------------------------------------------------------------------------
// 配置管理
// ---------------------------------------------------------------------------
let currentConfig = null;

async function loadConfigs() {
  const box = $("config-files");
  const r = await api("/api/admin/configs");
  if (!r.ok) return;
  box.textContent = "";
  for (const name of r.data.files) {
    const btn = document.createElement("button");
    btn.className = "file-item";
    btn.textContent = name;
    btn.addEventListener("click", () => openConfig(name));
    box.appendChild(btn);
  }
}

async function openConfig(name) {
  currentConfig = name;
  $("config-name").textContent = name;
  const r = await api("/api/admin/configs/" + encodeURIComponent(name));
  if (r.ok) {
    $("config-text").value = r.data.content;
    setStatus($("config-status"), "");
  } else {
    setStatus($("config-status"), r.data?.detail || "读取失败", "err");
  }
}

$("config-save").addEventListener("click", async () => {
  if (!currentConfig) return;
  const content = $("config-text").value;
  const r = await api("/api/admin/configs/" + encodeURIComponent(currentConfig), {
    method: "PUT",
    body: JSON.stringify({ content }),
  });
  if (r.ok && r.data.ok) {
    setStatus($("config-status"), "已保存并热重载。", "ok");
  } else if (r.ok && !r.data.ok) {
    setStatus($("config-status"), "已写回但热重载被拒绝（配置未生效）：" + (r.data.error || ""), "err");
  } else {
    setStatus($("config-status"), r.data?.detail || "保存失败", "err");
  }
});

// ---------------------------------------------------------------------------
// 玩家管理
// ---------------------------------------------------------------------------
async function loadPlayers() {
  const body = $("players-body");
  const r = await api("/api/admin/players");
  if (!r.ok) { setStatus($("players-status"), r.data?.detail || "加载失败", "err"); return; }
  body.textContent = "";
  for (const p of r.data.players) {
    const tr = document.createElement("tr");
    tr.style.cursor = "pointer";
    const cells = [
      p.name || "(无名)",
      p.depth,
      (p.hp ?? "?") + "/" + (p.hp_max ?? "?"),
      p.infection,
      p.in_combat ? "是" : "否",
      p.score,
    ];
    for (const c of cells) {
      const td = document.createElement("td");
      td.textContent = c == null ? "" : String(c);
      tr.appendChild(td);
    }
    tr.addEventListener("click", () => openPlayer(p.run_id, p.name));
    body.appendChild(tr);
  }
  if (!r.data.players.length) {
    setStatus($("players-status"), "当前没有存活在玩的玩家。");
  } else {
    setStatus($("players-status"), "");
  }
}

let currentRunId = null;

async function openPlayer(runId, name) {
  currentRunId = runId;
  $("player-name").textContent = name || runId;
  hide(document.querySelector(".players-list"));
  show($("player-detail"));
  const r = await api("/api/admin/players/" + encodeURIComponent(runId));
  if (r.ok) {
    $("player-state").value = JSON.stringify(r.data.state, null, 2);
    setStatus($("player-status"), "");
  } else {
    setStatus($("player-status"), r.data?.detail || "读取失败", "err");
  }
}

$("player-back").addEventListener("click", () => {
  hide($("player-detail"));
  show(document.querySelector(".players-list"));
  currentRunId = null;
  loadPlayers();
});

$("player-save").addEventListener("click", async () => {
  if (!currentRunId) return;
  let state;
  try {
    state = JSON.parse($("player-state").value);
  } catch (e) {
    setStatus($("player-status"), "JSON 解析失败：" + e.message, "err");
    return;
  }
  const r = await api("/api/admin/players/" + encodeURIComponent(currentRunId), {
    method: "PUT",
    body: JSON.stringify({ state }),
  });
  if (r.ok) {
    setStatus($("player-status"), "已保存，玩家下次行动会加载这份状态。", "ok");
  } else {
    setStatus($("player-status"), r.data?.detail || "保存失败", "err");
  }
});

boot();
