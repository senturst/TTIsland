/** 入口：装配各模块，驱动整局流程。 */

import { api } from "./api.js";
import { state, mutate, applyServerState } from "./state.js";
import { initTerm, appendLine, playLines, skipTyping, patchLine } from "./term.js";
import {
  renderAll, setBusy, flashHorde, setDegraded, pushWorldEvent, renderLeaderboard,
  armGiveUp, giveUpArmed,
} from "./render.js";

let host = null;
let idleTimer = null;
let lastHorde = false;

/* ------------------------------------------------------------------ */
/* 信号衰减：静止 30 秒后画面变暗抖一下，任意操作恢复。 */
function resetIdle() {
  const term = document.getElementById("term");
  if (!term) return;
  term.classList.remove("degraded", "shake");
  clearTimeout(idleTimer);
  idleTimer = setTimeout(() => {
    term.classList.add("degraded", "shake");
    setTimeout(() => term.classList.remove("shake"), 200);
  }, 30000);
}

/* ------------------------------------------------------------------ */
/* 动态视口高度：iOS Safari 的底部工具栏（地址栏/标签栏）会一直盖在页面之上，
   而 100dvh 不把它算进高度，于是最底部的命令区被压在工具栏底下、看不全。
   本游戏是 overflow:hidden 的内部滚动布局（无页面级滚动），Safari 永远不会
   因此收起工具栏，所以必须用 visualViewport 取「真正看得见」的高度，
   并在工具栏伸缩时实时同步——这样命令区始终完整落在可见区里。 */
function setupViewport() {
  const vv = window.visualViewport;
  const apply = () => {
    const h = vv ? vv.height : window.innerHeight;
    document.documentElement.style.setProperty("--app-vh", `${h}px`);
  };
  apply();
  if (vv) {
    vv.addEventListener("resize", apply);
    vv.addEventListener("scroll", apply);
  }
  window.addEventListener("resize", apply);
  window.addEventListener("orientationchange", apply);
}

/* ------------------------------------------------------------------ */
async function handleAction(a) {
  if (state.busy) return;
  await send(a.id, {
    index: a.index,
    to: a.to,
    choice: a.choice,
    item: a.item,
    pay: a.pay,
    uid: a.uid,
  });
}

async function send(action, payload = {}) {
  if (state.busy) return;
  resetIdle();
  skipTyping();
  setBusy(true);
  try {
    const data = await api.action(action, payload);
    await consume(data);
    // 任何一次响应后，刷新一次私人回执（墓碑被摸走的提醒）
    checkNotifications();
  } catch (err) {
    appendLine(host, `! ${err.message}`, "bad");
  } finally {
    setBusy(false);
  }
}

/** 消费一次响应：播放文本 → 应用补丁 → 覆盖状态 → 重渲染。 */
async function consume(data) {
  // 先播文本，让玩家立刻看到反馈
  const lines = data.narrative || [];
  if (lines.length) await playLines(host, lines);

  // AI 风味文本到达，原地替换之前那条模板
  for (const p of data.patches || []) {
    patchLine(host, p.target, p.text);
  }

  applyServerState(data);
  renderAll(handleAction);
  setDegraded(!!data.ai_degraded);

  if (state.horde && !lastHorde) flashHorde();
  lastHorde = state.horde;

  // 结局分支。
  // 注意：服务端已经写过"你最后能留下的只有一样东西"这类引导语，
  // 这里不要再拼一条，否则会出现两句几乎一样的提示。
  if (state.pendingDecision === "legacy" && state.legacyBlocked?.length) {
    // 重火力带不走，说明原因，否则玩家会以为装备被系统吞了
    appendLine(host,
      `太重、太吵、也修不好——${state.legacyBlocked.join("、")}带不走，只能留在这里。`,
      "sys");
  }
  if (state.status !== "active" && !state.pendingDecision) {
    appendLine(host, "— 本局结束 —", "divider");
    const btn = document.getElementById("cmd-buttons");
    if (btn && !btn.querySelector(".btn-restart")) {
      const b = document.createElement("button");
      b.className = "btn btn-primary btn-restart";
      b.textContent = "再来一局";
      b.addEventListener("click", () => startRun());
      btn.appendChild(b);
    }
  }
}

/* ------------------------------------------------------------------ */
async function startRun() {
  setBusy(true);
  try {
    const data = await api.start();
    host.lines.innerHTML = "";
    state.talent = null;
    await consume(data);
  } catch (err) {
    appendLine(host, `! ${err.message}`, "bad");
  } finally {
    setBusy(false);
  }
}

