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
    if (name === "config") {
      show($("config-panel")); hide($("players-panel")); hide($("stats-panel"));
    } else if (name === "players") {
      hide($("config-panel")); show($("players-panel")); hide($("stats-panel"));
    } else {
      hide($("config-panel")); hide($("players-panel")); show($("stats-panel"));
      loadStats();
    }
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
let itemCatalog = null;   // [{section,label,items:[{id,name,durability}]}]
let editorState = null;   // 当前玩家的完整 state（保存的数据源）
let editorBagCap = 0;

async function ensureCatalog() {
  if (itemCatalog) return;
  const r = await api("/api/admin/items");
  itemCatalog = r.ok ? r.data.groups : [];
}

function itemMeta(iid) {
  for (const g of itemCatalog || [])
    for (const it of g.items)
      if (it.id === iid) return it;
  return null;
}

function fillSelect(sel, allowEmpty) {
  sel.textContent = "";
  if (allowEmpty !== false) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "（无）";
    sel.appendChild(opt);
  }
  for (const g of itemCatalog || []) {
    const og = document.createElement("optgroup");
    og.label = g.label;
    for (const it of g.items) {
      const o = document.createElement("option");
      o.value = it.id;
      o.textContent = `${it.name} (${it.id})`;
      og.appendChild(o);
    }
    sel.appendChild(og);
  }
}

function renderStructuredEditor() {
  const st = editorState || {};
  // 普通值
  const vals = { "v-hp": "hp", "v-hp-max": "hp_max", "v-infection": "infection",
                 "v-stamina": "stamina", "v-cash": "cash", "v-scrap": "scrap",
                 "v-tape": "tape", "v-score": "score" };
  for (const [id, key] of Object.entries(vals)) {
    const el = $(id);
    el.value = st[key] == null ? "" : String(st[key]);
  }
  $("inv-cap").textContent = String(editorBagCap);

  // 背包行：容量数 = 行数；空位显示（无）
  const rows = $("inv-rows");
  rows.textContent = "";
  const inv = st.inventory || [];
  for (let i = 0; i < editorBagCap; i++) {
    const entry = inv[i] || null;
    const row = document.createElement("div");
    row.className = "inv-row";
    const no = document.createElement("span");
    no.className = "slot-no";
    no.textContent = `${i + 1}.`;
    row.appendChild(no);

    const sel = document.createElement("select");
    fillSelect(sel);
    sel.value = entry ? entry.id : "";
    if (sel.value !== (entry ? entry.id : "")) sel.value = "";  // 目录里没有的 id（如现金）兜底回（无）
    sel.addEventListener("change", () => syncRow(row, i));
    row.appendChild(sel);

    const qty = document.createElement("input");
    qty.type = "number"; qty.min = "0";
    qty.value = entry && entry.qty > 1 ? String(entry.qty) : "1";
    qty.title = "数量（可堆叠物有效）";
    qty.addEventListener("change", () => syncRow(row, i));
    row.appendChild(qty);

    const dur = document.createElement("input");
    dur.type = "number"; dur.min = "0"; dur.placeholder = "耐久";
    dur.value = entry && entry.durability != null ? String(entry.durability) : "";
    dur.title = "耐久（武器/护甲有效；留空 = 无耐久概念）";
    dur.addEventListener("change", () => syncRow(row, i));
    row.appendChild(dur);

    const tag = document.createElement("span");
    tag.className = "slot-empty";
    tag.textContent = entry ? "" : "无";
    row.appendChild(tag);

    row.dataset.slot = String(i);
    rows.appendChild(row);
  }
}

function syncRow(row, i) {
  // 行内改下拉/数量/耐久后：空位行补默认数量
  const sel = row.querySelector("select");
  if (sel.value) {
    const qty = row.querySelector("input[type=number]");
    const dur = row.querySelectorAll("input[type=number]")[0] === qty
      ? row.querySelectorAll("input[type=number]")[1]
      : qty;
    if (qty.value === "" || Number(qty.value) < 1) qty.value = "1";
    void dur;
  }
}