/** 续玩：拉回完整画面并回放最近日志，让玩家接上上下文。 */
async function resume() {
  const data = await api.active();
  if (!data.active) return false;
  host.lines.innerHTML = "";
  for (const line of data.history || []) {
    appendLine(host, line);
  }
  appendLine(host, "— 连接恢复 —", "divider");
  applyServerState(data);
  renderAll(handleAction);
  lastHorde = state.horde;
  return true;
}

/* ------------------------------------------------------------------ */
/** 快捷键分发：按命令区按钮顺序 Z/X/C/V 触发前 4 个动作。
 * C 同时保留为「使用物品」备用键：当命令区没有第 3 个按钮时生效。
 * 只在「本局进行中且无可决断」时响应，避免误触。
 * 「放弃这一局」（give_up）是唯一例外：快捷键只进入确认态，
 * 必须再用鼠标点一次按钮才真正放弃——V 键连按永远不会误杀当局。 */
function dispatchHotkey(rawKey) {
  const k = rawKey.toLowerCase();
  if (state.busy) return;
  if (state.status !== "active" || state.pendingDecision) return;

  const idx = ["z", "x", "c", "v"].indexOf(k);
  if (idx === -1) return;

  const acts = state.actions || [];
  const a = acts[idx];
  if (a) {
    if (a.id === "give_up") {
      if (!giveUpArmed) {
        armGiveUp(true);
        renderAll(handleAction);  // 立即重绘，让按钮显示确认态文案
      }
      return;  // 已处于确认态时按 V：什么都不做（不解除也不执行）
    }
    send(a.id, { index: a.index, to: a.to, choice: a.choice, item: a.item });
    return;
  }

  // C 键 fallback：命令区没占满 3 个时，使用/装备随身第一个可用物品
  if (k === "c") {
    const it = (state.inventory || []).find((i) => i.usable || i.wearable);
    if (it) send(it.usable ? "use" : "equip", { item: it.id });
  }
}

/* ------------------------------------------------------------------ */
/* 世界事件 SSE：只订阅"重大事件"广播（死亡/撤离/破纪录），不做聊天。
   收到后更新顶部播报条 + 世界面板的实时列表。 */
let worldEventBuffer = [];

function setupSSE() {
  const es = new EventSource("/api/events/stream");
  es.addEventListener("broadcast", (e) => {
    try {
      const ev = JSON.parse(e.data);
      pushWorldEvent(ev);
    } catch { /* 坏帧忽略 */ }
  });
  es.onerror = () => { /* 浏览器会自动重连，静默 */ };
}

/* ------------------------------------------------------------------ */
/* 世界面板：三榜切换 + 实时播报列表。 */
let currentBoard = "score";

async function refreshLeaderboard(by = currentBoard) {
  currentBoard = by;
  const listEl = document.getElementById("world-list");
  if (!listEl) return;
  try {
    const data = await api.leaderboard(by);
    renderLeaderboard(data.entries || [], by);
  } catch {
    listEl.innerHTML = `<li class="world-events-empty">榜单加载失败</li>`;
  }
}

function setupWorldPanel() {
  const overlay = document.getElementById("world-overlay");
  const openBtn = document.getElementById("btn-world");
  const closeBtn = document.getElementById("world-close");
  if (!overlay || !openBtn) return;

  openBtn.addEventListener("click", () => {
    overlay.classList.remove("hidden");
    refreshLeaderboard(currentBoard);
  });
  closeBtn.addEventListener("click", () => overlay.classList.add("hidden"));
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) overlay.classList.add("hidden");
  });

  document.querySelectorAll(".wtab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".wtab").forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      refreshLeaderboard(tab.dataset.board);
    });
  });
}

/* ------------------------------------------------------------------ */
/* 私人回执：墓碑被别人摸走时，原主人收到"谁动了我"。
   只在本机上线时拉取一次并标记已读，避免重复打扰。 */
async function checkNotifications() {
  try {
    const data = await api.notifications();
    const items = data.items || [];
    if (!items.length) return;
    for (const n of items) {
      appendLine(host, `📨 回执：${n.body}`, "sys");
    }
    await api.markNotifications(items.map((n) => n.id));
  } catch { /* 回执丢失不致命 */ }
}