function collectEditorState() {
  const st = JSON.parse(JSON.stringify(editorState || {}));
  const num = (id, fallback) => {
    const v = $(id).value;
    return v === "" ? fallback : Number(v);
  };
  st.hp = num("v-hp", st.hp);
  st.hp_max = num("v-hp-max", st.hp_max);
  st.infection = num("v-infection", st.infection ?? 0);
  if (st.stamina != null) st.stamina = num("v-stamina", st.stamina);
  st.cash = num("v-cash", st.cash ?? 0);
  // 废料/胶带在背包堆叠条目里——从条目改写（保持堆叠语义）
  const scrap = num("v-scrap", null);
  const tape = num("v-tape", null);
  const rows = [...document.querySelectorAll("#inv-rows .inv-row")];
  const newInv = [];
  rows.forEach((row, i) => {
    const sel = row.querySelector("select");
    const nums = row.querySelectorAll("input[type=number]");
    const qtyEl = nums[0], durEl = nums[1];
    if (!sel.value) return;  // 空位
    const orig = (editorState.inventory || [])[i] || null;
    const entry = { id: sel.value, qty: Math.max(1, Number(qtyEl.value) || 1) };
    if (durEl.value !== "") entry.durability = Number(durEl.value);
    // 保留原条目的其他实例字段（同名时）：clip/mag 等
    if (orig && orig.id === sel.value) {
      for (const k of Object.keys(orig))
        if (!(k in entry) && k !== "qty") entry[k] = orig[k];
    }
    newInv.push(entry);
  });
  st.inventory = newInv;
  // 废料/胶带以堆叠条目形式存在于背包——写回为标准条目
  if (scrap != null) {
    const i = newInv.findIndex((e) => e.id === "scrap");
    if (i >= 0) newInv[i].qty = Math.max(0, scrap);
    else newInv.push({ id: "scrap", qty: Math.max(0, scrap), durability: null });
  }
  if (tape != null) {
    const i = newInv.findIndex((e) => e.id === "duct_tape");
    if (i >= 0) newInv[i].qty = Math.max(0, tape);
    else newInv.push({ id: "duct_tape", qty: Math.max(0, tape), durability: null });
  }
  return st;
}

async function openPlayer(runId, name) {
  currentRunId = runId;
  $("player-name").textContent = name || runId;
  hide(document.querySelector(".players-list"));
  show($("player-detail"));
  await ensureCatalog();
  const r = await api("/api/admin/players/" + encodeURIComponent(runId));
  if (!r.ok) {
    setStatus($("player-status"), r.data?.detail || "读取失败", "err");
    return;
  }
  editorState = r.data.state;
  editorBagCap = r.data.bag_cap || (editorState.inventory || []).length;
  renderStructuredEditor();
  $("player-state").value = JSON.stringify(r.data.state, null, 2);
  setStatus($("player-status"), "");
}

$("player-back").addEventListener("click", () => {
  hide($("player-detail"));
  show(document.querySelector(".players-list"));
  currentRunId = null;
  loadPlayers();
});

async function savePlayerState(state) {
  const r = await api("/api/admin/players/" + encodeURIComponent(currentRunId), {
    method: "PUT",
    body: JSON.stringify({ state }),
  });
  if (r.ok) {
    setStatus($("player-status"), "已保存，玩家下次行动会加载这份状态。", "ok");
    return true;
  }
  setStatus($("player-status"), r.data?.detail || "保存失败", "err");
  return false;
}

// 保存全部：数值输入框 + 背包下拉行 → 结构化收集
$("player-save").addEventListener("click", async () => {
  if (!currentRunId) return;
  if (!editorState) { setStatus($("player-status"), "尚未加载玩家状态。", "err"); return; }
  await savePlayerState(collectEditorState());
});

// 高级：完整 JSON 保存（深度编辑武器/护甲/clip 等）
$("player-save-json").addEventListener("click", async () => {
  if (!currentRunId) return;
  let state;
  try {
    state = JSON.parse($("player-state").value);
  } catch (e) {
    setStatus($("player-status"), "JSON 解析失败：" + e.message, "err");
    return;
  }
  await savePlayerState(state);
});

// ---------------------------------------------------------------------------
// 数据统计（P8）：死亡分布 + 各层通过率
// ---------------------------------------------------------------------------
async function loadStats() {
  const r = await api("/api/admin/stats");
  if (!r.ok) {
    $("stats-summary").textContent = r.data?.detail || "统计读取失败";
    return;
  }
  const d = r.data;
  $("stats-summary").textContent =
    `已完结 ${d.total_finished} 局（排除主动放弃 ${d.abandoned_excluded} 局），进行中 ${d.active_now} 局。`;

  const deathsBody = $("deaths-body");
  deathsBody.textContent = "";
  if (!d.deaths.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 3;
    td.textContent = "还没有死亡记录。";
    tr.appendChild(td);
    deathsBody.appendChild(tr);
  }
  for (const row of d.deaths) {
    const tr = document.createElement("tr");
    const td1 = document.createElement("td");
    td1.textContent = row.region;
    const td2 = document.createElement("td");
    td2.textContent = `第 ${row.depth} 层`;
    const td3 = document.createElement("td");
    td3.textContent = String(row.count);
    tr.append(td1, td2, td3);
    deathsBody.appendChild(tr);
  }

  const prBody = $("passrate-body");
  prBody.textContent = "";
  for (const row of d.pass_rates) {
    const tr = document.createElement("tr");
    const cells = [
      `第 ${row.level} 层`,
      row.region,
      String(row.reached),
      String(row.passed),
      row.rate == null ? "—" : `${(row.rate * 100).toFixed(1)}%`,
    ];
    for (const c of cells) {
      const td = document.createElement("td");
      td.textContent = c;
      tr.appendChild(td);
    }
    prBody.appendChild(tr);
  }
}

$("stats-refresh").addEventListener("click", loadStats);

boot();