/* ------------------------------------------------------------------ */
/** 开局命名弹窗：返回玩家最终确认的名字（经服务端 DeepSeek 审核）。 */
function promptName() {
  return new Promise((resolve) => {
    const overlay = document.getElementById("name-overlay");
    const input = document.getElementById("name-input");
    const err = document.getElementById("name-err");
    const btn = document.getElementById("name-confirm");
    if (!overlay || !input || !btn) { resolve(null); return; }

    overlay.classList.remove("hidden");
    input.value = "";
    err.classList.add("hidden");
    input.focus();

    const showErr = (m) => { err.textContent = m; err.classList.remove("hidden"); };
    const cleanup = () => {
      btn.removeEventListener("click", done);
      input.removeEventListener("keydown", onKey);
    };
    const done = async () => {
      const name = input.value.trim();
      if (!name) { showErr("先起个名字吧"); return; }
      btn.disabled = true;
      try {
        const data = await api.setName(name);
        if (data.ok) {
          overlay.classList.add("hidden");
          cleanup();
          resolve(data.name);
        } else {
          showErr(data.reason || "这个名字不行");
          btn.disabled = false;
        }
      } catch (e) {
        showErr(e.message);
        btn.disabled = false;
      }
    };
    const onKey = (e) => { if (e.key === "Enter") done(); };

    btn.addEventListener("click", done);
    input.addEventListener("keydown", onKey);
  });
}

/* ------------------------------------------------------------------ */
/* UI 缩放：高分屏下 15px 基准字太小。A+/A- 调整根字号缩放系数，
   localStorage 持久化；范围 0.8 ~ 1.8，步进 0.1。 */
const ZOOM_KEY = "ttisland_ui_scale";
const ZOOM_MIN = 0.8;
const ZOOM_MAX = 1.8;

function applyZoom(scale) {
  const clamped = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, Math.round(scale * 10) / 10));
  document.documentElement.style.setProperty("--ui-scale", String(clamped));
  try { localStorage.setItem(ZOOM_KEY, String(clamped)); } catch { /* 隐私模式忽略 */ }
}

function setupZoom() {
  let saved = 1;
  try { saved = parseFloat(localStorage.getItem(ZOOM_KEY)) || 1; } catch { /* 忽略 */ }
  applyZoom(saved);

  const root = document.getElementById("zoom-ctrl");
  const zin = document.getElementById("zoom-in");
  const zout = document.getElementById("zoom-out");
  if (!root || !zin || !zout) return;
  zin.addEventListener("click", () => {
    const cur = parseFloat(
      getComputedStyle(document.documentElement).getPropertyValue("--ui-scale")
    ) || 1;
    applyZoom(cur + 0.1);
  });
  zout.addEventListener("click", () => {
    const cur = parseFloat(
      getComputedStyle(document.documentElement).getPropertyValue("--ui-scale")
    ) || 1;
    applyZoom(cur - 0.1);
  });
}

/* ------------------------------------------------------------------ */
async function boot() {
  setupViewport();
  setupZoom();
  const statusNode = document.getElementById("boot-status");
  host = initTerm(document.getElementById("term"));

  try {
    statusNode.textContent = "校验身份…";
    const hello = await api.hello();
    let playerName = hello.player?.name;
    if (hello.needs_name) {
      // 关键：先撤掉启动屏再弹名字。启动屏 z-index(100) 高于弹窗(60) 且背景不透明，
      // 不先隐藏的话弹窗被完全盖住，玩家只会看到永远停在"校验身份…"。
      document.getElementById("boot").classList.add("hidden");
      const chosen = await promptName();
      if (chosen) playerName = chosen;
    }
    mutate((s) => { s.player = { ...hello.player, name: playerName }; });

    statusNode.textContent = "同步配置…";
    const meta = await api.meta();
    mutate((s) => {
      s.meta = meta;
      s.maxDepth = meta.max_level;
      s.icons = { ...(meta.items || {}), ...(meta.monsters || {}) };
      // 地区进度：已从哪个地区撤离过 → 解锁到哪个地区
      const rp = hello.player?.region_progress ?? 0;
      s.regionProgress = rp;
      s.regionUnlocked = Math.min(rp + 1, Object.keys(meta.regions || {}).length || 1);
    });

    document.getElementById("boot").classList.add("hidden");
    document.getElementById("app").classList.remove("hidden");
    resetIdle();

    setupSSE();
    setupWorldPanel();

    statusNode.textContent = "恢复进度…";
    const resumed = await resume();
    if (!resumed) {
      appendLine(host, "孤岛残响", "level");
      appendLine(host, "五层之下，有一架直升机。它不会等你第二次。", "flavor");
      await startRun();
    } else {
      checkNotifications();
    }
  } catch (err) {
    statusNode.textContent = `接入失败：${err.message}`;
    statusNode.style.color = "var(--danger)";
    return;
  }

  // 点击跳过打字机
  document.getElementById("term").addEventListener("click", () => {
    skipTyping();
  });

  // 键盘：Esc 跳过打字机；Z/X/C/V 依次触发命令区前四个按钮
  document.addEventListener("keydown", (e) => {
    resetIdle();
    if (e.key === "Escape") { skipTyping(); return; }
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    dispatchHotkey(e.key);
  });
}

boot();
